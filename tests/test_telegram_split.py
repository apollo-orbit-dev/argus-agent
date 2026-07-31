"""Over-long Telegram replies must be SPLIT, and every piece must be valid HTML.

Telegram refuses a message over 4096 chars AND a message whose HTML is malformed. Before this,
reply_html sent the whole thing unsplit, the plain-text fallback re-sent the SAME over-long
string, and the second failure was logged at debug: the user got nothing, silently. A naive
splitter would trade the length rejection for a markup rejection, so the splitter is
markup-aware and these tests check the markup, not just the length.
"""
import random
import re

from backend.telegram_bot import (
    TELEGRAM_MAX_CHARS,
    deliver,
    html_to_plain,
    reply_html,
    split_html_for_telegram,
    to_telegram_html,
)


def assert_balanced(chunk: str) -> None:
    """Every tag opened in a chunk is closed in that same chunk, correctly nested."""
    stack = []
    for m in re.finditer(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9-]*)", chunk):
        name = m.group(2).lower()
        if m.group(1):
            assert stack and stack[-1] == name, f"stray/mismatched </{name}> in {chunk[:60]!r}"
            stack.pop()
        else:
            stack.append(name)
    assert not stack, f"unclosed {stack} in chunk ending {chunk[-60:]!r}"


def assert_valid(chunks, limit=TELEGRAM_MAX_CHARS):
    assert chunks, "the splitter must always emit something"
    for c in chunks:
        assert len(c) <= limit, f"chunk of {len(c)} chars exceeds the {limit} limit"
        assert_balanced(c)


# ---- shape -----------------------------------------------------------------
def test_short_text_is_one_unchanged_chunk():
    s = "<b>all good</b>\nnothing to split here"
    assert split_html_for_telegram(s) == [s]
    assert split_html_for_telegram("x" * TELEGRAM_MAX_CHARS) == ["x" * TELEGRAM_MAX_CHARS]


def test_long_text_splits_into_valid_chunks_and_keeps_every_word():
    body = "\n\n".join(f"Paragraph {i}: " + "word " * 120 for i in range(40))
    chunks = split_html_for_telegram(body)
    assert len(chunks) > 1
    assert_valid(chunks)
    joined = " ".join(chunks)
    for i in range(40):
        assert f"Paragraph {i}:" in joined                      # nothing is dropped


def test_prefers_a_paragraph_break_over_a_mid_sentence_cut():
    body = "\n\n".join("p%02d " % i + "word " * 40 for i in range(60))
    chunks = split_html_for_telegram(body, limit=1000)
    assert_valid(chunks, 1000)
    # a paragraph-boundary cut leaves each chunk starting on a new paragraph
    assert all(c.lstrip().startswith("p") for c in chunks)


# ---- the subtlety: markup must survive the cut ------------------------------
def test_split_inside_bold_closes_and_reopens_it():
    chunks = split_html_for_telegram("<b>" + "word " * 3000 + "</b>", limit=500)
    assert len(chunks) > 5
    assert_valid(chunks, 500)
    assert chunks[0].endswith("</b>") and chunks[1].startswith("<b>")


def test_split_inside_code_and_pre_stays_balanced():
    code = "\n".join(f"line {i} of code" for i in range(400))
    chunks = split_html_for_telegram(f"intro\n\n<pre>{code}</pre>\n\ntail", limit=400)
    assert_valid(chunks, 400)
    inner = [c for c in chunks if "line 100 of code" in c][0]
    assert inner.startswith("<pre>") and inner.endswith("</pre>")
    chunks = split_html_for_telegram("<code>" + "tok " * 2000 + "</code>", limit=300)
    assert_valid(chunks, 300)


def test_a_code_block_is_cut_on_newlines_not_mid_line():
    """Inside <pre>/<code> a word boundary is not a legal cut, so code splits between lines even
    when a mid-line cut would have packed the message fuller."""
    # a long line, then a newline early in the window: a word-boundary cut would pack more in.
    code = "\n".join("x" * 90 + f" tail{i}" for i in range(60))
    chunks = split_html_for_telegram(f"<pre>{code}</pre>", limit=400)
    assert_valid(chunks, 400)
    for i in range(60):                                       # every line survives intact
        assert ("x" * 90 + f" tail{i}") in "".join(chunks)


def test_an_unbroken_code_line_still_splits_without_losing_a_character():
    code = " ".join(f"word{i}" for i in range(2000))           # ONE line, no newline to cut on
    chunks = split_html_for_telegram(f"<pre>{code}</pre>", limit=400)
    assert_valid(chunks, 400)
    body = "".join(re.sub(r"</?pre>", "", c) for c in chunks).replace("…", "")
    assert body.split() == code.split()                        # no word broken, none lost


def test_link_href_is_carried_into_the_next_chunk():
    long_label = "label " * 900
    chunks = split_html_for_telegram(f'<a href="https://example.com/x">{long_label}</a>', limit=400)
    assert len(chunks) > 1
    assert_valid(chunks, 400)
    assert all('href="https://example.com/x"' in c for c in chunks)


def test_entities_are_never_cut_in_half():
    chunks = split_html_for_telegram("<b>" + "a &amp; b &lt;c&gt; " * 600 + "</b>", limit=300)
    assert_valid(chunks, 300)
    for c in chunks:
        # every '&' that starts an entity in the source must still be a whole entity
        assert not re.search(r"&(?:amp|lt|gt)?$", c)
        assert not re.match(r"^(?:amp|lt|gt);", c)


