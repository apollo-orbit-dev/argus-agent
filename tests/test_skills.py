from pathlib import Path

from engine.skills.base import SkillRegistry, parse_frontmatter

LIB = str(Path(__file__).resolve().parents[1] / "engine" / "skills" / "library")


def test_parse_frontmatter_scalar_and_list():
    meta, body = parse_frontmatter(
        "---\nname: research\ndescription: do research\ntools: [web_search, fetch_page]\n---\nStep 1.\n")
    assert meta["name"] == "research"
    assert meta["description"] == "do research"
    assert meta["tools"] == ["web_search", "fetch_page"]
    assert body == "Step 1."


def test_parse_frontmatter_requires_delimiters():
    import pytest
    with pytest.raises(ValueError):
        parse_frontmatter("no frontmatter here")


def test_loads_research_skill():
    reg = SkillRegistry()
    reg.load_dir(LIB)
    r = reg.get("research")
    assert r is not None
    assert "web_search" in r.tools and "fetch_page" in r.tools
    assert "web_search" in r.procedure.lower()
    assert reg.list()[0].description


def test_malformed_skill_is_skipped(tmp_path):
    (tmp_path / "bad.md").write_text("no frontmatter, just text")
    (tmp_path / "good.md").write_text(
        "---\nname: good\ndescription: ok\ntools: [calculator]\n---\nDo the thing.")
    reg = SkillRegistry()
    reg.load_dir(str(tmp_path))
    assert reg.get("bad") is None
    assert reg.get("good") is not None


def test_missing_dir_does_not_crash():
    reg = SkillRegistry()
    reg.load_dir("/nonexistent/skills/dir")
    assert reg.list() == []
