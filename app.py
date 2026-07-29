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

import asyncio
import base64
import os
import random
import re
import time

import httpx
import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from x402 import x402ResourceServer
from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
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
BATCH_PRICE = "$0.05"  # flat price for up to MAX_BATCH_COUNTRIES countries in one call
ASSET_ADDRESS = "0x036CbD53842c5426634e7929541eC2318f3dCF7e" if NETWORK == "eip155:84532" else "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

app = FastAPI(
    title="Macro Pulse",
    description=(
        "Pay-per-call macroeconomic indicator API for AI agents, sourced from the "
        "World Bank (GDP growth, inflation, unemployment trend). Single-country lookups "
        "at $0.02, and an 8-country flat-priced batch endpoint at $0.05. Settled in USDC "
        "on Base mainnet via the x402 protocol. No signup or API key required."
    ),
    version="1.1.0",
    contact={
        "name": "Benji Ferguson",
        "email": "benjiferguson@gmail.com",
        "url": "https://macro-pulse-x402.onrender.com",
    },
)


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

# --- Wire-level facilitator logging -------------------------------------------
# The x402 SDK itself already inspects the CDP facilitator's response for an
# EXTENSION-RESPONSES header (see x402/http/facilitator_client.py,
# _log_extension_responses_header) and, if present, logs allowlisted fields
# (status/rejectedReason/reason/code) via Python's stdlib `logging` at INFO
# level under the "x402" logger name. We were never actually configuring
# stdlib logging to go anywhere, so that diagnostic signal was being silently
# dropped. Wire it to stdout (Render captures stdout) so we can see directly
# whether CDP is sending that header at all, and if so, what it says about
# our bazaar extension specifically.
import logging as _stdlib_logging

_stdlib_logging.basicConfig(
    level=_stdlib_logging.INFO,
    format="X402_SDK_LOG %(name)s %(message)s",
    stream=__import__("sys").stdout,
    force=True,
)
_stdlib_logging.getLogger("x402").setLevel(_stdlib_logging.INFO)

# Additionally, wrap facilitator.verify/settle directly so we can print the
# exact PaymentPayload + PaymentRequirements our server forwards to CDP (the
# actual wire payload, not just what the SDK source claims it constructs),
# plus the full parsed VerifyResponse/SettleResponse we get back.
_orig_facilitator_verify = facilitator.verify
_orig_facilitator_settle = facilitator.settle


def _dump_model(obj):
    try:
        return obj.model_dump(by_alias=True, exclude_none=True)
    except Exception:
        return repr(obj)


async def _debug_facilitator_verify(payload, requirements):
    print("X402_WIRE verify.request.payload =", _dump_model(payload), flush=True)
    print("X402_WIRE verify.request.requirements =", _dump_model(requirements), flush=True)
    result = await _orig_facilitator_verify(payload, requirements)
    print("X402_WIRE verify.response =", _dump_model(result), flush=True)
    return result


async def _debug_facilitator_settle(payload, requirements):
    print("X402_WIRE settle.request.payload =", _dump_model(payload), flush=True)
    print("X402_WIRE settle.request.requirements =", _dump_model(requirements), flush=True)
    result = await _orig_facilitator_settle(payload, requirements)
    print("X402_WIRE settle.response =", _dump_model(result), flush=True)
    return result


facilitator.verify = _debug_facilitator_verify
facilitator.settle = _debug_facilitator_settle
print("X402_WIRE debug wrappers installed on facilitator client", flush=True)
# --- End wire-level facilitator logging ---------------------------------------

# --- Temporary debug logging -------------------------------------------------
# The x402 middleware deliberately returns an empty body on failure (so payer
# clients don't see internal details), which makes it impossible to diagnose
# failures from the outside. Wrap the key server methods on our own server
# instance with plain print()s (guaranteed to reach Render's log stream,
# unlike the logging module which can get swallowed by uvicorn's own config)
# so we can see exactly which step rejects the payment and why.
import sys


def _dbg(*parts):
    print("X402_DEBUG", *parts, file=sys.stdout, flush=True)


