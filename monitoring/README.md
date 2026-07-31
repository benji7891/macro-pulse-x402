# Macro Pulse monitoring scripts

These scripts back the daily automated health check for the Macro Pulse x402
API. They are **not** part of the deployed service — Render only runs
`app.py` from the repo root. This folder exists purely so the monitoring
tooling has a durable home in version control instead of only living in an
ephemeral sandbox.

- `monitor.py` — fetches Render app logs (request counts / 5xx error rates)
  and on-chain USDC transfers to the service wallet on Base mainnet, then
  compares against a rolling 7-day baseline to decide whether a revenue/error
  alert is warranted. Run in three steps (`render_fetch`, `chain_fetch`,
  `combine`) because the Render-credential proxy and the public Base RPC call
  can't share a bash invocation.
- `bazaar_check.py` — checks whether Macro Pulse is indexed in Coinbase's
  public CDP x402 Bazaar discovery feed.
- `gold402_check.py` — checks whether the Macro Pulse listing page has gone
  live on the 24K Labs Gold-402 x402 directory.
- `balance_watch.py` — ad hoc wallet balance check against the service
  wallet.

Runtime state files (`state.json`, `bazaar_state.json`, `gold402_state.json`,
`balance_state.json`) and log data (`daily_stats.jsonl`) are intentionally
**not** checked in here — they're local run artifacts, not code.

## Known gotcha (fixed 2026-07-31)

Render's `/v1/logs` endpoint rate-limits the pagination loop in
`fetch_render_logs()` after roughly 4 rapid sequential page requests. This can
surface as either a clean `{"error": "rate limit exceeded"}` JSON body (curl
exit code 22) or an outright dropped connection (curl exit code 7, empty
stdout/stderr). `http_get_json_via_curl()` now retries on both failure modes
with exponential backoff, and the pagination loop paces itself with a short
sleep between pages to avoid triggering the limit in the first place.
