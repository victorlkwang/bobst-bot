"""
Bobst room booking bot -- ONE DAY AT A TIME (single credential).

Because booking all 14 days in one cart trips the "180 min per day" limit and
loses the whole batch, this books each date independently:

  search availability -> go to the date -> book the room's green cells ->
  Begin Booking Request -> (login once, Duo) -> Submit my Booking
    * confirmed (pic1): screenshot -> confirmations/<room>/<date>/  -> Make Another Booking
    * error e.g. "exceeds 180 min" (pic5): click Remove
  ... repeat for the next date until the 14-day window ends.

"Leave site?" browser popups (pic3) are auto-accepted. A per-date summary
prints at the end.

Usage:
    python booking_bot.py                 # default room (see ROOM_TO_BOOK)
    python booking_bot.py LL1-20          # pick the room
    python booking_bot.py LL2-07 --sections 2 --max-days 7
    python booking_bot.py --help          # all options

The constants below are the defaults; command-line arguments override them.
"""
import argparse
import os
import re
import time
from datetime import datetime
from playwright.sync_api import sync_playwright

URL = "https://nyu.libcal.com/r/new"
CRED_FILE = "credential.txt"

ROOM_TO_BOOK = "LL2-07"   # default room, e.g. "LL1-20" or "LL2-07" (CLI overrides)
SECTIONS = 3              # clicks/day: up to 3 * 4 cells = 12 cells = 3 hrs
MAX_DAYS = 20             # safety cap (window is ~14 days)
DUO_WAIT_SECONDS = 60    # Duo push times out ~60s
CONFIRM_DIR = "confirmations"


# ---------- per-run logging ----------
_LOG_TAG = ""


def log(msg):
    print(f"{_LOG_TAG}{msg}", flush=True)


# ---------- small helpers ----------
def _safe(name):
    return re.sub(r"[/\\\x00-\x1f]", "-", str(name)).strip()


def _date_iso(text):
    cleaned = re.sub(r"^[A-Za-z]+,\s*", "", (text or "").strip())
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return (re.sub(r"[^0-9A-Za-z_-]", "_", (text or "unknown"))[:40]) or "unknown"


def load_credentials(path):
    """Each line: NetID (no @nyu.edu), password, then name.
    Name may contain spaces; it's used for console/summary labels."""
    creds = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            netid, password = parts[0], parts[1]
            name = parts[2] if len(parts) >= 3 else parts[0]
            if "@" not in netid:
                netid += "@nyu.edu"
            creds.append((netid, password, name))
    return creds


# ---------- locators ----------
def get_scroller(page):
    return page.locator(".fc-scroller").filter(
        has=page.locator(".fc-timeline-body")
    ).first


def resolve_room_row(page, room):
    info = page.evaluate("""
        (room) => {
            const norm = s => (s || '').replace(/[\\u2010-\\u2015]/g, '-');
            const want = norm(room);
            for (const el of document.querySelectorAll('[data-resource-id]')) {
                if (norm(el.textContent).includes(want)) {
                    return { rid: el.getAttribute('data-resource-id') };
                }
            }
            const rows = Array.from(document.querySelectorAll('.fc-datagrid-body tr'));
            for (let i = 0; i < rows.length; i++) {
                if (norm(rows[i].textContent).includes(want)) return { index: i };
            }
            return null;
        }
    """, room)
    if not info:
        return None
    if info.get("rid") is not None:
        rid = info["rid"]
        loc = page.locator(f'.fc-timeline-lane.fc-resource[data-resource-id="{rid}"]')
        if loc.count() > 0:
            return loc.first
        loc = page.locator(f'.fc-timeline-body [data-resource-id="{rid}"]')
        if loc.count() > 0:
            return loc.first
    if info.get("index") is not None:
        return page.locator(".fc-timeline-lane.fc-resource").nth(info["index"])
    return None


# ---------- state checks ----------
def grid_at_end(page):
    scroller = get_scroller(page)
    return scroller.evaluate(
        "(el) => el.scrollLeft >= el.scrollWidth - el.clientWidth - 2"
    )


def end_notice_visible(page):
    notice = page.get_by_text("reached the end of the bookable window", exact=False)
    try:
        return notice.count() > 0 and notice.first.is_visible()
    except Exception:
        return False


def read_current_date(page):
    """Return (human, iso) for the date shown above 'Go To Date' (pic2)."""
    for sel in [".fc-toolbar-title", ".fc-toolbar h2", "h2.fc-toolbar-title"]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                t = loc.first.inner_text().strip()
                if t:
                    return t, _date_iso(t)
        except Exception:
            pass
    try:
        body = page.inner_text("body")
        m = re.search(r"[A-Z][a-z]+day,\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4}", body)
        if m:
            return m.group(0), _date_iso(m.group(0))
    except Exception:
        pass
    return "unknown-date", "unknown-date"