_orig_find_matching_requirements = server.find_matching_requirements
_orig_validate_extensions = server.validate_extensions
_orig_verify_payment = server.verify_payment
_orig_settle_payment = server.settle_payment


def _debug_find_matching_requirements(available, payload):
    result = _orig_find_matching_requirements(available, payload)
    try:
        _dbg(
            "find_matching_requirements -> matched=", result is not None,
            "| available=", [(r.network, r.asset, r.pay_to, r.amount) for r in available],
            "| payload.accepted=", getattr(payload, "accepted", None),
        )
    except Exception as e:
        _dbg("find_matching_requirements logging failed:", repr(e))
    return result


def _debug_validate_extensions(payment_required, payment_payload):
    result = _orig_validate_extensions(payment_required, payment_payload)
    try:
        _dbg("validate_extensions -> valid=", result.valid, "invalid_reason=", result.invalid_reason)
    except Exception as e:
        _dbg("validate_extensions logging failed:", repr(e))
    return result


async def _debug_verify_payment(*args, **kwargs):
    try:
        result = await _orig_verify_payment(*args, **kwargs)
    except Exception as e:
        _dbg("verify_payment RAISED:", repr(e))
        raise
    try:
        _dbg(
            "verify_payment -> is_valid=", result.is_valid,
            "invalid_reason=", result.invalid_reason,
            "invalid_message=", result.invalid_message,
            "payer=", result.payer,
        )
    except Exception as e:
        _dbg("verify_payment logging failed:", repr(e))
    return result


async def _debug_settle_payment(*args, **kwargs):
    try:
        result = await _orig_settle_payment(*args, **kwargs)
    except Exception as e:
        _dbg("settle_payment RAISED:", repr(e))
        raise
    try:
        _dbg(
            "settle_payment -> success=", result.success,
            "error_reason=", result.error_reason,
            "error_message=", result.error_message,
            "transaction=", result.transaction,
            "payer=", result.payer,
        )
    except Exception as e:
        _dbg("settle_payment logging failed:", repr(e))
    return result


server.find_matching_requirements = _debug_find_matching_requirements
server.validate_extensions = _debug_validate_extensions
server.verify_payment = _debug_verify_payment
server.settle_payment = _debug_settle_payment
_dbg("debug wrappers installed at startup")
# --- End temporary debug logging ---------------------------------------------

routes = {
    "GET /macro-pulse/:country_code": {
        "accepts": {
            "scheme": "exact",
            "payTo": PAY_TO_ADDRESS,
            "price": PRICE,
            "network": NETWORK,
        },
        "description": "Computed economic momentum score for a country (GDP growth, inflation, unemployment trend, synthesized into a single directional signal).",
        # Bazaar discovery metadata: once this route completes its first real
        # settlement through the CDP facilitator, Coinbase's Bazaar catalog
        # (https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources)
        # will index it using this description/schema/example so agents can
        # find and evaluate it programmatically. The payment_middleware below
        # auto-registers the bazaar extension because this key is present.
        "extensions": declare_discovery_extension(
            path_params_schema={
                "properties": {
                    "country_code": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 or alpha-3 country code, e.g. US, GB, JP, DEU",
                    }
                },
                "required": ["country_code"],
            },
            output=OutputConfig(
                example={
                    "country": "US",
                    "indicators": {
                        "gdp_growth": {"latest_value": 2.8, "latest_year": "2024", "trend_momentum": 0.6},
                        "inflation": {"latest_value": 2.9, "latest_year": "2024", "trend_momentum": -1.1},
                        "unemployment": {"latest_value": 4.1, "latest_year": "2024", "trend_momentum": -0.2},
                    },
                    "momentum_score": 0.87,
                    "momentum_label": "improving",
                    "source": "World Bank Open Data (public domain, https://data.worldbank.org)",
                    "disclaimer": "Directional context only. Not financial advice, not a buy/sell signal.",
                }
            ),
        ),
    },
    "GET /macro-pulse-batch/:country_codes": {
        "accepts": {
            "scheme": "exact",
            "payTo": PAY_TO_ADDRESS,
            "price": BATCH_PRICE,
            "network": NETWORK,
        },
        "description": (
            "Same computed economic momentum score as /macro-pulse, but for up to "
            "8 countries in a single call (comma-separated ISO codes). Flat price "
            "regardless of country count -- cheaper than calling the single-country "
            "endpoint repeatedly."
        ),
        "extensions": declare_discovery_extension(
            path_params_schema={
                "properties": {
                    "country_codes": {
                        "type": "string",
                        "description": "Comma-separated ISO 3166-1 alpha-2 or alpha-3 country codes, e.g. US,GB,JP,DE (max 8)",
                    }
                },
                "required": ["country_codes"],
            },
            output=OutputConfig(
                example={
                    "countries": {
                        "US": {
                            "country": "US",
                            "momentum_score": 0.87,
                            "momentum_label": "improving",
                        },
                        "GB": {
                            "country": "GB",
                            "momentum_score": -0.32,
                            "momentum_label": "stable",
                        },
                    },
                    "count": 2,
                    "source": "World Bank Open Data (public domain, https://data.worldbank.org)",
                    "disclaimer": "Directional context only. Not financial advice, not a buy/sell signal.",
                }
            ),
        ),
    },
}

