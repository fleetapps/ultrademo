"""The outbox relay: `python -m ultrademo_api.relay`.

Two loops share one process and can run as several replicas:

1. Publish. For each org with unpublished outbox rows, one tenant transaction claims the rows
   (`FOR UPDATE SKIP LOCKED`), writes a delivery per target (every matching webhook and Slack
   endpoint, plus the viewer's follow-up email on `session.ended`) and marks the rows published.
   Fan-out and "published" commit together, so an event gets exactly one delivery per target.
2. Deliver. Due deliveries are leased (their next attempt pushed out by `LEASE`), sent outside any
   transaction, then marked sent, rescheduled with backoff, or dead. A relay that dies mid-send
   leaves the lease to expire and another replica retries, so delivery is at least once: webhook
   receivers dedupe on `webhook-id`.
"""

import asyncio
import contextlib
import signal
from datetime import timedelta
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import structlog
from ultrademo_protocol import WebhookEvent, WebhookTopic

from ultrademo_api import follow_up
from ultrademo_api.db import Database
from ultrademo_api.senders import Mailer, Outcome, SmtpMailer, post_json
from ultrademo_api.settings import Settings, get_settings

log = structlog.get_logger()

# Wait before attempt 2, 3, ...; after the last one the delivery is dead (about 31 h in all).
BACKOFF = [
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
]
LEASE = timedelta(minutes=5)
MAX_ORGS_PER_PASS = 50
_TOPICS = frozenset(t.value for t in WebhookTopic)


def _wants(endpoint: asyncpg.Record, topic: str) -> bool:
    return not endpoint["topics"] or topic in endpoint["topics"]