def leftmost_visible_slot_is_green(page, row):
    return row.evaluate("""
        (row) => {
            const scroller = row.closest(".fc-scroller");
            const scrollerBox = scroller.getBoundingClientRect();
            const events = Array.from(row.querySelectorAll("a.fc-timeline-event"));
            let leftmost = null, bestLeft = Infinity;
            for (const event of events) {
                const box = event.getBoundingClientRect();
                if (box.right > scrollerBox.left && box.left < scrollerBox.right) {
                    if (box.left < bestLeft) { bestLeft = box.left; leftmost = event; }
                }
            }
            if (!leftmost) return false;
            return leftmost.classList.contains("s-lc-eq-avail");
        }
    """)


# ---------- scrolling ----------
def reset_scroll(page):
    scroller = get_scroller(page)
    scroller.wait_for(timeout=10000)
    scroller.scroll_into_view_if_needed()
    scroller.evaluate("el => el.scrollLeft = 0")
    page.wait_for_timeout(400)


def scroll_until_leftmost_green(page, row):
    scroller = get_scroller(page)
    scroller.wait_for(timeout=10000)
    scroller.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    while True:
        if leftmost_visible_slot_is_green(page, row):
            return True
        if grid_at_end(page):
            return False
        scroller.evaluate("el => el.scrollLeft += 60")
        page.wait_for_timeout(200)


# ---------- booking cells ----------
def click_leftmost_green(page, row):
    return row.evaluate("""
        (row) => {
            const scroller = row.closest(".fc-scroller");
            const scrollerBox = scroller.getBoundingClientRect();
            const greens = Array.from(row.querySelectorAll("a.s-lc-eq-avail"));
            let leftmostGreen = null, bestLeft = Infinity;
            for (const green of greens) {
                const box = green.getBoundingClientRect();
                if (box.right > scrollerBox.left && box.left < scrollerBox.right) {
                    if (box.left < bestLeft) { bestLeft = box.left; leftmostGreen = green; }
                }
            }
            if (!leftmostGreen) return false;
            leftmostGreen.click();
            return true;
        }
    """)


