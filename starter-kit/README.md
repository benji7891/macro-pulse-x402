# x402 Paid API Starter Kit

A clean, documented template for building pay-per-call APIs using the [x402 protocol](https://x402.org) with USDC on Base. Deploy on Render, Railway, or any Python hosting platform.

## What is x402?

x402 is an open protocol for API monetization where:
- Agents/users pay per request in USDC on Base (L2, ~$0.001 fees)
- No signup, no API keys needed for payers - just sign and pay
- Payments settle on-chain in real-time
- Works with AI agents, MCP clients, and regular HTTP clients

## Quick Start

1. **Clone and install:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
   cd YOUR_REPO
   pip install -r requirements.txt
   ```

2. **Configure environment variables** (see `.env.example`):
   ```bash
   cp .env.example .env
   # Edit .env with your wallet address
   ```

3. **Run locally:**
   ```bash
   uvicorn app:app --reload --port 8000
   ```

4. **Test the payment flow:**
   ```bash
   # First call returns 402 with payment instructions
   curl http://localhost:8000/your-endpoint/test
   
   # Use the /pay page to make a test payment (testnet first!)
   open http://localhost:8000/pay
   ```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PAY_TO_ADDRESS` | Yes | Your Base wallet address (0x...) to receive payments |
| `NETWORK` | Yes | `eip155:84532` (testnet) or `eip155:8453` (mainnet) |
| `CDP_API_KEY_ID` | Mainnet only | Coinbase Developer Platform API key ID |
| `CDP_API_KEY_SECRET` | Mainnet only | CDP API key secret (base64 Ed25519) |
| `PUBLIC_BASE_URL` | Yes | Your deployed URL (for discovery manifests) |

## Project Structure

```
.
├── app.py              # Main FastAPI application with x402 middleware
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template (no secrets!)
└── README.md           # This file
```

## Adding Your Own Endpoints

1. Define your business logic function:
   ```python
   async def my_api_function(param: str) -> dict:
       # Your logic here
       return {"result": "data", "param": param}
   ```

2. Add the route configuration with pricing:
   ```python
   routes = {
       "GET /my-endpoint/:param": {
           "accepts": {
               "scheme": "exact",
               "payTo": PAY_TO_ADDRESS,
               "price": "$0.01",  # Your price
               "network": NETWORK,
           },
           "description": "Description for discovery catalogs",
           "extensions": declare_discovery_extension_with_method(
               method="GET",
               path_params_schema={
                   "properties": {"param": {"type": "string"}},
                   "required": ["param"],
               },
               output=OutputConfig(example={"result": "example"}),
           ),
       },
   }
   ```

3. Add the FastAPI route handler:
   ```python
   @app.get("/my-endpoint/{param}")
   async def my_endpoint(param: str):
       return await my_api_function(param)
   ```

## Testing Payments

### Testnet (Recommended First)
1. Set `NETWORK=eip155:84532` (Base Sepolia)
2. Get testnet USDC from a faucet
3. The public facilitator at `https://x402.org/facilitator` works for testnet

### Mainnet
1. Set `NETWORK=eip155:8453` (Base Mainnet)
2. Get CDP API credentials from https://portal.cdp.coinbase.com
3. Set `CDP_API_KEY_ID` and `CDP_API_KEY_SECRET`

## Discovery & Listings

Your API will be discoverable via:
- `/.well-known/x402` - Standard x402 manifest
- `/llms.txt` - For LLM/agent frameworks
- `/.well-known/ai-plugin.json` - ChatGPT-style plugin manifest

To get listed on marketplaces:
- [Coinbase Bazaar](https://api.cdp.coinbase.com/platform/v2/x402/discovery) - Auto-indexed after successful payments
- [Gold402](https://24klabs.ai) - Submit for verification
- [x402scan](https://x402scan.com) - Indexed from x402 manifest
- [agentic.market](https://agentic.market) - Agent-focused marketplace

## Deployment

### Render
1. Create a new Web Service
2. Connect your repo
3. Set environment variables in Render dashboard
4. Deploy!

### Railway / Fly.io / Others
Similar process - set env vars and deploy the FastAPI app.

## Pricing Guidelines

- Consider what value you're providing
- $0.01-$0.05 is typical for simple lookups
- $0.05-$0.50 for compute-intensive or data-rich endpoints
- Offer free preview endpoints to let buyers evaluate before paying

## Security Notes

- **NEVER commit secrets, API keys, or wallet private keys**
- Use environment variables for all sensitive config
- The `PAY_TO_ADDRESS` is your PUBLIC wallet address (safe to share)
- Keep your wallet's PRIVATE KEY/SEED PHRASE completely separate

## License

MIT - Use this template however you like.

## Resources

- [x402 Protocol Docs](https://x402.org)
- [Coinbase Developer Platform](https://portal.cdp.coinbase.com)
- [Base Network](https://base.org)
- [FastAPI Documentation](https://fastapi.tiangolo.com)