# Mirror every paid GET route as a paid HEAD route with identical accepts/
# description/extensions. Without this, HEAD requests to a paid path don't
# match any key in `routes`, so http_server.requires_payment() returns False
# and the request falls through to FastAPI's router -- which returns a bare
# 405 instead of the 402 challenge. That's a real gap: at least one other
# x402 Bazaar-listed service operator (GitHub issue x402-foundation/x402#2207)
# confirmed HEAD returns 402 on their service, implying some discovery/health
# probes do send HEAD instead of GET. Keep this in sync automatically rather
# than hand-duplicating each route entry, so future paid routes get it free.
for _route_key in list(routes.keys()):
    _method, _path = _route_key.split(" ", 1)
    if _method == "GET":
        routes[f"HEAD {_path}"] = routes[_route_key]


# Build the x402 middleware function ONCE at startup, not per-request.
# payment_middleware(routes, server) re-registers/validates the bazaar
# extension AND (with sync_facilitator_on_start, the default) makes a
# blocking network call to the facilitator's /supported endpoint on the
# FIRST protected request it sees. Calling payment_middleware(...) fresh
# inside the per-request handler (as the library's own docstring example
# literally shows, which this code previously copied verbatim) recreates
# that whole init-on-first-request state machine from scratch on every
# single request -- meaning EVERY request, not just the first, pays the
# cost of a synchronous facilitator round-trip and bazaar re-validation.
# Under any burst of concurrent requests (bots probing the endpoint, or
# just a couple of retries) this can starve the single free-tier worker
# and make the whole service briefly unresponsive (observed as Cloudflare
# 522 errors). Building it once fixes both the correctness risk and the
# performance/availability risk.
_x402_middleware_fn = payment_middleware(routes, server)


@app.middleware("http")
async def x402_middleware(request: Request, call_next):
    return await _x402_middleware_fn(request, call_next)


# World Bank indicator codes (free, public domain, no API key required)
INDICATORS = {
    "gdp_growth": "NY.GDP.MKTP.KD.ZG",
    "inflation": "FP.CPI.TOTL.ZG",
    "unemployment": "SL.UEM.TOTL.ZS",
}


# In-memory cache for World Bank indicator series. World Bank macro data
# (annual GDP/inflation/unemployment) changes at most a few times a year, so
# caching it aggressively costs nothing in freshness but buys a lot: (1) paid
# calls no longer depend on World Bank's live API being fast/up at that exact
# moment -- a customer who already paid shouldn't get a slow/failed response
# because an upstream free API hiccuped; (2) a burst of agent traffic (bots,
# retries, or the new batch endpoint below fanning out to many countries)
# hits this cache instead of re-hammering World Bank on every request, which
# is also what caused the worker-starvation issue behind the earlier 522s.
# Only successful lookups are cached -- a failed/empty fetch is never stored,
# so a transient World Bank outage self-heals on the next call instead of
# being cached as "no data" for hours.
_INDICATOR_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
INDICATOR_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours


