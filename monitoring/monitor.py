#!/usr/bin/env python3
"""
Macro Pulse x402 daily monitor.

Checks Render app logs (for real per-request error rates, parsed from uvicorn's
access-log lines) and the Base mainnet USDC transfers to the service wallet
(for real revenue/payment volume), compares against a rolling history file,
and prints a JSON result indicating whether an alert-worthy condition was hit.

Usage:
    python3 monitor.py            # normal run: updates state + history, prints result
    python3 monitor.py --dry-run  # does not persist state/history changes

Requires the RENDER_API_TOKEN env var (injected via api_credentials=["custom-cred:api.render.com"]
when invoked through bash) to be reachable as a Bearer token against https://api.render.com/v1.
Render's REST client picks it up automatically via the HTTPS_PROXY injection - no code needed
to read the token itself, just make the request and the proxy injects auth.
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
import datetime

import requests

WORKDIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(WORKDIR, "state.json")
HISTORY_FILE = os.path.join(WORKDIR, "daily_stats.jsonl")

RENDER_OWNER_ID = "tea-d9haha58nd3s73ck6lpg"
RENDER_SERVICE_ID = "srv-d9hb2mrtqb8s7398pd00"
RENDER_API_BASE = "https://api.render.com/v1"

BASE_RPC = "https://mainnet.base.org"
USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WALLET = "0xb4a9238c9400a7f1bb7924606ff2ea634a0f3ec4"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOPIC_TO_WALLET = "0x" + "0" * 24 + WALLET[2:]
MAX_BLOCK_RANGE = 9500  # public RPC caps at 10000, leave margin

# Only USDC transfers landing on our exact advertised price points count as real
# x402 API revenue. Anything else arriving at this wallet (e.g. an unrelated
# batch multicall someone sent) is NOT revenue and must not pollute the revenue
# baseline - it's tracked separately as "other_usdc" so it's still visible, just
# not conflated with actual paid API usage.
#   20000 units = $0.02  -> GET /macro-pulse/{country_code}
#   50000 units = $0.05  -> GET /macro-pulse-batch/{country_codes}
KNOWN_PRICE_POINTS_UNITS = {20000, 50000}

ACCESS_LOG_RE = re.compile(r'"[A-Z]+ (\S+) HTTP/\d\.\d" (\d{3})')

ERROR_RATE_ALERT_THRESHOLD = 0.01  # 1%


def http_get_json_via_curl(url, retries=5):
    """Used for Render API calls: curl correctly handles the credential-injection
    proxy's TLS/auth setup out of the box, unlike Python's requests/urllib here.

    Retries on ALL transient failure modes, not just clean rate-limit JSON bodies.
    Render's log API rate-limits the pagination loop after ~4 rapid requests, and
    this can surface two different ways: (a) a clean 429-style JSON body with
    "rate limit exceeded" text (curl exit code 22 via --fail-with-body), or
    (b) the connection simply getting dropped (curl exit code 7, empty
    stdout/stderr). Both are transient and retryable - only genuinely fatal
    errors (auth failures, malformed requests) should raise immediately, and
    those come back with a non-empty error body that doesn't look like a
    connection-level failure.
    """
    import time
    last_err = None
    RETRYABLE_CURL_CODES = {7, 22, 28, 35, 52, 56}  # connect/timeout/rate-limit-ish failures
    for attempt in range(retries):
        result = subprocess.run(
            ["curl", "-s", "-m", "30", "--fail-with-body", url],
            capture_output=True, text=True, timeout=40,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        last_err = f"curl failed (code {result.returncode}): {result.stderr or result.stdout}"
        looks_rate_limited = "rate limit" in (result.stdout or "").lower()
        looks_transient = result.returncode in RETRYABLE_CURL_CODES and not result.stdout.strip()
        if looks_rate_limited or looks_transient:
            time.sleep(15 * (2 ** attempt))
            continue
        raise RuntimeError(last_err)
    raise RuntimeError(last_err)


def rpc_call(method, params):
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    resp = requests.post(
        BASE_RPC, json=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    resp.raise_for_status()
    r = resp.json()
    if "error" in r:
        raise RuntimeError(f"RPC error: {r['error']}")
    return r["result"]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_history(entry):
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    out = []
    with open(HISTORY_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def fetch_render_logs(start_iso, end_iso):
    """Fetch all Render app logs for [start_iso, end_iso), paginating via nextStartTime."""
    all_logs = []
    cur_start = start_iso
    cur_end = end_iso
    safety = 0
    while True:
        safety += 1
        if safety > 200:
            break
        params = {
            "ownerId": RENDER_OWNER_ID,
            "resource": RENDER_SERVICE_ID,
            "startTime": cur_start,
            "endTime": cur_end,
            "limit": "100",
            "direction": "forward",
        }
        url = f"{RENDER_API_BASE}/logs?" + urllib.parse.urlencode(params)
        data = http_get_json_via_curl(url)
        logs = data.get("logs") or []
        all_logs.extend(logs)
        if safety > 1:
            import time
            time.sleep(1.5)  # pace pagination requests to avoid Render's log-API rate limit
        if data.get("hasMore") and data.get("nextStartTime"):
            cur_start = data["nextStartTime"]
            if data.get("nextEndTime"):
                cur_end = data["nextEndTime"]
        else:
            break
    return all_logs


def parse_render_logs(logs):
    total_requests = 0
    error_5xx = 0
    error_lines = []
    for entry in logs:
        msg = entry.get("message", "")
        m = ACCESS_LOG_RE.search(msg)
        if m:
            total_requests += 1
            status = int(m.group(2))
            if status >= 500:
                error_5xx += 1
                error_lines.append({"timestamp": entry.get("timestamp"), "message": msg})
    return total_requests, error_5xx, error_lines


def fetch_usdc_transfers(from_block, to_block):
    """Fetch USDC Transfer logs to WALLET between from_block and to_block inclusive, chunked."""
    all_logs = []
    cur = from_block
    while cur <= to_block:
        chunk_end = min(cur + MAX_BLOCK_RANGE, to_block)
        result = rpc_call("eth_getLogs", [{
            "fromBlock": hex(cur),
            "toBlock": hex(chunk_end),
            "address": USDC_CONTRACT,
            "topics": [TRANSFER_TOPIC, None, TOPIC_TO_WALLET],
        }])
        all_logs.extend(result)
        cur = chunk_end + 1
    return all_logs


def classify_usdc_transfers(transfer_logs):
    """Split raw incoming USDC transfer logs into real x402 revenue (amount
    matches one of our advertised price points) vs everything else (unrelated
    incoming transfers that must not be counted as revenue)."""
    revenue_units = 0
    revenue_count = 0
    other_units = 0
    other_count = 0
    other_tx_hashes = []
    for log in transfer_logs:
        amount = int(log["data"], 16)
        if amount in KNOWN_PRICE_POINTS_UNITS:
            revenue_units += amount
            revenue_count += 1
        else:
            other_units += amount
            other_count += 1
            other_tx_hashes.append(log.get("transactionHash"))
    return {
        "revenue_usdc": revenue_units / 1_000_000,
        "tx_count": revenue_count,
        "other_usdc": other_units / 1_000_000,
        "other_tx_count": other_count,
        "other_tx_hashes": other_tx_hashes,
    }


RENDER_TMP = os.path.join(WORKDIR, "_render_result.json")
CHAIN_TMP = os.path.join(WORKDIR, "_chain_result.json")


def cmd_render_fetch():
    """Step 1: run with api_credentials=['custom-cred:api.render.com']. Talks ONLY to Render."""
    state = load_state()
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    last_log_time = state["last_log_time"] if state else (
        now - datetime.timedelta(hours=24)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    render_error = None
    total_requests = error_5xx = 0
    error_lines = []
    try:
        logs = fetch_render_logs(last_log_time, now_iso)
        total_requests, error_5xx, error_lines = parse_render_logs(logs)
    except Exception as e:
        render_error = str(e)

    with open(RENDER_TMP, "w") as f:
        json.dump({
            "now_iso": now_iso,
            "last_log_time": last_log_time,
            "total_requests": total_requests,
            "error_5xx": error_5xx,
            "error_lines": error_lines,
            "render_error": render_error,
        }, f)
    print(f"Render fetch done: {total_requests} requests, {error_5xx} 5xx errors, error={render_error}")


def cmd_chain_fetch():
    """Step 2: run WITHOUT any api_credentials (public RPC, no proxy). Talks ONLY to Base RPC."""
    state = load_state()
    last_block = None
    if state:
        last_block = state.get("last_block")

    chain_error = None
    tx_count = 0
    revenue_usdc = 0.0
    other_usdc = 0.0
    other_tx_count = 0
    other_tx_hashes = []
    latest_block = None
    try:
        latest_block = int(rpc_call("eth_blockNumber", []), 16)
        if last_block is None:
            last_block = latest_block - 9000  # bootstrap ~5h of history
        if latest_block > last_block:
            transfer_logs = fetch_usdc_transfers(last_block + 1, latest_block)
            classified = classify_usdc_transfers(transfer_logs)
            tx_count = classified["tx_count"]
            revenue_usdc = classified["revenue_usdc"]
            other_usdc = classified["other_usdc"]
            other_tx_count = classified["other_tx_count"]
            other_tx_hashes = classified["other_tx_hashes"]
    except Exception as e:
        chain_error = str(e)

    with open(CHAIN_TMP, "w") as f:
        json.dump({
            "last_block_used": last_block,
            "latest_block": latest_block,
            "tx_count": tx_count,
            "revenue_usdc": revenue_usdc,
            "other_usdc": other_usdc,
            "other_tx_count": other_tx_count,
            "other_tx_hashes": other_tx_hashes,
            "chain_error": chain_error,
        }, f)
    print(f"Chain fetch done: {tx_count} real x402 payment(s) totaling ${revenue_usdc} revenue; "
          f"{other_tx_count} unrelated incoming transfer(s) totaling ${other_usdc} (not counted as revenue); "
          f"error={chain_error}")


def cmd_combine():
    """Step 3: run WITHOUT credentials. Merges temp files, updates state/history, prints result."""
    dry_run = "--dry-run" in sys.argv
    with open(RENDER_TMP) as f:
        r = json.load(f)
    with open(CHAIN_TMP) as f:
        c = json.load(f)

    now_iso = r["now_iso"]
    total_requests = r["total_requests"]
    error_5xx = r["error_5xx"]
    error_lines = r["error_lines"]
    render_error = r["render_error"]

    tx_count = c["tx_count"]
    revenue_usdc = c["revenue_usdc"]
    other_usdc = c.get("other_usdc", 0.0)
    other_tx_count = c.get("other_tx_count", 0)
    other_tx_hashes = c.get("other_tx_hashes", [])
    chain_error = c["chain_error"]
    latest_block = c["latest_block"] if c["latest_block"] is not None else c["last_block_used"]

    error_rate = (error_5xx / total_requests) if total_requests > 0 else 0.0

    entry = {
        "run_time": now_iso,
        "window_start": r["last_log_time"],
        "request_count": total_requests,
        "error_5xx_count": error_5xx,
        "error_rate": round(error_rate, 4),
        "tx_count": tx_count,
        "revenue_usdc": revenue_usdc,
        "other_usdc": other_usdc,
        "other_tx_count": other_tx_count,
        "other_tx_hashes": other_tx_hashes,
        "render_error": render_error,
        "chain_error": chain_error,
    }

    history = load_history()
    # trailing 7 prior entries (excluding this one) for baseline context
    baseline_entries = history[-7:]
    baseline_avg_requests = (
        sum(h["request_count"] for h in baseline_entries) / len(baseline_entries)
        if baseline_entries else None
    )
    baseline_avg_revenue = (
        sum(h["revenue_usdc"] for h in baseline_entries) / len(baseline_entries)
        if baseline_entries else None
    )

    # Real revenue (tx_count>0) always alerts per standing preference ("tell me
    # any day revenue is nonzero"). Unexplained/other incoming USDC also alerts
    # separately - it's not revenue, but it's not something to silently ignore
    # either.
    alert = (
        (error_rate > ERROR_RATE_ALERT_THRESHOLD)
        or (tx_count > 0)
        or (other_tx_count > 0)
        or render_error
        or chain_error
    )

    result = {
        **entry,
        "baseline_avg_requests_7d": baseline_avg_requests,
        "baseline_avg_revenue_7d": baseline_avg_revenue,
        "alert": bool(alert),
        "sample_error_lines": error_lines[:5],
    }

    if not dry_run:
        append_history(entry)
        save_state({"last_log_time": now_iso, "last_block": latest_block})
        # clean up temp files
        for tmp in (RENDER_TMP, CHAIN_TMP):
            if os.path.exists(tmp):
                os.remove(tmp)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("render_fetch", "chain_fetch", "combine"):
        print("Usage: monitor.py [render_fetch|chain_fetch|combine] [--dry-run]", file=sys.stderr)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "render_fetch":
        cmd_render_fetch()
    elif cmd == "chain_fetch":
        cmd_chain_fetch()
    elif cmd == "combine":
        cmd_combine()
