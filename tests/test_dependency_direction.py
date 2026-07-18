"""Tauri-readiness invariant: nothing under engine/ may import backend/ or dashboard/."""
import pathlib
import re


def test_engine_never_imports_interfaces():
    root = pathlib.Path(__file__).resolve().parents[1] / "engine"
    offenders = []
    for p in root.rglob("*.py"):
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if re.match(r"\s*(from|import)\s+(backend|dashboard)\b", line):
                offenders.append(f"{p}:{i}: {line.strip()}")
    assert not offenders, "engine must not import interface layers:\n" + "\n".join(offenders)
