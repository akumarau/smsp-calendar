from __future__ import annotations

# Repository setup trigger: run the calendar sync workflow after initial configuration.

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

TARGET_URL = os.environ.get(
    "SMSP_CALENDAR_URL",
    "https://events.sydneymotorsportpark.com.au/eventcalendar/s/",
)
OUT_DIR = Path(os.environ.get("SMSP_OUTPUT_DIR", "docs"))
TIMEZONE = "Australia/Sydney"

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

NAV_WORDS = {
    "home",
    "calendar",
    "contact",
    "privacy",
    "terms",
    "login",
    "log in",
    "sign in",
    "refresh",
}


@dataclass
class Event:
    uid: str
    title: str
    start: str
    end: str
    all_day: bool
    url: str
    location: str = "Sydney Motorsport Park"
    description: str = ""
    source_text: str = ""


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def fold_ics(line: str, limit: int = 73) -> list[str]:
    if len(line.encode("utf-8")) <= limit:
        return [line]
    parts: list[str] = []
    current = ""
    for ch in line:
        candidate = current + ch
        if current and len(candidate.encode("utf-8")) > limit:
            parts.append(current)
            current = " " + ch
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def stable_uid(url: str, title: str, start: str) -> str:
    basis = url.strip() or f"{title}|{start}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return f"{digest}@smsp-calendar.akumarau"


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def parse_date_from_text(text: str, today: date | None = None) -> date | None:
    today = today or date.today()
    t = clean_text(text)

    patterns = [
        re.compile(
            r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+"
            r"(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)"
            r"(?:\s+(?P<year>20\d{2}))?\b",
            re.I,
        ),
        re.compile(
            r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)\s+"
            r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
            r"(?:,?\s+(?P<year>20\d{2}))?\b",
            re.I,
        ),
        re.compile(r"\b(?P<day>\d{1,2})/(?P<month_num>\d{1,2})/(?P<year>20\d{2})\b"),
    ]

    for pattern in patterns:
        match = pattern.search(t)
        if not match:
            continue
        groups = match.groupdict()
        day = int(groups["day"])
        if groups.get("month_num"):
            month = int(groups["month_num"])
        else:
            month_name = groups["month"].lower()
            month = MONTHS[month_name]
        year = int(groups["year"]) if groups.get("year") else today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if not groups.get("year") and candidate < today - timedelta(days=45):
            try:
                candidate = candidate.replace(year=year + 1)
            except ValueError:
                pass
        return candidate
    return None


def parse_time_from_text(text: str) -> tuple[int, int] | None:
    match = re.search(
        r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)\b",
        text,
        re.I,
    )
    if not match:
        match24 = re.search(r"\b(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\b", text)
        if not match24:
            return None
        return int(match24.group("hour")), int(match24.group("minute"))

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = match.group("ampm").lower()
    if hour == 12:
        hour = 0
    if ampm == "pm":
        hour += 12
    return hour, minute


def event_from_jsonld(obj: dict[str, Any], base_url: str) -> Event | None:
    obj_type = obj.get("@type")
    if isinstance(obj_type, list):
        is_event = any(str(x).lower() == "event" for x in obj_type)
    else:
        is_event = str(obj_type).lower() == "event"
    if not is_event:
        return None

    title = clean_text(str(obj.get("name", "")))
    start_raw = obj.get("startDate")
    if not title or not start_raw:
        return None

    start_dt = parse_iso(start_raw)
    end_dt = parse_iso(obj.get("endDate")) or start_dt
    if start_dt is None:
        return None

    url = obj.get("url") or base_url
    if isinstance(url, dict):
        url = url.get("@id") or base_url
    url = urljoin(base_url, str(url))

    location = "Sydney Motorsport Park"
    loc = obj.get("location")
    if isinstance(loc, dict):
        location = clean_text(str(loc.get("name") or "")) or location
        address = loc.get("address")
        if isinstance(address, dict):
            parts = [
                clean_text(str(address.get(k) or ""))
                for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")
            ]
            address_text = ", ".join(x for x in parts if x)
            if address_text and address_text.lower() not in location.lower():
                location = f"{location}, {address_text}"
        elif isinstance(address, str) and address.strip():
            location = f"{location}, {clean_text(address)}"

    description = clean_text(str(obj.get("description") or ""))
    all_day = bool(
        isinstance(start_raw, str)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_raw.strip())
    )

    if all_day:
        start = start_dt.date().isoformat()
        end_date = (end_dt.date() if end_dt else start_dt.date()) + timedelta(days=1)
        end = end_date.isoformat()
    else:
        start = start_dt.replace(tzinfo=None).isoformat(timespec="minutes")
        end_value = end_dt or (start_dt + timedelta(hours=1))
        if end_value == start_dt:
            end_value = start_dt + timedelta(hours=1)
        end = end_value.replace(tzinfo=None).isoformat(timespec="minutes")

    return Event(
        uid=stable_uid(url, title, start),
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        url=url,
        location=location,
        description=description,
    )


def iter_jsonld_events(value: Any, base_url: str) -> list[Event]:
    found: list[Event] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ev = event_from_jsonld(node, base_url)
            if ev:
                found.append(ev)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def parse_salesforce_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(ZoneInfo(TIMEZONE))


