# Macro Pulse — x402-paid API

An x402-protected endpoint that gives AI agents a computed "economic momentum
score" per country. Built entirely on free, public-domain World Bank data
(https://data.worldbank.org) — nothing here repackages a licensed/paid feed.

## What it does

`GET /macro-pulse/{country_code}` (e.g. `/macro-pulse/US`) — price: $0.02/call

Pulls GDP growth, inflation, and unemployment trend data from the World Bank
API and synthesizes a single directional "momentum" score + label
(improving / stable / deteriorating), with the raw indicator history included
for transparency.

Tested locally — confirmed the payment gate correctly returns HTTP 402 with
valid payment requirements (scheme=exact, asset=USDC, network=Base) when no
payment is attached, and the underlying data/scoring logic returns correct
World Bank data.

## What YOU need to do before this can earn real money

1. **Get a Base wallet address** (Coinbase Wallet or MetaMask configured for
   Base). This is where USDC payments land. Set it as `PAY_TO_ADDRESS`.
   NEVER share your private key/seed phrase with anyone, including me.

2. **Decide testnet vs. mainnet:**
   - Testnet (Base Sepolia, `NETWORK=eip155:84532`) — what this is configured
     for right now. Safe to test with fake USDC, $0 real risk.
   - Mainnet (`NETWORK=eip155:8453`) — real money. For mainnet settlement
     you need a facilitator that supports it. The public
     `https://x402.org/facilitator` is testnet-only — for mainnet, sign up
     for a free Coinbase Developer Platform (CDP) account
     (https://portal.cdp.coinbase.com) and use their hosted facilitator +
     API key.

3. **Deploy it somewhere publicly reachable** (a real domain/URL, not
   localhost) so agents and marketplaces can find it — happy to help with
   this once wallet + network choice are set.

4. **List it on a discovery layer** — https://www.x402scan.com or
   https://x402.org's Bazaar so agents can find and pay for it automatically.
   Also add a `/.well-known/x402` manifest for auto-discovery.

## Realistic expectations

At $0.02/call, this needs real volume (hundreds to thousands of calls) to
add up to meaningful money. Treat this as a slow-build side project, not a
quick win — see prior discussion for realistic revenue ranges.

## Files

- `app.py` — the full FastAPI service with x402 payment middleware
