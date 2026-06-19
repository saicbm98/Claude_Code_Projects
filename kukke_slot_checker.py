"""
kukke_slot_checker.py
Monitors ITMS portal for available Sarpa Samskara seva slots at Kukke Subrahmanya Temple.
Sends email alert when cancellation slots open in May/June 2026.

Usage:
  python kukke_slot_checker.py            -- validate once, then ask to start monitor
  python kukke_slot_checker.py --monitor  -- skip validation, go straight to monitor loop
"""

import re
import time
import smtplib
import logging
import getpass
import os
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv, set_key

# ── Config ──────────────────────────────────────────────────────────────────

HOME_URL = "https://itms.kar.nic.in/hrcehome/index_temple.php?tid=21"
SERVICE_URL = (
    "https://itms.kar.nic.in/ticketing/service_collection.php"
    "?tid=21&scode=21&sscode=1&group_id=4&action=P"
)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
FROM_EMAIL = "medicherlasaicharan@gmail.com"
TO_EMAIL   = "medicherlasaicharan@gmail.com"

WATCH_MONTHS = {5, 6}   # May and June
WATCH_YEAR   = 2026

CHECK_INTERVAL_SECONDS = 3 * 60    # 3 minutes
COOLDOWN_MINUTES       = 30        # suppress re-alert for same dates

ENV_FILE = ".env"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Credentials ──────────────────────────────────────────────────────────────

def load_or_prompt_password() -> str:
    load_dotenv(ENV_FILE)
    pwd = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    if pwd:
        return pwd
    print("\nGmail App Password not found in .env")
    print("Generate one at: https://myaccount.google.com/apppasswords")
    pwd = getpass.getpass("Enter Gmail App Password: ").strip()
    if not os.path.exists(ENV_FILE):
        open(ENV_FILE, "w").close()
    set_key(ENV_FILE, "GMAIL_APP_PASSWORD", pwd)
    print(f"Saved to {ENV_FILE}\n")
    return pwd

# ── Portal scraping (Playwright primary) ─────────────────────────────────────

def fetch_with_playwright() -> tuple[list[str], list[str], bool]:
    """
    Navigate to the portal with a real browser, extract JS arrays.
    Returns (disable_dates, booked_dates, success).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright not installed. Run: pip install playwright && python -m playwright install chromium")
        return [], [], False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=BROWSER_UA)
            page = context.new_page()

            page.goto(HOME_URL, wait_until="networkidle", timeout=30000)
            page.goto(SERVICE_URL, wait_until="networkidle", timeout=30000)

            dd = page.evaluate("typeof disableDates !== 'undefined' ? disableDates : []")
            bd = page.evaluate("typeof booked_date_array !== 'undefined' ? booked_date_array : []")
            browser.close()

        return dd, bd, True
    except Exception as exc:
        log.error("Playwright error: %s", exc)
        return [], [], False


def fetch_with_requests() -> tuple[list[str], list[str], bool]:
    """
    Fallback: requests + session. Works if the portal later becomes accessible
    without a full browser render.
    """
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    session = requests.Session()
    session.headers.update(headers)
    try:
        session.get(HOME_URL, timeout=20)
        r = session.get(SERVICE_URL, timeout=20, headers={"Referer": HOME_URL})
        r.raise_for_status()
        html = r.text

        def extract(var_name: str) -> list[str]:
            m = re.search(rf'var\s+{re.escape(var_name)}\s*=\s*(\[.*?\]);', html, re.DOTALL)
            if not m:
                return []
            return re.findall(r'"([^"]+)"', m.group(1))

        dd = extract("disableDates")
        bd = extract("booked_date_array")
        return dd, bd, bool(dd)
    except Exception as exc:
        log.error("Requests error: %s", exc)
        return [], [], False


# ── Date helpers ─────────────────────────────────────────────────────────────

def filter_watch_dates(dates: list[str]) -> list[str]:
    out = []
    for d in dates:
        parts = d.split("-")
        try:
            day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
            if year == WATCH_YEAR and month in WATCH_MONTHS:
                out.append(d)
        except (ValueError, IndexError):
            pass
    return out


def get_available_slots() -> tuple[list[str], list[str], list[str]]:
    """Returns (all_watch, booked_watch, available)."""
    # Try playwright first (portal requires full browser session)
    dd, bd, ok = fetch_with_playwright()
    if not ok or not dd:
        # If playwright found the page but there are simply no dates, that's fine
        log.debug("Playwright returned no disable_dates -- trying requests fallback")
        dd2, bd2, _ = fetch_with_requests()
        if dd2:
            dd, bd = dd2, bd2

    all_watch = filter_watch_dates(dd)
    booked_watch = filter_watch_dates(bd)
    booked_set = set(booked_watch)
    available = [d for d in all_watch if d not in booked_set]
    return all_watch, booked_watch, available


# ── Email ────────────────────────────────────────────────────────────────────

def send_alert(available_dates: list[str], app_password: str) -> bool:
    count = len(available_dates)
    subject = f"KUKKE SLOT OPEN -- {count} slot(s) -- ACT NOW"

    date_list_html = "".join(
        f"<li><strong>{d}</strong></li>" for d in sorted(available_dates)
    )
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_body = f"""
<html><body style="font-family: Arial, sans-serif; max-width: 620px; margin: 0 auto;">
  <h1 style="color: #cc0000; font-size: 28px;">
    Sarpa Samskara slot is OPEN on ITMS
  </h1>
  <p style="font-size: 18px;">
    <strong>{count} available slot(s)</strong> found for May / June 2026:
  </p>
  <ul style="font-size: 20px; color: #006600; line-height: 1.8;">
    {date_list_html}
  </ul>
  <p style="margin: 24px 0;">
    <a href="https://itms.kar.nic.in"
       style="background: #cc0000; color: #fff; padding: 14px 28px;
              text-decoration: none; border-radius: 6px;
              font-size: 18px; font-weight: bold; display: inline-block;">
      BOOK NOW at itms.kar.nic.in
    </a>
  </p>
  <p style="font-size: 16px; color: #333; border-left: 4px solid #cc0000; padding-left: 12px;">
    <strong>Reminder:</strong> Aadhaar ready. Fill form in under 3 minutes.
  </p>
  <hr style="margin: 24px 0; border: none; border-top: 1px solid #ddd;">
  <p style="font-size: 12px; color: #999;">
    Check ran at: {now_str}
  </p>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = FROM_EMAIL
    msg["To"]      = TO_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(FROM_EMAIL, app_password)
            smtp.sendmail(FROM_EMAIL, [TO_EMAIL], msg.as_string())
        log.info("Alert email sent -- subject: %s", subject)
        return True
    except Exception as exc:
        log.error("Failed to send email: %s", exc)
        return False