def wait_for_cells(page, row):
    """Give timeline events time to render before scanning (avoids misreading
    still-loading green cells as unavailable)."""
    try:
        row.locator("a.fc-timeline-event").first.wait_for(timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(1200)


def book_sections_for_day(page, room, sections=SECTIONS):
    reset_scroll(page)
    row = resolve_room_row(page, room)
    if row is None:
        log("  room row not found on this day")
        return 0

    wait_for_cells(page, row)
    scroll_until_leftmost_green(page, row)
    clicked = 0
    attempts = 0
    while clicked < sections and attempts < 100:
        if click_leftmost_green(page, row):
            clicked += 1
            page.wait_for_timeout(800)
        else:
            if grid_at_end(page):
                break
            get_scroller(page).evaluate("el => el.scrollLeft += 60")
            page.wait_for_timeout(250)
        attempts += 1
    return clicked


# ---------- generic button clicker ----------
def click_named_wait(page, name, timeout=20000):
    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        for loc in [
            page.get_by_role("button", name=name),
            page.locator(f"input[type=submit][value='{name}']"),
            page.get_by_text(name, exact=False),
        ]:
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.scroll_into_view_if_needed()
                    loc.first.click()
                    return True
            except Exception:
                continue
        page.wait_for_timeout(300)
    return False


# ---------- navigation ----------
def ensure_search_form(page):
    btn = page.get_by_role("button", name="Show Availability")
    try:
        btn.wait_for(timeout=5000)
        return
    except Exception:
        page.goto(URL, wait_until="domcontentloaded")
        page.get_by_role("button", name="Show Availability").wait_for(timeout=15000)


def show_availability(page):
    page.get_by_label("Location").select_option(label="Bobst Library")
    page.get_by_label("I need a space for").select_option(label="Collaborative Work")
    page.get_by_label("Space Type").select_option(label="Bobst Group Study Rooms")
    page.get_by_label("Capacity").select_option(label="9-12 people")
    page.get_by_role("button", name="Show Availability").click()
    page.get_by_text("Go To Date").wait_for(timeout=15000)
    page.wait_for_timeout(1500)


def advance_to_offset(page, offset):
    for _ in range(offset):
        if end_notice_visible(page):
            return
        page.locator("button.fc-next-button").click()
        page.wait_for_function(
            """() => document.querySelector('.fc-timeline-lane.fc-resource')
                  || document.body.innerText.includes('reached the end of the bookable window')""",
            timeout=15000,
        )
        page.wait_for_timeout(900)


# ---------- login ----------
def login_error_visible(page):
    for loc in [
        page.locator("#passwordError"),
        page.locator("#usernameError"),
        page.get_by_text("incorrect", exact=False),
        page.get_by_text("isn't correct", exact=False),
        page.get_by_text("couldn't find an account", exact=False),
        page.get_by_text("account or password", exact=False),
    ]:
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return True
        except Exception:
            continue
    return False


def login(page, netid, password):
    """Returns 'ok', 'no_login_page', 'bad_credentials', 'duo_no_prompt', 'duo_timeout'."""
    email = page.locator("input[type=email], input[name=loginfmt]").first
    try:
        email.wait_for(timeout=10000)
    except Exception:
        return "no_login_page"

    log("  entering NetID / password...")
    email.fill(netid)
    if not click_named_wait(page, "Next", timeout=8000):
        email.press("Enter")

    try:
        pw_field = page.locator("input[type=password], input[name=passwd]").first
        pw_field.wait_for(timeout=15000)
    except Exception:
        return "bad_credentials" if login_error_visible(page) else "duo_no_prompt"

    pw_field.fill(password)
    if not click_named_wait(page, "Sign in", timeout=10000):
        pw_field.press("Enter")

    deadline = time.time() + 25
    duo_clicked = False
    while time.time() < deadline:
        if login_error_visible(page):
            return "bad_credentials"
        if click_named_wait(page, "Approve with MFA (Duo)", timeout=800):
            log("  clicked 'Approve with MFA (Duo)'")
            duo_clicked = True
            break
        page.wait_for_timeout(300)
    if not duo_clicked:
        return "bad_credentials" if login_error_visible(page) else "duo_no_prompt"

    log(f"  >>> Approve the Duo push on your phone ({DUO_WAIT_SECONDS}s) <<<")
    deadline = time.time() + DUO_WAIT_SECONDS
    while time.time() < deadline:
        if "nyu.libcal.com" in page.url:
            log("  login complete")
            return "ok"
        for yes in [
            page.get_by_role("button", name="Yes"),
            page.locator("#idSIButton9"),
            page.locator("input[value=Yes]"),
        ]:
            try:
                if yes.count() > 0 and yes.first.is_visible():
                    yes.first.click()
                    break
            except Exception:
                continue
        page.wait_for_timeout(1000)
    return "duo_timeout"


# ---------- submit / classify ----------
def advance_to_submit(page):
    for _ in range(3):
        if page.get_by_role("button", name="Submit my Booking").count() > 0:
            return True
        if click_named_wait(page, "Continue", timeout=4000):
            page.wait_for_timeout(1500)
            continue
        break
    return page.get_by_role("button", name="Submit my Booking").count() > 0


def find_error_text(page):
    for loc in [
        page.get_by_text("exceeds the 180", exact=False),
        page.get_by_text("Sorry, this exceeds", exact=False),
        page.locator(".alert-danger"),
        page.locator("[role=alert]"),
    ]:
        try:
            if loc.count() > 0 and loc.first.is_visible():
                t = loc.first.inner_text().strip()
                if t:
                    return " ".join(t.split())[:140]
        except Exception:
            continue
    return None


def submit_and_classify(page):
    click_named_wait(page, "Submit my Booking", timeout=8000)
    deadline = time.time() + 20
    while time.time() < deadline:
        if page.get_by_text("Booking Confirmed", exact=False).count() > 0:
            return "confirmed", None
        err = find_error_text(page)
        if err:
            return "error", err
        page.wait_for_timeout(500)
    if page.get_by_text("Booking Confirmed", exact=False).count() > 0:
        return "confirmed", None
    return "error", find_error_text(page) or "unknown error"


def read_booked_time(page):
    """Read the booked time range off the confirmation page, e.g. '12:00am-3:00am'.
    Uses the earliest start and latest end if the booking spans several cards."""
    try:
        body = page.inner_text("body")
    except Exception:
        return ""
    matches = re.findall(
        r"Time:\s*(\d{1,2}:\d{2}\s*[ap]m)\s*[-\u2013\u2014]\s*(\d{1,2}:\d{2}\s*[ap]m)",
        body, re.I)
    if not matches:
        return ""
    start = re.sub(r"\s+", "", matches[0][0])
    end = re.sub(r"\s+", "", matches[-1][1])
    return f"{start}-{end}"


def save_confirmation(page, room, date_iso):
    folder = os.path.join(CONFIRM_DIR, _safe(room), _safe(date_iso))
    os.makedirs(folder, exist_ok=True)  # may or may not already exist
    trange = read_booked_time(page)
    if trange:
        fname = f"{_safe(room)} {_safe(date_iso)} {_safe(trange)}.png"
    else:
        fname = f"{_safe(room)} {_safe(date_iso)}.png"
    path = os.path.join(folder, fname)
    try:
        page.screenshot(path=path, full_page=True)
        log(f"  saved {path}")
    except Exception as e:
        log(f"  confirmation screenshot failed: {e}")


# ---------- per-credential (per-day) run ----------
def run_for_credential(page, netid, password, name, idx, room=ROOM_TO_BOOK,
                       sections=SECTIONS, max_days=MAX_DAYS):
    page.on("dialog", lambda d: d.accept())  # auto-accept "Leave site?" (pic3)

    result = {"idx": idx, "netid": netid, "name": name, "room": room,
              "days": [], "login_failed": False}
    page.goto(URL, wait_until="domcontentloaded")

    first_login_done = False
    offset = 0
    while offset < max_days:
        ensure_search_form(page)
        show_availability(page)
        advance_to_offset(page, offset)
        page.wait_for_timeout(1000)  # extra load buffer for green cells

        if end_notice_visible(page):
            log("reached end of bookable window")
            break

        date_human, date_iso = read_current_date(page)
        booked = book_sections_for_day(page, room, sections)

        if booked == 0:
            log(f"{date_human}: no available slot")
            result["days"].append((date_human, "no available slot"))
            offset += 1
            continue

        if not click_begin_booking_request(page):
            log(f"{date_human}: could not start booking")
            result["days"].append((date_human, "error (no Begin button)"))
            offset += 1
            continue

        if not first_login_done:
            status = login(page, netid, password)
            if status in ("ok", "no_login_page"):
                first_login_done = True
            else:
                log(f"{date_human}: login failed ({status})")
                result["days"].append((date_human, f"login failed ({status})"))
                result["login_failed"] = True
                break

        if not advance_to_submit(page):
            log(f"{date_human}: booking details not reached")
            result["days"].append((date_human, "error (no Submit)"))
            offset += 1
            continue

        outcome, errtext = submit_and_classify(page)
        if outcome == "confirmed":
            save_confirmation(page, room, date_iso)
            log(f"{date_human}: booked")
            result["days"].append((date_human, "booked"))
            click_named_wait(page, "Make Another Booking", timeout=8000)
        else:
            log(f"{date_human}: error -- {errtext}")
            result["days"].append((date_human, f"error: {errtext}"))
            click_named_wait(page, "Remove", timeout=8000)

        page.wait_for_timeout(1500)
        offset += 1

    return result


def click_begin_booking_request(page):
    return click_named_wait(page, "Begin Booking Request", timeout=15000)


# ---------- runner ----------
def run_one_credential(netid, password, name, idx=1, total=None, room=ROOM_TO_BOOK,
                       sections=SECTIONS, max_days=MAX_DAYS, headless=False):
    global _LOG_TAG
    _LOG_TAG = f"[{name}] "
    print(f"\n===== {name}: booking {room} for {netid}, one day at a time =====", flush=True)

    result = {"idx": idx, "netid": netid, "name": name, "room": room,
              "days": [], "login_failed": False}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=150)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()
        try:
            result = run_for_credential(page, netid, password, name, idx, room,
                                        sections, max_days)
        except Exception as e:
            log(f"ERROR: {e}")
            result["error"] = str(e)
        finally:
            page.wait_for_timeout(1500)
            browser.close()
            log("browser closed.")
    return result


