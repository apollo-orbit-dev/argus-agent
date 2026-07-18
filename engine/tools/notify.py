"""Outbound notifications — reach the user off-Telegram via email (SMTP) and push (ntfy).

OWNER-ONLY by construction: email goes to the configured `notify_email_to`, push to the configured
`ntfy_topic`. There is no recipient argument — the agent notifies the USER, never a third party
(sending to arbitrary recipients is a separate, riskier feature deliberately not built here).
Used by the `notify` tool (agent-initiated) and by background-delivery fan-out (scheduler/watcher).
"""
from __future__ import annotations

import asyncio
import logging
from email.message import EmailMessage

import httpx
from pydantic import BaseModel, Field

from engine.tools.base import Tool

log = logging.getLogger("argus.notify")

_ALIASES = {"push": "ntfy", "phone": "ntfy", "mail": "email", "e-mail": "email", "tg": "telegram"}
_PRIORITIES = {"min": 1, "low": 2, "default": 3, "normal": 3, "high": 4, "urgent": 5, "max": 5,
               "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}


def _norm_priority(p):
    if p is None or str(p).strip() == "":
        return None
    return _PRIORITIES.get(str(p).strip().lower())


def _default_tags(prio):
    return {5: ["rotating_light"], 4: ["warning"]}.get(prio, [])


class Notifier:
    """Sends a message on a channel. `telegram_deliver(session_id, text)` is set by main.py so the
    telegram channel reuses the existing bot path; email/push use the configured owner destinations."""

    _MAX_ATTACH = 20 * 1024 * 1024   # 20 MB total — email chokes on more

    def __init__(self, config, telegram_deliver=None, workspace=None, artifacts_dir=None):
        self.config = config
        self.telegram_deliver = telegram_deliver
        self.workspace = workspace          # for resolving attachment filenames (path-safe)
        self.artifacts_dir = artifacts_dir

    def _resolve_attachment(self, name: str):
        """Resolve an attachment filename to a real path — ONLY inside the workspace or artifacts
        dir (path-safe basename; no arbitrary filesystem access). Returns None if not found."""
        import os
        from engine.tools.files import safe_name
        safe = safe_name(name)
        if not safe:
            return None
        if self.workspace is not None:
            p = self.workspace.path_if_exists(safe)
            if p:
                return p
        if self.artifacts_dir:
            p = os.path.join(self.artifacts_dir, safe)
            if os.path.isfile(p):
                return p
        return None

    def available(self) -> dict:
        c = self.config
        return {
            "email": bool(c.smtp_host and c.notify_email_to),
            "ntfy": bool(c.ntfy_topic),
            "telegram": self.telegram_deliver is not None,
        }

    def default_channel(self, session_id: str = None) -> str:
        avail = self.available()
        if session_id and avail["telegram"]:
            return "telegram"
        for ch in ("email", "ntfy", "telegram"):
            if avail[ch]:
                return ch
        return "telegram"

    async def send(self, channel: str, message: str, *, subject: str = "",
                   session_id: str = None, attachments: list = None,
                   priority: str = "", tags: list = None) -> tuple[bool, str]:
        channel = _ALIASES.get((channel or "").lower().strip(), (channel or "").lower().strip())
        if channel == "email":
            return await self._email(subject, message, attachments)
        if channel == "ntfy":
            return await self._ntfy(subject, message, priority=priority, tags=tags)
        if channel == "telegram":
            if not self.telegram_deliver or not session_id:
                return False, "telegram isn't available here"
            try:
                await self.telegram_deliver(session_id, message)
                return True, "sent via telegram"
            except Exception as e:
                return False, f"telegram send failed: {e}"
        return False, f"unknown channel '{channel}' (use email, push, or telegram)"

    async def _email(self, subject: str, body: str, attachments: list = None) -> tuple[bool, str]:
        import os
        c = self.config
        if not (c.smtp_host and c.notify_email_to):
            return False, "email isn't configured (set smtp_host + notify_email_to)"
        resolved, total = [], 0
        for name in (attachments or []):
            path = self._resolve_attachment(name)
            if not path:
                return False, f"attachment '{name}' isn't in the workspace or artifacts"
            total += os.path.getsize(path)
            resolved.append(path)
        if total > self._MAX_ATTACH:
            return False, f"attachments total {total // 1024 // 1024} MB (limit 20 MB) — too large to email"
        try:
            await asyncio.to_thread(self._email_sync, subject, body, resolved)
            extra = f" with {len(resolved)} attachment(s)" if resolved else ""
            return True, f"emailed {c.notify_email_to}{extra}"
        except Exception as e:
            log.debug("email send failed", exc_info=True)
            return False, f"email failed: {e}"

    def _email_sync(self, subject: str, body: str, attachments: list = None) -> None:
        import mimetypes
        import os
        import smtplib
        c = self.config
        msg = EmailMessage()
        msg["From"] = c.notify_email_from or c.smtp_user or c.notify_email_to
        msg["To"] = c.notify_email_to
        msg["Subject"] = subject or "Argus notification"
        msg.set_content(body)
        for path in (attachments or []):
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(path, "rb") as fh:
                msg.add_attachment(fh.read(), maintype=maintype, subtype=subtype,
                                   filename=os.path.basename(path))
        if int(c.smtp_port) == 465:
            with smtplib.SMTP_SSL(c.smtp_host, c.smtp_port, timeout=20) as s:
                if c.smtp_user:
                    s.login(c.smtp_user, c.smtp_password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=20) as s:
                s.ehlo()
                try:
                    s.starttls()
                    s.ehlo()
                except smtplib.SMTPException:
                    pass                       # server without STARTTLS (e.g. localhost relay)
                if c.smtp_user:
                    s.login(c.smtp_user, c.smtp_password)
                s.send_message(msg)

    async def _ntfy(self, title: str, body: str, priority: str = "",
                    tags: list = None) -> tuple[bool, str]:
        c = self.config
        if not c.ntfy_topic:
            return False, "push isn't configured (set ntfy_topic)"
        url = f"{c.ntfy_server.rstrip('/')}/{c.ntfy_topic}"
        headers = {"Markdown": "yes"}                       # render the body as Markdown on the phone
        if title:
            headers["Title"] = title.encode("ascii", "ignore").decode()  # ntfy headers are latin-1
        prio = _norm_priority(priority)
        if prio:
            headers["Priority"] = str(prio)                 # 1=min … 5=urgent (sound/urgency)
        tg = tags or _default_tags(prio)                    # emoji/keyword tags (⚠️ etc.)
        if tg:
            headers["Tags"] = ",".join(str(t).encode("ascii", "ignore").decode().strip() for t in tg if str(t).strip())
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, content=(body or "").encode("utf-8"), headers=headers)
            if r.status_code < 400:
                return True, "pushed via ntfy"
            return False, f"ntfy returned HTTP {r.status_code}"
        except Exception as e:
            return False, f"ntfy failed: {e}"


