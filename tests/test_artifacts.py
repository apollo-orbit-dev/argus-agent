"""build_web_page tool + artifact list/serve/delete."""
import asyncio

import httpx
import pytest

from config import Config
from engine.engine import Engine
from engine.tools.artifacts import (
    BuildWebPageTool, InspectArtifactTool, delete_artifact, ensure_document,
    list_artifacts, slugify, validate_page,
)
from backend.app import create_app


def test_slugify():
    assert slugify("My Sales Dashboard!") == "my-sales-dashboard"
    assert slugify("   ") == "artifact"


def test_ensure_document_wraps_fragment():
    doc = ensure_document("<h1>hi</h1>", "Greeting")
    assert doc.lstrip().lower().startswith("<!doctype")
    assert "<title>Greeting</title>" in doc and "<h1>hi</h1>" in doc


def test_ensure_document_trusts_full_doc_and_injects_title():
    full = "<!DOCTYPE html><html><head></head><body>x</body></html>"
    doc = ensure_document(full, "T")
    assert "<title>T</title>" in doc            # injected because none present


def test_build_write_list_delete(tmp_path):
    d = str(tmp_path / "artifacts")
    tool = BuildWebPageTool(d)
    out = asyncio.run(tool.run(tool.Params(title="Sales Dash", html="<h1>Q3</h1>")))
    assert "/artifacts/sales-dash.html" in out
    arts = list_artifacts(d)
    assert len(arts) == 1
    assert arts[0]["title"] == "Sales Dash" and arts[0]["filename"] == "sales-dash.html"
    # same title updates in place (no duplicate)
    asyncio.run(tool.run(tool.Params(title="Sales Dash", html="<h1>Q4</h1>")))
    assert len(list_artifacts(d)) == 1
    assert delete_artifact(d, "sales-dash.html") is True
    assert list_artifacts(d) == []


def test_delete_artifact_rejects_traversal(tmp_path):
    d = str(tmp_path / "a")
    assert delete_artifact(d, "../secret.html") is False
    assert delete_artifact(d, "evil.txt") is False


def test_validate_catches_undefined_handler():
    """The real field bug: tabs with onclick but no <script> defining the function."""
    html = '<button class="tab" onclick="showTab(\'x\')">X</button>'
    warns = validate_page(html)
    assert any("showTab" in w for w in warns)
    # once the function is defined, no warning
    ok = html + "<script>function showTab(n){}</script>"
    assert not any("showTab" in w for w in validate_page(ok))


def test_validate_catches_external_resource():
    assert any("external" in w.lower() for w in validate_page(
        '<script src="https://cdn.example.com/x.js"></script>'))
    # an <a href> navigation link is fine (not a loaded resource)
    assert not any("external" in w.lower() for w in validate_page('<a href="https://x.com">link</a>'))


def test_validate_catches_truncation():
    """A page cut off mid-CSS (no </body>/</html>) is flagged — the real field failure."""
    truncated = "<!doctype html><html><head><style>.a{color:red}.b{color:"
    assert any("incomplete" in w.lower() for w in validate_page(truncated))
    # a complete document is not flagged as truncated
    complete = "<!doctype html><html><body><h1>ok</h1></body></html>"
    assert not any("incomplete" in w.lower() for w in validate_page(complete))


def test_build_web_page_surfaces_warnings(tmp_path):
    tool = BuildWebPageTool(str(tmp_path))
    out = asyncio.run(tool.run(tool.Params(
        title="Broken", html="<button onclick=\"go()\">Go</button>")))
    assert "⚠️" in out and "go()" in out          # warning surfaced so the model fixes it


def test_inspect_artifact(tmp_path):
    d = str(tmp_path / "a")
    bt = BuildWebPageTool(d)
    asyncio.run(bt.run(bt.Params(title="My Dash", html="<h1>hi there</h1>")))
    it = InspectArtifactTool(d)
    out = asyncio.run(it.run(it.Params(title="My Dash")))
    assert "hi there" in out
    miss = asyncio.run(it.run(it.Params(title="nope")))
    assert "no artifact" in miss.lower()


def test_empty_html_errors(tmp_path):
    tool = BuildWebPageTool(str(tmp_path))
    out = asyncio.run(tool.run(tool.Params(title="x", html="   ")))
    assert "error" in out.lower()


# ---- API ----

def _engine(tmp_path):
    cfg = Config(model_base_url="http://x/v1", model_name="main", telegram_bot_token="")
    e = Engine(cfg)
    e._artifacts_dir = str(tmp_path / "artifacts")
    return e


@pytest.fixture
def client(tmp_path):
    e = _engine(tmp_path)
    BuildWebPageTool(e._artifacts_dir)  # dir created on first write
    asyncio.run(BuildWebPageTool(e._artifacts_dir).run(
        BuildWebPageTool.Params(title="Report", html="<p>hello world</p>")))
    app = create_app(e)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_api_list_view_delete(client):
    async with client as c:
        r = await c.get("/artifacts")
        arts = r.json()["artifacts"]
        assert len(arts) == 1 and arts[0]["filename"] == "report.html"
        v = await c.get("/artifacts/report.html")
        assert v.status_code == 200 and "hello world" in v.text
        bad = await c.get("/artifacts/..%2Fx.html")
        assert bad.status_code in (400, 404)
        d = await c.post("/artifacts/delete", json={"filename": "report.html"})
        assert d.json()["ok"] is True
        assert (await c.get("/artifacts")).json()["artifacts"] == []
