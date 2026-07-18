"""currency_convert — convert between currencies via Frankfurter (ECB, keyless)."""
from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from engine.tools.base import Tool

_URL = "https://api.frankfurter.app/latest"


class CurrencyConvertTool(Tool):
    name = "currency_convert"
    description = ("Convert an amount from one currency to another using daily ECB rates. "
                   "Use for currency/exchange-rate questions. Codes are 3-letter (USD, EUR).")

    class Params(BaseModel):
        amount: float = Field(..., description="The amount to convert")
        from_currency: str = Field(..., description="Source 3-letter currency code, e.g. USD")
        to_currency: str = Field(..., description="Target 3-letter currency code, e.g. EUR")

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    async def run(self, args: "CurrencyConvertTool.Params") -> str:
        frm = args.from_currency.strip().upper()
        to = args.to_currency.strip().upper()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as c:
                r = await c.get(_URL, params={
                    "amount": args.amount,
                    "from": frm,
                    "to": to,
                })
        except httpx.HTTPError as e:
            return f"currency_convert error: could not reach exchange service ({e})"
        if r.status_code == 404:
            return f"currency_convert error: unknown currency code ({frm} or {to})."
        if r.status_code != 200:
            return f"currency_convert error: HTTP {r.status_code}"
        try:
            data = r.json()
        except Exception as e:
            return f"currency_convert error: could not parse response ({e})"
        rates = data.get("rates") or {}
        if to not in rates:
            return f"currency_convert error: unknown or unsupported currency code ({frm} or {to})."
        converted = rates[to]
        rate = converted / args.amount if args.amount else 0.0
        return (f"{args.amount:g} {frm} = {converted:.2f} {to} (rate {rate:.4f})")
