"""
Macro Pulse — an x402-paid API that gives AI agents a computed
"economic momentum score" per country, built entirely on free,
public-domain World Bank data (no licensed/paid feeds involved).

Endpoint: GET /macro-pulse/{country_code}   price: $0.02 per call

Before going live on mainnet you need to set these environment variables:
  PAY_TO_ADDRESS      -> your own Base wallet address (0x...) that will receive USDC
  NETWORK             -> "eip155:84532" for Base Sepolia TESTNET (safe to test with)
                         "eip155:8453"  for Base MAINNET (real money)
  CDP_API_KEY_ID       -> Coinbase Developer Platform Secret API Key ID (mainnet only)
  CDP_API_KEY_SECRET   -> Coinbase Developer Platform Secret API Key secret (mainnet only)

For testing, the public facilitator at https://x402.org/facilitator works
on Base Sepolia testnet only, no auth required. For MAINNET settlement, this
app switches to Coinbase's hosted CDP facilitator, which requires signed
requests. Each request to the CDP facilitator is authenticated with a short
lived JWT ("Bearer" token) built from your CDP Secret API Key, following the
same scheme Coinbase's own SDKs use (JWT signed with the Ed25519 key, header
carries the key id + a nonce, payload carries a "uris" claim binding the
token to the exact HTTP method+host+path being called, 120s expiry). This is
implemented locally below (no extra Coinbase SDK dependency needed) using
only PyJWT + cryptography, both of which are lightweight and already needed
by the underlying x402 EVM stack.
"""

import base64
import os
import random
import time

import httpx
import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI, Request
from x402 import x402ResourceServer
from x402.http import CreateHeadersAuthProvider, FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact.register import register_exact_evm_server

# ---------------------------------------------------------------------------
# Config — fill these in before going live
# ---------------------------------------------------------------------------
PAY_TO_ADDRESS = os.environ.get("PAY_TO_ADDRESS", "0xREPLACE_WITH_YOUR_BASE_WALLET_ADDRESS")
NETWORK = os.environ.get("NETWORK", "eip155:84532")  # default: Base Sepolia TESTNET
FACILITATOR_URL = os.environ.get("FACILITATOR_URL", "https://x402.org/facilitator")
CDP_API_KEY_ID = os.environ.get("CDP_API_KEY_ID")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET")
CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://macro-pulse-x402.onrender.com")
PRICE = "$0.02"
ASSET_ADDRESS = "0x036CbD53842c5426634e7929541eC2318f3dCF7e" if NETWORK == "eip155:84532" else "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

app = FastAPI(title="Macro Pulse")


def _generate_cdp_jwt(method: str, path: str, expires_in: int = 120) -> str:
    """Build a short-lived Bearer JWT for one CDP facilitator request.

    Mirrors Coinbase's own JWT construction (see cdp-sdk's
    cdp.auth.utils.jwt.generate_jwt) so we can authenticate without pulling
    in the full cdp-sdk package (which drags in web3/solana deps we don't
    need for a read-only facilitator client).
    """
    secret_bytes = base64.b64decode(CDP_API_KEY_SECRET)
    if len(secret_bytes) != 64:
        raise ValueError(
            "CDP_API_KEY_SECRET must be the base64 Ed25519 secret from the CDP "
            "'Create secret API key' modal (64 raw bytes once decoded)."
        )
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(secret_bytes[:32])

    nonce = "".join(random.choices("0123456789", k=16))
    header = {"alg": "EdDSA", "kid": CDP_API_KEY_ID, "typ": "JWT", "nonce": nonce}

    now = int(time.time())
    uri = f"{method} api.cdp.coinbase.com{path}"
    claims = {
        "sub": CDP_API_KEY_ID,
        "iss": "cdp",
        "aud": None,
        "nbf": now,
        "exp": now + expires_in,
        "uris": [uri],
    }
    return pyjwt.encode(claims, private_key, algorithm="EdDSA", headers=header)


def _cdp_create_headers() -> dict[str, dict[str, str]]:
    """Per-endpoint auth headers for the CDP facilitator (verify/settle/supported)."""
    base_path = "/platform/v2/x402"
    return {
        "verify": {"Authorization": f"Bearer {_generate_cdp_jwt('POST', f'{base_path}/verify')}"},
        "settle": {"Authorization": f"Bearer {_generate_cdp_jwt('POST', f'{base_path}/settle')}"},
        "supported": {"Authorization": f"Bearer {_generate_cdp_jwt('GET', f'{base_path}/supported')}"},
    }


if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
    # Mainnet (and CDP-backed testnet): authenticated Coinbase facilitator.
    facilitator_config = FacilitatorConfig(
        url=CDP_FACILITATOR_URL,
        auth_provider=CreateHeadersAuthProvider(_cdp_create_headers),
    )
else:
    # No CDP credentials set — fall back to the open, unauthenticated
    # testnet-only facilitator (works only on Base Sepolia / Solana Devnet).
    facilitator_config = FacilitatorConfig(url=FACILITATOR_URL)

facilitator = HTTPFacilitatorClient(facilitator_config)
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