def event_from_salesforce_record(record: dict[str, Any]) -> Event | None:
    event_id = clean_text(str(record.get("Id") or ""))
    title = clean_text(str(record.get("Subject") or ""))
    start_raw = clean_text(str(record.get("StartDateTime") or ""))
    end_raw = clean_text(str(record.get("EndDateTime") or ""))

    if not event_id.startswith("00U") or not title or not start_raw:
        return None

    start_dt = parse_salesforce_datetime(start_raw)
    end_dt = parse_salesforce_datetime(end_raw) if end_raw else None
    if start_dt is None:
        return None
    if end_dt is None or end_dt <= start_dt:
        end_dt = start_dt + timedelta(hours=1)

    all_day = bool(record.get("IsAllDayEvent"))
    location = clean_text(str(record.get("Location") or "")) or "Sydney Motorsport Park"
    source_url = f"{TARGET_URL}#salesforce-event-{event_id}"

    if all_day:
        activity_date = clean_text(str(record.get("ActivityDate") or ""))
        try:
            start_date = date.fromisoformat(activity_date) if activity_date else start_dt.date()
        except ValueError:
            start_date = start_dt.date()
        end_date = max(start_date + timedelta(days=1), end_dt.date())
        start = start_date.isoformat()
        end = end_date.isoformat()
    else:
        start = start_dt.replace(tzinfo=None).isoformat(timespec="minutes")
        end = end_dt.replace(tzinfo=None).isoformat(timespec="minutes")

    return Event(
        uid=f"{event_id}@smsp-calendar.akumarau",
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        url=source_url,
        location=location,
        description="Source: Sydney Motorsport Park public event calendar",
    )


def extract_salesforce_events(payload: Any) -> list[Event]:
    found: dict[str, Event] = {}
    visited_strings: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ev = event_from_salesforce_record(node)
            if ev:
                found[ev.uid] = ev
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, str):
            candidate = node.strip()
            if (
                candidate
                and candidate not in visited_strings
                and len(candidate) > 20
                and candidate[0] in "[{"
            ):
                visited_strings.add(candidate)
                try:
                    decoded = json.loads(candidate)
                except Exception:
                    return
                walk(decoded)

    walk(payload)
    return list(found.values())


def event_from_candidate(candidate: dict[str, str]) -> Event | None:
    text = clean_text(candidate.get("context") or candidate.get("text") or "")
    title = clean_text(candidate.get("text") or "")
    url = candidate.get("href") or TARGET_URL

    if not title or title.lower() in NAV_WORDS:
        return None
    if len(title) > 180:
        return None

    event_date = parse_date_from_text(text)
    if not event_date:
        return None

    event_time = parse_time_from_text(text)
    if event_time:
        start_dt = datetime.combine(event_date, datetime.min.time()).replace(
            hour=event_time[0], minute=event_time[1]
        )
        start = start_dt.isoformat(timespec="minutes")
        end = (start_dt + timedelta(hours=1)).isoformat(timespec="minutes")
        all_day = False
    else:
        start = event_date.isoformat()
        end = (event_date + timedelta(days=1)).isoformat()
        all_day = True

    return Event(
        uid=stable_uid(url, title, start),
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        url=url,
        source_text=text,
    )


def render_ics(events: list[Event]) -> str:
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//akumarau//Sydney Motorsport Park Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Sydney Motorsport Park",
        f"X-WR-TIMEZONE:{TIMEZONE}",
        "REFRESH-INTERVAL;VALUE=DURATION:P1D",
        "X-PUBLISHED-TTL:P1D",
    ]

    for event in sorted(events, key=lambda x: (x.start, x.title.lower())):
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{event.uid}")
        lines.append(f"DTSTAMP:{now}")
        if event.all_day:
            start = event.start.replace("-", "")
            end = event.end.replace("-", "")
            lines.append(f"DTSTART;VALUE=DATE:{start}")
            lines.append(f"DTEND;VALUE=DATE:{end}")
        else:
            start = datetime.fromisoformat(event.start).strftime("%Y%m%dT%H%M%S")
            end = datetime.fromisoformat(event.end).strftime("%Y%m%dT%H%M%S")
            lines.append(f"DTSTART;TZID={TIMEZONE}:{start}")
            lines.append(f"DTEND;TZID={TIMEZONE}:{end}")
        lines.append(f"SUMMARY:{ics_escape(event.title)}")
        lines.append(f"LOCATION:{ics_escape(event.location)}")
        if event.description:
            lines.append(f"DESCRIPTION:{ics_escape(event.description)}")
        if event.url:
            lines.append(f"URL:{event.url}")
        lines.append("STATUS:CONFIRMED")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    folded: list[str] = []
    for line in lines:
        folded.extend(fold_ics(line))
    return "\r\n".join(folded) + "\r\n"