# ---------- summary ----------
def print_summary(results):
    for r in results:
        print("\n" + "=" * 52, flush=True)
        print(f"SUMMARY -- {r.get('name', r.get('netid'))} ({r.get('netid')}) -- room {r.get('room')}", flush=True)
        print("=" * 52, flush=True)
        for date_human, status in r.get("days", []):
            print(f"{date_human}: {status}", flush=True)
        if r.get("login_failed"):
            print("(stopped early: login failed)", flush=True)
        if not r.get("days"):
            print("(no dates processed)", flush=True)


def build_parser():
    p = argparse.ArgumentParser(
        description="Book a Bobst group study room, one day at a time.")
    p.add_argument("room", nargs="?", default=ROOM_TO_BOOK,
                   help=f"room number, e.g. LL2-07 or LL1-20 (default: {ROOM_TO_BOOK})")
    p.add_argument("--sections", type=int, default=SECTIONS,
                   help=f"click groups per day; each is ~1 hr (default: {SECTIONS})")
    p.add_argument("--max-days", type=int, default=MAX_DAYS,
                   help=f"safety cap on days to walk (default: {MAX_DAYS})")
    p.add_argument("--headless", action="store_true",
                   help="run without a visible browser window "
                        "(only works with a saved session; Duo needs a window)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    creds = load_credentials(CRED_FILE)
    if not creds:
        print("No credentials found in credential.txt")
        return
    results = [run_one_credential(netid, password, name, i, len(creds),
                                  room=args.room, sections=args.sections,
                                  max_days=args.max_days, headless=args.headless)
               for i, (netid, password, name) in enumerate(creds, start=1)]
    print_summary(results)


if __name__ == "__main__":
    main()
