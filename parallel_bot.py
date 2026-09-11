"""
parallel_bot.py  --  run several credentials, one Duo push at a time.

Each credential runs in its own PROCESS (own browser, own cookie jar), but the
Duo pushes are handed off in order: credential 1 logs in first, and the moment
its push resolves -- approved OR denied -- credential 2 starts its own login
while credential 1 carries on booking. By the time the last login is done they
are all booking at once.

This replaces the old fixed --stagger delay, which guessed how long Duo would
take and fired overlapping pushes whenever it guessed low, leaving you unable
to tell which push belonged to which account.

Every log line is prefixed with the credential's [name], and a per-date summary
prints per credential at the end.

    python parallel_bot.py                 # defaults
    python parallel_bot.py LL1-20          # pick the room
    python parallel_bot.py LL2-07 --sections 2
"""
import argparse
import multiprocessing as mp
import queue

from booking_bot import (
    load_credentials, run_one_credential, print_summary,
    CRED_FILE, ROOM_TO_BOOK, SECTIONS, MAX_DAYS,
)

LOGIN_TIMEOUT = 300     # seconds to wait on the credential ahead of us


def _worker(job, start_event, next_event, login_timeout, results):
    """Wait our turn to log in, release the next credential, then keep booking."""
    idx, netid, password, name, total, room, sections, max_days = job

    if start_event is not None and not start_event.wait(timeout=login_timeout):
        print(f"[{name}] the credential ahead did not finish logging in within "
              f"{login_timeout}s; starting anyway", flush=True)

    handed_off = {"done": False}

    def hand_off(status=None):
        """Duo has resolved -- let the next credential push while we book on."""
        if handed_off["done"]:
            return
        handed_off["done"] = True
        print(f"[{name}] login finished ({status}); starting next credential",
              flush=True)
        if next_event is not None:
            next_event.set()

    result = {"idx": idx, "netid": netid, "name": name, "room": room,
              "days": [], "login_failed": False}
    try:
        result = run_one_credential(netid, password, name, idx, total,
                                    room=room, sections=sections,
                                    max_days=max_days, on_login_done=hand_off)
    except Exception as e:
        result["error"] = str(e)
        print(f"[{name}] ERROR: {e}", flush=True)
    finally:
        # Backstop: a crash before the login must not strand everyone behind us.
        hand_off("run ended")
        results.put(result)


def build_parser():
    p = argparse.ArgumentParser(
        description="Book a Bobst room for several credentials, one Duo push at a time.")
    p.add_argument("room", nargs="?", default=ROOM_TO_BOOK,
                   help=f"room number, e.g. LL2-07 or LL1-20 (default: {ROOM_TO_BOOK})")
    p.add_argument("--sections", type=int, default=SECTIONS,
                   help=f"click groups per day; each is ~1 hr (default: {SECTIONS})")
    p.add_argument("--max-days", type=int, default=MAX_DAYS,
                   help=f"safety cap on days to walk (default: {MAX_DAYS})")
    p.add_argument("--login-timeout", type=int, default=LOGIN_TIMEOUT,
                   help="seconds to wait for the previous credential's Duo push "
                        f"before starting anyway (default: {LOGIN_TIMEOUT})")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    creds = load_credentials(CRED_FILE)
    total = len(creds)
    if total == 0:
        print("No credentials found.")
        return

    # events[i] is set once credential i's login has resolved, and that is what
    # releases credential i+1. The first credential waits on nothing.
    events = [mp.Event() for _ in range(total)]
    results = mp.Queue()

    procs = []
    for i, (netid, pw, name) in enumerate(creds):
        job = (i + 1, netid, pw, name, total, args.room, args.sections,
               args.max_days)
        procs.append(mp.Process(
            target=_worker,
            args=(job, events[i - 1] if i > 0 else None, events[i],
                  args.login_timeout, results),
            name=f"cred-{i + 1}-{name}",
        ))

    print(f"Running {total} credential(s) in room {args.room}, one Duo push at a "
          f"time.\nApprove each push as it arrives -- the next credential starts "
          f"the moment you do.")

    for p in procs:
        p.start()

    # Drain as they finish rather than after join(), so a full queue pipe can
    # never deadlock a worker that is trying to hand back its result.
    collected = []
    while len(collected) < total:
        try:
            collected.append(results.get(timeout=5))
        except queue.Empty:
            if not any(p.is_alive() for p in procs):
                break

    for p in procs:
        p.join(timeout=30)

    print_summary(sorted(collected, key=lambda r: r.get("idx", 0)))


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)  # required on macOS/Windows
    main()
