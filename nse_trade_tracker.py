"""
NSE Trade Count Tracker
------------------------
Fetches NSE bhavcopy data and compares each symbol's latest No. of Trades
against (a) its N-day average and (b) yesterday's figure. Also shows the
close-to-close Price % change vs yesterday alongside the trade-count move.

Also tracks 1-month high/low hits -- EVERY time a stock makes a fresh
1-month high or low, that date is appended to its history (not overwritten),
so the hit-counter table always shows every date it happened, not just the
most recent one. Every stock also gets a clickable TradingView chart link.
Final report is exported as a single PDF.

RELIABILITY FIXES:
  - Each day's bhavcopy, once successfully fetched, is cached to disk
    (nse_bhavcopy_cache/). Re-running the script on the same or a later day
    reuses the cached data instead of re-fetching, so results for a given
    historical date are identical every time -- no more run-to-run drift
    caused by NSE's servers occasionally failing on one date but not another.
  - The high/low hit log de-duplicates same-day entries: if you run the
    script twice in one day, a real hit only gets logged once, not twice.
    Any duplicates already in your log file get auto-cleaned on next run.
  - TradingView chart links now use the <a href="..."> tag instead of
    <link href="...">. reportlab's <link> tag is for internal document
    bookmarks only -- it does NOT reliably open external URLs, which is why
    the chart links weren't clickable before. <a href="..."> is the
    documented, reliable way to make an external hyperlink in a reportlab
    Paragraph.

Usage:
    pip install requests reportlab
    python nse_trade_tracker.py

Edit SYMBOLS and LOOKBACK_DAYS below to your needs.
"""

import csv
import io
import json
import os
import time
import urllib.parse
from datetime import date, datetime, timedelta

import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)

# ---- CONFIG ----
SYMBOLS = [
    "RELIANCE", "TCS", "INFY", "BHARTIARTL", "ITC", "LT", "HINDUNILVR",
    "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO", "WIPRO",
    "NESTLEIND", "TATASTEEL", "POWERGRID", "INDIGO", "HCLTECH", "ADANIENT",
    "JSWSTEEL", "ONGC", "HAL", "M&M", "TECHM", "COALINDIA",
    "GRASIM", "DRREDDY", "CIPLA", "TVSMOTOR", "EICHERMOT", "BAJAJ-AUTO",
    "APOLLOHOSP", "DIVISLAB", "VOLTAS", "UPL",
]
LOOKBACK_DAYS = 25   # trading days pulled for the "1-month" average window
                     # (also acts as a safety buffer -- must be enough to cover
                     # every trading day back to the 1st of the current month;
                     # a month has at most ~23 trading sessions)
MAX_CALENDAR_DAYS_TO_SCAN = 70  # safety cap (accounts for weekends/holidays)
STATE_FILE = "nse_high_low_log.json"  # persists high/low hits across runs
DISPLAY_DAYS = 5    # how many recent days of "No. of Trades" to show per stock
CACHE_DIR = "nse_bhavcopy_cache"  # per-date cache so results never drift between runs

BASE_ARCHIVE_URL = "https://archives.nseindia.com/products/content/sec_bhavdata_full_{}.csv"
HOMEPAGE_URL = "https://www.nseindia.com"

# Telegram credentials are read from environment variables (set as GitHub
# Secrets in the workflow) rather than hardcoded, so this file is safe to
# commit to a public or private repo without leaking your bot token.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def tradingview_url(sym):
    """Build a TradingView chart link for an NSE symbol."""
    encoded = urllib.parse.quote(sym, safe="")
    return f"https://www.tradingview.com/chart/?symbol=NSE%3A{encoded}"


def get_session():
    """NSE requires a warm session (cookies) before serving archive files."""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(HOMEPAGE_URL, timeout=15)  # sets cookies
    return s


def _cache_path(d):
    return os.path.join(CACHE_DIR, f"{d.isoformat()}.json")


