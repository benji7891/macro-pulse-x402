"""
Checks whether Macro Pulse is indexed in Coinbase's public CDP x402 Bazaar
discovery feed (the underlying catalog that powers agentic.market and other
Base-ecosystem agent marketplaces). No auth required - this is a public feed.

Usage: python3 bazaar_check.py
Prints a JSON object: {"found": bool, "total_catalog_items": int, "checked_at": iso timestamp}
Also updates bazaar_state.json with the last-known found/not-found status so the
monitoring cron can detect a state change (not_found -> found) and alert once.
"""
import json
import subprocess
import datetime
import os

DOMAIN = "macro-pulse-x402.onrender.com"
WALLET = "0xb4a9238c9400a7f1bb7924606ff2ea634a0f3ec4"
STATE_FILE = os.path.join(os.path.dirname(__file__), "bazaar_state.json")
FEED_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"


def fetch_page(limit, offset):
    result = subprocess.run(
        ["curl", "-s", f"{FEED_URL}?limit={limit}&offset={offset}"],
        capture_output=True, text=True, timeout=30
    )
    return json.loads(result.stdout)


def check_bazaar():
    first = fetch_page(1000, 0)
    total = first.get("pagination", {}).get("total", 0)
    all_items = list(first.get("items", []))
    offset = 1000
    while offset < total:
        page = fetch_page(1000, offset)
        all_items.extend(page.get("items", []))
        offset += 1000

    blob = json.dumps(all_items).lower()
    found = DOMAIN.lower() in blob or WALLET.lower() in blob

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    prev_state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            prev_state = json.load(f)

    newly_found = found and not prev_state.get("found", False)

    state = {"found": found, "total_catalog_items": total, "checked_at": now}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    return {"found": found, "newly_found": newly_found, "total_catalog_items": total, "checked_at": now}


if __name__ == "__main__":
    print(json.dumps(check_bazaar()))
