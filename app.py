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
import contextlib
from collections import OrderedDict
import json
import os
import random
import re
import threading
import time
from typing import Any, TypeVar
import urllib.parse

import httpx
import jwt as pyjwt


# ---------------------------------------------------------------------------
# Bounded LRU cache with TTL eviction (memory-safe replacement for plain dicts)
# ---------------------------------------------------------------------------
# Render's free/starter tier has ~512MB RAM. Unbounded caches can exhaust
# this under sustained traffic (Sep 9, Sep 20, Sep 27, Oct 6 2026 OOMs).
# This class bounds the cache to a fixed number of entries (LRU eviction)
# and removes stale entries on every access (TTL eviction).
# ---------------------------------------------------------------------------
_KT = TypeVar("_KT")
_VT = TypeVar("_VT")


class BoundedTTLCache:
    """Thread-safe LRU cache with per-entry TTL and bounded size.

    - max_size: maximum number of entries; oldest is evicted when exceeded.
    - ttl_seconds: entries older than this are considered stale and evicted.
    - Stale entries are removed lazily (on get/set) to avoid background threads.
    """

    def __init__(self, max_size: int, ttl_seconds: float):
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: _KT) -> tuple[bool, _VT | None]:
        """Return (found, value). If not found or stale, returns (False, None)."""
        now = time.time()
        with self._lock:
            self._evict_stale(now)
            entry = self._data.get(key)
            if entry is None:
                return False, None
            ts, val = entry
            if (now - ts) >= self._ttl:
                del self._data[key]
                return False, None
            self._data.move_to_end(key)
            return True, val

    def set(self, key: _KT, value: _VT) -> None:
        """Insert or update an entry, evicting oldest if over capacity."""
        now = time.time()
        with self._lock:
            self._evict_stale(now)
            if key in self._data:
                del self._data[key]
            self._data[key] = (now, value)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def invalidate(self, key: _KT) -> None:
        """Remove a single entry (if present)."""
        with self._lock:
            self._data.pop(key, None)

    def _evict_stale(self, now: float) -> None:
        """Remove all entries older than TTL (called under lock)."""
        cutoff = now - self._ttl
        keys_to_delete = [k for k, (ts, _) in self._data.items() if ts < cutoff]
        for k in keys_to_delete:
            del self._data[k]

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from x402 import x402ResourceServer
from x402.extensions.bazaar import (
    DeclareMcpDiscoveryConfig,
    OutputConfig,
    bazaar_resource_server_extension,
    declare_discovery_extension as _declare_discovery_extension,
    declare_mcp_discovery_extension,
)
import pycountry


def declare_discovery_extension_with_method(
    method: str,
    path_params_schema: dict | None = None,
    output: OutputConfig | None = None,
    **kwargs
) -> dict:
    """Wrapper around declare_discovery_extension that injects method into info.input.

    The x402 SDK's declare_discovery_extension creates a schema requiring 'method' in
    info.input, but doesn't populate it - that's supposed to happen at runtime via
    bazaar_resource_server_extension. However, validation happens before that injection,
    causing "input: 'method' is a required property" warnings. This wrapper manually
    adds the method field to avoid validation failures and ensure proper Bazaar indexing.
    """
    ext = _declare_discovery_extension(
        path_params_schema=path_params_schema,
        output=output,
        **kwargs
    )
    ext["bazaar"]["info"]["input"]["method"] = method
    return ext
from x402.http import CreateHeadersAuthProvider, FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact.register import register_exact_evm_server
from x402.mcp import create_payment_wrapper
from x402.schemas import ResourceConfig
# x402.mcp.types.ResourceInfo is a plain class without model_dump(); the SDK's
# own create_payment_wrapper() calls resource.model_dump(by_alias=True, ...)
# unconditionally when building payment-required/settlement-failed results,
# which crashes with AttributeError on every unpaid call (i.e. every real
# call, since discovery always starts unpaid) -- confirmed still broken in
# x402 2.16.0 through 2.19.0 (latest). x402.schemas.payments.ResourceInfo is
# a pydantic model with the identical field set (url, description, mime_type,
# service_name, tags, icon_url) and correct by_alias camelCase output, so we
# use that one as a drop-in fix instead of the one create_payment_wrapper's
# own docstring points to.
from x402.schemas.payments import ResourceInfo

import stripe_tier

