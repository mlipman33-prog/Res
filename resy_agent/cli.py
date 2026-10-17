"""Command-line entry point: `python -m resy_agent ...`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from .agent import NYC, ReservationAgent, Venue
from .client import DEFAULT_API_KEY, ResyClient, ResyError
from .matching import Preferences, parse_time


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (KEY=value lines); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def make_client(require_auth: bool) -> ResyClient:
    client = ResyClient(api_key=os.environ.get("RESY_API_KEY", DEFAULT_API_KEY))
    token = os.environ.get("RESY_AUTH_TOKEN")
    email, password = os.environ.get("RESY_EMAIL"), os.environ.get("RESY_PASSWORD")
    if email and password:
        client.login(email, password)
    elif token:
        client.auth_token = token
        client.load_user()
    elif require_auth:
        sys.exit("Booking needs credentials: set RESY_EMAIL and RESY_PASSWORD (or RESY_AUTH_TOKEN) in .env")
    return client


def resolve_venues(client: ResyClient, specs: list[str], location: str) -> list[Venue]:
    venues = []
    for spec in specs:
        if spec.isdigit():
            venues.append(Venue(int(spec), f"venue {spec}"))
        else:
            vid, name = client.venue_id_from_slug(spec, location)
            venues.append(Venue(vid, name))
    return venues


def parse_nyc_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=NYC)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="resy_agent", description="Watch Resy for openings and book them.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("venue", help="Look up a venue ID from its resy.com URL slug")
    v.add_argument("slug", help="e.g. 'carbone' from resy.com/cities/new-york-ny/venues/carbone")
    v.add_argument("--location", default="new-york-ny")

    def common(sp):
        sp.add_argument("--venue", action="append", required=True,
                        help="Venue slug or numeric ID; repeat to watch several restaurants")
        sp.add_argument("--date", required=True, help="Reservation date, YYYY-MM-DD")
        sp.add_argument("--party", type=int, default=2, help="Party size (default 2)")
        sp.add_argument("--earliest", default="17:00", help="Earliest acceptable time (default 17:00)")
        sp.add_argument("--latest", default="22:00", help="Latest acceptable time (default 22:00)")
        sp.add_argument("--target", help="Preferred time; closest slots are tried first")
        sp.add_argument("--seating", action="append", default=[],
                        help="Only accept seating types containing this text (e.g. 'Dining Room')")
        sp.add_argument("--exclude-seating", action="append", default=[],
                        help="Reject seating types containing this text (e.g. 'Bar', 'Patio')")
        sp.add_argument("--location", default="new-york-ny")

    c = sub.add_parser("check", help="Show current availability once")
    common(c)

    w = sub.add_parser("watch", help="Poll until a matching slot opens, then notify or book")
    common(w)
    w.add_argument("--book", action="store_true",
                   help="Actually book the best matching slot (otherwise only notify)")
    w.add_argument("--interval", type=float, default=30.0, help="Seconds between checks (default 30)")
    w.add_argument("--release-at",
                   help="When reservations drop, NYC time 'YYYY-MM-DD HH:MM'; polls rapidly around it")
    w.add_argument("--burst-seconds", type=float, default=120.0,
                   help="How long to poll rapidly after the release time (default 120)")
    w.add_argument("--until", help="Stop watching at this NYC time 'YYYY-MM-DD HH:MM' "
                                   "(default: the reservation date at --latest)")
    return p


def prefs_from_args(args) -> Preferences:
    return Preferences(
        earliest=parse_time(args.earliest),
        latest=parse_time(args.latest),
        target=parse_time(args.target) if args.target else None,
        seating_include=args.seating,
        seating_exclude=args.exclude_seating,
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        if args.command == "venue":
            vid, name = make_client(require_auth=False).venue_id_from_slug(args.slug, args.location)
            print(f"{name}: {vid}")
            return 0

        datetime.strptime(args.date, "%Y-%m-%d")  # validate early
        prefs = prefs_from_args(args)
        client = make_client(require_auth=args.command == "watch" and args.book)
        venues = resolve_venues(client, args.venue, args.location)

        if args.command == "check":
            for venue in venues:
                slots = client.find_slots(venue.id, args.date, args.party)
                print(f"\n{venue.name} — {args.date}, party of {args.party}: {len(slots)} open slot(s)")
                for s in sorted(slots, key=lambda s: s.start):
                    mark = "*" if prefs.accepts(s) else " "
                    print(f"  {mark} {s.start:%I:%M %p}  {s.seating_type}")
            print("\n(* = matches your preferences)")
            return 0

        deadline = (
            parse_nyc_datetime(args.until) if args.until
            else datetime.combine(datetime.strptime(args.date, "%Y-%m-%d").date(), prefs.latest, NYC)
        )
        agent = ReservationAgent(
            client, venues, args.date, args.party, prefs,
            book=args.book,
            interval=args.interval,
            release_at=parse_nyc_datetime(args.release_at) if args.release_at else None,
            burst_seconds=args.burst_seconds,
            deadline=deadline,
        )
        result = agent.run()
        if result and result.booking:
            b = result.booking
            print(f"\nBOOKED: {b.slot.describe()} — reservation {b.reservation_id or b.resy_token}")
            print("Check the Resy app / your email for confirmation.")
            return 0
        return 0 if result else 1
    except ResyError as e:
        logging.error("%s", e)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
