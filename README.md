# Resy Reservation Agent

Watches [Resy](https://resy.com) for openings at hard-to-book NYC restaurants and books
the best match automatically, or just alerts you.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then add your Resy email/password
```

Your Resy account needs a **payment method on file**. Many NYC spots require a card to hold
the table. The agent uses your default card.

## Usage

**1. Find the restaurant.** Use the slug from its Resy URL, e.g.
`resy.com/cities/new-york-ny/venues/carbone` → `carbone`.

```bash
python -m resy_agent venue carbone
```

**2. See what's open right now:**

```bash
python -m resy_agent check --venue carbone --date 2026-11-14 --party 2 --earliest 18:00 --latest 21:30
```

**3. Watch and book.** Without `--book` the agent only alerts you. Add `--book` when you're ready:

```bash
python -m resy_agent watch --venue carbone --date 2026-11-14 --party 2 \
    --earliest 18:00 --latest 21:30 --target 19:30 --exclude-seating bar --book
```

The agent checks every ~30s (with random jitter), tries matching slots closest to `--target`
first, and moves to the next slot if one is taken before it can book. It stops once it books
a table or when the reservation night arrives (change that with `--until`).

### Catching the moment reservations drop

Most hard-to-get NYC restaurants release tables at a fixed time, e.g. "10:00 AM, 30 days
ahead". The restaurant's Resy page usually says when. Pass that time with `--release-at`
(NYC time). The agent waits quietly, then checks every second from 10s before the release
until `--burst-seconds` after it:

```bash
python -m resy_agent watch --venue carbone --date 2026-11-14 --party 2 \
    --earliest 18:00 --latest 22:00 --book --release-at "2026-10-15 10:00"
```

Start it a few minutes early on a machine that will stay awake. After the burst window ends
it keeps watching at the normal interval to catch cancellations.

### More options

- `--venue` can be repeated to watch several restaurants at once. It books the first hit.
- `--seating "Dining Room"` accepts only matching seating types. `--exclude-seating Patio`
  rejects them.
- Phone alerts: install the free [ntfy](https://ntfy.sh) app, subscribe to a hard-to-guess
  topic, and set `NTFY_TOPIC` in `.env`.

## Notes and caveats

- This uses the private API that resy.com's own website calls. It is not an official public API,
  so Resy can change it without notice. If requests start failing with 401/419 errors, the public
  web key may have changed. Copy the new `api_key` from the `Authorization` header in your
  browser's dev tools on resy.com and set it as `RESY_API_KEY`.
- Automated access may break Resy's Terms of Service, and accounts can be flagged. Keep the
  polling interval reasonable. Use this for your own reservations only, and cancel any you
  won't use.
- Bookings are real. A cancellation or no-show fee may apply.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```
