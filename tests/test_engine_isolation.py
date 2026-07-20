"""Engine(data_dir=tmp) must keep ALL persistent state inside tmp — no writes to the project root.

This is the safety net for the data_dir refactor: if any store is missed and still resolves off the
project root, the snapshot assertion below fails. It's also what lets the skill-eval harness build
throwaway Engines in-process without polluting the repo.
"""
from pathlib import Path

from config import Config
from engine.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_engine_data_dir_fully_isolates(tmp_path):
    before = {p.name for p in PROJECT_ROOT.iterdir()}
    Engine(Config(), data_dir=str(tmp_path))
    after = {p.name for p in PROJECT_ROOT.iterdir()}
    assert after == before, f"Engine leaked into the project root: {sorted(after - before)}"

    # the core sqlite stores are opened at construction, so they must exist under the tmp data_dir
    created = {p.name for p in tmp_path.rglob("*")}
    for expected in ("tables.db", "memory.db", "knowledge.db", "datastore.db"):
        assert expected in created, f"{expected} was not created under data_dir (still rooted elsewhere?)"