def load_cached_day(d):
    """Returns the cached {symbol: {...}} dict for a date, or None if not cached."""
    path = _cache_path(d)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def save_cached_day(d, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(_cache_path(d), "w") as f:
            json.dump(data, f)
    except IOError:
        pass


def fetch_bhavcopy(session, d):
    """Fetch and parse one day's bhavcopy. Returns dict {symbol: {...}} or None.

    Checks the local cache first -- once a date's data has been successfully
    fetched and parsed, it never needs to be fetched from NSE again, and the
    result for that date will be identical on every future run.
    """
    cached = load_cached_day(d)
    if cached is not None:
        return cached

    url = BASE_ARCHIVE_URL.format(d.strftime("%d%m%Y"))
    resp = session.get(url, timeout=15)
    if resp.status_code != 200 or "will be right back" in resp.text[:200]:
        return None

    reader = csv.DictReader(io.StringIO(resp.text))
    # Normalize header names (NSE sometimes has leading/trailing spaces)
    fieldmap = {name.strip().upper(): name for name in reader.fieldnames or []}
    sym_col = fieldmap.get("SYMBOL")
    series_col = fieldmap.get("SERIES")
    trades_col = fieldmap.get("NO_OF_TRADES")
    high_col = fieldmap.get("HIGH_PRICE")
    low_col = fieldmap.get("LOW_PRICE")
    close_col = fieldmap.get("CLOSE_PRICE")

    if not sym_col or not trades_col:
        return None

    def to_float(s):
        s = (s or "").strip().replace(",", "")
        try:
            return float(s)
        except ValueError:
            return None

    data = {}
    for row in reader:
        if series_col and row.get(series_col, "").strip() != "EQ":
            continue
        sym = row.get(sym_col, "").strip()
        raw = row.get(trades_col, "").strip().replace(",", "")
        if not sym or not raw.isdigit():
            continue
        data[sym] = {
            "trades": int(raw),
            "high": to_float(row.get(high_col)) if high_col else None,
            "low": to_float(row.get(low_col)) if low_col else None,
            "close": to_float(row.get(close_col)) if close_col else None,
        }

    if data:
        save_cached_day(d, data)
    return data if data else None


def load_hl_log(current_month):
    """Loads the persistent high/low log.

    Structure: {"month": "YYYY-MM", "symbols": {sym: {...}}}
    Each symbol entry stores FULL history lists for the current month:
        high_dates: [ "YYYY-MM-DD", ... ]   -- every date it hit a 1M high
        high_prices: [ price, ... ]          -- parallel list of prices
        low_dates / low_prices: same, for lows
    If the stored month is different from current_month, the old month's
    data is archived to a dated file and a fresh, empty log is returned
    (this is the "month end -> fresh table" reset).

    Also self-heals: if a symbol has the SAME date appearing more than once
    in a row (e.g. from re-running the script twice in one day before this
    fix existed), those duplicates get collapsed down to a single entry.
    """
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, IOError):
            raw = {}
    else:
        raw = {}

    if "symbols" in raw and "month" in raw:
        stored_month = raw["month"]
        symbols = raw["symbols"]
    else:
        # Legacy flat-format file (pre-month-tracking) - migrate as-is.
        stored_month = current_month
        symbols = raw

    if stored_month != current_month:
        archive_name = f"nse_high_low_log_{stored_month}.json"
        try:
            with open(archive_name, "w") as f:
                json.dump({"month": stored_month, "symbols": symbols}, f, indent=2)
            print(f"\nNew month detected - archived old log to {archive_name}, starting fresh.")
        except IOError:
            pass
        return {"month": current_month, "symbols": {}}

    # Migrate any legacy single-date entries ("high_date"/"low_date") into the
    # new list-based format so old state files keep working.
    for sym, entry in symbols.items():
        if "high_dates" not in entry:
            entry["high_dates"] = [entry["high_date"]] if entry.get("high_date") else []
            entry["high_prices"] = [entry["high_price"]] if entry.get("high_price") else []
        if "low_dates" not in entry:
            entry["low_dates"] = [entry["low_date"]] if entry.get("low_date") else []
            entry["low_prices"] = [entry["low_price"]] if entry.get("low_price") else []

        # De-duplicate: collapse consecutive repeats of the same date down to one.
        def dedupe(dates, prices):
            new_dates, new_prices = [], []
            for i, dt in enumerate(dates):
                if new_dates and new_dates[-1] == dt:
                    continue
                new_dates.append(dt)
                new_prices.append(prices[i] if i < len(prices) else None)
            return new_dates, new_prices

        entry["high_dates"], entry["high_prices"] = dedupe(entry["high_dates"], entry["high_prices"])
        entry["low_dates"], entry["low_prices"] = dedupe(entry["low_dates"], entry["low_prices"])

    return {"month": current_month, "symbols": symbols}


def save_hl_log(log):
    with open(STATE_FILE, "w") as f:
        json.dump(log, f, indent=2)


# ---------------------------------------------------------------------------
# TELEGRAM DELIVERY
# ---------------------------------------------------------------------------

