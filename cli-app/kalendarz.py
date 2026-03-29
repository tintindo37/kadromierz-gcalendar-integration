"""
kalendarz.py — Schedule PDF → Google Calendar importer
Usage:  python kalendarz.py schedule.pdf
        python kalendarz.py schedule.pdf --dry-run
        python kalendarz.py schedule.pdf --list-calendars

Settings are read from a .env file in the same directory
"""

import io
import os
import re
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ─────────────────────────────────────────
#  LOAD SETTINGS FROM .env
# ─────────────────────────────────────────

load_dotenv()

SCOPES           = ['https://www.googleapis.com/auth/calendar']
CALENDAR_ID      = os.getenv("CALENDAR_ID",      "primary")
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "30"))
EVENT_COLOR      = os.getenv("EVENT_COLOR",      "7")
LOCATION         = os.getenv("LOCATION",         "PP Wrocław Świdnicka")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE       = os.getenv("TOKEN_FILE",        "token.json")

COLOR_NAMES = {
    '1': 'Lavender',  '2': 'Sage',       '3': 'Grape',
    '4': 'Flamingo',  '5': 'Banana',     '6': 'Tangerine',
    '7': 'Peacock',   '8': 'Blueberry',  '9': 'Blueberry',
    '10': 'Basil',    '11': 'Tomato',
}


# ─────────────────────────────────────────
#  GOOGLE AUTH
# ─────────────────────────────────────────

def get_calendar_service():
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("🔄 Refreshing access token...")
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                print(f"❌ Credentials file not found: {CREDENTIALS_FILE}")
                print("   Download it from Google Cloud Console → APIs & Services → Credentials")
                sys.exit(1)
            print("🔐 Opening browser for Google login...")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
        print(f"✅ Token saved to {TOKEN_FILE}")

    return build('calendar', 'v3', credentials=creds)


# ─────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────

def parse_year_month(pdf) -> tuple[str, str]:
    """Extract YYYY, MM from any date-like pattern found in the PDF text."""
    for page in pdf.pages:
        text = page.extract_text() or ""
        match = re.search(r'\d{2}\.(\d{2})\.(\d{4})', text)
        if match:
            return match.group(2), match.group(1)   # rok, miesiac
    # fallback to current month
    now = datetime.now()
    return str(now.year), f"{now.month:02d}"


def make_datetimes(date_iso: str, start_h: str, end_h: str) -> tuple[datetime, datetime]:
    """Parse start/end strings into datetime objects, rolling end to next day if needed."""
    start_dt = datetime.strptime(f"{date_iso} {start_h}", "%Y-%m-%d %H:%M")
    end_dt   = datetime.strptime(f"{date_iso} {end_h}",   "%Y-%m-%d %H:%M")
    if end_dt <= start_dt:          # overnight shift (e.g. 22:00 → 06:00)
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def build_event(summary: str, start_dt: datetime, end_dt: datetime) -> dict:
    event = {
        'summary':  summary,
        'location': LOCATION,
        'colorId':  EVENT_COLOR,
        'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Europe/Warsaw'},
        'end':   {'dateTime': end_dt.isoformat(),   'timeZone': 'Europe/Warsaw'},
    }
    if REMINDER_MINUTES > 0:
        event['reminders'] = {
            'useDefault': False,
            'overrides': [{'method': 'popup', 'minutes': REMINDER_MINUTES}],
        }
    else:
        event['reminders'] = {'useDefault': False, 'overrides': []}
    return event


# ─────────────────────────────────────────
#  PDF PARSING
# ─────────────────────────────────────────

def parse_shifts(pdf_source) -> list[dict]:
    """
    Parse a schedule PDF and return a list of shift dicts:
      { day_name, date_iso, month, start_h, end_h, task, summary }

    pdf_source can be a file path (str/Path) or raw bytes.
    """
    shifts = []

    open_args = (io.BytesIO(pdf_source),) if isinstance(pdf_source, bytes) else (pdf_source,)

    with pdfplumber.open(*open_args) as pdf:
        rok, miesiac = parse_year_month(pdf)

        tabela = pdf.pages[0].extract_table()
        if not tabela or len(tabela) < 2:
            raise ValueError("Could not find a schedule table on the first page of the PDF.")

        dni_tygodnia = tabela[0]
        dane_wiersz  = tabela[1]

        for i, komorka in enumerate(dane_wiersz):
            if not komorka:
                continue

            linie = [l.strip() for l in komorka.split('\n') if l.strip()]
            if not linie:
                continue

            # Hours line must match HH:MM-HH:MM exactly
            godziny_linia = next(
                (l for l in linie if re.match(r'^\d{2}:\d{2}-\d{2}:\d{2}$', l)), None
            )
            if not godziny_linia:
                continue

            start_h, end_h = godziny_linia.split('-')

            # Task: first line that isn't the hours, a bare number, or a location prefix
            task = next(
                (l for l in linie
                 if l != godziny_linia
                 and not re.match(r'^\d{1,2}$', l)
                 and not l.startswith('PP ')),
                "Praca"
            )

            # Day number: last bare 1-2 digit token in the cell
            nr_dnia = next(
                (l.zfill(2) for l in reversed(linie) if re.match(r'^\d{1,2}$', l)),
                None
            )
            if not nr_dnia:
                continue

            date_iso = f"{rok}-{miesiac}-{nr_dnia}"
            day_name = (dni_tygodnia[i] or "").strip()

            shifts.append({
                'day_name': day_name,
                'date_iso': date_iso,
                'month':    miesiac,
                'nr_dnia':  nr_dnia,
                'start_h':  start_h,
                'end_h':    end_h,
                'task':     task,
                'summary':  f"Praca: {task}",
            })

    return shifts


