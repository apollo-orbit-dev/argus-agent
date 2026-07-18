from backend.telegram_bot import strip_markdown, to_telegram_html


def test_bold_and_italic():
    assert to_telegram_html("**bold** and *italic*") == "<b>bold</b> and <i>italic</i>"


def test_header_becomes_bold():
    assert to_telegram_html("## Key Points") == "<b>Key Points</b>"


def test_bullets_become_dots():
    out = to_telegram_html("- one\n- two")
    assert "• one" in out and "• two" in out


def test_inline_code_and_fence():
    assert to_telegram_html("use `pip`") == "use <code>pip</code>"
    out = to_telegram_html("```\nprint(1)\n```")
    assert "<pre>print(1)\n</pre>" in out or "<pre>print(1)</pre>" in out


def test_link():
    assert to_telegram_html("[docs](https://x.com)") == '<a href="https://x.com">docs</a>'


def test_html_is_escaped():
    out = to_telegram_html("a < b & c > d")
    assert "&lt;" in out and "&amp;" in out and "&gt;" in out


def test_code_content_escaped_not_transformed():
    # markdown/html inside code must not be interpreted
    out = to_telegram_html("`a < b **x**`")
    assert "<code>a &lt; b **x**</code>" == out


def test_strip_markdown_fallback_is_clean():
    md = "## **Project**\n\nSome **bold** text.\n- item\nUse `code` and [x](https://y.com)."
    s = strip_markdown(md)
    assert "**" not in s and "##" not in s
    assert "Project" in s and "• item" in s and "code" in s and "https://y.com" in s