# ---------------------------------------------------------------------------
# ISO 3166-1 country code validation
# ---------------------------------------------------------------------------
# Valid ISO 3166-1 alpha-2 and alpha-3 country codes (real countries only)
_ISO_ALPHA2_CODES = frozenset(c.alpha_2 for c in pycountry.countries)
_ISO_ALPHA3_CODES = frozenset(c.alpha_3 for c in pycountry.countries)
_VALID_ISO_CODES = _ISO_ALPHA2_CODES | _ISO_ALPHA3_CODES

# World Bank aggregate/region codes that look like country codes but are NOT
# individual countries. These return data from World Bank API but charging for
# them is misleading since they're just aggregates of real countries' data.
_WORLD_BANK_AGGREGATES = frozenset({
    "AFE", "AFW", "ARB", "CEB", "CSS", "EAP", "EAR", "EAS", "ECA", "ECS",
    "EMU", "EUU", "FCS", "HIC", "HPC", "IBD", "IBT", "IDA", "IDB", "IDX",
    "INX", "LAC", "LCN", "LDC", "LIC", "LMC", "LMY", "LTE", "MEA", "MIC",
    "MNA", "NAC", "OED", "OSS", "PRE", "PSS", "PST", "SAS", "SSA", "SSF",
    "SST", "TEA", "TEC", "TLA", "TMN", "TSA", "TSS", "UMC", "WLD",
})


def validate_country_code(code: str) -> tuple[bool, str | None]:
    """Validate a country code against ISO 3166-1 and reject World Bank aggregates.

    Returns (is_valid, error_message). If valid, error_message is None.
    """
    code_upper = code.upper()
    if code_upper in _WORLD_BANK_AGGREGATES:
        return False, (
            f"'{code}' is a World Bank aggregate/region code, not an individual country. "
            "This endpoint only accepts ISO 3166-1 country codes for individual nations "
            "(e.g., US, GB, JP, DEU)."
        )
    if code_upper not in _VALID_ISO_CODES:
        return False, (
            f"'{code}' is not a valid ISO 3166-1 country code. "
            "Use a 2-letter (alpha-2) or 3-letter (alpha-3) code, e.g., US, GB, JP, DEU."
        )
    return True, None


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

