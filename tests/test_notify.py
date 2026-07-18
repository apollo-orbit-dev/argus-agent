"""Outbound notifications: channel availability, routing, owner-only, ntfy + email paths."""
import asyncio
import types

from config import Config
from engine.tools.notify import Notifier, NotifyTool


def _cfg(**kw):
    base = dict(model_base_url="http://x/v1", model_name="m", telegram_bot_token="")
    base.update(kw)
    return Config(**base)


def test_available_and_default():
    n = Notifier(_cfg())
    assert n.available() == {"email": False, "ntfy": False, "telegram": False}
    n2 = Notifier(_cfg(ntfy_topic="t", smtp_host="s", notify_email_to="me@x.com"))
    a = n2.available()
    assert a["email"] and a["ntfy"] and not a["telegram"]
    # telegram preferred when a session + deliver exist
    n2.telegram_deliver = lambda sid, txt: None
    assert n2.default_channel("123") == "telegram"
    assert n2.default_channel(None) == "email"        # no session → first configured


def _ntfy_capture(monkeypatch):
    calls = {}

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, content=None, headers=None):
            calls["url"] = url; calls["headers"] = headers or {}; calls["body"] = content
            return _Resp()
    monkeypatch.setattr("engine.tools.notify.httpx.AsyncClient", _Client)
    return calls


async def test_ntfy_send(monkeypatch):
    n = Notifier(_cfg(ntfy_topic="argus-abc", ntfy_server="https://ntfy.sh"))
    calls = _ntfy_capture(monkeypatch)
    ok, detail = await n.send("push", "hello phone", subject="Alert")
    assert ok and "ntfy" in detail
    assert calls["url"] == "https://ntfy.sh/argus-abc"
    assert calls["headers"]["Title"] == "Alert" and calls["body"] == b"hello phone"
    assert calls["headers"]["Markdown"] == "yes"          # markdown always on for push


async def test_ntfy_priority_and_tags(monkeypatch):
    n = Notifier(_cfg(ntfy_topic="t"))
    calls = _ntfy_capture(monkeypatch)
    await n.send("push", "big drop", priority="urgent", tags=["moneybag", "chart"])
    h = calls["headers"]
    assert h["Priority"] == "5" and h["Tags"] == "moneybag,chart"


async def test_ntfy_priority_auto_tag(monkeypatch):
    """high/urgent with no explicit tags gets a sensible default warning tag."""
    n = Notifier(_cfg(ntfy_topic="t"))
    calls = _ntfy_capture(monkeypatch)
    await n.send("push", "heads up", priority="high")
    assert calls["headers"]["Priority"] == "4" and calls["headers"]["Tags"] == "warning"


async def test_ntfy_priority_normalization(monkeypatch):
    from engine.tools.notify import _norm_priority
    assert _norm_priority("urgent") == 5 and _norm_priority("low") == 2
    assert _norm_priority("3") == 3 and _norm_priority("") is None and _norm_priority("bogus") is None


async def test_email_send(monkeypatch):
    n = Notifier(_cfg(smtp_host="smtp.x.com", smtp_port=587, smtp_user="u",
                      smtp_password="p", notify_email_to="me@x.com"))
    sent = {}

    class _SMTP:
        def __init__(self, host, port, timeout=None): sent["host"] = host
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self): sent["tls"] = True
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, msg): sent["to"] = msg["To"]; sent["subj"] = msg["Subject"]
    monkeypatch.setattr("smtplib.SMTP", _SMTP)
    ok, detail = await n.send("email", "the body", subject="Report")
    assert ok and "me@x.com" in detail
    assert sent["to"] == "me@x.com" and sent["subj"] == "Report" and sent["login"] == ("u", "p")


async def test_unconfigured_channel_reports():
    n = Notifier(_cfg())                                # nothing configured
    ok, detail = await n.send("email", "x")
    assert not ok and "isn't configured" in detail


async def test_notify_tool_owner_only_has_no_recipient_field():
    # owner-only by design: NO recipient/to/cc/bcc field anywhere (attachments is files, not people)
    fields = set(NotifyTool.Params.model_fields)
    assert fields == {"message", "subject", "channel", "attachments", "priority", "tags"}
    assert not (fields & {"to", "recipient", "recipients", "cc", "bcc", "email_to"})


async def test_notify_tool_reports_unavailable():
    n = Notifier(_cfg())                                # ntfy not configured
    t = NotifyTool(n, session_id="123")
    out = await t.run(t.Params(message="hi", channel="push"))
    assert "isn't set up" in out.lower()


# ---- attachments (email-only, path-safe, owner-only) ----

def _ws(tmp_path):
    from engine.tools.files import FileWorkspace
    ws = FileWorkspace(str(tmp_path / "ws"))
    ws.save_bytes("chart.png", b"\x89PNG\r\nfakepngbytes")
    return ws


async def test_email_with_attachment(tmp_path, monkeypatch):
    n = Notifier(_cfg(smtp_host="s", smtp_port=587, notify_email_to="me@x.com"), workspace=_ws(tmp_path))
    captured = {}

    class _SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg):
            captured["atts"] = [p.get_filename() for p in msg.iter_attachments()]
    monkeypatch.setattr("smtplib.SMTP", _SMTP)
    ok, detail = await n.send("email", "here's the chart", attachments=["chart.png"])
    assert ok and "1 attachment" in detail
    assert captured["atts"] == ["chart.png"]


async def test_attachment_not_found(tmp_path):
    n = Notifier(_cfg(smtp_host="s", notify_email_to="me@x.com"), workspace=_ws(tmp_path))
    ok, detail = await n.send("email", "x", attachments=["missing.png"])
    assert not ok and "isn't in the workspace" in detail


def test_resolve_attachment_path_safe(tmp_path):
    n = Notifier(_cfg(), workspace=_ws(tmp_path))
    assert n._resolve_attachment("chart.png")
    assert n._resolve_attachment("../../etc/passwd") is None    # sanitized basename not in ws
    assert n._resolve_attachment("/abs/secret.txt") is None


async def test_tool_auto_routes_attachment_to_email(tmp_path):
    n = Notifier(_cfg(smtp_host="s", notify_email_to="me@x.com"), workspace=_ws(tmp_path))
    captured = {}

    async def fake_send(channel, message, **kw):
        captured["channel"] = channel
        captured["att"] = kw.get("attachments")
        return True, "ok"
    n.send = fake_send
    t = NotifyTool(n, session_id="123")
    # asked for push, but an attachment forces email
    await t.run(t.Params(message="here", channel="push", attachments=["chart.png"]))
    assert captured["channel"] == "email" and captured["att"] == ["chart.png"]