async def fetch_indicator(country_code: str, indicator: str) -> list[dict]:
    cache_key = (country_code, indicator)
    cached = _INDICATOR_CACHE.get(cache_key)
    if cached is not None and (time.time() - cached[0]) < INDICATOR_CACHE_TTL_SECONDS:
        return cached[1]

    url = f"https://api.worldbank.org/v2/country/{country_code}/indicator/{indicator}"
    # NOTE: this must be "mrv" (most recent values), NOT "mrnev". The World
    # Bank API rejects "mrnev" with an HTTP 400 for every country/indicator,
    # which our error handling below silently swallowed into an empty []
    # result -- meaning every call ever returned null indicators regardless
    # of country. It also cost a full 10s timeout per indicator (3 indicators
    # x up to 10s each = up to ~30s per request), which is what made the
    # paid endpoint slow enough for normal agent client timeouts to fire
    # before the response (and settlement) completed. Found via a paid test
    # call from a PayAPI Market reviewer on 2026-07-29 who reported both
    # symptoms (null data + charged-but-no-response) for a single call.
    params = {"format": "json", "per_page": 6, "mrv": 6}
    # World Bank's public API is occasionally flaky on an individual call --
    # observed directly: the exact same request succeeds in well under a
    # second on one attempt, then times out entirely on the very next one,
    # for otherwise well-covered countries. A single retry wasn't always
    # enough (still saw full-timeout failures for e.g. Germany), so this
    # allows up to 3 attempts at a shorter 6s timeout each (worst case ~18s
    # for one indicator, and since all 3 indicators already run concurrently
    # via asyncio.gather in _compute_macro_pulse, this is also the worst
    # case for the whole response -- not 3x that).
    data = None
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=6) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            break
        except (httpx.HTTPError, ValueError):
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.3)
                continue
    if data is None:
        # World Bank API is down/slow/returned bad JSON on every attempt —
        # degrade gracefully instead of bubbling up an unhandled exception
        # (which would return a generic 500 to a client who already paid
        # for this call). Do NOT cache this outcome, so the next request
        # retries against the live API instead of being stuck with "no
        # data" for the full TTL.
        return []
    if not isinstance(data, list) or len(data) < 2 or not data[1]:
        return []
    series = [row for row in data[1] if row.get("value") is not None]
    if series:
        _INDICATOR_CACHE[cache_key] = (time.time(), series)
    return series


def compute_momentum(series: list[dict]) -> float | None:
    """Simple trend: latest value minus average of prior values, in std-dev units."""
    values = [row["value"] for row in series]
    if len(values) < 2:
        return None
    latest = values[0]
    prior = values[1:]
    avg_prior = sum(prior) / len(prior)
    return round(latest - avg_prior, 2)


COUNTRY_CODE_RE = re.compile(r"^[A-Za-z]{2,3}$")


async def _compute_macro_pulse(country_code: str) -> dict:
    """Shared computation used by both the single-country and batch endpoints."""
    # Fetch all three indicators concurrently rather than one-at-a-time.
    # These are three independent upstream calls, so there's no reason to
    # pay their latency sequentially (this used to compound with the
    # mrnev/mrv bug above to produce ~30s response times on a paid route).
    names = list(INDICATORS.keys())
    series_list = await asyncio.gather(
        *(fetch_indicator(country_code, INDICATORS[name]) for name in names)
    )
    results = {}
    for name, series in zip(names, series_list):
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


@app.api_route("/macro-pulse/{country_code}", methods=["GET", "HEAD"])
async def macro_pulse(country_code: str):
    if not COUNTRY_CODE_RE.match(country_code):
        raise HTTPException(
            status_code=400,
            detail="country_code must be a 2 or 3 letter ISO 3166-1 code, e.g. US, GB, DEU.",
        )
    return await _compute_macro_pulse(country_code.upper())


