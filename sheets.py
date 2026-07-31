import yaml
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from loguru import logger

with open("config.yaml", encoding="utf-8") as f:
    _config = yaml.safe_load(f)

_creds_file = _config["google_sheets"]["credentials_file"]
_spreadsheet_id = _config["google_sheets"]["spreadsheet_id"]
_sheet_name = _config["google_sheets"]["sheet_name"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "date", "channel", "source", "score", "title", "company",
    "salary", "format", "pros", "cons", "red_flags",
    "match_summary", "url", "status", "external_id",
]


def _sheets_configured() -> bool:
    return (
        _spreadsheet_id not in ("", "YOUR_SPREADSHEET_ID")
        and _creds_file != ""
    )


def _get_sheet():
    if not _sheets_configured():
        raise RuntimeError("Google Sheets not configured (spreadsheet_id placeholder)")
    creds = Credentials.from_service_account_file(_creds_file, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(_spreadsheet_id)
    try:
        sheet = spreadsheet.worksheet(_sheet_name)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(_sheet_name, rows=1000, cols=20)
        sheet.append_row(HEADERS)
    return sheet


def _ensure_headers(sheet):
    first_row = sheet.row_values(1)
    if first_row != HEADERS:
        sheet.insert_row(HEADERS, 1)


def append_vacancy(data: dict) -> bool:
    """Append a vacancy row. Returns True on success."""
    if not _sheets_configured():
        return False
    try:
        sheet = _get_sheet()
        _ensure_headers(sheet)
        pros = "; ".join(data.get("pros", []))
        cons = "; ".join(data.get("cons", []))
        flags = "; ".join(data.get("red_flags", []))
        row = [
            datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            data.get("channel", ""),
            data.get("source", ""),
            data.get("score", ""),
            data.get("title", ""),
            data.get("company", ""),
            data.get("salary", ""),
            data.get("format", ""),
            pros,
            cons,
            flags,
            data.get("match_summary", ""),
            data.get("url", ""),
            data.get("status", "new"),
            data.get("external_id", ""),
        ]
        sheet.append_row(row)
        return True
    except Exception as e:
        logger.error(f"Google Sheets append error: {e}")
        return False


def update_status(external_id: str, status: str) -> bool:
    """Find row by external_id and update status column."""
    if not _sheets_configured():
        return False
    try:
        sheet = _get_sheet()
        col_ext = HEADERS.index("external_id") + 1
        col_status = HEADERS.index("status") + 1
        cell = sheet.find(external_id)
        if cell:
            sheet.update_cell(cell.row, col_status, status)
            return True
        logger.warning(f"Vacancy {external_id} not found in Sheets")
        return False
    except Exception as e:
        logger.error(f"Google Sheets update error: {e}")
        return False