def test_nested_tags_all_reopen():
    chunks = split_html_for_telegram("<b><i>" + "deep " * 3000 + "</i></b>", limit=400)
    assert_valid(chunks, 400)
    assert chunks[1].startswith("<b><i>") and chunks[1].endswith("</i></b>")


# ---- the degenerate case ----------------------------------------------------
def test_a_single_token_longer_than_the_limit_terminates_and_is_marked():
    blob = "A" * 9000                                          # no whitespace anywhere
    chunks = split_html_for_telegram(f"<code>{blob}</code>", limit=1000)
    assert_valid(chunks, 1000)
    assert sum(c.count("A") for c in chunks) == 9000            # sliced, not truncated
    assert all(c.endswith("…</code>") for c in chunks[:-1])     # and the cut is marked


def test_empty_and_whitespace_only_input():
    assert split_html_for_telegram("") == [""]
    assert split_html_for_telegram(None) == [""]


def test_fuzz_random_markup_always_yields_valid_chunks():
    rnd = random.Random(20260731)
    tags = ["b", "i", "u", "s", "code", "pre", "tg-spoiler", "blockquote"]
    for _ in range(40):
        out, depth = [], []
        while len("".join(out)) < 12000:
            r = rnd.random()
            if r < 0.12 and len(depth) < 3:
                t = rnd.choice(tags)
                depth.append(t)
                out.append(f"<{t}>")
            elif r < 0.22 and depth:
                out.append(f"</{depth.pop()}>")
            elif r < 0.28:
                out.append(rnd.choice(["\n\n", "\n", " ", "&amp;", "&lt;"]))
            else:
                out.append("x" * rnd.randint(1, 60) + rnd.choice([" ", "\n", "\n\n"]))
        while depth:
            out.append(f"</{depth.pop()}>")
        limit = rnd.choice([200, 500, 4096])
        assert_valid(split_html_for_telegram("".join(out), limit), limit)


# ---- the send paths ---------------------------------------------------------
class _Recorder:
    """A Telegram message that behaves like the real API: it REFUSES anything over the limit."""

    def __init__(self, refuse_html=False):
        self.sent = []
        self.refuse_html = refuse_html

    async def reply_text(self, text, **kw):
        if len(text) > TELEGRAM_MAX_CHARS:
            raise RuntimeError("Message_too_long")
        if self.refuse_html and kw.get("parse_mode"):
            raise RuntimeError("Can't parse entities")
        self.sent.append(text)

    edit_text = reply_text


async def test_reply_html_splits_instead_of_dropping_the_reply():
    """The /update case: pip tail + stash paragraph + recovery commands over the limit."""
    msg = _Recorder()
    body = "<b>Update failed</b>\n<pre>" + ("ERROR: no matching distribution\n" * 200) + "</pre>"
    assert len(body) > TELEGRAM_MAX_CHARS
    await reply_html(msg, body)
    assert len(msg.sent) > 1                       # not one refused message, several accepted ones
    for s in msg.sent:
        assert_balanced(s)
    assert "Update failed" in msg.sent[0]


async def test_plain_text_fallback_sends_split_text_not_the_same_overlong_string():
    """The original bug: the fallback re-sent the SAME over-long text, so it failed too."""
    msg = _Recorder(refuse_html=True)
    await reply_html(msg, "<b>x</b> " + "word " * 2000)
    assert msg.sent, "the user must receive something"
    assert all(len(s) <= TELEGRAM_MAX_CHARS for s in msg.sent)
    assert all("<b>" not in s for s in msg.sent)   # fallback is clean plain text
    assert "word word" in msg.sent[0]


async def test_reply_html_short_message_is_still_a_single_send():
    msg = _Recorder()
    await reply_html(msg, "<b>hi</b>")
    assert msg.sent == ["<b>hi</b>"]


async def test_deliver_splits_a_long_markdown_answer():
    msg = _Recorder()
    answer = "**Result**\n\n" + "\n\n".join(f"Step {i}: " + "detail " * 60 for i in range(40))
    await deliver(msg, answer)
    assert len(msg.sent) > 1
    for s in msg.sent:
        assert len(s) <= TELEGRAM_MAX_CHARS
        assert_balanced(s)
    assert "Step 39:" in " ".join(msg.sent)         # the tail — the conclusion — still arrives


async def test_undeliverable_message_is_reported_to_the_user():
    """Failing loudly beats failing silently."""
    class _Broken:
        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kw):
            if kw.get("parse_mode"):
                raise RuntimeError("Can't parse entities")
            if "could not be delivered" not in text:
                raise RuntimeError("network down")
            self.sent.append(text)

    msg = _Broken()
    await reply_html(msg, "<b>hello</b>")
    assert msg.sent and "could not be delivered" in msg.sent[0]


def test_html_to_plain_strips_every_tag_including_pre_and_links():
    out = html_to_plain('<b>a</b> <pre>b</pre> <a href="http://x">c</a> &amp; d')
    assert "<" not in out and ">" not in out
    assert "a" in out and "b" in out and "c" in out and "& d" in out


def test_rendered_html_of_a_long_answer_is_within_the_limit_after_splitting():
    """to_telegram_html EXPANDS text (escapes, tags), so splitting the markdown first is not
    enough — the split has to happen after the render."""
    md = "\n\n".join(f"**Bold {i}** with <angle> & ampersand " + "word " * 50 for i in range(40))
    assert_valid(split_html_for_telegram(to_telegram_html(md)))