# Explicit OPTIONS handlers for both paid routes. `routes` (above) has no
# "OPTIONS ..." key, so http_server.requires_payment() is False for OPTIONS
# and the x402 middleware passes it straight through here -- OPTIONS never
# requires payment, matching normal HTTP/CORS semantics. Without this, any
# cross-origin JS client (e.g. a browser-based agent, not just same-origin
# pages like our own /pay) sending the custom X-PAYMENT header on the paid
# retry would have its CORS preflight OPTIONS request 405, silently breaking
# the whole payment flow before the real GET ever went out.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "X-PAYMENT, Payment-Signature, Content-Type, Accept",
    "Access-Control-Max-Age": "86400",
}


@app.options("/macro-pulse/{country_code}")
async def macro_pulse_options(country_code: str) -> Response:
    return Response(status_code=204, headers=_CORS_HEADERS)


MAX_BATCH_COUNTRIES = 8


@app.api_route("/macro-pulse-batch/{country_codes}", methods=["GET", "HEAD"])
async def macro_pulse_batch(country_codes: str):
    """Higher-value bundled endpoint: many countries in one paid call.

    Priced flat (see BATCH_PRICE / routes below) regardless of how many
    countries are requested (up to MAX_BATCH_COUNTRIES). Because indicator
    lookups are cached (see fetch_indicator), the marginal backend cost of
    each extra country in a batch is near zero, so this bundle earns
    materially more per call than the single-country endpoint while still
    undercutting what the same countries would cost as separate calls
    (up to MAX_BATCH_COUNTRIES x $0.02), giving agents a real incentive to
    use it instead of looping the single-country endpoint.
    """
    codes = [c.strip().upper() for c in country_codes.split(",") if c.strip()]
    if not codes:
        raise HTTPException(status_code=400, detail="Provide at least one country code.")
    if len(codes) > MAX_BATCH_COUNTRIES:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_BATCH_COUNTRIES} countries per batch call. Got {len(codes)}.",
        )
    for c in codes:
        if not COUNTRY_CODE_RE.match(c):
            raise HTTPException(
                status_code=400,
                detail=f"'{c}' is not a valid 2 or 3 letter ISO 3166-1 code.",
            )
    # Dedupe while preserving order so a lazy/buggy caller repeating a code
    # doesn't get charged for redundant work either.
    seen = set()
    unique_codes = [c for c in codes if not (c in seen or seen.add(c))]
    countries = {c: await _compute_macro_pulse(c) for c in unique_codes}
    return {
        "countries": countries,
        "count": len(countries),
        "source": "World Bank Open Data (public domain, https://data.worldbank.org)",
        "disclaimer": "Directional context only. Not financial advice, not a buy/sell signal.",
    }


@app.options("/macro-pulse-batch/{country_codes}")
async def macro_pulse_batch_options(country_codes: str) -> Response:
    return Response(status_code=204, headers=_CORS_HEADERS)


@app.get("/pay", response_class=HTMLResponse)
async def pay_page():
    """Self-serve bootstrap-payment page.

    Lets the wallet-owning human pay this API's own endpoint once, straight
    from a browser-injected wallet (Base app / MetaMask), with no private
    key ever touching this server or any script. Served from the same
    origin as the API so the in-page fetch() calls are same-origin and need
    no CORS configuration at all.
    """
    html_path = os.path.join(os.path.dirname(__file__), "pay_page.html")
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    return html.replace("__PAY_TO__", PAY_TO_ADDRESS).replace("__ASSET__", ASSET_ADDRESS)


