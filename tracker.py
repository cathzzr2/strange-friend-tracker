import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from typing import Any, Dict, List, Tuple

import httpx
from bs4 import BeautifulSoup

URL = "https://www.filmlinc.org/films/silent-friend/"

STATE_FILE = "state.json"

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


def fetch_html() -> str:
    with httpx.Client(follow_redirects=True, timeout=20.0, http2=True) as client:
        response = client.get(URL, headers=HEADERS)
        response.raise_for_status()
        return response.text


def extract_state(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    page_text = normalize_text(soup.get_text(" ", strip=True))

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

    # determine key changes on the web page
    fingerprint_parts: List[str] = []
    fingerprint_parts.extend(sorted(matched_targets))
    fingerprint_parts.extend(sorted(matched_hints))
    fingerprint_parts.extend(
        sorted([f"{label}|{href}" for label, href in ticket_links[:20]])
    )

    fingerprint = " || ".join(fingerprint_parts)

    return {
        "matched_targets": matched_targets,
        "matched_hints": matched_hints,
        "ticket_links": ticket_links,
        "fingerprint": fingerprint,
        "text_sample": page_text[:1000],
    }


def load_previous_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {}


def save_current_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def looks_like_ticket_release(current_state: Dict[str, Any], previous_state: Dict[str, Any]) -> bool:
    current_links = current_state.get("ticket_links", [])
    previous_links = previous_state.get("ticket_links", [])
    current_hints = set(current_state.get("matched_hints", []))
    previous_hints = set(previous_state.get("matched_hints", []))

    strong_words = {"Buy Tickets", "Buy Ticket", "On Sale", "Purchase", "Waitlist", "Sold Out"}

    # no ticket links before, now has
    if current_links and current_links != previous_links:
        return True

    # new strong hints
    if any(word in current_hints and word not in previous_hints for word in strong_words):
        return True

    # fingerprint changed and contains ticket clues
    if current_state.get("fingerprint") != previous_state.get("fingerprint"):
        if current_links or current_hints:
            return True

    return False


def send_email(subject: str, body: str) -> None:
    smtp_server = os.environ["SMTP_SERVER"]
    smtp_port = int(os.environ["SMTP_PORT"])
    sender_email = os.environ["SENDER_EMAIL"]
    sender_password = os.environ["SENDER_PASSWORD"]
    recipient_email = os.environ["RECIPIENT_EMAIL"]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = recipient_email

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, [recipient_email], msg.as_string())


def main() -> None:
    html = fetch_html()
    current_state = extract_state(html)
    previous_state = load_previous_state()

    print("Current hints:", current_state.get("matched_hints", []))
    print("Current ticket links:", current_state.get("ticket_links", [])[:5])

    should_alert = False

    # first run only records state, no email, avoid cold start false alerts
    if previous_state:
        should_alert = looks_like_ticket_release(current_state, previous_state)

    if should_alert:
        subject = "Silent Friend tracker alert"
        body_lines = [
            "Possible ticket-related update detected.",
            "",
            f"URL: {URL}",
            "",
            f"Matched targets: {current_state.get('matched_targets', [])}",
            f"Matched hints: {current_state.get('matched_hints', [])}",
            f"Ticket links: {current_state.get('ticket_links', [])[:10]}",
            "",
            "Open the page now and check the Q&A sessions.",
        ]
        body = "\n".join(body_lines)
        send_email(subject, body)
        print("Alert email sent.")
    else:
        print("No alert this run.")

    save_current_state(current_state)


if __name__ == "__main__":
    main()