def send_telegram_document(filepath, caption=""):
    """Send the finished PDF straight to Telegram. Silently skips if no
    credentials are configured, so the script still works standalone."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n(Telegram not configured - skipping delivery. "
              "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable it.)")
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    with open(filepath, "rb") as f:
        files = {"document": f}
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
        resp = requests.post(url, data=data, files=files, timeout=60)
    result = resp.json()
    if result.get("ok"):
        print("Telegram: PDF delivered successfully.")
    else:
        print(f"Telegram delivery failed: {result}")
    return result


# ---------------------------------------------------------------------------
# PDF EXPORT
# ---------------------------------------------------------------------------

def _fmt(v):
    return f"{v:,}" if isinstance(v, (int, float)) else "--"


def _day_headers(last5_days):
    """Column headers (oldest -> newest) for the last N days, e.g. '08-Jul'."""
    dates = [d for d, _ in last5_days] + [None] * (DISPLAY_DAYS - len(last5_days))
    dates = dates[:DISPLAY_DAYS]
    dates_oldest_first = list(reversed(dates))
    return [d.strftime("%d-%b") if d else "--" for d in dates_oldest_first]


def _day_values(row_last5):
    """Trade counts (oldest -> newest) for a single stock's last5 list."""
    vals = list(reversed(row_last5))
    return [_fmt(v) for _, v in vals]


