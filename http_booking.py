"""
http_booking.py -- book via direct HTTP calls instead of driving the browser.

Once capture_requests.py has recorded a real booking, the two endpoints that
matter (fetch availability, submit booking) are visible in the .jsonl. This
module talks to them directly with your saved cookie, so a run takes seconds
instead of minutes and none of the flaky scroll/click/sleep logic is needed.

The intended workflow:

  1. python capture_requests.py            # log in once (Duo), do one booking
  2. python http_booking.py --inspect captures/session-*.jsonl
                                           # see the availability + book calls
  3. python http_booking.py --replay captures/session-*.jsonl --match book
                                           # re-send the captured booking as-is,
                                           # with your cookie, to prove it works
  4. Fill AVAILABILITY and BOOK below from what you saw, then use
     get_availability() / submit_booking() to book new dates programmatically.

Why a scaffold and not a finished booker: LibCal's exact endpoint paths and
POST field names differ by install and change over time, so they must come from
YOUR capture rather than being hard-coded blind. Steps 1-3 work today; step 4 is
a few-line fill-in once you've looked at the capture.

Auth model is unchanged from the browser bots: you still authenticate as
yourself via Duo (in capture_requests.py). This only reuses that session's
cookie -- it does not bypass login.
"""
import argparse
import glob
import json
import sys

import httpx

STATE_FILE = "state.json"
ORIGIN = "https://nyu.libcal.com"

# ---- fill these two in from your capture (step 4) ---------------------------
# Look in the .jsonl for the POST that returned the availability grid and the
# POST that submitted the booking. Copy their paths and body field names here.
AVAILABILITY = {
    "method": "POST",
    "path": None,          # e.g. "/spaces/availability/grid"  (from capture)
    "fields": {},          # static form fields the request always sends
}
BOOK = {
    "method": "POST",
    "path": None,          # e.g. "/ajax/space/book"           (from capture)
    "fields": {},          # static form fields the request always sends
}
# -----------------------------------------------------------------------------


def load_cookies(state_file=STATE_FILE):
    """Pull cookies out of the Playwright storage_state.json into a dict httpx
    can use. state.json is produced by capture_requests.py after Duo login."""
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)
    jar = {}
    for c in state.get("cookies", []):
        if ORIGIN.endswith(c.get("domain", "").lstrip(".")) or \
                c.get("domain", "").lstrip(".") in ORIGIN:
            jar[c["name"]] = c["value"]
    if not jar:
        raise SystemExit(
            f"No {ORIGIN} cookies in {state_file}. Run capture_requests.py "
            "first and complete a login.")
    return jar


def make_client(state_file=STATE_FILE):
    return httpx.Client(
        base_url=ORIGIN,
        cookies=load_cookies(state_file),
        headers={
            "User-Agent": "Mozilla/5.0",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/r/new",
        },
        timeout=30,
        follow_redirects=True,
    )


# ---- tools that work today (no fill-in needed) ------------------------------
def _iter_capture(pattern):
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"No capture files match: {pattern}")
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield path, json.loads(line)


def inspect(pattern):
    """Print the POSTs and interesting endpoints from a capture, so you can
    identify which is availability and which is the booking submit."""
    keywords = ("avail", "book", "space", "reserve", "request", "grid",
                "process", "checkout")
    print(f"{'METHOD':6} {'STATUS':6} URL")
    print("-" * 72)
    for _, rec in _iter_capture(pattern):
        if rec["method"] == "POST" or any(k in rec["url"].lower() for k in keywords):
            print(f"{rec['method']:6} {rec['status']:<6} {rec['url'].split('?', 1)[0]}")
            if rec.get("post_data"):
                body = rec["post_data"]
                print(f"       body: {body[:300]}")


def replay(pattern, match):
    """Find the captured request whose URL contains `match` and re-send it with
    your current cookie. Proves the cookie is valid and the endpoint replayable
    before you build anything on top of it. Uses the FIRST matching request."""
    target = None
    for _, rec in _iter_capture(pattern):
        if match.lower() in rec["url"].lower():
            target = rec
            break
    if not target:
        raise SystemExit(f"No captured request URL contains: {match!r}")

    print(f"Replaying: {target['method']} {target['url'].split('?', 1)[0]}")
    with make_client() as client:
        content_type = target.get("request_headers", {}).get("content-type", "")
        kwargs = {}
        if target.get("post_data"):
            if "json" in content_type:
                kwargs["content"] = target["post_data"]
                kwargs["headers"] = {"Content-Type": content_type}
            else:
                # form-encoded body: parse "a=1&b=2" back into a dict
                kwargs["data"] = dict(
                    kv.split("=", 1) for kv in target["post_data"].split("&")
                    if "=" in kv)
        resp = client.request(target["method"], target["url"], **kwargs)
    print(f"-> {resp.status_code}")
    print(resp.text[:1000])


# ---- the real booking API (fill AVAILABILITY / BOOK first) ------------------
def get_availability(client, **params):
    if not AVAILABILITY["path"]:
        raise SystemExit("Fill in AVAILABILITY['path'] from your capture first.")
    data = {**AVAILABILITY["fields"], **params}
    resp = client.request(AVAILABILITY["method"], AVAILABILITY["path"], data=data)
    resp.raise_for_status()
    return resp.json()


def submit_booking(client, **params):
    if not BOOK["path"]:
        raise SystemExit("Fill in BOOK['path'] from your capture first.")
    data = {**BOOK["fields"], **params}
    resp = client.request(BOOK["method"], BOOK["path"], data=data)
    resp.raise_for_status()
    return resp


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inspect", metavar="GLOB",
                    help="list POST/interesting endpoints in a capture file")
    ap.add_argument("--replay", metavar="GLOB",
                    help="re-send a captured request with your saved cookie")
    ap.add_argument("--match", default="book",
                    help="substring of the URL to replay (default: book)")
    args = ap.parse_args()

    if args.inspect:
        inspect(args.inspect)
    elif args.replay:
        replay(args.replay, args.match)
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
