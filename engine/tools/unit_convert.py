"""Unit conversion across temperature, length, mass, volume, speed, data. Network-free."""
from __future__ import annotations

from pydantic import BaseModel, Field

from engine.tools.base import Tool

# Linear-factor categories: value in unit * factor = value in base unit.
_LINEAR: dict[str, dict[str, float]] = {
    "length": {  # base: meter
        "m": 1.0, "km": 1000.0, "mi": 1609.344, "ft": 0.3048,
        "in": 0.0254, "cm": 0.01, "mm": 0.001, "yd": 0.9144,
    },
    "mass": {  # base: gram
        "g": 1.0, "kg": 1000.0, "lb": 453.59237, "oz": 28.349523125,
    },
    "volume": {  # base: liter
        "l": 1.0, "ml": 0.001, "gal": 3.785411784,
        "cup": 0.2365882365, "floz": 0.0295735295625,
    },
    "speed": {  # base: meter/second
        "m/s": 1.0, "mph": 0.44704, "kph": 0.277777777778, "knot": 0.514444444444,
    },
    "data": {  # base: byte (decimal)
        "b": 1.0, "kb": 1000.0, "mb": 1000.0 ** 2,
        "gb": 1000.0 ** 3, "tb": 1000.0 ** 4,
    },
}

# Temperature handled specially (offsets, not pure factors).
_TEMP = {"c", "f", "k"}

# Map every unit token (lowercased) to its category for quick lookup.
_UNIT_CATEGORY: dict[str, str] = {}
for _cat, _units in _LINEAR.items():
    for _u in _units:
        _UNIT_CATEGORY[_u] = _cat
for _t in _TEMP:
    _UNIT_CATEGORY[_t] = "temperature"

# Preferred display casing for units.
_DISPLAY = {
    "m/s": "m/s",
    "c": "C", "f": "F", "k": "K",
    "b": "B", "kb": "KB", "mb": "MB", "gb": "GB", "tb": "TB",
}


def _norm(unit: str) -> str:
    return unit.strip().lower()


def _display_unit(norm: str, original: str) -> str:
    if norm in _DISPLAY:
        return _DISPLAY[norm]
    return original.strip()


def _to_kelvin(value: float, unit: str) -> float:
    if unit == "c":
        return value + 273.15
    if unit == "f":
        return (value - 32.0) * 5.0 / 9.0 + 273.15
    return value  # kelvin


def _from_kelvin(value: float, unit: str) -> float:
    if unit == "c":
        return value - 273.15
    if unit == "f":
        return (value - 273.15) * 9.0 / 5.0 + 32.0
    return value  # kelvin


def _fmt(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.2f}"


class UnitConvertTool(Tool):
    name = "unit_convert"
    description = (
        "Convert a numeric value between units of the same category: temperature "
        "(C/F/K), length (m, km, mi, ft, in, cm, mm, yd), mass (g, kg, lb, oz), "
        "volume (l, ml, gal, cup, floz), speed (mph, kph, m/s, knot), or data "
        "(B, KB, MB, GB, TB). Use when the user needs to convert measurements."
    )

    class Params(BaseModel):
        value: float = Field(..., description="The numeric value to convert.")
        from_unit: str = Field(..., description="Source unit, e.g. 'F', 'km', 'lb'.")
        to_unit: str = Field(..., description="Target unit, e.g. 'C', 'mi', 'kg'.")

    async def run(self, args: "UnitConvertTool.Params") -> str:
        try:
            src = _norm(args.from_unit)
            dst = _norm(args.to_unit)
            src_cat = _UNIT_CATEGORY.get(src)
            dst_cat = _UNIT_CATEGORY.get(dst)
            if src_cat is None:
                return (
                    f"unit_convert error: unknown from_unit '{args.from_unit}'. "
                    f"Known units: {', '.join(sorted(_UNIT_CATEGORY))}."
                )
            if dst_cat is None:
                return (
                    f"unit_convert error: unknown to_unit '{args.to_unit}'. "
                    f"Known units: {', '.join(sorted(_UNIT_CATEGORY))}."
                )
            if src_cat != dst_cat:
                return (
                    f"unit_convert error: cannot convert {src_cat} ('{args.from_unit}') "
                    f"to {dst_cat} ('{args.to_unit}')."
                )

            if src_cat == "temperature":
                result = _from_kelvin(_to_kelvin(args.value, src), dst)
            else:
                factors = _LINEAR[src_cat]
                base = args.value * factors[src]
                result = base / factors[dst]

            from_disp = _display_unit(src, args.from_unit)
            to_disp = _display_unit(dst, args.to_unit)
            return f"{_fmt(args.value)} {from_disp} = {_fmt(result)} {to_disp}"
        except Exception as e:  # defensive: never crash the loop
            return f"unit_convert error: {e}"
