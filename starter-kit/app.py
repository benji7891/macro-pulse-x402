"""
x402 Paid API Starter Template

A minimal FastAPI application with x402 payment middleware.
Replace the example endpoint with your own business logic.

Environment variables (set before running):
  PAY_TO_ADDRESS     -> Your Base wallet address (0x...)
  NETWORK            -> "eip155:84532" (testnet) or "eip155:8453" (mainnet)
  PUBLIC_BASE_URL    -> Your deployed URL (for discovery)
  CDP_API_KEY_ID     -> CDP API key ID (mainnet only)
  CDP_API_KEY_SECRET -> CDP API key secret (mainnet only)
"""

import base64
import os
import random
import time

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from x402 import x402ResourceServer
from x402.extensions.bazaar import (
    OutputConfig,
    bazaar_resource_server_extension,
    declare_discovery_extension as _declare_discovery_extension,
)
from x402.http import CreateHeadersAuthProvider, FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact.register import register_exact_evm_server

# ---------------------------------------------------------------------------
# Configuration - Set these via environment variables
# ---------------------------------------------------------------------------
PAY_TO_ADDRESS = os.environ.get("PAY_TO_ADDRESS", "0xYOUR_WALLET_ADDRESS_HERE")
NETWORK = os.environ.get("NETWORK", "eip155:84532")  # Default: testnet
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
CDP_API_KEY_ID = os.environ.get("CDP_API_KEY_ID")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET")
CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
FACILITATOR_URL = os.environ.get("FACILITATOR_URL", "https://x402.org/facilitator")

# USDC contract addresses
ASSET_ADDRESS = (
    "0x036CbD53842c5426634e7929541eC2318f3dCF7e"  # Base Sepolia
    if NETWORK == "eip155:84532"
    else "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # Base Mainnet
)

# Your pricing - adjust as needed
PRICE = "$0.01"

# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="My x402 API",
    description="A pay-per-call API using the x402 protocol (USDC on Base).",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# CDP JWT Authentication (for mainnet facilitator)
# ---------------------------------------------------------------------------
def _generate_cdp_jwt(method: str, path: str, expires_in: int = 120) -> str:
    """Build a short-lived JWT for CDP facilitator authentication."""
    if not CDP_API_KEY_SECRET:
        raise ValueError("CDP_API_KEY_SECRET not set")
    secret_bytes = base64.b64decode(CDP_API_KEY_SECRET)
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(secret_bytes[:32])
    nonce = "".join(random.choices("0123456789", k=16))
    header = {"alg": "EdDSA", "kid": CDP_API_KEY_ID, "typ": "JWT", "nonce": nonce}
    now = int(time.time())
    claims = {
        "sub": CDP_API_KEY_ID,
        "iss": "cdp",
        "aud": None,
        "nbf": now,
        "exp": now + expires_in,
        "uris": [f"{method} api.cdp.coinbase.com{path}"],
    }
    return pyjwt.encode(claims, private_key, algorithm="EdDSA", headers=header)


def _cdp_create_headers() -> dict[str, dict[str, str]]:
    base_path = "/platform/v2/x402"
    return {
        "verify": {"Authorization": f"Bearer {_generate_cdp_jwt('POST', f'{base_path}/verify')}"},
        "settle": {"Authorization": f"Bearer {_generate_cdp_jwt('POST', f'{base_path}/settle')}"},
        "supported": {"Authorization": f"Bearer {_generate_cdp_jwt('GET', f'{base_path}/supported')}"},
    }


# ---------------------------------------------------------------------------
# x402 Server Setup
# ---------------------------------------------------------------------------
if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
    # Mainnet: authenticated CDP facilitator
    facilitator_config = FacilitatorConfig(
        url=CDP_FACILITATOR_URL,
        auth_provider=CreateHeadersAuthProvider(_cdp_create_headers),
    )
else:
    # Testnet: public facilitator (no auth needed)
    facilitator_config = FacilitatorConfig(url=FACILITATOR_URL)

facilitator = HTTPFacilitatorClient(facilitator_config)
server = x402ResourceServer(facilitator)
register_exact_evm_server(server, networks=NETWORK)
server.register_extension(bazaar_resource_server_extension)


