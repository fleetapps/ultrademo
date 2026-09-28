"""What a finished demo turns into: a Slack alert for the sales team and an email for the viewer.

Rendering is pure (a `SessionFacts` in, a request body out) so the relay can store the result on
the delivery row and every retry sends exactly the same thing.

The agent's summary is written for the sales team (`session_end`), so it goes to Slack and never
into the viewer's email.
"""

import html
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from ultrademo_api.settings import Settings

# Context links store addresses unchecked; only a plain address goes into a To header.
_EMAIL = re.compile(r"^[^@\s<>,;\"]{1,64}@[^@\s<>,;\"]{1,255}\.[^@\s<>,;\"]{2,63}$")


@dataclass(frozen=True)
class SessionFacts:
    session_id: UUID
    type: str
    validity: str
    participant_joined: bool
    duration_s: int | None
    end_reason: str | None
    summary: str | None
    org_name: str
    product_name: str
    agent_name: str
    launch_config_slug: str
    launch_config_name: str
    ctas: list[dict[str, Any]]
    follow_up: dict[str, Any]
    form: dict[str, Any]
    recipient: dict[str, Any]
    sender: dict[str, Any]
    link_token: str | None
    clicked: list[str] = field(default_factory=list)
    rating: int | None = None

    @property
    def viewer_email(self) -> str | None:
        return self.recipient.get("email") or self.form.get("email") or None

    @property
    def viewer_first_name(self) -> str | None:
        return self.recipient.get("first_name") or self.form.get("first_name") or None

    @property
    def viewer_label(self) -> str:
        name = " ".join(
            x for x in (self.recipient.get("first_name"), self.recipient.get("last_name")) if x
        ) or self.form.get("name")
        company = self.recipient.get("company") or self.form.get("company")
        who = name or self.viewer_email or "An anonymous viewer"
        return f"{who} ({company})" if company else who

    @property
    def calendar_url(self) -> str | None:
        """The rep's own booking link first, then the launch config's `book` CTA."""
        url = self.sender.get("calendar_url")
        if isinstance(url, str) and url.startswith("https://"):
            return url
        for c in self.ctas:
            if isinstance(c, dict) and c.get("kind") == "book":
                url = c.get("url")
                if isinstance(url, str) and url.startswith("https://"):
                    return url
        return None


async def load_session(conn: asyncpg.Connection, session_id: UUID) -> SessionFacts | None:
    """Everything the follow-up templates need, read inside the relay's tenant transaction."""
    r = await conn.fetchrow(
        "SELECT s.id, s.type, s.validity, s.participant_joined, s.duration_s, s.end_reason,"
        " s.summary, s.form, o.name AS org_name, p.name AS product_name, a.name AS agent_name,"
        " lc.slug, lc.name AS lc_name, lc.ctas, lc.follow_up,"
        " cl.recipient, cl.sender, cl.token,"
        " (SELECT array_agg(e.payload->>'label' ORDER BY e.created_at) FROM session_events e"
        "   WHERE e.session_id = s.id AND e.type = 'cta.clicked') AS clicked,"
        " (SELECT (e.payload->>'rating')::int FROM session_events e"
        "   WHERE e.session_id = s.id AND e.type = 'feedback') AS rating"
        " FROM sessions s"
        " JOIN organizations o ON o.id = s.org_id"
        " JOIN launch_configs lc ON lc.id = s.launch_config_id"
        " JOIN agent_versions av ON av.id = s.agent_version_id"
        " JOIN agents a ON a.id = av.agent_id"
        " JOIN products p ON p.id = a.product_id"
        " LEFT JOIN context_links cl ON cl.id = s.context_link_id"
        " WHERE s.id = $1",
        session_id,
    )
    if r is None:
        return None
    return SessionFacts(
        session_id=r["id"],
        type=r["type"],
        validity=r["validity"],
        participant_joined=r["participant_joined"],
        duration_s=r["duration_s"],
        end_reason=r["end_reason"],
        summary=r["summary"],
        org_name=r["org_name"],
        product_name=r["product_name"],
        agent_name=r["agent_name"],
        launch_config_slug=r["slug"],
        launch_config_name=r["lc_name"],
        ctas=list(r["ctas"] or []),
        follow_up=r["follow_up"] or {},
        form=r["form"] or {},
        recipient=r["recipient"] or {},
        sender=r["sender"] or {},
        link_token=r["token"],
        clicked=list(dict.fromkeys(r["clicked"] or [])),
        rating=r["rating"],
    )


