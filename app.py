"""
Macro Pulse — an x402-paid API that gives AI agents a computed
"economic momentum score" per country, built entirely on free,
public-domain World Bank data (no licensed/paid feeds involved).

Endpoint: GET /macro-pulse/{country_code}   price: $0.02 per call

Before going live on mainnet you need to set two environment variables:
  PAY_TO_ADDRESS   -> your own Base wallet address (0x...) that will receive USDC
  NETWORK          -> "eip155:84532" for Base Sepolia TESTNET (safe to test with)
                      "eip155:8453"  for Base MAINNET (real money)

For testing, the public facilitator at https://x402.org/facilitator works
on Base Sepolia testnet only. For MAINNET settlement, you need a facilitator
that supports Base mainnet — Coinbase's hosted facilitator (via a free
Coinbase Developer Platform API key) is the standard choice.
"""

import os
import httpx
from fastapi import FastAPI, Request
from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact.register import register_exact_evm_server

# ---------------------------------------------------------------------------
# Config — fill these in before going live
# ---------------------------------------------------------------------------
PAY_TO_ADDRESS = os.environ.get("PAY_TO_ADDRESS", "0xREPLACE_WITH_YOUR_BASE_WALLET_ADDRESS")
NETWORK = os.environ.get("NETWORK", "eip155:84532")  # default: Base Sepolia TESTNET
FACILITATOR_URL = os.environ.get("FACILITATOR_URL", "https://x402.org/facilitator")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://macro-pulse-x402.onrender.com")
PRICE = "$0.02"
ASSET_ADDRESS = "0x036CbD53842c5426634e7929541eC2318f3dCF7e" if NETWORK == "eip155:84532" else "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

app = FastAPI(title="Macro Pulse")

facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))
server = x402ResourceServer(facilitator)
register_exact_evm_server(server, networks=NETWORK)

routes = {
    "GET /macro-pulse/*": {
        "accepts": {
            "scheme": "exact",
            "payTo": PAY_TO_ADDRESS,
            "price": PRICE,
            "network": NETWORK,
        },
        "description": "Computed economic momentum score for a country (GDP growth, inflation, unemployment trend, synthesized into a single directional signal).",
    }
}


@app.middleware("http")
async def x402_middleware(request: Request, call_next):
    return await payment_middleware(routes, server)(request, call_next)


# World Bank indicator codes (free, public domain, no API key required)
INDICATORS = {
    "gdp_growth": "NY.GDP.MKTP.KD.ZG",
    "inflation": "FP.CPI.TOTL.ZG",
    "unemployment": "SL.UEM.TOTL.ZS",
}


async def fetch_indicator(country_code: str, indicator: str) -> list[dict]:
    url = f"https://api.worldbank.org/v2/country/{country_code}/indicator/{indicator}"
    params = {"format": "json", "per_page": 6, "mrnev": 6}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if len(data) < 2 or not data[1]:
            return []
        return [row for row in data[1] if row["value"] is not None]


def compute_momentum(series: list[dict]) -> float | None:
    """Simple trend: latest value minus average of prior values, in std-dev units."""
    values = [row["value"] for row in series]
    if len(values) < 2:
        return None
    latest = values[0]
    prior = values[1:]
    avg_prior = sum(prior) / len(prior)
    return round(latest - avg_prior, 2)


@app.get("/macro-pulse/{country_code}")
async def macro_pulse(country_code: str):
    country_code = country_code.upper()
    results = {}
    for name, code in INDICATORS.items():
        series = await fetch_indicator(country_code, code)
        results[name] = {
            "latest_value": series[0]["value"] if series else None,
            "latest_year": series[0]["date"] if series else None,
            "trend_momentum": compute_momentum(series),
            "history": [{"year": r["date"], "value": r["value"]} for r in series],
        }

    # Synthesize a single directional score: positive GDP momentum and falling
    # inflation/unemployment momentum push the score up.
    score = 0.0
    weights = {"gdp_growth": 1.0, "inflation": -0.7, "unemployment": -0.7}
    contributions = 0
    for name, w in weights.items():
        m = results[name]["trend_momentum"]
        if m is not None:
            score += w * m
            contributions += 1
    momentum_score = round(score / contributions, 2) if contributions else None

    if momentum_score is None:
        label = "insufficient_data"
    elif momentum_score > 0.5:
        label = "improving"
    elif momentum_score < -0.5:
        label = "deteriorating"
    else:
        label = "stable"

    return {
        "country": country_code,
        "indicators": results,
        "momentum_score": momentum_score,
        "momentum_label": label,
        "source": "World Bank Open Data (public domain, https://data.worldbank.org)",
        "disclaimer": "Directional context only. Not financial advice, not a buy/sell signal.",
    }


@app.get("/")
async def root():
    return {
        "service": "Macro Pulse",
        "endpoint": "/macro-pulse/{country_code}  e.g. /macro-pulse/US",
        "price": PRICE,
        "network": NETWORK,
    }


# Machine-readable discovery manifest — lets x402 directories (x402scan,
# x402-list, agentcash, etc.) and AI agents find this service automatically.
@app.get("/.well-known/x402")
async def x402_discovery_manifest():
    return {
        "x402Version": 2,
        "resources": [
            {
                "resource": f"{PUBLIC_BASE_URL}/macro-pulse/{{country_code}}",
                "method": "GET",
                "description": (
                    "Computed economic momentum score for any country (ISO-2 code, e.g. US, GB, JP). "
                    "Synthesizes GDP growth, inflation, and unemployment trend data from the World Bank "
                    "into a single directional signal (improving / stable / deteriorating)."
                ),
                "price": PRICE,
                "network": NETWORK,
                "asset": ASSET_ADDRESS,
                "payTo": PAY_TO_ADDRESS,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "country_code": {
                            "type": "string",
                            "description": "ISO 3166-1 alpha-2 country code, e.g. US, GB, JP, DE",
                        }
                    },
                    "required": ["country_code"],
                },
            }
        ],
    }
