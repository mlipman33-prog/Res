"""Thin client for the Resy API (the same API resy.com's web app uses)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import requests

API_BASE = "https://api.resy.com"
# Public key embedded in resy.com's web frontend. Override with RESY_API_KEY if it rotates.
DEFAULT_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"


class ResyError(Exception):
    """Raised when the Resy API returns an error or an unexpected response."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Slot:
    venue_id: int
    venue_name: str
    start: datetime
    seating_type: str
    config_token: str

    def describe(self) -> str:
        return f"{self.venue_name} @ {self.start:%a %b %d %I:%M %p} ({self.seating_type})"


@dataclass(frozen=True)
class Booking:
    reservation_id: str | None
    resy_token: str | None
    slot: Slot


class ResyClient:
    def __init__(
        self,
        api_key: str = DEFAULT_API_KEY,
        auth_token: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ):
        self.api_key = api_key
        self.auth_token = auth_token
        self.payment_method_id: int | None = None
        self.session = session or requests.Session()
        self.timeout = timeout

    # --- plumbing -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f'ResyAPI api_key="{self.api_key}"',
            "Origin": "https://resy.com",
            "Referer": "https://resy.com/",
            "X-Origin": "https://resy.com",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
        }
        if self.auth_token:
            headers["X-Resy-Auth-Token"] = self.auth_token
            headers["X-Resy-Universal-Auth"] = self.auth_token
        return headers

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self.session.request(
                method, API_BASE + path, headers=self._headers(), timeout=self.timeout, **kwargs
            )
        except requests.RequestException as e:
            raise ResyError(f"{method} {path} network error: {e}") from e
        if resp.status_code >= 400:
            raise ResyError(
                f"{method} {path} failed: HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )
        try:
            return resp.json()
        except ValueError as e:
            raise ResyError(f"{method} {path} returned non-JSON: {resp.text[:300]}") from e

    # --- auth -----------------------------------------------------------

    def login(self, email: str, password: str) -> None:
        data = self._request("POST", "/3/auth/password", data={"email": email, "password": password})
        self.auth_token = data.get("token")
        if not self.auth_token:
            raise ResyError("Login succeeded but no auth token was returned")
        self.payment_method_id = _pick_payment_method(data)

    def load_user(self) -> None:
        """Fetch the account (used when authenticating with a saved token instead of a password)."""
        data = self._request("GET", "/2/user")
        self.payment_method_id = _pick_payment_method(data)

    # --- discovery ------------------------------------------------------

    def venue_id_from_slug(self, slug: str, location: str = "new-york-ny") -> tuple[int, str]:
        """Resolve a resy.com URL slug (e.g. 'carbone') to (venue_id, name)."""
        data = self._request("GET", "/3/venue", params={"url_slug": slug, "location": location})
        try:
            return int(data["id"]["resy"]), data.get("name", slug)
        except (KeyError, TypeError, ValueError) as e:
            raise ResyError(f"Could not resolve venue slug {slug!r}") from e

    def find_slots(self, venue_id: int, day: str, party_size: int) -> list[Slot]:
        data = self._request(
            "GET",
            "/4/find",
            params={"lat": 0, "long": 0, "day": day, "party_size": party_size, "venue_id": venue_id},
        )
        venues = (data.get("results") or {}).get("venues") or []
        slots: list[Slot] = []
        for v in venues:
            name = (v.get("venue") or {}).get("name", str(venue_id))
            for s in v.get("slots") or []:
                cfg = s.get("config") or {}
                start = (s.get("date") or {}).get("start")
                if not cfg.get("token") or not start:
                    continue
                slots.append(
                    Slot(
                        venue_id=venue_id,
                        venue_name=name,
                        start=datetime.strptime(start, "%Y-%m-%d %H:%M:%S"),
                        seating_type=cfg.get("type") or "Unknown",
                        config_token=cfg["token"],
                    )
                )
        return slots

    # --- booking --------------------------------------------------------

    def get_book_token(self, slot: Slot, party_size: int) -> str:
        data = self._request(
            "POST",
            "/3/details",
            json={
                "commit": 1,
                "config_id": slot.config_token,
                "day": slot.start.strftime("%Y-%m-%d"),
                "party_size": party_size,
            },
        )
        token = (data.get("book_token") or {}).get("value")
        if not token:
            raise ResyError("Slot details did not include a book token (slot may be gone)")
        return token

    def book(self, slot: Slot, party_size: int) -> Booking:
        if not self.auth_token:
            raise ResyError("Must be logged in to book")
        book_token = self.get_book_token(slot, party_size)
        form = {"book_token": book_token, "source_id": "resy.com-venue-details"}
        if self.payment_method_id is not None:
            form["struct_payment_method"] = json.dumps({"id": self.payment_method_id})
        data = self._request("POST", "/3/book", data=form)
        if not (data.get("resy_token") or data.get("reservation_id")):
            raise ResyError(f"Unexpected booking response: {str(data)[:300]}")
        return Booking(
            reservation_id=str(data["reservation_id"]) if data.get("reservation_id") else None,
            resy_token=data.get("resy_token"),
            slot=slot,
        )


def _pick_payment_method(data: dict) -> int | None:
    methods = data.get("payment_methods") or []
    for m in methods:
        if m.get("is_default"):
            return m.get("id")
    if methods:
        return methods[0].get("id")
    return data.get("payment_method_id")
