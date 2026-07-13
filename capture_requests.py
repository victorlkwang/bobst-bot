"""
capture_requests.py -- record the real LibCal API calls behind the booking UI.

The booking bots drive the page like a human (scroll, click green cells, submit).
That's slow and brittle. Underneath, the LibCal page is just firing XHR/fetch
requests to a handful of JSON endpoints. This script lets you *watch* those
requests during one real booking so we can talk to the endpoints directly later.

How to use it:

    python capture_requests.py

1. A browser opens. If a saved session exists (state.json) you're already logged
   in; otherwise log in normally -- approve the Duo push on your phone.
2. Do ONE complete booking by hand in that window (search -> pick a room's green
   cells -> Begin Booking Request -> Submit). Every network call is recorded.
3. Come back to the terminal and press Enter. The script writes:
      captures/session-<timestamp>.jsonl   every non-asset request + response
      state.json                           your logged-in cookies, for reuse
   and prints the endpoints that look like "availability" and "book".

Nothing here is sent anywhere -- it only observes your own browser. Cookie and
Authorization headers are REDACTED in the .jsonl so you can share it safely; the
real cookies live only in state.json (which .gitignore keeps out of git).
"""
import json
import os
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

URL = "https://nyu.libcal.com/r/new"
STATE_FILE = "state.json"
CAPTURE_DIR = "captures"

# We only care about API-ish traffic, not fonts/images/styles/scripts.
SKIP_TYPES = {"image", "stylesheet", "font", "media", "manifest"}
SKIP_SUFFIXES = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                 ".woff", ".woff2", ".ttf", ".ico", ".map")
REDACT_HEADERS = {"cookie", "authorization", "set-cookie", "x-csrf-token"}

# Substrings that hint a request is the interesting one, used only for the
# end-of-run summary (the .jsonl keeps everything regardless).
INTERESTING = ("avail", "book", "space", "reserve", "request", "grid",
               "process", "checkout", "hours")


def _interesting_headers(headers):
    return {k: ("<redacted>" if k.lower() in REDACT_HEADERS else v)
            for k, v in headers.items()}


def _should_skip(request):
    if request.resource_type in SKIP_TYPES:
        return True
    url = request.url.split("?", 1)[0].lower()
    return url.endswith(SKIP_SUFFIXES)


def main():
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(CAPTURE_DIR, f"session-{stamp}.jsonl")

    records = []

    def on_response(response):
        request = response.request
        if _should_skip(request):
            return
        rec = {
            "t": round(time.time(), 3),
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "status": response.status,
            "request_headers": _interesting_headers(request.headers),
            "post_data": request.post_data,   # form/JSON body we'd need to replay
            "response_headers": _interesting_headers(response.headers),
        }
        # Grab the response body when it's JSON/text and not huge.
        ctype = response.headers.get("content-type", "")
        if any(x in ctype for x in ("json", "text", "javascript")):
            try:
                body = response.text()
                rec["response_body"] = body[:20000]
            except Exception:
                rec["response_body"] = None
        records.append(rec)
        marker = "  <-- looks relevant" if any(
            s in request.url.lower() for s in INTERESTING) else ""
        print(f"  [{response.status}] {request.method} "
              f"{request.url.split('?', 1)[0]}{marker}", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=50)
        ctx_args = {"viewport": {"width": 1400, "height": 900}}
        if os.path.exists(STATE_FILE):
            ctx_args["storage_state"] = STATE_FILE
            print(f"Loaded saved session from {STATE_FILE} "
                  "(no Duo needed if it's still valid).")
        context = browser.new_context(**ctx_args)
        context.on("response", on_response)

        page = context.new_page()
        page.on("dialog", lambda d: d.accept())
        page.goto(URL, wait_until="domcontentloaded")

        print("\n" + "=" * 64)
        print("Do ONE full booking by hand in the browser window.")
        print("Log in + approve Duo if prompted. I'm recording every API call.")
        print("When the booking is confirmed, come back here and press Enter.")
        print("=" * 64 + "\n")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass

        # Persist the logged-in cookies so http_booking.py can reuse them.
        try:
            context.storage_state(path=STATE_FILE)
            print(f"\nSaved session cookies -> {STATE_FILE}")
        except Exception as e:
            print(f"\nCould not save session state: {e}")
        browser.close()

    with open(out_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"Wrote {len(records)} request(s) -> {out_path}")

    # Summary: surface the POSTs and the interesting-looking endpoints first,
    # since the booking action is almost certainly a POST.
    print("\n----- candidate endpoints (POST / keyword matches) -----")
    seen = set()
    for rec in records:
        url = rec["url"].split("?", 1)[0]
        key = (rec["method"], url)
        if key in seen:
            continue
        hit = rec["method"] == "POST" or any(
            s in rec["url"].lower() for s in INTERESTING)
        if hit:
            seen.add(key)
            has_body = "body" if rec.get("post_data") else "no-body"
            print(f"  {rec['method']:4} [{rec['status']}] {url}  ({has_body})")
    print("\nOpen the .jsonl to see full headers + bodies for these, then fill "
          "the endpoint constants in http_booking.py.")


if __name__ == "__main__":
    main()
