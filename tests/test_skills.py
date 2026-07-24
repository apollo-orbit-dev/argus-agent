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


def test_parse_frontmatter_requires_closing_delimiter():
    import pytest
    with pytest.raises(ValueError):
        parse_frontmatter("---\nname: x\ndescription: y\n")


def test_parse_frontmatter_multiline_yaml_list():
    # The old hand-rolled splitter silently dropped multi-line YAML lists (it only
    # understood `tools: [a, b]` / `tools: a, b` on a single line). yaml.safe_load
    # must pick these up correctly.
    meta, body = parse_frontmatter(
        "---\n"
        "name: multiline\n"
        "description: has multiline lists\n"
        "tools:\n"
        "  - web_search\n"
        "  - fetch_page\n"
        "triggers:\n"
        "  - look up\n"
        "  - find out\n"
        "---\n"
        "Step 1.\n")
    assert meta["tools"] == ["web_search", "fetch_page"]
    assert meta["triggers"] == ["look up", "find out"]
    assert body == "Step 1."


def test_parse_frontmatter_quoted_value_with_colon():
    meta, body = parse_frontmatter(
        '---\nname: colon-test\ndescription: "shape it correctly for its channel: how concise vs detailed"\ntools: [calculator]\n---\nDo it.\n')
    assert meta["description"] == "shape it correctly for its channel: how concise vs detailed"
    assert meta["tools"] == ["calculator"]
    assert body == "Do it."


def test_parse_frontmatter_unquoted_value_with_colon_still_works():
    # Back-compat: existing library skills (e.g. report_builder.md) have an unquoted
    # description containing "word: word" mid-sentence, which is invalid as a bare
    # YAML plain scalar. parse_frontmatter must still handle it without requiring the
    # author to add quotes.
    meta, body = parse_frontmatter(
        "---\nname: unquoted-colon\n"
        "description: shape it for the channel: how concise vs detailed, how many paragraphs\n"
        "tools: [calculator]\n---\nDo it.\n")
    assert meta["description"] == "shape it for the channel: how concise vs detailed, how many paragraphs"
    assert meta["tools"] == ["calculator"]


def test_parse_frontmatter_malformed_yaml_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_frontmatter("---\nname: [unclosed list\n---\nbody\n")


def test_parse_frontmatter_single_scalar_tools():
    meta, _ = parse_frontmatter(
        "---\nname: single\ndescription: d\ntools: calculator\n---\nbody\n")
    assert meta["tools"] == ["calculator"]


def test_all_library_skills_load_including_report_builder():
    # report_builder.md's description contains an unquoted "channel: how concise..."
    # which is invalid as a bare YAML plain scalar — this is the sharpest back-compat
    # case for switching to yaml.safe_load.
    reg = SkillRegistry()
    reg.load_dir(LIB)
    md_files = [p for p in Path(LIB).iterdir() if p.suffix == ".md"]
    assert len(reg.list()) == len(md_files)
    rb = reg.get("report_builder")
    assert rb is not None
    assert "channel: how concise" in rb.description


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


def test_design_table_skill_loads():
    reg = SkillRegistry()
    reg.load_dir(LIB)
    s = reg.get("design_table")
    assert s is not None
    assert s.description and s.procedure
    assert set(s.tools) == {"list_tables", "create_table", "read_document", "read_file", "insert_row"}
    assert s.steps == []                      # prose-only, no deterministic steps
    assert any("table" in t for t in s.triggers)
    # the schema-design teeth are present
    assert "json" in s.procedure.lower()
    assert "grain" in s.procedure.lower() or "one row" in s.procedure.lower()


def test_extract_to_table_skill_loads():
    reg = SkillRegistry()
    reg.load_dir(LIB)
    s = reg.get("extract_to_table")
    assert s is not None
    assert s.description and s.procedure
    assert set(s.tools) == {"download_file", "read_document", "read_file",
                            "create_table", "insert_row", "list_tables", "query_table"}
    assert s.steps == []                       # prose-only
    body = s.procedure.lower()
    assert "read_document" in body and "read_file" in body and "insert_row" in body
    assert "one row" in body                    # the structuring pitfall is addressed
