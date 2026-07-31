"""Lightweight on-chain USDC balance watcher for the Macro Pulse service wallet.

Queries the public Base mainnet RPC for the current USDC balance of the
service wallet and compares it to the last known balance stored in
balance_state.json. Prints a single JSON line describing the result.
No credentials required (public RPC only).
"""
import json
import os
import sys
import urllib.request

WALLET = "0xb4a9238c9400a7f1bb7924606ff2ea634a0f3ec4"
USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RPC_URL = "https://mainnet.base.org"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "balance_state.json")


def get_balance():
    padded = WALLET[2:].rjust(64, "0")
    data = "0x70a08231" + padded
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": USDC_CONTRACT, "data": data}, "latest"],
        "id": 1,
    }).encode()
    req = urllib.request.Request(
        RPC_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; MacroPulseBalanceWatch/1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.load(resp)
    units = int(result["result"], 16)
    return units / 1e6


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_balance": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    state = load_state()
    last_balance = state.get("last_balance")

    try:
        current_balance = get_balance()
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(0)

    if last_balance is None:
        # First run — establish baseline, do not alert.
        save_state({"last_balance": current_balance})
        print(json.dumps({
            "status": "baseline_set",
            "current_balance": current_balance,
        }))
        return

    if current_balance != last_balance:
        save_state({"last_balance": current_balance})
        print(json.dumps({
            "status": "changed",
            "previous_balance": last_balance,
            "current_balance": current_balance,
            "delta": round(current_balance - last_balance, 6),
        }))
    else:
        print(json.dumps({
            "status": "unchanged",
            "current_balance": current_balance,
        }))


if __name__ == "__main__":
    main()
