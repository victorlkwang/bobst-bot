"""
parallel_bot.py  --  run several credentials at once (per-day booking each).

Each credential runs in its own PROCESS (own browser, own cookie jar). Up to
MAX_CONCURRENT run at a time; extras queue and start when a slot frees. First
batch is staggered by STAGGER_SECONDS so the Duo pushes don't collide. Every
log line is prefixed with the credential's [name], and a per-date summary
prints per credential at the end.

    python parallel_bot.py
"""
import multiprocessing as mp
import time

from booking_bot import (
    load_credentials, run_one_credential, print_summary, CRED_FILE, ROOM_TO_BOOK,
)

MAX_CONCURRENT = 4      # simultaneous browsers (lower if the machine struggles)
STAGGER_SECONDS = 10    # gap before each launch in the first batch


def _worker(job):
    idx, netid, password, name, delay, total = job
    time.sleep(delay)
    return run_one_credential(netid, password, name, idx, total)


def main():
    creds = load_credentials(CRED_FILE)
    total = len(creds)
    if total == 0:
        print("No credentials found.")
        return

    workers = min(MAX_CONCURRENT, total)
    # Stagger only the first batch; queued jobs start with no extra delay.
    jobs = [(i + 1, netid, pw, name, (i if i < workers else 0) * STAGGER_SECONDS, total)
            for i, (netid, pw, name) in enumerate(creds)]

    print(f"Running {total} credential(s), up to {workers} at once, "
          f"{STAGGER_SECONDS}s apart, room {ROOM_TO_BOOK}. "
          f"Approve each Duo push as it arrives.")

    with mp.Pool(processes=workers) as pool:
        results = pool.map(_worker, jobs)

    print_summary(results)


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)  # required on macOS/Windows
    main()
