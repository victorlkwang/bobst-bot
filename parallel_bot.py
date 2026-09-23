"""
parallel_bot.py  --  stack booking_bot runs for several credentials, one SSO
login at a time.

Each credential runs booking_bot in its own PROCESS (own browser, own cookie
jar). They start one after another, gated on the SSO login:

  credential 1 starts right away. The moment its SSO login passes OR fails
  (Duo approved, denied or timed out, wrong password -- any outcome),
  credential 2 starts while credential 1 carries on booking. When credential
  2's login resolves, credential 3 starts, and so on. Only one Duo push is
  ever waiting on your phone, and by the time the last one is answered every
  credential is booking at once.

A credential never starts just because the one ahead is slow: it waits until
that login has passed or failed, or that process has died. --login-timeout
puts a cap on the wait if you want one.

All credentials book the same room, so while they overlap they also take
turns per day: a credential only loads a date once every credential ahead of
it has finished booking that date. Otherwise two of them would see the same
green cells, both would select them, and the second would be refused at
"Submit my Booking" and lose that day.

Every log line is prefixed with the credential's [name], and a per-date summary
prints per credential at the end.

    python parallel_bot.py                 # defaults
    python parallel_bot.py LL1-20          # pick the room
    python parallel_bot.py LL2-07 --sections 2
"""
import argparse
import multiprocessing as mp
import queue
import time

from booking_bot import (
    load_credentials, run_one_credential, print_summary,
    CRED_FILE, ROOM_TO_BOOK, SECTIONS, MAX_DAYS,
)

LOGIN_TIMEOUT = None    # seconds to wait on the login ahead; None = until it resolves
ALL_DAYS = 1 << 30      # progress value: this credential is done with every day


def _worker(job, start_event, next_event, progress, login_timeout, results):
    """Wait for the credential ahead to pass/fail its SSO login, then book,
    releasing the next credential as soon as our own login resolves."""
    idx, netid, password, names, room, sections, max_days = job
    me, name, total = idx - 1, names[idx - 1], len(names)

    if start_event is not None and not start_event.wait(timeout=login_timeout):
        print(f"[{name}] {names[me - 1]}'s login still hadn't resolved after "
              f"{login_timeout}s; starting anyway", flush=True)

    handed_off = {"done": False}

    def hand_off(status=None):
        """Our Duo has resolved -- let the next credential log in."""
        if handed_off["done"]:
            return
        handed_off["done"] = True
        then = "; starting the next credential" if next_event is not None else ""
        print(f"[{name}] login finished ({status}){then}", flush=True)
        if next_event is not None:
            next_event.set()

    def before_day(offset):
        """Don't load a date until every credential ahead has booked it, so we
        see their bookings rather than racing them for the same cells. (All of
        them, not just the one in front: that one may have dropped out.)"""
        since, told = time.time(), False
        while True:
            unfinished = [j for j in range(me) if progress[j] < offset]
            if not unfinished:
                return
            if not told and time.time() - since > 2:
                print(f"[{name}] waiting for {names[unfinished[-1]]} to finish day "
                      f"{offset + 1} before loading it", flush=True)
                told = True
            time.sleep(0.25)

    def after_day(offset):
        progress[me] = max(progress[me], offset)

    result = {"idx": idx, "netid": netid, "name": name, "room": room,
              "days": [], "login_failed": False}
    try:
        result = run_one_credential(netid, password, name, idx, total,
                                    room=room, sections=sections,
                                    max_days=max_days, on_login_done=hand_off,
                                    before_day=before_day, after_day=after_day)
    except Exception as e:
        result["error"] = str(e)
        print(f"[{name}] ERROR: {e}", flush=True)
    finally:
        # Backstops: a run that ends (or crashes) before logging in, or before
        # reaching every day, must not strand the credentials behind it.
        hand_off("run ended")
        progress[me] = ALL_DAYS
        results.put(result)


def build_parser():
    p = argparse.ArgumentParser(
        description="Book a Bobst room for several credentials, one SSO login at a time.")
    p.add_argument("room", nargs="?", default=ROOM_TO_BOOK,
                   help=f"room number, e.g. LL2-07 or LL1-20 (default: {ROOM_TO_BOOK})")
    p.add_argument("--sections", type=int, default=SECTIONS,
                   help=f"click groups per day; each is ~1 hr (default: {SECTIONS})")
    p.add_argument("--max-days", type=int, default=MAX_DAYS,
                   help=f"safety cap on days to walk (default: {MAX_DAYS})")
    p.add_argument("--login-timeout", type=int, default=LOGIN_TIMEOUT,
                   help="start the next credential after N seconds even if the "
                        "previous SSO login hasn't passed or failed yet "
                        "(default: wait until it does)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    creds = load_credentials(CRED_FILE)
    total = len(creds)
    if total == 0:
        print("No credentials found.")
        return

    # login_done[i] is set once credential i's SSO login has passed or failed,
    # and that is what starts credential i+1. progress[i] is the last day
    # offset credential i has finished (ALL_DAYS once its run is over).
    login_done = [mp.Event() for _ in range(total)]
    progress = mp.Array("i", [-1] * total)
    results = mp.Queue()

    names = tuple(name for _, _, name in creds)
    procs = []
    for i, (netid, pw, name) in enumerate(creds):
        job = (i + 1, netid, pw, names, args.room, args.sections, args.max_days)
        procs.append(mp.Process(
            target=_worker,
            args=(job, login_done[i - 1] if i > 0 else None,
                  login_done[i] if i + 1 < total else None,
                  progress, args.login_timeout, results),
            name=f"cred-{i + 1}-{name}",
        ))

    print(f"Running {total} credential(s) in room {args.room}, one SSO login at "
          f"a time.\nApprove each Duo push as it arrives -- the next credential "
          f"starts the moment that login passes or fails.")

    for p in procs:
        p.start()

    # Drain as they finish rather than after join(), so a full queue pipe can
    # never deadlock a worker that is trying to hand back its result.
    collected = []
    while len(collected) < total:
        try:
            collected.append(results.get(timeout=1))
            continue
        except queue.Empty:
            pass
        for i, p in enumerate(procs):
            # A worker killed outright never reaches its finally: release the
            # credentials queued behind it here instead.
            if p.exitcode is not None and progress[i] != ALL_DAYS:
                print(f"[{names[i]}] process died (exit {p.exitcode}); "
                      f"releasing the credentials behind it", flush=True)
                login_done[i].set()
                progress[i] = ALL_DAYS
                collected.append({"idx": i + 1, "netid": creds[i][0],
                                  "name": names[i], "room": args.room, "days": [],
                                  "error": f"process died (exit {p.exitcode})"})
        if not any(p.is_alive() for p in procs):
            break

    for p in procs:
        p.join(timeout=30)

    print_summary(sorted(collected, key=lambda r: r.get("idx", 0)))


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)  # required on macOS/Windows
    main()