class Relay:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        http: httpx.AsyncClient,
        mailer: Mailer | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._http = http
        self._mailer = mailer
        self._slots = asyncio.Semaphore(settings.relay_concurrency)

    # -- publish -------------------------------------------------------------------------------

    async def publish_once(self) -> int:
        async with self._db.app() as conn:
            orgs = [
                r["org_id"]
                for r in await conn.fetch(
                    "SELECT org_id FROM orgs_with_unpublished_outbox($1)", MAX_ORGS_PER_PASS
                )
            ]
        return sum([await self._publish_org(org_id) for org_id in orgs])

    async def _publish_org(self, org_id: UUID) -> int:
        async with self._db.tenant(org_id) as conn:
            rows = await conn.fetch(
                "SELECT id, topic, payload, created_at FROM outbox WHERE published_at IS NULL"
                " ORDER BY created_at LIMIT $1 FOR UPDATE SKIP LOCKED",
                self._settings.relay_batch,
            )
            if not rows:
                return 0
            endpoints = await conn.fetch(
                "SELECT id, kind, topics FROM endpoints WHERE disabled_at IS NULL"
            )
            deliveries: list[tuple[Any, ...]] = []
            for row in rows:
                deliveries.extend(await self._fan_out(conn, org_id, row, endpoints))
            await conn.executemany(
                "INSERT INTO deliveries (org_id, outbox_id, channel, endpoint_id, request)"
                " VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                deliveries,
            )
            await conn.execute(
                "UPDATE outbox SET published_at = now() WHERE id = ANY($1::uuid[])",
                [r["id"] for r in rows],
            )
        log.info(
            "outbox_published", org_id=str(org_id), events=len(rows), deliveries=len(deliveries)
        )
        return len(rows)

    async def _fan_out(
        self,
        conn: asyncpg.Connection,
        org_id: UUID,
        row: asyncpg.Record,
        endpoints: list[asyncpg.Record],
    ) -> list[tuple[Any, ...]]:
        topic, payload = row["topic"], row["payload"]
        out: list[tuple[Any, ...]] = []
        if topic in _TOPICS:
            event = WebhookEvent(
                id=str(row["id"]),
                type=WebhookTopic(topic),
                created_at=row["created_at"],
                org_id=str(org_id),
                data=payload,
            ).model_dump(mode="json")
            for ep in endpoints:
                if ep["kind"] == "webhook" and _wants(ep, topic):
                    out.append((org_id, row["id"], "webhook", ep["id"], event))

        facts = None
        if topic in ("session.ended", "cta.clicked") and "session_id" in payload:
            facts = await follow_up.load_session(conn, UUID(payload["session_id"]))
        if facts is None:
            return out
        slack = follow_up.slack_message(self._settings, topic, facts, payload)
        if slack is not None:
            for ep in endpoints:
                if ep["kind"] == "slack" and _wants(ep, topic):
                    out.append((org_id, row["id"], "slack", ep["id"], slack))
        if topic == "session.ended" and self._mailer is not None:
            email = follow_up.follow_up_email(self._settings, facts)
            if email is not None:
                out.append((org_id, row["id"], "email", None, email))
        return out

    # -- deliver -------------------------------------------------------------------------------

    async def deliver_once(self) -> int:
        async with self._db.app() as conn:
            orgs = [
                r["org_id"]
                for r in await conn.fetch(
                    "SELECT org_id FROM orgs_with_due_deliveries($1)", MAX_ORGS_PER_PASS
                )
            ]
        counts = await asyncio.gather(*(self._deliver_org(org_id) for org_id in orgs))
        return sum(counts)

    async def _deliver_org(self, org_id: UUID) -> int:
        async with self._db.tenant(org_id) as conn:
            due = await conn.fetch(
                "SELECT d.id, d.channel, d.request, d.attempts, e.url, e.secret, e.disabled_at"
                " FROM deliveries d LEFT JOIN endpoints e ON e.id = d.endpoint_id"
                " WHERE d.status = 'pending' AND d.next_attempt_at <= now()"
                " ORDER BY d.next_attempt_at LIMIT $1 FOR UPDATE OF d SKIP LOCKED",
                self._settings.relay_batch,
            )
            if not due:
                return 0
            await conn.execute(
                "UPDATE deliveries SET attempts = attempts + 1, next_attempt_at = now() + $2::interval"
                " WHERE id = ANY($1::uuid[])",
                [d["id"] for d in due],
                LEASE,
            )
        outcomes = await asyncio.gather(*(self._send(d) for d in due))
        async with self._db.tenant(org_id) as conn:
            for d, outcome in zip(due, outcomes, strict=True):
                await self._record(conn, d, outcome)
        return len(due)

    async def _send(self, d: asyncpg.Record) -> Outcome:
        async with self._slots:
            if d["channel"] == "email":
                if self._mailer is None:
                    return Outcome(ok=False, error="email is not configured")
                return await self._mailer.send(d["request"])
            if d["url"] is None or d["disabled_at"] is not None:
                return Outcome(ok=False, error="endpoint disabled", permanent=True)
            return await post_json(
                self._http,
                d["url"],
                d["request"],
                allow_private=self._settings.allow_private_targets,
                signing=(d["secret"], str(d["id"])) if d["channel"] == "webhook" else None,
            )

    async def _record(self, conn: asyncpg.Connection, d: asyncpg.Record, outcome: Outcome) -> None:
        attempts = d["attempts"] + 1
        if outcome.ok:
            await conn.execute(
                "UPDATE deliveries SET status = 'sent', sent_at = now(), response_status = $2,"
                " last_error = NULL WHERE id = $1",
                d["id"],
                outcome.status,
            )
            return
        dead = outcome.permanent or attempts > len(BACKOFF)
        await conn.execute(
            "UPDATE deliveries SET status = $2, next_attempt_at = now() + $3::interval,"
            " response_status = $4, last_error = $5 WHERE id = $1",
            d["id"],
            "dead" if dead else "pending",
            timedelta(0) if dead else BACKOFF[attempts - 1],
            outcome.status,
            outcome.error,
        )
        log.warning(
            "delivery_failed",
            delivery_id=str(d["id"]),
            channel=d["channel"],
            attempt=attempts,
            dead=dead,
            error=outcome.error,
        )

    # -- loop ----------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                busy = await self.publish_once() + await self.deliver_once()
            except (asyncpg.PostgresError, OSError) as e:
                log.error("relay_pass_failed", error=str(e)[:300])
                busy = 0
            if not busy:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self._settings.relay_poll_s)


def mailer_from_settings(settings: Settings) -> Mailer | None:
    url = settings.smtp_url.get_secret_value()
    if not url or not settings.email_from:
        return None
    return SmtpMailer(url, settings.email_from)


async def _main() -> None:
    settings = get_settings()
    settings.check_secrets()
    db = Database(settings.database_url, 1, 4)
    await db.connect()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    mailer = mailer_from_settings(settings)
    log.info("relay_started", email=mailer is not None)
    try:
        async with httpx.AsyncClient(timeout=settings.webhook_timeout_s) as http:
            await Relay(db, settings, http, mailer).run(stop)
    finally:
        await db.close()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