def dedupe(events: list[Event]) -> list[Event]:
    """Merge duplicate venue records into one calendar event.

    SMSP often publishes the same event once per circuit or facility. Events
    with the same normalized title, start, end, and all-day state are treated
    as one logical event. Their locations are combined so no venue information
    is lost.
    """
    grouped: dict[tuple[str, str, str, bool], list[Event]] = {}

    for event in events:
        key = (
            clean_text(event.title).casefold(),
            event.start,
            event.end,
            event.all_day,
        )
        grouped.setdefault(key, []).append(event)

    merged: list[Event] = []

    for key, group in grouped.items():
        first = group[0]

        locations = sorted(
            {
                clean_text(event.location)
                for event in group
                if clean_text(event.location)
                and clean_text(event.location) != "Sydney Motorsport Park"
            },
            key=str.casefold,
        )
        location = ", ".join(locations) if locations else "Sydney Motorsport Park"

        descriptions = []
        for event in group:
            value = clean_text(event.description)
            if value and value not in descriptions:
                descriptions.append(value)

        identity = "|".join(
            [
                key[0],
                first.start,
                first.end,
                "all-day" if first.all_day else "timed",
            ]
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        uid = f"{digest}@smsp-calendar.akumarau"

        merged.append(
            Event(
                uid=uid,
                title=first.title,
                start=first.start,
                end=first.end,
                all_day=first.all_day,
                url=TARGET_URL,
                location=location,
                description=" ".join(descriptions)
                or "Source: Sydney Motorsport Park public event calendar",
            )
        )

    return merged


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    network_json: list[dict[str, Any]] = []
    events: list[Event] = []
    candidates: list[dict[str, str]] = []
    page_title = ""
    body_text = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-AU",
            timezone_id=TIMEZONE,
            viewport={"width": 1600, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        def capture_response(response) -> None:
            try:
                ctype = response.headers.get("content-type", "")
                if "json" not in ctype.lower():
                    return
                if len(network_json) >= 20:
                    return
                data = response.json()
                network_json.append(
                    {
                        "url": response.url,
                        "status": response.status,
                        "data": data,
                    }
                )
            except Exception:
                return

        page.on("response", capture_response)
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(10000)

        for selector in (
            "button:has-text('Accept')",
            "button:has-text('Allow')",
            "button:has-text('OK')",
            "button:has-text('Got it')",
        ):
            try:
                locator = page.locator(selector).first
                if locator.is_visible(timeout=500):
                    locator.click(timeout=1000)
            except Exception:
                pass

        page.wait_for_timeout(2500)
        page_title = page.title()
        body_text = clean_text(page.locator("body").inner_text(timeout=10000))

        scripts = page.locator("script[type='application/ld+json']")
        for i in range(scripts.count()):
            raw = scripts.nth(i).text_content() or ""
            try:
                payload = json.loads(raw)
                events.extend(iter_jsonld_events(payload, TARGET_URL))
            except Exception:
                continue

        candidates = page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const nodes = [...document.querySelectorAll('a[href]')];
                for (const a of nodes) {
                    const text = (a.innerText || a.textContent || a.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
                    const href = a.href;
                    if (!href || !text) continue;

                    let context = text;
                    let node = a;
                    for (let depth = 0; depth < 7 && node; depth++, node = node.parentElement) {
                        const tag = (node.tagName || '').toLowerCase();
                        const cls = String(node.className || '').toLowerCase();
                        const role = (node.getAttribute && node.getAttribute('role')) || '';
                        if (
                            tag === 'article' ||
                            tag === 'li' ||
                            tag === 'td' ||
                            role === 'listitem' ||
                            cls.includes('event') ||
                            cls.includes('card') ||
                            cls.includes('tile')
                        ) {
                            const candidateText = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                            if (candidateText.length >= text.length && candidateText.length <= 1600) {
                                context = candidateText;
                                break;
                            }
                        }
                    }

                    const key = href + '|' + text + '|' + context;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    out.push({text, href, context});
                }
                return out;
            }"""
        )

        for candidate in candidates:
            ev = event_from_candidate(candidate)
            if ev:
                events.append(ev)

        browser.close()

    # The calendar is a Salesforce Experience Cloud app. Its event records are
    # returned through Aura JSON responses rather than normal anchor elements.
    # Parse those responses recursively, including JSON strings nested inside
    # Salesforce returnValue fields.
    for response in network_json:
        events.extend(extract_salesforce_events(response.get("data")))

    raw_event_count = len(events)
    events = dedupe(events)

    debug = {
        "target_url": TARGET_URL,
        "generated_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "page_title": page_title,
        "body_text_preview": body_text[:12000],
        "candidate_count": len(candidates),
        "raw_event_count": raw_event_count,
        "event_count": len(events),
        "candidates": candidates[:250],
        "network_json": network_json[:20],
    }

    (OUT_DIR / "debug.json").write_text(
        json.dumps(debug, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (OUT_DIR / "events.json").write_text(
        json.dumps([asdict(e) for e in sorted(events, key=lambda x: x.start)], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (OUT_DIR / "calendar.ics").write_text(render_ics(events), encoding="utf-8")

    print(f"Rendered {len(events)} events to {OUT_DIR / 'calendar.ics'}")
    print(f"Captured {len(candidates)} link candidates and {len(network_json)} JSON responses")


if __name__ == "__main__":
    main()
