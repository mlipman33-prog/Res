"""The polling loop: watch for openings and (optionally) book the best one."""

from __future__ import annotations

import logging
import random
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .client import Booking, ResyClient, ResyError, Slot
from .matching import Preferences
from .notify import notify

log = logging.getLogger(__name__)
NYC = ZoneInfo("America/New_York")


@dataclass
class Venue:
    id: int
    name: str


@dataclass
class WatchResult:
    booking: Booking | None = None
    found: list[Slot] | None = None


class ReservationAgent:
    def __init__(
        self,
        client: ResyClient,
        venues: list[Venue],
        day: str,
        party_size: int,
        prefs: Preferences,
        book: bool = False,
        interval: float = 30.0,
        jitter: float = 0.25,
        release_at: datetime | None = None,
        burst_lead: float = 10.0,
        burst_seconds: float = 120.0,
        burst_interval: float = 1.0,
        deadline: datetime | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(NYC),
        sleep: Callable[[float], None] = _time.sleep,
    ):
        self.client = client
        self.venues = venues
        self.day = day
        self.party_size = party_size
        self.prefs = prefs
        self.book = book
        self.interval = interval
        self.jitter = jitter
        self.release_at = release_at
        self.burst_lead = burst_lead
        self.burst_seconds = burst_seconds
        self.burst_interval = burst_interval
        self.deadline = deadline
        self.now = now
        self.sleep = sleep

    # --- one pass -------------------------------------------------------

    def check_once(self) -> WatchResult | None:
        """Poll every venue once. Returns a result if something was found/booked."""
        for venue in self.venues:
            try:
                slots = self.client.find_slots(venue.id, self.day, self.party_size)
            except ResyError as e:
                if e.status_code == 429:
                    log.warning("Rate limited by Resy; backing off")
                    self.sleep(max(self.interval, 60))
                else:
                    log.warning("Lookup failed for %s: %s", venue.name, e)
                continue

            candidates = self.prefs.rank(slots)
            log.info(
                "%s: %d open slot(s), %d match your preferences",
                venue.name, len(slots), len(candidates),
            )
            if not candidates:
                continue

            if not self.book:
                summary = "\n".join(s.describe() for s in candidates)
                log.info("Matching availability:\n%s", summary)
                notify("Resy availability found", summary)
                return WatchResult(found=candidates)

            for slot in candidates:
                try:
                    log.info("Attempting to book %s", slot.describe())
                    booking = self.client.book(slot, self.party_size)
                except ResyError as e:
                    log.warning("Booking failed for %s: %s", slot.describe(), e)
                    continue
                msg = f"Booked {slot.describe()} for {self.party_size}"
                log.info("%s (reservation %s)", msg, booking.reservation_id)
                notify("Resy reservation booked!", msg)
                return WatchResult(booking=booking)
        return None

    # --- the loop -------------------------------------------------------

    def _next_delay(self) -> float:
        now = self.now()
        if self.release_at is not None:
            burst_start = self.release_at - timedelta(seconds=self.burst_lead)
            burst_end = self.release_at + timedelta(seconds=self.burst_seconds)
            if now < burst_start:
                # Sleep up to the burst window, but never longer than a normal interval
                # (so a long wait still produces periodic log output / checks).
                return min((burst_start - now).total_seconds(), self.interval)
            if now <= burst_end:
                return self.burst_interval
        return self.interval * (1 + random.uniform(-self.jitter, self.jitter))

    def run(self) -> WatchResult | None:
        log.info(
            "Watching %s on %s for %d (%s-%s)%s",
            ", ".join(v.name for v in self.venues), self.day, self.party_size,
            self.prefs.earliest.strftime("%H:%M"), self.prefs.latest.strftime("%H:%M"),
            "" if self.book else " [notify only]",
        )
        if self.release_at:
            log.info("Release time: %s (burst polling around it)", self.release_at.strftime("%Y-%m-%d %H:%M %Z"))

        while True:
            if self.deadline and self.now() >= self.deadline:
                log.info("Deadline reached without a booking; stopping.")
                return None

            waiting_for_release = (
                self.release_at is not None
                and self.now() < self.release_at - timedelta(seconds=self.burst_lead)
            )
            if not waiting_for_release:
                result = self.check_once()
                if result:
                    return result

            self.sleep(max(self._next_delay(), 0.0))