def _slack_escape(text: str) -> str:
    # Slack mrkdwn treats &, < and > as control characters; nothing else needs escaping.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _minutes(duration_s: int | None) -> str:
    if not duration_s:
        return "under a minute"
    m = round(duration_s / 60)
    return "under a minute" if m < 1 else f"{m} min"


def session_url(settings: Settings, session_id: UUID) -> str:
    # Same link the Client API returns as `details_url`.
    return f"{settings.public_base_url.rstrip('/')}/app/sessions/{session_id}"


def slack_message(
    settings: Settings, topic: str, facts: SessionFacts, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """A Slack incoming-webhook body, or None when this event is not worth an alert."""
    # Test and eval sessions are the customer's own; only real viewers reach the sales channel.
    if facts.type != "prod" or not facts.participant_joined:
        return None
    who = _slack_escape(facts.viewer_label)
    link = f"<{session_url(settings, facts.session_id)}|Open the session>"
    if topic == "session.ended":
        head = f"*{who}* finished a demo of *{_slack_escape(facts.product_name)}*"
        facts_line = [_minutes(facts.duration_s)]
        facts_line.append("engaged" if facts.validity == "valid" else "did not engage")
        if facts.end_reason == "handoff":
            facts_line.append(":raising_hand: asked for a person")
        if facts.clicked:
            facts_line.append(
                "clicked " + ", ".join(f"“{_slack_escape(c)}”" for c in facts.clicked)
            )
        lines = [head, " · ".join(facts_line)]
        if facts.summary:
            lines.append("> " + _slack_escape(facts.summary).replace("\n", "\n> "))
        if facts.viewer_email:
            lines.append(f"Email: {_slack_escape(facts.viewer_email)}")
        lines.append(link)
    elif topic == "cta.clicked":
        label = _slack_escape(str(payload.get("label") or payload.get("cta_id") or "a button"))
        where = "after" if payload.get("phase") == "post_call" else "during"
        lines = [f":point_right: *{who}* clicked “{label}” {where} a demo", link]
    else:
        return None
    text = "\n".join(lines)
    return {"text": text, "unfurl_links": False}


def demo_url(settings: Settings, facts: SessionFacts) -> str:
    base = f"{settings.public_base_url.rstrip('/')}/d/{facts.launch_config_slug}"
    return f"{base}?t={facts.link_token}" if facts.link_token else base


def follow_up_email(settings: Settings, facts: SessionFacts) -> dict[str, Any] | None:
    """The viewer's follow-up email, or None when this session should not get one.

    Sent only when the launch config opts in, the viewer really engaged with a production demo,
    and we know their address.
    """
    cfg = facts.follow_up.get("email") if isinstance(facts.follow_up, dict) else None
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return None
    if facts.type != "prod" or facts.validity != "valid":
        return None
    if not facts.viewer_email or not _EMAIL.match(facts.viewer_email):
        return None
    if not settings.email_from:
        return None

    rep = " ".join(x for x in (facts.sender.get("first_name"), facts.sender.get("last_name")) if x)
    sign_off = rep or f"The {facts.org_name} team"
    subject = str(cfg.get("subject") or f"Your {facts.product_name} demo")[:200]
    message = str(cfg.get("message") or "")[:2000]
    calendar = facts.calendar_url
    again = demo_url(settings, facts)

    paragraphs = [
        f"Hi {facts.viewer_first_name or 'there'},",
        f"Thanks for exploring {facts.product_name} with {facts.agent_name}.",
    ]
    if message:
        paragraphs.append(message)
    text_parts = list(paragraphs)
    html_parts = [f"<p>{html.escape(p)}</p>" for p in paragraphs]
    if calendar:
        who = rep.split(" ")[0] if rep else "our team"
        text_parts.append(f"Pick a time with {who}: {calendar}")
        html_parts.append(
            f'<p><a href="{html.escape(calendar)}">Pick a time with {html.escape(who)}</a></p>'
        )
    text_parts.append(f"Open the demo again any time: {again}")
    html_parts.append(f'<p><a href="{html.escape(again)}">Open the demo again</a> any time.</p>')
    text_parts.append(sign_off)
    html_parts.append(f"<p>{html.escape(sign_off)}</p>")

    email: dict[str, Any] = {
        "to": facts.viewer_email,
        "subject": subject,
        "text": "\n\n".join(text_parts) + "\n",
        "html": "\n".join(html_parts),
    }
    reply_to = facts.sender.get("email")
    if isinstance(reply_to, str) and _EMAIL.match(reply_to):
        email["reply_to"] = reply_to
    return email