class NotifyTool(Tool):
    name = "notify"
    description = (
        "Send the USER a notification through a channel: 'email', 'push' (their phone via ntfy), or "
        "'telegram'. Use when they ask to be emailed / texted / pinged, or to proactively alert them "
        "about something important. This reaches the USER ONLY — you cannot send to anyone else. "
        "To EMAIL A FILE (a chart from make_chart, a report from build_web_page, a downloaded PDF, "
        "etc.), put its workspace/artifact file name(s) in `attachments` — attachments are emailed "
        "(the channel becomes email automatically). For PUSH, set `priority` by importance "
        "('urgent'/'high' for time-sensitive alerts, 'low' for FYIs) — it controls how attention-"
        "grabbing the phone notification is; the message renders as Markdown. Args: message; optional "
        "subject (email); optional channel; optional attachments; optional priority; optional tags."
    )

    class Params(BaseModel):
        message: str = Field(..., description="the notification text (Markdown renders on push)")
        subject: str = Field("", description="subject line (email only)")
        channel: str = Field("", description="email | push | telegram (optional)")
        attachments: list[str] = Field(default_factory=list,
                                       description="file names to attach (emailed; from workspace/artifacts)")
        priority: str = Field("", description="push urgency: min|low|default|high|urgent")
        tags: list[str] = Field(default_factory=list,
                                description="push emoji/keyword tags, e.g. ['warning'] → ⚠️ (optional)")

    def __init__(self, notifier: Notifier, session_id: str = None):
        self.notifier = notifier
        self.session_id = session_id

    async def run(self, args: "NotifyTool.Params") -> str:
        # attachments only ride on email — auto-route there if files are attached
        channel = (args.channel or "").strip()
        if args.attachments and channel.lower() not in ("email", "mail", "e-mail"):
            channel = "email"
        channel = channel or self.notifier.default_channel(self.session_id)
        avail = self.notifier.available()
        resolved = _ALIASES.get(channel.lower(), channel.lower())
        if resolved in avail and not avail[resolved]:
            on = [k for k, v in avail.items() if v] or ["(none configured)"]
            return (f"notify: the '{channel}' channel isn't set up. Available: {', '.join(on)}. "
                    "Configure channels in the dashboard's Notifications settings.")
        ok, detail = await self.notifier.send(resolved, args.message, subject=args.subject,
                                              session_id=self.session_id,
                                              attachments=args.attachments,
                                              priority=args.priority, tags=args.tags)
        return f"notify: {detail}." if ok else f"notify: couldn't send — {detail}."
