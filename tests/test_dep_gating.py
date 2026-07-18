"""External web deps (searxng, firecrawl) are gated by config PRESENCE, not a flag.

An empty URL means "not configured" — the tool must NOT be registered, so the model
can't try to use a dependency that isn't set up. Set the URL and the tools appear.
"""
from config import Config
from engine.engine import build_base_registry


def _mk(**over):
    base = dict(model_base_url="http://x/v1", model_name="main", telegram_bot_token="")
    base.update(over)
    return Config(**base)


# --- searxng gates web_search ------------------------------------------------

def test_web_search_absent_when_searxng_unset():
    names = build_base_registry(_mk(searxng_base_url="")).names()
    assert "web_search" not in names


def test_web_search_present_when_searxng_set():
    names = build_base_registry(_mk(searxng_base_url="http://127.0.0.1:8080")).names()
    assert "web_search" in names


# --- firecrawl gates the scrape/crawl family ---------------------------------

_FIRECRAWL_TOOLS = {"fetch_page", "map_site", "crawl_site", "extract_data"}


def test_firecrawl_tools_absent_when_unset():
    names = set(build_base_registry(_mk(firecrawl_base_url="")).names())
    assert not (_FIRECRAWL_TOOLS & names)


def test_firecrawl_tools_present_when_set():
    names = set(build_base_registry(_mk(firecrawl_base_url="http://127.0.0.1:3002")).names())
    assert _FIRECRAWL_TOOLS <= names


def test_config_defaults_are_empty():
    # Ship-safe default: no web-dep URLs baked into the code (a fresh install with no
    # .env registers no web tools). Asserted on the class defaults, not a loaded .env.
    assert Config.model_fields["searxng_base_url"].default == ""
    assert Config.model_fields["firecrawl_base_url"].default == ""


def test_no_web_tools_when_both_unset():
    names = set(build_base_registry(_mk(searxng_base_url="", firecrawl_base_url="")).names())
    assert "web_search" not in names
    assert not (_FIRECRAWL_TOOLS & names)