# ── Validation run ───────────────────────────────────────────────────────────

def validation_run(app_password: str):
    print("\n" + "=" * 60)
    print("VALIDATION RUN")
    print("=" * 60)

    print("Fetching portal via Playwright...")
    dd, bd, pw_ok = fetch_with_playwright()

    print(f"\nPlaywright success      : {pw_ok}")
    print(f"disableDates found      : {bool(dd)} ({len(dd)} total entries)")
    print(f"booked_date_array found : {bool(bd)} ({len(bd)} total entries)")

    if not dd:
        print("\nPlaywright returned no disableDates.")
        print("This means all May/June slots are currently fully booked,")
        print("OR the temple has not activated this seva online right now.")
        print("The monitor will keep checking every 3 minutes and alert when a slot opens.")
    else:
        all_watch   = filter_watch_dates(dd)
        booked_watch = filter_watch_dates(bd)
        booked_set  = set(booked_watch)
        available   = [d for d in all_watch if d not in booked_set]

        print(f"\nMay/June scheduled  : {len(all_watch)}")
        print(f"May/June booked     : {len(booked_watch)}")
        print(f"May/June AVAILABLE  : {len(available)}")

        if available:
            print("\nAvailable slots:", available)
            print("\nSending test alert email...")
            send_alert(available, app_password)
        else:
            print("\nNo available slots right now (all scheduled dates are booked).")

    print("=" * 60 + "\n")


# ── Monitor loop ─────────────────────────────────────────────────────────────

def run_monitor(app_password: str):
    alerted: dict[frozenset, datetime] = {}

    log.info(
        "Kukke slot monitor started. Checking every %d minutes for May/June %d slots.",
        CHECK_INTERVAL_SECONDS // 60,
        WATCH_YEAR,
    )
    log.info("Press Ctrl+C to stop.")

    while True:
        now = datetime.now()
        try:
            all_watch, booked_watch, available = get_available_slots()
            log.info(
                "[%s]  Scheduled: %d | Booked: %d | Available: %d",
                now.strftime("%H:%M:%S"),
                len(all_watch),
                len(booked_watch),
                len(available),
            )

            if available:
                key = frozenset(available)
                last = alerted.get(key)
                if last is None or (now - last) > timedelta(minutes=COOLDOWN_MINUTES):
                    log.info("OPEN SLOTS: %s", available)
                    if send_alert(available, app_password):
                        alerted[key] = now
                else:
                    mins_left = COOLDOWN_MINUTES - int((now - last).total_seconds() // 60)
                    log.info("Slots open but in cooldown (%d min remaining).", mins_left)

        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log.error("Error during check (will retry next cycle): %s", exc)

        try:
            time.sleep(CHECK_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    app_password = load_or_prompt_password()

    if "--monitor" in sys.argv:
        run_monitor(app_password)
    else:
        # Default: validate first, then optionally start monitor
        validation_run(app_password)
        try:
            answer = input("Start continuous monitor? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer == "y":
            run_monitor(app_password)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
