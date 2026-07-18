"""crypto_price — current cryptocurrency price via CoinGecko (keyless)."""
from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from engine.tools.base import Tool

_URL = "https://api.coingecko.com/api/v3/simple/price"


class CryptoPriceTool(Tool):
    name = "crypto_price"
    description = ("Get the current price of a cryptocurrency in a fiat currency. "
                   "Use for crypto price questions. Coin is a CoinGecko id like 'bitcoin'.")

    class Params(BaseModel):
        coin: str = Field(default="bitcoin", description="CoinGecko coin id, e.g. 'bitcoin', 'ethereum'")
        vs_currency: str = Field(default="usd", description="Fiat currency code, e.g. 'usd', 'eur'")

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    async def run(self, args: "CryptoPriceTool.Params") -> str:
        coin = args.coin.strip().lower()
        vs = args.vs_currency.strip().lower()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(_URL, params={"ids": coin, "vs_currencies": vs})
        except httpx.HTTPError as e:
            return f"crypto_price error: could not reach price service ({e})"
        if r.status_code != 200:
            return f"crypto_price error: HTTP {r.status_code}"
        try:
            data = r.json()
        except Exception as e:
            return f"crypto_price error: could not parse response ({e})"
        entry = data.get(coin)
        if not entry or vs not in entry:
            return (f"crypto_price: no price found for coin {args.coin!r} "
                    f"in {args.vs_currency!r} (check the coin id/currency).")
        price = entry[vs]
        label = coin.replace("-", " ").title()
        return f"{label}: {price:,.2f} {vs.upper()}"