# ---------------------------------------------------------------------------
# Helper: Bazaar Extension with Method
# ---------------------------------------------------------------------------
def declare_discovery_extension_with_method(
    method: str,
    path_params_schema: dict | None = None,
    output: OutputConfig | None = None,
    **kwargs
) -> dict:
    """Wrapper that adds HTTP method to bazaar info.input for proper validation."""
    ext = _declare_discovery_extension(
        path_params_schema=path_params_schema,
        output=output,
        **kwargs
    )
    ext["bazaar"]["info"]["input"]["method"] = method
    return ext


# ---------------------------------------------------------------------------
# Route Configuration - Define your paid endpoints here
# ---------------------------------------------------------------------------
routes = {
    "GET /example/:param": {
        "accepts": {
            "scheme": "exact",
            "payTo": PAY_TO_ADDRESS,
            "price": PRICE,
            "network": NETWORK,
        },
        "description": "Example paid endpoint - replace with your own!",
        "extensions": declare_discovery_extension_with_method(
            method="GET",
            path_params_schema={
                "properties": {
                    "param": {
                        "type": "string",
                        "description": "Your parameter description",
                    }
                },
                "required": ["param"],
            },
            output=OutputConfig(
                example={
                    "message": "Hello!",
                    "param": "example_value",
                    "paid": True,
                }
            ),
        ),
    },
}

# Mirror GET routes as HEAD routes (for discovery probes)
for route_key in list(routes.keys()):
    method, path = route_key.split(" ", 1)
    if method == "GET":
        routes[f"HEAD {path}"] = routes[route_key]

# Build middleware once at startup
_x402_middleware_fn = payment_middleware(routes, server)


@app.middleware("http")
async def x402_middleware(request: Request, call_next):
    """x402 payment middleware - checks for payment on protected routes."""
    return await _x402_middleware_fn(request, call_next)


# ---------------------------------------------------------------------------
# Your Endpoints - Replace with your business logic
# ---------------------------------------------------------------------------
@app.get("/example/{param}")
async def example_endpoint(param: str):
    """Example paid endpoint. Replace this with your actual API logic."""
    return {
        "message": f"You paid {PRICE} to call this endpoint!",
        "param": param,
        "paid": True,
    }


@app.get("/")
async def root(request: Request):
    """Landing page / API manifest."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept.lower():
        return HTMLResponse(f"""
<!DOCTYPE html>
<html>
<head><title>My x402 API</title></head>
<body style="font-family: system-ui; max-width: 600px; margin: 50px auto; padding: 20px;">
    <h1>My x402 API</h1>
    <p>A pay-per-call API using USDC on Base.</p>
    <h2>Endpoints</h2>
    <ul>
        <li><code>GET /example/{{param}}</code> - {PRICE} per call</li>
    </ul>
    <h2>Payment</h2>
    <p>All endpoints return 402 until paid. Use any x402-compatible client or wallet.</p>
    <p><a href="/.well-known/x402">View x402 manifest</a></p>
</body>
</html>
        """)
    return {
        "service": "My x402 API",
        "endpoints": {"/example/{param}": {"price": PRICE}},
        "network": NETWORK,
        "manifest": f"{PUBLIC_BASE_URL}/.well-known/x402",
    }


@app.get("/.well-known/x402")
async def x402_manifest():
    """x402 discovery manifest for directories and agents."""
    return {
        "x402Version": 2,
        "resources": [
            {
                "resource": f"{PUBLIC_BASE_URL}/example/{{param}}",
                "method": "GET",
                "description": "Example paid endpoint",
                "price": PRICE,
                "network": NETWORK,
                "asset": ASSET_ADDRESS,
                "payTo": PAY_TO_ADDRESS,
                "inputSchema": {
                    "type": "object",
                    "properties": {"param": {"type": "string"}},
                    "required": ["param"],
                },
            },
        ],
    }


@app.get("/llms.txt", response_class=HTMLResponse)
async def llms_txt():
    """Plain-text description for LLM/agent frameworks."""
    return HTMLResponse(
        content=f"""# My x402 API

A pay-per-call API using the x402 protocol (USDC on Base).

## Endpoints

- GET {PUBLIC_BASE_URL}/example/{{param}} — {PRICE} per call

## Payment

Returns HTTP 402 until paid. Network: {NETWORK}

## Manifest

{PUBLIC_BASE_URL}/.well-known/x402
""",
        media_type="text/plain",
    )