# --- Stripe card-payment tier (human developers, no crypto wallet needed) ---
# Independent of the x402/CDP config above -- see stripe_tier.py for the
# full flow. Configuring these is optional: if STRIPE_SECRET_KEY is unset,
# /subscribe cleanly 503s and X-API-Key checks always fail closed, so the
# x402 paywall behavior for everyone else is completely unaffected.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")
STRIPE_PRICE_LABEL = os.environ.get("STRIPE_PRICE_LABEL", "$9/month")
stripe_tier.configure(STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_ID, STRIPE_PRICE_LABEL)

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
# Without this, discovery declarations never get an HTTP `method` injected
# into their info/schema (that injection is this extension's whole job --
# see x402/extensions/bazaar/server.py:BazaarResourceServerExtension). The
# resulting declarations fail the Bazaar facilitator's own schema validation
# ("input: 'method' is a required property", visible in our startup logs),
# which is the most likely reason Macro Pulse dropped out of the Bazaar
# discovery catalog despite `declare_discovery_extension(...)` being present
# on every route below.
server.register_extension(bazaar_resource_server_extension)

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
        "extensions": declare_discovery_extension_with_method(
            method="GET",
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
        "extensions": declare_discovery_extension_with_method(
            method="GET",
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

# Paths that carry a payment-gated path parameter -- used by the guard below
# to scope its check to the routes that can actually receive a raw template.
_PAID_TEMPLATE_PREFIXES = ("/macro-pulse/", "/macro-pulse-batch/")
# Real country codes (single or comma-separated in the batch case) are always
# plain letters -- any of these punctuation characters in the final path
# segment can only come from an unsubstituted URL template, never a genuine
# request.
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"[:{}\[\]]")


@app.middleware("http")
async def x402_middleware(request: Request, call_next):
    # Defensive guard against literal, unsubstituted route templates.
    #
    # Root cause (confirmed by reading the installed x402 SDK source, not
    # guessed): the Bazaar discovery extension (x402/extensions/bazaar/
    # resource_service.py + server.py) requires colon-style ":param" or
    # bracket-style "[param]" route keys -- it has no support for FastAPI's
    # "{param}" syntax at all, and neither does payment_middleware's own
    # path matcher (x402/http/x402_http_server_base.py:_parse_route_pattern).
    # So the colon-style keys in `routes` above are correct and required,
    # not a bug to "fix" by switching to curly braces.
    #
    # The actual leak: by SDK/spec design, the public Bazaar catalog
    # (https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources)
    # publishes each parameterized route's "resource" field as a
    # fully-qualified URL with that raw template baked in verbatim, e.g.
    # "https://macro-pulse-x402.onrender.com/macro-pulse/:country_code"
    # (verified directly against the live feed). Well-behaved agents are
    # supposed to read the accompanying pathParams JSON Schema and
    # substitute a real value; naive crawlers/directories just GET that
    # literal resource string instead. This isn't something our app can
    # suppress from Coinbase's catalog -- it's how the SDK reports every
    # path-parameterized listing.
    #
    # Without this guard, those literal-template requests reach
    # payment_middleware, whose matcher treats any non-slash path segment
    # as a valid parameter value, so they get a generic 402 -- indistinguishable
    # in logs/metrics from someone who actually intended to pay. Short-circuit
    # them here with a fast, clean 400 before the paywall (and the facilitator
    # round-trip it can trigger) ever runs.
    path = request.url.path
    if path.startswith(_PAID_TEMPLATE_PREFIXES):
        last_segment = path.rsplit("/", 1)[-1]
        if _TEMPLATE_PLACEHOLDER_RE.search(last_segment):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_path_parameter",
                    "message": (
                        "This looks like a raw, unsubstituted URL template rather "
                        "than a real request. Replace the placeholder with an "
                        "actual value, e.g. /macro-pulse/US or "
                        "/macro-pulse-batch/US,GB,JP. See /.well-known/x402 or "
                        "/openapi.json for the full spec."
                    ),
                },
            )

        # Validate country codes BEFORE payment challenge -- reject fake/invalid
        # codes and World Bank aggregates with 400, not 402, so callers don't
        # get charged for requests that would inevitably fail after payment.
        if path.startswith("/macro-pulse-batch/"):
            # Batch endpoint: validate all comma-separated codes
            codes = [c.strip().upper() for c in last_segment.split(",") if c.strip()]
            if len(codes) > MAX_BATCH_COUNTRIES:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "too_many_countries",
                        "message": f"Maximum {MAX_BATCH_COUNTRIES} countries per batch call. Got {len(codes)}.",
                    },
                )
            for code in codes:
                is_valid, error_msg = validate_country_code(code)
                if not is_valid:
                    return JSONResponse(
                        status_code=400,
                        content={"error": "invalid_country_code", "message": error_msg},
                    )
        else:
            # Single-country endpoint: validate the one code
            is_valid, error_msg = validate_country_code(last_segment)
            if not is_valid:
                return JSONResponse(
                    status_code=400,
                    content={"error": "invalid_country_code", "message": error_msg},
                )

        # Stripe card-payment tier: an active-subscription API key skips the
        # x402 challenge entirely and goes straight to the real handler,
        # same as if payment had already been verified. Checked via Stripe
        # (with an in-process cache -- see stripe_tier.is_active_api_key),
        # never a local secret store, and fails closed if Stripe isn't
        # configured or the lookup errors, so this can never accidentally
        # let an unpaid request through.
        api_key = request.headers.get("x-api-key")
        if api_key and await stripe_tier.is_active_api_key(api_key):
            return await call_next(request)
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
#
# MEMORY FIX (Oct 2026): Switched from unbounded dict to BoundedTTLCache.
# With 195 countries x 3 indicators = 585 possible entries, 600 is generous.
# Render's 512MB free tier OOM'd (Sep 9/20/27, Oct 6) because the old dict
# never evicted stale entries -- they just accumulated. Now entries are
# evicted both by TTL (6h) and by LRU when the cap is hit.
INDICATOR_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours
INDICATOR_CACHE_MAX_SIZE = 600  # ~195 countries x 3 indicators, with margin
_INDICATOR_CACHE: BoundedTTLCache = BoundedTTLCache(
    max_size=INDICATOR_CACHE_MAX_SIZE,
    ttl_seconds=INDICATOR_CACHE_TTL_SECONDS,
)

