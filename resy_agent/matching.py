"""Decide which open slots are acceptable and in what order to try them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time

from .client import Slot


@dataclass
class Preferences:
    earliest: time = time(17, 0)
    latest: time = time(22, 0)
    # Slots closest to this time are tried first. Defaults to the middle of the window.
    target: time | None = None
    # Case-insensitive substrings; empty means any seating type is fine.
    seating_include: list[str] = field(default_factory=list)
    seating_exclude: list[str] = field(default_factory=list)

    def accepts(self, slot: Slot) -> bool:
        t = slot.start.time()
        if not (self.earliest <= t <= self.latest):
            return False
        seating = slot.seating_type.lower()
        if self.seating_include and not any(s.lower() in seating for s in self.seating_include):
            return False
        if any(s.lower() in seating for s in self.seating_exclude):
            return False
        return True

    def _target_minutes(self) -> float:
        if self.target is not None:
            return _minutes(self.target)
        return (_minutes(self.earliest) + _minutes(self.latest)) / 2

    def rank(self, slots: list[Slot]) -> list[Slot]:
        """Acceptable slots, best first (closest to target time, earlier wins ties)."""
        target = self._target_minutes()
        ok = [s for s in slots if self.accepts(s)]
        return sorted(ok, key=lambda s: (abs(_minutes(s.start.time()) - target), s.start))


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def parse_time(value: str) -> time:
    """Accepts '19:30', '7:30pm', '7pm'."""
    v = value.strip().lower().replace(" ", "")
    for fmt in ("%H:%M", "%I:%M%p", "%I%p", "%H"):
        try:
            return datetime.strptime(v, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized time: {value!r}")
