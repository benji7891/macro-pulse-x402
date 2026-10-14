"""
Stripe-paid API-key tier for Macro Pulse.

Adds a SECOND way to pay for the same data, alongside the existing x402
USDC-on-Base flow in app.py: a plain-old $/month Stripe subscription that
issues a long-lived API key. This targets human developers/quant users who
don't have (and don't want to set up) a crypto wallet -- the x402 path stays
exactly as-is for AI agents that do.

Flow:
  1. GET /subscribe                     -> redirects to a Stripe Checkout
     session for the monthly subscription.
  2. Stripe webhook: checkout.session.completed
     -> POST /stripe/webhook            -> generate a random API key, store
        it in the new Subscription's metadata (Stripe is the only store of
        record -- no local DB needed, survives Render redeploys for free).
  3. GET /subscribe/success?session_id=...
     -> looks up the Checkout Session, reads the API key back out of the
        subscription metadata, shows it to the user once.
  4. Any request to a paid route with a valid `X-API-Key` header (checked in
     app.py's x402_middleware, before the x402 challenge) skips the x402
     paywall entirely and is served for free (already paid via Stripe).
     Validity = "there exists an active Subscription whose metadata.api_key
     matches", checked via Stripe's Search API and cached in-process for a
     few minutes so we don't hit Stripe on every single API call.

Env vars required for this tier (independent of the x402 env vars):
  STRIPE_SECRET_KEY     -> sk_live_... / sk_test_...
  STRIPE_WEBHOOK_SECRET -> whsec_... (from the webhook endpoint's settings)
  STRIPE_PRICE_ID       -> price_... for the recurring monthly price

If STRIPE_SECRET_KEY is unset, every function here is a safe no-op: /subscribe
returns a friendly "not configured yet" message and API-key checks always
fail closed (so an absent/misconfigured Stripe integration can never
accidentally bypass payment).
"""

import asyncio
from collections import OrderedDict
import secrets
import threading
import time

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

STRIPE_SECRET_KEY = None
STRIPE_WEBHOOK_SECRET = None
STRIPE_PRICE_ID = None
PRICE_LABEL = "$9/month"


# ---------------------------------------------------------------------------
# Bounded LRU cache for API key validation (memory-safe)
# ---------------------------------------------------------------------------
# The original unbounded dict could grow without limit if probed with many
# random API keys. This bounded version uses LRU eviction and TTL. 200
# entries is plenty -- active Stripe subscribers are typically <100.
# ---------------------------------------------------------------------------
class _BoundedAPIKeyCache:
    """Thread-safe LRU cache with TTL, specialized for API key validation."""

    def __init__(self, max_size: int, ttl_seconds: float):
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, bool]] = OrderedDict()

    def get(self, key: str) -> tuple[bool, bool | None]:
        """Return (found, is_valid). If not found or stale, returns (False, None)."""
        now = time.time()
        with self._lock:
            self._evict_stale(now)
            entry = self._data.get(key)
            if entry is None:
                return False, None
            ts, is_valid = entry
            if (now - ts) >= self._ttl:
                del self._data[key]
                return False, None
            self._data.move_to_end(key)
            return True, is_valid

    def set(self, key: str, is_valid: bool) -> None:
        """Insert or update an entry, evicting oldest if over capacity."""
        now = time.time()
        with self._lock:
            self._evict_stale(now)
            if key in self._data:
                del self._data[key]
            self._data[key] = (now, is_valid)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def invalidate(self, key: str) -> None:
        """Remove a single entry (if present)."""
        with self._lock:
            self._data.pop(key, None)

    def _evict_stale(self, now: float) -> None:
        """Remove all entries older than TTL (called under lock)."""
        cutoff = now - self._ttl
        keys_to_delete = [k for k, (ts, _) in self._data.items() if ts < cutoff]
        for k in keys_to_delete:
            del self._data[k]


def configure(secret_key: str | None, webhook_secret: str | None, price_id: str | None, price_label: str = "$9/month"):
    """Called once from app.py at import time with the real env var values."""
    global STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_ID, PRICE_LABEL
    STRIPE_SECRET_KEY = secret_key
    STRIPE_WEBHOOK_SECRET = webhook_secret
    STRIPE_PRICE_ID = price_id
    PRICE_LABEL = price_label
    if secret_key:
        stripe.api_key = secret_key


router = APIRouter()

# MEMORY FIX (Oct 2026): Replaced unbounded dict with bounded LRU cache.
# The old dict could grow without limit if probed with random API keys,
# contributing to Render's 512MB OOMs. 200 entries @ 5min TTL is plenty.
API_KEY_CACHE_TTL_SECONDS = 5 * 60  # 5 minutes
API_KEY_CACHE_MAX_SIZE = 200
_API_KEY_CACHE = _BoundedAPIKeyCache(
    max_size=API_KEY_CACHE_MAX_SIZE,
    ttl_seconds=API_KEY_CACHE_TTL_SECONDS,
)


def _sync_check_api_key(api_key: str) -> bool:
    """Blocking Stripe Search API call -- always run via asyncio.to_thread."""
    try:
        result = stripe.Subscription.search(
            query=f"status:'active' AND metadata['api_key']:'{api_key}'"
        )
        return len(result.data) > 0
    except Exception:
        return False


