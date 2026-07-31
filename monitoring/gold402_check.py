"""
Checks whether the Macro Pulse listing page has gone live on 24K Labs' Gold-402
directory site (24klabs.ai). The underlying data (directory/apis.md on the
Haustorium12/gold-402 GitHub repo) already includes Macro Pulse as of the PR
#56 merge, but the live site is server-rendered/pre-built (entry counts like
"415 curated entries" are baked into the HTML at build time), so the actual
per-listing page needs a fresh deploy before it renders real content instead
of falling back to the generic homepage shell.

This check is curl-only (no browser/JS execution needed): once deployed, the
per-listing page's <title> tag switches from the generic site title to one
that mentions "Macro Pulse" and its body contains the phrase "Macro Pulse".

Usage: python3 gold402_check.py
Prints a JSON object and updates gold402_state.json so the monitoring cron can
detect a fresh state change (not-live -> live) and alert once.
"""
import re
import subprocess
import json
import datetime
import os

LISTING_URL = "https://24klabs.ai/listing/macro-pulse/"
HOME_URL = "https://24klabs.ai/"
STATE_FILE = os.path.join(os.path.dirname(__file__), "gold402_state.json")


def fetch(url):
    result = subprocess.run(["curl", "-s", url], capture_output=True, text=True, timeout=30)
    return result.stdout


def check_gold402():
    listing_html = fetch(LISTING_URL)
    home_html = fetch(HOME_URL)

    title_match = re.search(r"<title>(.*?)</title>", listing_html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1) if title_match else ""

    live = "macro pulse" in title.lower() or "macro pulse" in listing_html.lower()

    count_match = re.search(r"([\d,]{2,6})\s*</[^>]+>\s*<[^>]+>\s*Curated entries", home_html, re.IGNORECASE)
    if not count_match:
        # fallback: just grab the first 2-4 digit number near "curated"
        count_match = re.search(r"(\d{2,6})[^\d]{0,80}[Cc]urated entries", home_html)
    curated_entries = int(count_match.group(1).replace(",", "")) if count_match else None

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    prev_state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            prev_state = json.load(f)

    newly_live = live and not prev_state.get("live", False)

    state = {
        "live": live,
        "listing_title": title,
        "curated_entries": curated_entries,
        "checked_at": now,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    return {
        "live": live,
        "newly_live": newly_live,
        "listing_title": title,
        "curated_entries": curated_entries,
        "prev_curated_entries": prev_state.get("curated_entries"),
        "checked_at": now,
    }


if __name__ == "__main__":
    print(json.dumps(check_gold402()))