@app.get("/")
async def root():
    return {
        "service": "Macro Pulse",
        "endpoints": {
            "/macro-pulse/{country_code}": {"price": PRICE, "example": "/macro-pulse/US"},
            "/macro-pulse-batch/{country_codes}": {
                "price": BATCH_PRICE,
                "example": "/macro-pulse-batch/US,GB,JP",
                "max_countries": MAX_BATCH_COUNTRIES,
            },
        },
        "network": NETWORK,
        "verification": {
            "gold402_verified": True,
            "listing": "https://24klabs.ai/listing/macro-pulse",
            "badge_svg": "https://24klabs.ai/badge.svg?resource=https%3A%2F%2Fmacro-pulse-x402.onrender.com",
            "card_svg": "https://24klabs.ai/card.svg?resource=https%3A%2F%2Fmacro-pulse-x402.onrender.com",
        },
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
            },
            {
                "resource": f"{PUBLIC_BASE_URL}/macro-pulse-batch/{{country_codes}}",
                "method": "GET",
                "description": (
                    "Same momentum score as /macro-pulse, for up to 8 countries in one "
                    "call (comma-separated ISO codes). Flat price, cheaper than repeated "
                    "single-country calls."
                ),
                "price": BATCH_PRICE,
                "network": NETWORK,
                "asset": ASSET_ADDRESS,
                "payTo": PAY_TO_ADDRESS,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "country_codes": {
                            "type": "string",
                            "description": "Comma-separated ISO 3166-1 alpha-2 country codes, e.g. US,GB,JP,DE (max 8)",
                        }
                    },
                    "required": ["country_codes"],
                },
            },
        ],
    }


# Plain-text summary for LLM/agent frameworks that check /llms.txt before
# deciding whether (and how) to call a service.
_LLMS_TXT = f"""# Macro Pulse

> Pay-per-call macroeconomic indicator API for AI agents. Computes a per-country
> economic momentum score from World Bank Open Data (GDP growth, inflation,
> unemployment trend), synthesized into one directional signal. Settled in
> USDC on Base mainnet via the x402 protocol. No signup, no API key.

## Endpoints

- GET {PUBLIC_BASE_URL}/macro-pulse/{{country_code}} — {PRICE} per call.
  ISO 3166-1 alpha-2 or alpha-3 country code (e.g. US, GB, JP, DEU).
  Example: {PUBLIC_BASE_URL}/macro-pulse/US
- GET {PUBLIC_BASE_URL}/macro-pulse-batch/{{country_codes}} — {BATCH_PRICE} flat,
  up to {MAX_BATCH_COUNTRIES} comma-separated country codes in one call.
  Example: {PUBLIC_BASE_URL}/macro-pulse-batch/US,GB,JP

## Payment

Both endpoints return HTTP 402 with an x402 v2 payment-required payload
(scheme: exact, network: {NETWORK}, asset: USDC) until paid. No account or
API key is ever required — pay per call and get the response in the same
round trip.

## Discovery manifest

{PUBLIC_BASE_URL}/.well-known/x402

## Disclaimer

Output is directional context only (momentum_label: improving / stable /
deteriorating), not financial advice or a trading signal.
"""


@app.get("/llms.txt", response_class=HTMLResponse)
async def llms_txt():
    return HTMLResponse(content=_LLMS_TXT, media_type="text/plain")


# AI-plugin style manifest — some agent frameworks (ChatGPT plugins-era
# conventions, various MCP bridges) still probe this well-known path when
# deciding whether a domain exposes a machine-usable API.
@app.get("/.well-known/ai-plugin.json")
async def ai_plugin_manifest():
    return {
        "schema_version": "v1",
        "name_for_human": "Macro Pulse",
        "name_for_model": "macro_pulse",
        "description_for_human": (
            "Pay-per-call macroeconomic indicator API: per-country economic "
            "momentum score from World Bank data."
        ),
        "description_for_model": (
            "Use to get a quick macroeconomic momentum signal for a country "
            "(GDP growth, inflation, unemployment trend synthesized into one "
            "directional label) before financial analysis, market commentary, "
            "or country-risk screening. Call GET /macro-pulse/{country_code} "
            "for a single country, or GET /macro-pulse-batch/{country_codes} "
            "for up to 8 at once. Both require x402 USDC payment on Base "
            "mainnet — no API key."
        ),
        "auth": {"type": "none"},
        "api": {"type": "openapi", "url": f"{PUBLIC_BASE_URL}/openapi.json"},
        "payments": {"protocol": "x402", "manifest": f"{PUBLIC_BASE_URL}/.well-known/x402"},
        "contact_email": "benjiferguson@gmail.com",
        "legal_info_url": PUBLIC_BASE_URL,
    }