# ─────────────────────────────────────────
#  CALENDAR OPERATIONS
# ─────────────────────────────────────────

def list_calendars(service):
    """Print all calendars available to the authenticated account."""
    result = service.calendarList().list().execute()
    calendars = result.get('items', [])
    if not calendars:
        print("No calendars found.")
        return
    print(f"\n{'ID':<55} Name")
    print("-" * 80)
    for cal in calendars:
        marker = " ← current" if cal['id'] == CALENDAR_ID else ""
        print(f"{cal['id']:<55} {cal.get('summary', '?')}{marker}")
    print(f"\nTo use a calendar, set CALENDAR_ID=<id> in your .env file.\n")


def import_shifts(service, shifts: list[dict], dry_run: bool = False) -> int:
    """Insert shifts into Google Calendar. Returns count of events added."""
    added = 0
    for shift in shifts:
        start_dt, end_dt = make_datetimes(shift['date_iso'], shift['start_h'], shift['end_h'])
        event = build_event(shift['summary'], start_dt, end_dt)

        label = (
            f"{shift['nr_dnia']}.{shift['month']} "
            f"({shift['day_name']}) "
            f"{shift['start_h']}-{shift['end_h']} "
            f"— {shift['task']}"
        )

        if dry_run:
            print(f"  [dry-run] Would add: {label}")
        else:
            service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
            print(f"  ✅ Added: {label}")

        added += 1

    return added


# ─────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────

def print_settings():
    reminder_text = f"{REMINDER_MINUTES} min before" if REMINDER_MINUTES > 0 else "disabled"
    color_name = COLOR_NAMES.get(EVENT_COLOR, EVENT_COLOR)
    print("\n⚙️  Current settings (from .env):")
    print(f"   CALENDAR_ID      = {CALENDAR_ID}")
    print(f"   REMINDER_MINUTES = {REMINDER_MINUTES}  ({reminder_text})")
    print(f"   EVENT_COLOR      = {EVENT_COLOR}  ({color_name})")
    print(f"   LOCATION         = {LOCATION}")
    print(f"   CREDENTIALS_FILE = {CREDENTIALS_FILE}")
    print(f"   TOKEN_FILE       = {TOKEN_FILE}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Import a work schedule PDF into Google Calendar.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python kalendarz.py schedule.pdf                 # import shifts
  python kalendarz.py schedule.pdf --dry-run       # preview without adding
  python kalendarz.py schedule.pdf --list-calendars  # show available calendars
  python kalendarz.py --settings                   # show current .env settings
        """
    )
    parser.add_argument("pdf", nargs="?", help="Path to the schedule PDF")
    parser.add_argument("--dry-run",         action="store_true", help="Preview shifts without adding to calendar")
    parser.add_argument("--list-calendars",  action="store_true", help="List all Google Calendars and exit")
    parser.add_argument("--settings",        action="store_true", help="Show current settings from .env and exit")
    args = parser.parse_args()

    if args.settings:
        print_settings()
        return

    if not args.pdf and not args.list_calendars:
        parser.print_help()
        return

    print_settings()
    service = get_calendar_service()

    if args.list_calendars:
        list_calendars(service)
        return

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"❌ File not found: {pdf_path}")
        sys.exit(1)

    print(f"📄 Parsing: {pdf_path.name}")
    try:
        shifts = parse_shifts(str(pdf_path))
    except ValueError as e:
        print(f"❌ PDF parse error: {e}")
        sys.exit(1)

    if not shifts:
        print("⚠️  No shifts found in PDF.")
        return

    mode = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{mode}Found {len(shifts)} shift(s) — importing to calendar '{CALENDAR_ID}':\n")
    added = import_shifts(service, shifts, dry_run=args.dry_run)

    if args.dry_run:
        print(f"\n✅ Dry run complete. {added} shift(s) would be added.")
    else:
        print(f"\n✅ Done. {added} shift(s) added to Google Calendar.")


if __name__ == "__main__":
    main()