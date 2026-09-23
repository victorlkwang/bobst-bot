# bobst-bot

Scripts to book NYU Bobst group study rooms on [LibCal](https://nyu.libcal.com/r/new).
LibCal has no self-serve public API, so these tools authenticate as you (NetID +
Duo) and then either drive the booking page with a browser or talk to LibCal's
own internal endpoints directly.

> **Heads up on account rules.** This automates bookings against a system that
> isn't meant to be automated. Run it with **your own** NetID only — sharing NetID
> credentials violates NYU's IT acceptable-use policy, and bulk-reserving rooms is
> the pattern that gets booking privileges revoked. Use it lightly.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium          # one-time browser download
```

Then create `credential.txt` in the repo root — one credential per line, fields
separated by spaces:

```
NetID  password  Display Name
```

For example:

```
abc1234 your-password-here Alice Example
```

- NetID works with or without `@nyu.edu`.
- Display Name is optional; it only labels console and summary output.
- Lines starting with `#` are ignored.

`credential.txt`, `state.json`, `captures/`, and `confirmations/` are all
git-ignored so your password and cookies never get committed.

## The scripts

| Script | What it does |
| --- | --- |
| `booking_bot.py` | Browser bot. Books one room across the ~14-day window, one day at a time (avoids LibCal's 180-min/day cart limit), jumping to each date with **Go To Date**. You approve the Duo push once. |
| `parallel_bot.py` | Stacks `booking_bot` runs for several credentials, each in its own process. The next credential starts only once the previous one's SSO login has passed or failed, so only one Duo push is ever pending, and they all end up booking at once. |
| `capture_requests.py` | Records the real API calls LibCal fires during one manual booking, so they can be replayed without a browser. Also saves your login cookie to `state.json`. |
| `http_booking.py` | Talks to those endpoints directly with the saved cookie — seconds instead of minutes, no browser. |

### Browser bot (works out of the box)

```bash
python booking_bot.py                 # single credential, default room
python booking_bot.py LL1-20          # pick the room on the command line
python parallel_bot.py LL2-07         # all credentials in credential.txt, one room

python booking_bot.py --help          # all options
```

The room is a positional argument, so `python booking_bot.py <room-number>` is
all you need for the common case. Extra flags tune the run without editing code:

| Flag | booking_bot | parallel_bot | Meaning |
| --- | :---: | :---: | --- |
| `<room>` | ✓ | ✓ | room number, e.g. `LL2-07` (positional; defaults to `LL2-07`) |
| `--sections N` | ✓ | ✓ | click groups per day, ~1 hr each (default 3) |
| `--max-days N` | ✓ | ✓ | safety cap on days to walk (default 20) |
| `--headless` | ✓ | | no visible window (only with a saved session; Duo needs a window) |
| `--login-timeout N` | | ✓ | start the next credential after N seconds even if the previous SSO login hasn't passed or failed (default: wait until it has) |

A browser window opens; watch your phone for the Duo push and approve it.
Confirmation screenshots land in `confirmations/`.

Each date is reached through the **Go To Date** calendar, so every day costs the
same couple of clicks instead of one next-arrow click per day ahead. If that
calendar ever can't be used, the bot says so in the log and falls back to the
arrows.

With `parallel_bot.py` you get one window per credential, but only one Duo push
at a time: approve (or deny) the first, and the next credential logs in while
the first carries on booking. Every credential is running by the time the last
push is answered. Since they all book the same room, they also take turns per
day: a credential only loads a date once the credentials ahead of it have
finished booking it. Otherwise two of them would pick the same free slots and
the second would lose that day.

### The faster HTTP path (opt-in, needs a one-time capture)

The browser bot is slow and brittle because it scrolls, clicks, and sleeps.
Underneath, LibCal is just POSTing to a couple of JSON endpoints. To use those
directly:

```bash
# 1. Record one real booking (log in + Duo, then book by hand). Writes
#    captures/session-*.jsonl and saves your cookie to state.json.
python capture_requests.py

# 2. See which endpoints handle availability vs. booking:
python http_booking.py --inspect "captures/session-*.jsonl"

# 3. Replay the captured booking request with your cookie, to confirm it works:
python http_booking.py --replay "captures/session-*.jsonl" --match book
```

Then fill the `AVAILABILITY` and `BOOK` constants in `http_booking.py` from what
the capture showed, and you can book new dates with `get_availability()` /
`submit_booking()` — no browser needed. The endpoint paths and field names come
from *your* capture rather than being hard-coded, because they vary by LibCal
install and change over time.

## How it fits together

```
capture_requests.py  --(records)-->  captures/*.jsonl   +   state.json (cookie)
                                          |                     |
                                          v                     v
                                   http_booking.py  --(replays / books directly)
```

`booking_bot.py` remains the fallback that always works even if the internal
endpoints change.
