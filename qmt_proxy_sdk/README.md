# qmt_proxy_sdk

`qmt_proxy_sdk` is the async-first Python SDK for the REST interface exposed by `qmt-proxy`.

This package is designed to be installed independently and only covers REST communication.

## Installation

Install from the package directory during local development:

```bash
pip install ./libs/qmt_proxy_sdk
```

When published to an index, install the distribution name:

```bash
pip install qmt-proxy-sdk
```

Or build and install a wheel from inside `libs/qmt_proxy_sdk`:

```bash
python -m build
pip install dist/qmt_proxy_sdk-*.whl
```

## Quick Start

```python
import asyncio

from qmt_proxy_sdk import AsyncQmtProxyClient


async def main():
    async with AsyncQmtProxyClient(
        base_url="http://localhost:8000",
        api_key="dev-api-key-001",
    ) as client:
        health = await client.system.check_health()
        market = await client.data.get_market_data(
            stock_codes=["000001.SZ"],
            start_date="20240101",
            end_date="20240131",
        )
        status = await client.trading.get_connection_status(session_id="demo-session")

        print(health.status)
        print(market[0].stock_code if market else "no data")
        print(status.connected)


asyncio.run(main())
```

## Client Structure

- `client.system`: root info and health checks
- `client.data`: market data, downloads, L2, sector management, subscriptions
- `client.trading`: connect, account, positions, orders, trades, assets, risk
- `client.request(...)`: low-level escape hatch for custom calls

## Authentication

The SDK sends `Authorization: Bearer <api_key>`, which matches the current server implementation.

## Response Strategy

- Stable query endpoints are parsed into Pydantic models.
- Irregular download and bulk utility endpoints return decoded JSON payloads directly.
- API envelope responses are unwrapped automatically by `AsyncHttpTransport`.

## Notes

- This SDK only covers REST communication.
- WebSocket streaming is intentionally out of scope for this package.
- The package is self-contained and does not require importing the server-side `app` package.