# MEMORY FIX (Oct 2026): Shared httpx.AsyncClient for World Bank API calls.
# Creating a new client per request in fetch_indicator() caused memory churn
# (connection pools, SSL contexts created/destroyed rapidly). A single shared
# client reuses connections and is closed cleanly on shutdown.
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Return the shared HTTP client (created lazily, closed on app shutdown)."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=6)
    return _http_client


async def _close_http_client() -> None:
    """Close the shared HTTP client (called on app shutdown)."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


async def fetch_indicator(country_code: str, indicator: str) -> list[dict]:
    cache_key = (country_code, indicator)
    found, cached_series = _INDICATOR_CACHE.get(cache_key)
    if found:
        return cached_series

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
    client = _get_http_client()
    for attempt in range(max_attempts):
        try:
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
        _INDICATOR_CACHE.set(cache_key, series)
    return series


def compute_momentum(series: list[dict]) -> float | None:
    """Simple trend: latest value minus average of prior values."""
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
        latest_val = series[0]["value"] if series else None
        results[name] = {
            "latest_value": round(latest_val, 2) if latest_val is not None else None,
            "latest_year": series[0]["date"] if series else None,
            "trend_momentum": compute_momentum(series),
            "history": [
                {"year": r["date"], "value": round(r["value"], 2) if r["value"] is not None else None}
                for r in series
            ],
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
    code = country_code.upper()
    is_valid, error_msg = validate_country_code(code)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)
    return await _compute_macro_pulse(code)


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
        is_valid, error_msg = validate_country_code(c)
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)
    # Dedupe while preserving order so a lazy/buggy caller repeating a code
    # doesn't get charged for redundant work either.
    seen = set()
    unique_codes = [c for c in codes if not (c in seen or seen.add(c))]
    # Fetch all countries concurrently, not sequentially -- indicators
    # within a single country already run concurrently (see
    # _compute_macro_pulse), but without this, a batch of up to
    # MAX_BATCH_COUNTRIES countries would still stack each country's worst
    # case behind the last, reintroducing the same charged-but-timed-out
    # risk this whole fix was about, just on the (paid, $0.05) batch
    # endpoint instead of the single-country one.
    results = await asyncio.gather(*(_compute_macro_pulse(c) for c in unique_codes))
    countries = dict(zip(unique_codes, results))
    return {
        "countries": countries,
        "count": len(countries),
        "source": "World Bank Open Data (public domain, https://data.worldbank.org)",
        "disclaimer": "Directional context only. Not financial advice, not a buy/sell signal.",
    }


@app.options("/macro-pulse-batch/{country_codes}")
async def macro_pulse_batch_options(country_codes: str) -> Response:
    return Response(status_code=204, headers=_CORS_HEADERS)


# ---------------------------------------------------------------------------
# MCP surface -- same two paid operations, exposed as MCP tools so agent
# frameworks that speak MCP (Claude Desktop, Cursor, any streamable-HTTP MCP
# client) can call Macro Pulse directly instead of hand-rolling HTTP + x402.
# Reuses the SAME x402ResourceServer instance as the REST routes above (one
# facilitator connection, one bazaar registration, one source of truth for
# pay-to/network/pricing) -- this is additive, not a parallel payment stack.
#
# Uses x402.mcp.create_payment_wrapper -- the SDK's real, documented
# server-side FastMCP bridge (x402/mcp/server.py). This is a different
# function from x402.mcp.server_async.wrap_fastmcp_tool(), which IS broken
# in this SDK version (imports two helper functions that don't exist). This
# one is self-contained: it decorates a plain async handler (business args
# only, no ctx param needed -- the wrapper injects a synthetic `ctx: Context`
# parameter itself so FastMCP's schema builder excludes it automatically),
# extracts the x402 payment payload from the MCP request's `_meta`, verifies
# it, runs the handler, settles on success, and returns a fully-formed
# mcp.types.CallToolResult either way -- no manual result conversion needed.
#
# server.initialize() is normally lazy (first REST request pays the one-time
# facilitator round-trip -- see the comment above payment_middleware(...)).
# Building payment requirements for MCP tools happens at module load time,
# before any request exists to trigger that lazy path, so it's forced here
# instead. This is a pure no-op for the REST path: by the time a real
# request arrives, server._initialized is already True, so its own lazy-init
# check just skips re-initializing.
server.initialize()

# streamable_http_path="/" avoids double-nesting: FastMCP's own ASGI app
# already serves its endpoint at "/mcp" by default, and mounting that whole
# app at app.mount("/mcp", ...) below would otherwise put the real endpoint
# at /mcp/mcp instead of /mcp.
#
# transport_security: FastMCP auto-enables DNS-rebinding protection with
# allowed_hosts limited to loopback (127.0.0.1/localhost) whenever no `host`
# is passed to the constructor -- fine for local dev, but it 421s every real
# request once this is deployed behind Render's public hostname. Explicitly
# allow the public host this API actually serves from (same host already
# used for PUBLIC_BASE_URL/bazaar discovery below) instead of disabling the
# protection outright.
_mcp_public_host = urllib.parse.urlparse(PUBLIC_BASE_URL).hostname or "macro-pulse-x402.onrender.com"
mcp_server = FastMCP(
    "macro-pulse",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_mcp_public_host, f"{_mcp_public_host}:*"],
    ),
)


def _mcp_accepts(price: str) -> list:
    return server.build_payment_requirements(
        ResourceConfig(scheme="exact", pay_to=PAY_TO_ADDRESS, price=price, network=NETWORK)
    )


_macro_pulse_paid = create_payment_wrapper(
    server,
    accepts=_mcp_accepts(PRICE),
    resource=ResourceInfo(
        url="mcp://tool/macro_pulse",
        description=(
            "Computed economic momentum score for a country (GDP growth, "
            "inflation, unemployment trend synthesized into one directional "
            "signal: improving / stable / deteriorating)."
        ),
        service_name="Macro Pulse",
        tags=["macro", "economics", "worldbank", "finance"],
    ),
    extensions=declare_mcp_discovery_extension(
        DeclareMcpDiscoveryConfig(
            tool_name="macro_pulse",
            description="Computed economic momentum score for a single country.",
            transport="streamable-http",
            input_schema={
                "properties": {
                    "country_code": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 or alpha-3 country code, e.g. US, GB, DEU",
                    }
                },
                "required": ["country_code"],
            },
            example={"country_code": "US"},
        )
    ),
)

_macro_pulse_batch_paid = create_payment_wrapper(
    server,
    accepts=_mcp_accepts(BATCH_PRICE),
    resource=ResourceInfo(
        url="mcp://tool/macro_pulse_batch",
        description=(
            "Same momentum score as macro_pulse, for up to "
            f"{MAX_BATCH_COUNTRIES} countries in one call. Flat price."
        ),
        service_name="Macro Pulse",
        tags=["macro", "economics", "worldbank", "finance"],
    ),
    extensions=declare_mcp_discovery_extension(
        DeclareMcpDiscoveryConfig(
            tool_name="macro_pulse_batch",
            description="Momentum scores for up to 8 countries in one flat-priced call.",
            transport="streamable-http",
            input_schema={
                "properties": {
                    "country_codes": {
                        "type": "string",
                        "description": "Comma-separated ISO 3166-1 codes, e.g. US,GB,JP (max 8)",
                    }
                },
                "required": ["country_codes"],
            },
            example={"country_codes": "US,GB,JP"},
        )
    ),
)


@mcp_server.tool(
    name="macro_pulse",
    description=(
        "Computed economic momentum score for a country (GDP growth, inflation, "
        "unemployment trend synthesized into one directional signal: improving / "
        f"stable / deteriorating). Priced at {PRICE} per call via x402 (USDC on Base)."
    ),
)
@_macro_pulse_paid
async def mcp_macro_pulse(country_code: str):
    code = str(country_code).upper()
    is_valid, error_msg = validate_country_code(code)
    if not is_valid:
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=error_msg)],
        )
    return await _compute_macro_pulse(code)


@mcp_server.tool(
    name="macro_pulse_batch",
    description=(
        "Same momentum score as macro_pulse, for up to "
        f"{MAX_BATCH_COUNTRIES} comma-separated countries in one call. Flat "
        f"{BATCH_PRICE} price via x402 (USDC on Base), cheaper than calling "
        "macro_pulse repeatedly."
    ),
)
@_macro_pulse_batch_paid
async def mcp_macro_pulse_batch(country_codes: str):
    raw = str(country_codes)
    codes = [c.strip().upper() for c in raw.split(",") if c.strip()]
    if not codes:
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text="Provide at least one country code.")],
        )
    if len(codes) > MAX_BATCH_COUNTRIES:
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text=f"Maximum {MAX_BATCH_COUNTRIES} countries per batch call. Got {len(codes)}.",
                )
            ],
        )
    for c in codes:
        is_valid, error_msg = validate_country_code(c)
        if not is_valid:
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text=error_msg)],
            )
    seen = set()
    unique_codes = [c for c in codes if not (c in seen or seen.add(c))]
    results = await asyncio.gather(*(_compute_macro_pulse(c) for c in unique_codes))
    countries = dict(zip(unique_codes, results))
    return {
        "countries": countries,
        "count": len(countries),
        "source": "World Bank Open Data (public domain, https://data.worldbank.org)",
        "disclaimer": "Directional context only. Not financial advice, not a buy/sell signal.",
    }


# Mount FastMCP's own ASGI (streamable-HTTP) app at /mcp on the SAME FastAPI
# app that serves the REST routes -- one process, one Render service, no
# separate deploy or port.
app.mount("/mcp", mcp_server.streamable_http_app())

# Stripe card-payment tier routes: /subscribe, /subscribe/success,
# /subscribe/cancelled, /stripe/webhook. See stripe_tier.py.
app.include_router(stripe_tier.router)

# FastMCP's streamable-HTTP transport needs its session manager's task group
# started via `mcp_server.session_manager.run()` before it can handle a
# single request -- normally that happens inside the lifespan of the
# standalone Starlette app FastMCP builds for you. Mounting that app as a
# sub-app does NOT propagate its lifespan automatically, so without this the
# very first POST to /mcp fails with "Task group is not initialized." Wrap
# the existing app lifespan (a no-op by default -- app.py sets no lifespan=
# of its own) so the session manager starts/stops alongside the app itself.
_existing_lifespan = app.router.lifespan_context


@contextlib.asynccontextmanager
async def _lifespan_with_mcp(app_: FastAPI):
    async with mcp_server.session_manager.run():
        async with _existing_lifespan(app_) as state:
            try:
                yield state
            finally:
                # MEMORY FIX (Oct 2026): Close shared HTTP client on shutdown.
                await _close_http_client()


app.router.lifespan_context = _lifespan_with_mcp


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


def _root_json_payload():
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
        "card_payment_tier": {
            "description": (
                "Prefer a monthly card subscription instead of per-call USDC? "
                "Subscribe once, get an API key, pass it as the X-API-Key "
                "header on any endpoint above to skip the x402 paywall."
            ),
            "price": STRIPE_PRICE_LABEL,
            "subscribe_url": f"{PUBLIC_BASE_URL}/subscribe",
            "enabled": bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID),
        },
    }


# Human-readable landing page. Agents/directories/curl (no "text/html" in
# Accept) keep getting the JSON manifest below -- only browsers see this.
_LANDING_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Macro Pulse — economic momentum signal API</title>
<style>
  :root {
    --bg: #0b0d12; --panel: #12151c; --border: #23272f; --text: #e7e9ee;
    --muted: #8b93a1; --accent: #5ee6a8; --accent2: #6ea8fe; --mono: "SF Mono", Consolas, Menlo, monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    line-height: 1.6; padding: 0 1.25rem 4rem;
  }
  .wrap { max-width: 760px; margin: 0 auto; }
  header { padding: 3.5rem 0 1.5rem; }
  h1 { font-size: 1.9rem; font-weight: 700; letter-spacing: -0.02em; }
  .tag { color: var(--muted); font-size: 1.05rem; margin-top: 0.6rem; max-width: 620px; }
  .badges { display: flex; gap: 0.5rem; margin-top: 1rem; flex-wrap: wrap; }
  .badge { font-size: 0.78rem; padding: 0.25rem 0.6rem; border: 1px solid var(--border); border-radius: 999px; color: var(--muted); }
  section { margin-top: 2.25rem; }
  h2 { font-size: 1.05rem; font-weight: 600; margin-bottom: 0.75rem; color: var(--accent2); }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 1.1rem 1.25rem; }
  .cards { display: grid; grid-template-columns: 1fr 1fr; gap: 0.9rem; }
  @media (max-width: 620px) { .cards { grid-template-columns: 1fr; } }
  .price { font-size: 1.4rem; font-weight: 700; color: var(--accent); }
  .card p { color: var(--muted); font-size: 0.92rem; margin: 0.4rem 0 0.85rem; }
  pre {
    background: #05060a; border: 1px solid var(--border); border-radius: 8px; padding: 0.9rem 1rem;
    overflow-x: auto; font-family: var(--mono); font-size: 0.82rem; color: #cfe9dc; white-space: pre-wrap; word-break: break-all;
  }
  a.btn {
    display: inline-block; background: var(--accent); color: #05060a; font-weight: 600; font-size: 0.88rem;
    padding: 0.55rem 1rem; border-radius: 7px; text-decoration: none;
  }
  a.link { color: var(--accent2); text-decoration: none; }
  a.link:hover { text-decoration: underline; }
  .links { display: flex; flex-wrap: wrap; gap: 1.1rem; font-size: 0.9rem; }
  footer { margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid var(--border); color: var(--muted); font-size: 0.82rem; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Macro Pulse</h1>
    <p class="tag">A tiny API that turns World Bank data into one directional signal per country — GDP growth, inflation, and unemployment trend synthesized into <strong>improving / stable / deteriorating</strong>. Built for AI agents to pay per call, or humans to grab a key and use it directly.</p>
    <div class="badges">
      <span class="badge">x402 pay-per-call</span>
      <span class="badge">MCP-compatible</span>
      <span class="badge">Base (USDC)</span>
      <span class="badge">Gold402 verified</span>
    </div>
  </header>

  <section>
    <h2>Try it right now</h2>
    <pre>curl __PUBLIC_BASE_URL__/macro-pulse/US</pre>
    <p style="color:var(--muted); font-size:0.88rem; margin-top:0.5rem;">Returns a 402 with payment instructions — pay __PRICE__ in USDC on Base, or use a card subscription below, and retry.</p>
  </section>

  <section>
    <h2>Two ways to pay</h2>
    <div class="cards">
      <div class="card">
        <div class="price">__PRICE__ / call</div>
        <p>Pay per request in USDC on Base via the <a class="link" href="https://x402.org" target="_blank">x402</a> protocol. No account, no subscription — just sign and pay. Batch lookups (up to __MAX_BATCH_COUNTRIES__ countries) are a flat __BATCH_PRICE__.</p>
        <a class="link" href="/.well-known/x402">/.well-known/x402 manifest →</a>
      </div>
      <div class="card">
        <div class="price">__STRIPE_PRICE_LABEL__</div>
        <p>Prefer a card? Subscribe once, get an API key back immediately, and pass it as <code>X-API-Key</code> to skip the x402 paywall entirely.</p>
        <a class="btn" href="/subscribe">Subscribe →</a>
      </div>
    </div>
  </section>

  <section>
    <h2>For agents &amp; MCP clients</h2>
    <p style="color:var(--muted); font-size:0.9rem;">Macro Pulse is also exposed as an MCP server for agent frameworks that discover and pay for tools automatically.</p>
    <pre>__PUBLIC_BASE_URL__/mcp/</pre>
  </section>

  <section>
    <h2>Links</h2>
    <div class="links">
      <a class="link" href="/llms.txt">/llms.txt</a>
      <a class="link" href="/.well-known/x402">x402 manifest</a>
      <a class="link" href="https://github.com/benji7891/macro-pulse-x402" target="_blank">Source on GitHub</a>
      <a class="link" href="https://24klabs.ai/listing/macro-pulse" target="_blank">Gold402 listing</a>
    </div>
  </section>

  <footer>Macro Pulse runs on Base mainnet via the x402 protocol. Data derived from World Bank indicators, recomputed on each call.</footer>
</div>
</body>
</html>"""

_LANDING_PAGE_HTML = (
    _LANDING_PAGE_HTML
    .replace("__PUBLIC_BASE_URL__", PUBLIC_BASE_URL)
    .replace("__PRICE__", PRICE)
    .replace("__BATCH_PRICE__", BATCH_PRICE)
    .replace("__MAX_BATCH_COUNTRIES__", str(MAX_BATCH_COUNTRIES))
    .replace("__STRIPE_PRICE_LABEL__", STRIPE_PRICE_LABEL)
)


@app.get("/")
async def root(request: Request):
    accept = request.headers.get("accept", "")
    if "text/html" in accept.lower():
        return HTMLResponse(content=_LANDING_PAGE_HTML)
    return _root_json_payload()


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

Prefer a monthly card subscription instead of per-call USDC? Subscribe at
{PUBLIC_BASE_URL}/subscribe ({STRIPE_PRICE_LABEL}) and pass the API key you
receive as an `X-API-Key` header on either endpoint above to skip the x402
paywall entirely while your subscription is active.

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
