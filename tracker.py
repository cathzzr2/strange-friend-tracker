import hashlib
import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup

URL = "https://www.filmlinc.org/films/silent-friend/"
STATE_FILE = "state.json"
# ticket: 仅在购票相关信号变化时发信（默认，较少误报）
# any: 整页正文摘要变化即发信（排期、文案等任意更新都会通知）
TRACKER_ALERT_MODE_ENV = "TRACKER_ALERT_MODE"

TARGET_KEYWORDS = [
    "1:30 PM Q&A",
    "5:30 PM Q&A",
    "6:00 PM Q&A",
    "Q&A",
    "Silent Friend",
]

TICKET_HINTS = [
    "Buy Tickets",
    "Buy Ticket",
    "Tickets",
    "Purchase",
    "On Sale",
    "Sold Out",
    "Waitlist",
    "Member Presale",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required secret/env: {name}")
    return value


def fetch_html() -> str:
    with httpx.Client(follow_redirects=True, timeout=20.0) as client:
        response = client.get(URL, headers=HEADERS)
        response.raise_for_status()
        return response.text


def extract_state(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    page_text = normalize_text(soup.get_text(" ", strip=True))
    page_digest = hashlib.sha256(page_text.encode("utf-8")).hexdigest()

    links: List[Tuple[str, str]] = []
    for a_tag in soup.find_all("a", href=True):
        label = normalize_text(a_tag.get_text(" ", strip=True))
        href = a_tag["href"].strip()
        links.append((label, href))

    matched_targets = [
        keyword for keyword in TARGET_KEYWORDS
        if keyword.lower() in page_text.lower()
    ]

    matched_hints = [
        hint for hint in TICKET_HINTS
        if hint.lower() in page_text.lower()
    ]

    ticket_links: List[Tuple[str, str]] = []
    for label, href in links:
        combined = f"{label} {href}".lower()
        if any(word in combined for word in ["ticket", "purchase", "sale", "waitlist"]):
            ticket_links.append((label, href))

    fingerprint_parts: List[str] = []
    fingerprint_parts.extend(sorted(matched_targets))
    fingerprint_parts.extend(sorted(matched_hints))
    fingerprint_parts.extend(
        sorted([f"{label}|{href}" for label, href in ticket_links[:20]])
    )

    return {
        "matched_targets": matched_targets,
        "matched_hints": matched_hints,
        "ticket_links": ticket_links,
        "fingerprint": " || ".join(fingerprint_parts),
        "page_digest": page_digest,
    }


def load_previous_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def save_current_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def looks_like_ticket_release(current_state: Dict[str, Any], previous_state: Dict[str, Any]) -> bool:
    current_links = current_state.get("ticket_links", [])
    previous_links = previous_state.get("ticket_links", [])
    current_hints = set(current_state.get("matched_hints", []))
    previous_hints = set(previous_state.get("matched_hints", []))

    strong_words = {"Buy Tickets", "Buy Ticket", "On Sale", "Purchase", "Waitlist", "Sold Out"}

    if current_links and current_links != previous_links:
        return True

    if any(word in current_hints and word not in previous_hints for word in strong_words):
        return True

    if current_state.get("fingerprint") != previous_state.get("fingerprint"):
        if current_links or current_hints:
            return True

    return False


def alert_mode() -> str:
    mode = os.getenv(TRACKER_ALERT_MODE_ENV, "ticket").strip().lower()
    if mode not in ("ticket", "any"):
        return "ticket"
    return mode


def page_changed(current: Dict[str, Any], previous: Dict[str, Any]) -> bool:
    prev_d: Optional[str] = previous.get("page_digest")
    if prev_d is None:
        return False
    return current.get("page_digest") != prev_d


def should_alert(current: Dict[str, Any], previous: Dict[str, Any]) -> bool:
    if not previous:
        return False
    if alert_mode() == "any":
        return page_changed(current, previous)
    return looks_like_ticket_release(current, previous)


def format_ticket_links(links: List[Tuple[str, str]], limit: int = 15) -> str:
    lines = [f"  - {label}\n    {href}" for label, href in links[:limit]]
    if len(links) > limit:
        lines.append(f"  ... 另有 {len(links) - limit} 条链接未列出")
    return "\n".join(lines) if lines else "  (无)"


def build_alert_email_body(
    current: Dict[str, Any], previous: Dict[str, Any], mode: str
) -> str:
    lines = [
        f"页面: {URL}",
        f"检测模式: {mode}",
        "",
        "当前页面摘要 (SHA256 前 16 位):",
        f"  {str(current.get('page_digest', ''))[:16]}…",
    ]
    if previous.get("page_digest"):
        lines.append(f"上次: {str(previous.get('page_digest'))[:16]}…")

    lines.extend(
        [
            "",
            "购票相关关键词:",
            f"  {current.get('matched_hints', [])}",
            "",
            "场次 / 目标关键词:",
            f"  {current.get('matched_targets', [])}",
            "",
            "购票链接:",
            format_ticket_links(current.get("ticket_links", [])),
        ]
    )
    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    smtp_server = require_env("SMTP_SERVER")
    smtp_port = int(require_env("SMTP_PORT"))
    sender_email = require_env("SENDER_EMAIL")
    sender_password = require_env("SENDER_PASSWORD")
    recipient_email = require_env("RECIPIENT_EMAIL")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = recipient_email

    with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, [recipient_email], msg.as_string())


def main() -> None:
    print("Starting tracker...")

    html = fetch_html()
    print("Fetched page successfully.")

    current_state = extract_state(html)
    previous_state = load_previous_state()

    print("Current hints:", current_state.get("matched_hints", []))
    print("Current ticket links:", current_state.get("ticket_links", [])[:5])

    mode = alert_mode()
    fire = should_alert(current_state, previous_state)

    if fire:
        if mode == "any":
            subject = "Silent Friend 页面有更新"
        else:
            subject = "抢票提醒：Silent Friend 可能开票了"
        body = build_alert_email_body(current_state, previous_state, mode)
        send_email(subject, body)
        print("Alert email sent.")
    else:
        print("No alert this run.")

    save_current_state(current_state)
    print("Saved state.json successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        sys.exit(1)