"""
parallel_bot.py  --  run several credentials at once (per-day booking each).

Each credential runs in its own PROCESS (own browser, own cookie jar). Up to
MAX_CONCURRENT run at a time; extras queue and start when a slot frees. First
batch is staggered by STAGGER_SECONDS so the Duo pushes don't collide. Every
log line is prefixed with the credential's [name], and a per-date summary
prints per credential at the end.

    python parallel_bot.py                 # defaults
    python parallel_bot.py LL1-20          # pick the room
    python parallel_bot.py LL2-07 --max-concurrent 2 --stagger 15
"""
import argparse
import multiprocessing as mp
import time

from booking_bot import (
    load_credentials, run_one_credential, print_summary,
    CRED_FILE, ROOM_TO_BOOK, SECTIONS, MAX_DAYS,
)

MAX_CONCURRENT = 4      # simultaneous browsers (lower if the machine struggles)
STAGGER_SECONDS = 10    # gap before each launch in the first batch


def _worker(job):
    idx, netid, password, name, delay, total, room, sections, max_days = job
    time.sleep(delay)
    return run_one_credential(netid, password, name, idx, total,
                              room=room, sections=sections, max_days=max_days)


def build_parser():
    p = argparse.ArgumentParser(
        description="Book a Bobst room for several credentials at once.")
    p.add_argument("room", nargs="?", default=ROOM_TO_BOOK,
                   help=f"room number, e.g. LL2-07 or LL1-20 (default: {ROOM_TO_BOOK})")
    p.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT,
                   help=f"simultaneous browsers (default: {MAX_CONCURRENT})")
    p.add_argument("--stagger", type=int, default=STAGGER_SECONDS,
                   help=f"seconds between first-batch launches (default: {STAGGER_SECONDS})")
    p.add_argument("--sections", type=int, default=SECTIONS,
                   help=f"click groups per day; each is ~1 hr (default: {SECTIONS})")
    p.add_argument("--max-days", type=int, default=MAX_DAYS,
                   help=f"safety cap on days to walk (default: {MAX_DAYS})")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    creds = load_credentials(CRED_FILE)
    total = len(creds)
    if total == 0:
        print("No credentials found.")
        return

    workers = min(args.max_concurrent, total)
    # Stagger only the first batch; queued jobs start with no extra delay.
    jobs = [(i + 1, netid, pw, name, (i if i < workers else 0) * args.stagger,
             total, args.room, args.sections, args.max_days)
            for i, (netid, pw, name) in enumerate(creds)]

    print(f"Running {total} credential(s), up to {workers} at once, "
          f"{args.stagger}s apart, room {args.room}. "
          f"Approve each Duo push as it arrives.")

    with mp.Pool(processes=workers) as pool:
        results = pool.map(_worker, jobs)

    print_summary(results)


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)  # required on macOS/Windows
    main()