async def is_active_api_key(api_key: str | None) -> bool:
    """Fail closed: no key, no Stripe config, or a Stripe error all -> False."""
    if not api_key or not STRIPE_SECRET_KEY:
        return False
    found, is_valid = _API_KEY_CACHE.get(api_key)
    if found:
        return is_valid
    valid = await asyncio.to_thread(_sync_check_api_key, api_key)
    _API_KEY_CACHE.set(api_key, valid)
    return valid


def _require_configured():
    if not (STRIPE_SECRET_KEY and STRIPE_PRICE_ID):
        raise HTTPException(
            status_code=503,
            detail="The card-payment tier isn't configured on this server yet. Use the x402 (USDC on Base) endpoints instead, or contact benjiferguson@gmail.com.",
        )


@router.get("/subscribe")
async def subscribe(request: Request):
    """Redirect a human developer straight into a Stripe Checkout session."""
    _require_configured()
    base_url = str(request.base_url).rstrip("/")
    session = await asyncio.to_thread(
        stripe.checkout.Session.create,
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=f"{base_url}/subscribe/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/subscribe/cancelled",
        billing_address_collection="auto",
    )
    return RedirectResponse(session.url, status_code=303)


_SUCCESS_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Macro Pulse -- your API key</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 640px; margin: 60px auto; padding: 0 20px; color: #111; }}
.key {{ font-family: monospace; background: #f4f4f5; border: 1px solid #ddd; border-radius: 8px; padding: 16px; font-size: 15px; word-break: break-all; user-select: all; }}
code {{ background: #f4f4f5; padding: 2px 6px; border-radius: 4px; }}
pre {{ background: #f4f4f5; border-radius: 8px; padding: 16px; overflow-x: auto; }}
h1 {{ font-size: 22px; }}
.warn {{ color: #b45309; font-size: 14px; }}
</style></head>
<body>
<h1>You're subscribed -- here's your API key</h1>
<p class="warn">This key is shown only once. Save it somewhere safe (password manager, .env file).</p>
<div class="key">{api_key}</div>
<h3>Usage</h3>
<pre>curl -H "X-API-Key: {api_key}" \\
  {base_url}/macro-pulse/US</pre>
<p>Same endpoints as the x402 tier, no per-call USDC payment needed while your subscription is active:</p>
<ul>
<li><code>GET /macro-pulse/{{country_code}}</code></li>
<li><code>GET /macro-pulse-batch/{{country_codes}}</code> (up to 8 countries)</li>
</ul>
<p>Manage or cancel your subscription any time from the receipt email Stripe just sent you.</p>
<p>Questions: <a href="mailto:benjiferguson@gmail.com">benjiferguson@gmail.com</a></p>
</body></html>"""

_PENDING_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Macro Pulse -- almost ready</title>
<style>body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 640px; margin: 60px auto; padding: 0 20px; }}</style>
</head><body>
<h1>Payment received, provisioning your key...</h1>
<p>Stripe is finishing up. Refresh this page in a few seconds -- your API key will appear here once it's ready.</p>
<script>setTimeout(() => location.reload(), 3000);</script>
</body></html>"""


@router.get("/subscribe/success", response_class=HTMLResponse)
async def subscribe_success(request: Request, session_id: str):
    _require_configured()
    session = await asyncio.to_thread(
        stripe.checkout.Session.retrieve, session_id, expand=["subscription"]
    )
    subscription = session.subscription
    api_key = None
    if subscription is not None:
        api_key = subscription.get("metadata", {}).get("api_key")
    if not api_key:
        # Webhook may not have landed yet -- ask the user to refresh shortly.
        return HTMLResponse(_PENDING_HTML)
    base_url = str(request.base_url).rstrip("/")
    return HTMLResponse(_SUCCESS_HTML.format(api_key=api_key, base_url=base_url))


@router.get("/subscribe/cancelled", response_class=HTMLResponse)
async def subscribe_cancelled():
    return HTMLResponse(
        "<p>Checkout cancelled -- no charge was made. "
        '<a href="/subscribe">Try again</a> or use the free x402 (USDC on Base) endpoints instead.</p>'
    )


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    _require_configured()
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook secret not configured.")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        subscription_id = session.get("subscription")
        if subscription_id:
            api_key = secrets.token_urlsafe(24)
            await asyncio.to_thread(
                stripe.Subscription.modify,
                subscription_id,
                metadata={"api_key": api_key},
            )
            # Invalidate any stale cache entry so the very next request with
            # this brand-new key doesn't get a false "not found" from a
            # cached negative lookup made before it existed.
            _API_KEY_CACHE.invalidate(api_key)

    # Cache invalidation on cancellation/deletion so a churned subscriber's
    # key stops working within the TTL window immediately, not just after
    # the 5-minute cache would have expired anyway.
    if event["type"] in ("customer.subscription.deleted", "customer.subscription.updated"):
        sub = event["data"]["object"]
        api_key = sub.get("metadata", {}).get("api_key")
        if api_key:
            _API_KEY_CACHE.invalidate(api_key)

    return {"received": True}
