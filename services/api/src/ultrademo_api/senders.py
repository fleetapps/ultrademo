"""The three ways a delivery leaves the building: a signed webhook, a Slack post, an email.

Each sender returns an `Outcome`; the relay decides from it whether to retry. Nothing here touches
the database.
"""

import asyncio
import ipaddress
import json
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

import httpx
from ultrademo_protocol import webhooks


@dataclass(frozen=True)
class Outcome:
    ok: bool
    status: int | None = None
    error: str | None = None
    # A failure retrying cannot fix (a refused address, a target that is not allowed).
    permanent: bool = False


class TargetNotAllowed(Exception):
    pass


async def check_target(url: str, *, allow_private: bool) -> None:
    """Refuse URLs that would let a customer make the relay call into our own network.

    The resolved addresses are checked at send time, not only when the endpoint is saved, because
    DNS can change in between.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise TargetNotAllowed("not an http(s) URL")
    if allow_private:
        return
    if parts.scheme != "https":
        raise TargetNotAllowed("webhook URLs must use https")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or 443, type=0
        )
    except OSError as e:
        raise TargetNotAllowed(f"cannot resolve {parts.hostname}") from e
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise TargetNotAllowed(f"{parts.hostname} resolves to a non-public address")


def _describe(r: httpx.Response) -> str:
    return f"HTTP {r.status_code}: {r.text[:200]}"


async def post_json(
    http: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    *,
    allow_private: bool,
    extra_headers: dict[str, str] | None = None,
    signing: tuple[str, str] | None = None,
) -> Outcome:
    """POST one JSON body. `signing` is `(secret, message id)` for a Standard Webhooks signature."""
    try:
        await check_target(url, allow_private=allow_private)
    except TargetNotAllowed as e:
        return Outcome(ok=False, error=str(e), permanent=True)
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    headers = {"content-type": "application/json", "user-agent": "ultrademo-webhooks/1"}
    if signing is not None:
        headers.update(webhooks.headers(signing[0], signing[1], raw))
    headers.update(extra_headers or {})
    try:
        # Redirects are not followed: a 3xx could point the relay anywhere.
        r = await http.post(url, content=raw, headers=headers, follow_redirects=False)
    except httpx.HTTPError as e:
        return Outcome(ok=False, error=f"{type(e).__name__}: {e}"[:300])
    if 200 <= r.status_code < 300:
        return Outcome(ok=True, status=r.status_code)
    return Outcome(ok=False, status=r.status_code, error=_describe(r))


class Mailer(Protocol):
    async def send(self, email: dict[str, Any]) -> Outcome: ...


class SmtpMailer:
    """Sends through any SMTP relay (SES, Postmark, Resend, Mailgun all offer one)."""

    def __init__(self, url: str, sender: str, timeout_s: float = 20.0) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("smtp", "smtps") or not parts.hostname:
            raise ValueError("smtp_url must look like smtp://user:pass@host:587 or smtps://...")
        self._implicit_tls = parts.scheme == "smtps"
        self._host = parts.hostname
        self._port = parts.port or (465 if self._implicit_tls else 587)
        self._user = unquote(parts.username) if parts.username else None
        self._password = unquote(parts.password) if parts.password else None
        self._sender = sender
        self._timeout = timeout_s

    def _message(self, email: dict[str, Any]) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self._sender
        msg["To"] = email["to"]
        msg["Subject"] = email["subject"]
        if email.get("reply_to"):
            msg["Reply-To"] = email["reply_to"]
        msg["Message-ID"] = make_msgid(domain=self._sender.rpartition("@")[2].strip("> ") or None)
        msg.set_content(email["text"])
        if email.get("html"):
            msg.add_alternative(email["html"], subtype="html")
        return msg

    def _send_sync(self, msg: EmailMessage) -> None:
        ctx = ssl.create_default_context()
        if self._implicit_tls:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout, context=ctx
            )
        else:
            smtp = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        with smtp:
            if not self._implicit_tls:
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls(context=ctx)
                    smtp.ehlo()
                elif self._user:
                    raise smtplib.SMTPException("refusing to send credentials without TLS")
            if self._user:
                smtp.login(self._user, self._password or "")
            smtp.send_message(msg)

    async def send(self, email: dict[str, Any]) -> Outcome:
        try:
            msg = self._message(email)
        except (KeyError, ValueError) as e:
            return Outcome(ok=False, error=f"bad email: {e}", permanent=True)
        try:
            await asyncio.to_thread(self._send_sync, msg)
        except smtplib.SMTPRecipientsRefused as e:
            return Outcome(ok=False, error=f"recipient refused: {e}"[:300], permanent=True)
        except (smtplib.SMTPException, OSError) as e:
            return Outcome(ok=False, error=f"{type(e).__name__}: {e}"[:300])
        return Outcome(ok=True)
