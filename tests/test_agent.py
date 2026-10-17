import json
from datetime import datetime, time, timedelta

import pytest

from resy_agent.agent import NYC, ReservationAgent, Venue
from resy_agent.client import ResyClient, ResyError
from resy_agent.matching import Preferences, parse_time


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Routes (method, path) to queued responses and records calls."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        path = url.replace("https://api.resy.com", "")
        self.calls.append((method, path, kwargs, headers))
        queue = self.routes[(method, path)]
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        return FakeResponse(*resp)


def find_payload(*slots):
    return {
        "results": {
            "venues": [
                {
                    "venue": {"name": "Carbone"},
                    "slots": [
                        {"config": {"token": f"tok-{t}", "type": seat}, "date": {"start": f"2026-11-14 {t}:00"}}
                        for t, seat in slots
                    ],
                }
            ]
        }
    }


EMPTY = {"results": {"venues": []}}


def make_agent(session, book=True, **kw):
    client = ResyClient(auth_token="auth", session=session)
    client.payment_method_id = 42
    prefs = kw.pop("prefs", Preferences(earliest=time(18), latest=time(21), target=time(19, 30)))
    return ReservationAgent(
        client, [Venue(6194, "Carbone")], "2026-11-14", 2, prefs, book=book, sleep=lambda s: None, **kw
    )


def test_parse_time_formats():
    assert parse_time("19:30") == time(19, 30)
    assert parse_time("7:30pm") == time(19, 30)
    assert parse_time("7 PM") == time(19)
    with pytest.raises(ValueError):
        parse_time("dinner")


def test_preferences_rank_and_filter():
    session = FakeSession({("GET", "/4/find"): [(200, find_payload(
        ("17:00", "Dining Room"), ("18:15", "Bar"), ("19:45", "Dining Room"), ("20:30", "Patio"), ("21:30", "Dining Room"),
    ))]})
    slots = ResyClient(session=session).find_slots(6194, "2026-11-14", 2)
    prefs = Preferences(earliest=time(18), latest=time(21), target=time(19, 30), seating_exclude=["patio"])
    ranked = prefs.rank(slots)
    assert [s.start.strftime("%H:%M") for s in ranked] == ["19:45", "18:15"]
    prefs.seating_include = ["dining"]
    assert [s.start.strftime("%H:%M") for s in prefs.rank(slots)] == ["19:45"]


def test_books_best_slot_and_falls_back_when_first_fails():
    session = FakeSession({
        ("GET", "/4/find"): [(200, find_payload(("19:30", "Dining Room"), ("20:00", "Dining Room")))],
        # First slot gets taken between find and details; second succeeds.
        ("POST", "/3/details"): [(404, {"message": "gone"}), (200, {"book_token": {"value": "bt-2000"}})],
        ("POST", "/3/book"): [(201, {"resy_token": "rt", "reservation_id": 999})],
    })
    result = make_agent(session).run()
    assert result.booking.reservation_id == "999"
    assert result.booking.slot.start.strftime("%H:%M") == "20:00"
    book_call = [c for c in session.calls if c[1] == "/3/book"][0]
    assert book_call[2]["data"]["book_token"] == "bt-2000"
    assert json.loads(book_call[2]["data"]["struct_payment_method"]) == {"id": 42}
    assert book_call[3]["X-Resy-Auth-Token"] == "auth"


def test_notify_only_mode_never_books():
    session = FakeSession({("GET", "/4/find"): [(200, find_payload(("19:00", "Dining Room")))]})
    result = make_agent(session, book=False).run()
    assert result.booking is None and len(result.found) == 1
    assert all(c[1] == "/4/find" for c in session.calls)


def test_keeps_polling_until_slot_appears():
    session = FakeSession({
        ("GET", "/4/find"): [(200, EMPTY), (500, {}), (200, EMPTY), (200, find_payload(("19:00", "Dining Room")))],
        ("POST", "/3/details"): [(200, {"book_token": {"value": "bt"}})],
        ("POST", "/3/book"): [(201, {"resy_token": "rt"})],
    })
    result = make_agent(session).run()
    assert result.booking.resy_token == "rt"
    assert sum(1 for c in session.calls if c[1] == "/4/find") == 4


def test_stops_at_deadline():
    clock = [datetime(2026, 11, 14, 12, 0, tzinfo=NYC)]
    def sleep(s):
        clock[0] += timedelta(seconds=s)
    session = FakeSession({("GET", "/4/find"): [(200, EMPTY)]})
    agent = make_agent(session, interval=60, jitter=0, deadline=clock[0] + timedelta(minutes=5), now=lambda: clock[0])
    agent.sleep = sleep
    assert agent.run() is None
    assert 4 <= len(session.calls) <= 6


def test_waits_for_release_then_bursts():
    release = datetime(2026, 10, 18, 10, 0, tzinfo=NYC)
    clock = [release - timedelta(hours=1)]
    sleeps = []
    def sleep(s):
        sleeps.append(s)
        clock[0] += timedelta(seconds=s)
    session = FakeSession({
        ("GET", "/4/find"): [(200, EMPTY)] * 3 + [(200, find_payload(("19:00", "Dining Room")))],
        ("POST", "/3/details"): [(200, {"book_token": {"value": "bt"}})],
        ("POST", "/3/book"): [(201, {"resy_token": "rt"})],
    })
    agent = make_agent(session, interval=30, release_at=release, burst_lead=10, burst_interval=1, now=lambda: clock[0])
    agent.sleep = sleep
    result = agent.run()
    assert result.booking is not None
    # No API calls before the burst window opened, and fast polling once it did.
    first_call_time = release - timedelta(seconds=10)
    assert clock[0] >= first_call_time
    assert sleeps[-3:] == [1, 1, 1]


def test_login_picks_default_payment_method():
    session = FakeSession({("POST", "/3/auth/password"): [(200, {
        "token": "t", "payment_methods": [{"id": 1}, {"id": 7, "is_default": True}],
    })]})
    client = ResyClient(session=session)
    client.login("a@b.c", "pw")
    assert client.auth_token == "t" and client.payment_method_id == 7


def test_http_error_carries_status():
    session = FakeSession({("GET", "/4/find"): [(429, {"message": "slow down"})]})
    with pytest.raises(ResyError) as e:
        ResyClient(session=session).find_slots(1, "2026-11-14", 2)
    assert e.value.status_code == 429


def test_network_errors_do_not_crash_watch():
    import requests

    class FlakySession(FakeSession):
        def request(self, method, url, **kw):
            if not self.calls:
                self.calls.append(("boom",))
                raise requests.ConnectionError("wifi dropped")
            return super().request(method, url, **kw)

    session = FlakySession({
        ("GET", "/4/find"): [(200, find_payload(("19:00", "Dining Room")))],
        ("POST", "/3/details"): [(200, {"book_token": {"value": "bt"}})],
        ("POST", "/3/book"): [(201, {"resy_token": "rt"})],
    })
    assert make_agent(session).run().booking.resy_token == "rt"