def export_to_pdf(latest_date, yesterday_date, history_len, last5_days,
                   high_avg_group, low_avg_group,
                   high_yday_group, low_yday_group, no_yday_data,
                   hl_symbols, missing):
    # NOTE: <a href="..."> is the reliable reportlab tag for EXTERNAL
    # hyperlinks. <link href="..."> (used previously) is meant for internal
    # document bookmarks and does not reliably open external URLs -- that
    # was why the chart links weren't clickable.
    link_style = ParagraphStyle(
        "link", fontName="Helvetica-Bold", fontSize=9, textColor=colors.HexColor("#1a56db"),
    )
    sym_link_style = ParagraphStyle(
        "symlink", fontName="Helvetica-Bold", fontSize=8, textColor=colors.HexColor("#1a56db"),
        underlineWidth=0.5,
    )
    cell_style = ParagraphStyle("cell", fontName="Helvetica", fontSize=7, leading=8)
    header_style = ParagraphStyle(
        "header", fontName="Helvetica-Bold", fontSize=7, textColor=colors.white,
    )

    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    subtitle_style = ParagraphStyle("subtitle", parent=styles["Normal"], fontSize=10,
                                     textColor=colors.HexColor("#444444"), spaceAfter=6)
    section_style = ParagraphStyle("section", parent=styles["Heading2"],
                                    textColor=colors.HexColor("#1F4E24"), spaceBefore=14, spaceAfter=6)

    HEADER_FILL = colors.HexColor("#1F4E24")
    ROW_ALT = colors.HexColor("#F2F5F0")
    GREEN = colors.HexColor("#0B6E2E")
    RED = colors.HexColor("#B00020")

    def base_table_style(ncols, nrows):
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        for r in range(1, nrows):
            if r % 2 == 0:
                style.append(("BACKGROUND", (0, r), (-1, r), ROW_ALT))
        return TableStyle(style)

    def chart_cell(sym):
        url = tradingview_url(sym)
        return Paragraph(f'<a href="{url}"><b>Chart</b></a>', link_style)

    def symbol_cell(sym):
        # The symbol name itself is also a link -- much wider than the small
        # "Chart" button, so it's a far easier target to tap accurately on
        # a phone screen (this is the main fix: the link WAS working, but
        # the tiny "Chart" text created a hitbox too small to reliably tap).
        url = tradingview_url(sym)
        return Paragraph(f'<a href="{url}"><u>{sym}</u></a>', sym_link_style)

    def avg_table(group):
        day_hdrs = _day_headers(last5_days)
        header = ["Symbol"] + day_hdrs + ["Avg (1M)", "% vs Avg", "Price %", "Chart"]
        data = [header]
        for r in group:
            row = [symbol_cell(r["sym"])] + _day_values(r["last5"]) + [
                f"{r['avg']:,.0f}", f"{r['diff_pct']:+.1f}%",
                f"{r['price_pct']:+.1f}%" if r["price_pct"] is not None else "--",
                chart_cell(r["sym"]),
            ]
            data.append(row)
        if len(data) == 1:
            data.append(["(none)"] + [""] * (len(header) - 1))
        col_w = [1.0 * inch] + [0.5 * inch] * DISPLAY_DAYS + [0.65 * inch, 0.65 * inch, 0.6 * inch, 0.55 * inch]
        t = Table(data, colWidths=col_w, repeatRows=1)
        style = base_table_style(len(header), len(data))
        for i, r in enumerate(group, start=1):
            pct_col = len(header) - 3
            price_col = len(header) - 2
            style.add("TEXTCOLOR", (pct_col, i), (pct_col, i), GREEN if r["diff_pct"] >= 0 else RED)
            if r["price_pct"] is not None:
                style.add("TEXTCOLOR", (price_col, i), (price_col, i), GREEN if r["price_pct"] >= 0 else RED)
        t.setStyle(style)
        return t

    def yday_table(group):
        day_hdrs = _day_headers(last5_days)
        header = ["Symbol"] + day_hdrs + ["% vs Yesterday", "Price %", "Chart"]
        data = [header]
        for r in group:
            row = [symbol_cell(r["sym"])] + _day_values(r["last5"]) + [
                f"{r['diff_pct_yday']:+.1f}%",
                f"{r['price_pct']:+.1f}%" if r["price_pct"] is not None else "--",
                chart_cell(r["sym"]),
            ]
            data.append(row)
        if len(data) == 1:
            data.append(["(none)"] + [""] * (len(header) - 1))
        col_w = [1.0 * inch] + [0.5 * inch] * DISPLAY_DAYS + [0.85 * inch, 0.6 * inch, 0.55 * inch]
        t = Table(data, colWidths=col_w, repeatRows=1)
        style = base_table_style(len(header), len(data))
        for i, r in enumerate(group, start=1):
            pct_col = len(header) - 3
            price_col = len(header) - 2
            style.add("TEXTCOLOR", (pct_col, i), (pct_col, i), GREEN if r["diff_pct_yday"] >= 0 else RED)
            if r["price_pct"] is not None:
                style.add("TEXTCOLOR", (price_col, i), (price_col, i), GREEN if r["price_pct"] >= 0 else RED)
        t.setStyle(style)
        return t

    def hl_table(date_filter):
        """One row per HIT, not per symbol -- so every date a stock made a
        fresh 1-month high/low shows up as its own row with a running
        Hit # (1st time this month, 2nd time, etc)."""
        header = ["Symbol", "Type", "Hit Date", "Price", "Hit #", "Chart"]
        data = [header]
        any_rows = False
        for sym in sorted(hl_symbols.keys()):
            entry = hl_symbols[sym]

            high_dates = entry.get("high_dates", [])
            high_prices = entry.get("high_prices", [])
            for idx, d in enumerate(high_dates, start=1):
                if date_filter(d):
                    any_rows = True
                    price = high_prices[idx - 1] if idx - 1 < len(high_prices) else None
                    data.append([
                        symbol_cell(sym), "HIGH", d,
                        f"{price:.2f}" if isinstance(price, (int, float)) else "--",
                        str(idx), chart_cell(sym),
                    ])

            low_dates = entry.get("low_dates", [])
            low_prices = entry.get("low_prices", [])
            for idx, d in enumerate(low_dates, start=1):
                if date_filter(d):
                    any_rows = True
                    price = low_prices[idx - 1] if idx - 1 < len(low_prices) else None
                    data.append([
                        symbol_cell(sym), "LOW", d,
                        f"{price:.2f}" if isinstance(price, (int, float)) else "--",
                        str(idx), chart_cell(sym),
                    ])

        if not any_rows:
            data.append(["(none)", "", "", "", "", ""])

        col_w = [0.9 * inch, 0.6 * inch, 0.9 * inch, 0.7 * inch, 0.55 * inch, 0.55 * inch]
        t = Table(data, colWidths=col_w, repeatRows=1)
        style = base_table_style(len(header), len(data))
        for i in range(1, len(data)):
            sym = data[i][0]
            if sym != "(none)":
                if data[i][1] == "HIGH":
                    style.add("TEXTCOLOR", (1, i), (1, i), GREEN)
                elif data[i][1] == "LOW":
                    style.add("TEXTCOLOR", (1, i), (1, i), RED)
        t.setStyle(style)
        return t

    doc = SimpleDocTemplate(
        f"nse_trade_report_{latest_date}_{datetime.now().strftime('%H%M%S')}.pdf",
        pagesize=landscape(letter), topMargin=0.4 * inch, bottomMargin=0.4 * inch,
        leftMargin=0.4 * inch, rightMargin=0.4 * inch,
    )

    story = [
        Paragraph("NSE Trade Count Report", title_style),
        Paragraph(
            f"Today: {latest_date}"
            + (f" &nbsp;|&nbsp; Yesterday: {yesterday_date}" if yesterday_date else ""
