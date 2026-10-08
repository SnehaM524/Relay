"""Persistence for lead records and SLA state.

SQLite by default (stdlib only), with an in-memory variant for tests. The
interface is small on purpose so it can be swapped for Postgres/Redis.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import LeadRecord, LeadStage


class LeadStore:
    def __init__(self, database_url: str = "sqlite:///relay.db"):
        if database_url.startswith("sqlite:///"):
            path = database_url[len("sqlite:///") :]
        elif database_url in (":memory:", "sqlite://"):
            path = ":memory:"
        else:
            raise ValueError(f"Unsupported database_url: {database_url}")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    id TEXT PRIMARY KEY,
                    hubspot_contact_id TEXT,
                    stage TEXT,
                    sdr_id TEXT,
                    tier TEXT,
                    due_at TEXT,
                    updated_at TEXT,
                    payload TEXT NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_hs ON leads(hubspot_contact_id)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS round_robin (
                    team TEXT PRIMARY KEY,
                    cursor INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    # --- leads -------------------------------------------------------------

    def save(self, rec: LeadRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO leads (id, hubspot_contact_id, stage, sdr_id, tier, due_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    stage=excluded.stage, sdr_id=excluded.sdr_id, tier=excluded.tier,
                    due_at=excluded.due_at, updated_at=excluded.updated_at, payload=excluded.payload
                """,
                (
                    rec.id,
                    rec.lead.hubspot_contact_id,
                    rec.stage.value,
                    rec.routing.sdr.id if rec.routing and rec.routing.sdr else None,
                    rec.score.tier.value if rec.score else None,
                    rec.sla.due_at.isoformat() if rec.sla else None,
                    rec.updated_at.isoformat(),
                    rec.model_dump_json(),
                ),
            )
            self._conn.commit()

    def get(self, lead_id: str) -> LeadRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM leads WHERE id=?", (lead_id,)).fetchone()
        return LeadRecord.model_validate_json(row[0]) if row else None

    def get_by_hubspot_id(self, hubspot_contact_id: str) -> LeadRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM leads WHERE hubspot_contact_id=? ORDER BY updated_at DESC LIMIT 1",
                (hubspot_contact_id,),
            ).fetchone()
        return LeadRecord.model_validate_json(row[0]) if row else None

    def list(self, stages: Iterable[LeadStage] | None = None, limit: int = 500) -> list[LeadRecord]:
        with self._lock:
            if stages:
                vals = [s.value for s in stages]
                q = f"SELECT payload FROM leads WHERE stage IN ({','.join('?' * len(vals))}) ORDER BY updated_at DESC LIMIT ?"
                rows = self._conn.execute(q, (*vals, limit)).fetchall()
            else:
                rows = self._conn.execute("SELECT payload FROM leads ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [LeadRecord.model_validate_json(r[0]) for r in rows]

    def open_leads_by_sdr(self) -> dict[str, int]:
        """Count of leads currently sitting with each SDR (handed off, not yet contacted)."""
        open_stages = (LeadStage.HANDED_OFF.value, LeadStage.ACCEPTED.value, LeadStage.ROUTED.value)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT sdr_id, COUNT(*) FROM leads WHERE stage IN ({','.join('?' * len(open_stages))}) AND sdr_id IS NOT NULL GROUP BY sdr_id",
                open_stages,
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def overdue(self, now: datetime | None = None) -> list[LeadRecord]:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM leads WHERE stage IN (?, ?) AND due_at IS NOT NULL AND due_at <= ?",
                (LeadStage.HANDED_OFF.value, LeadStage.ACCEPTED.value, now.isoformat()),
            ).fetchall()
        return [LeadRecord.model_validate_json(r[0]) for r in rows]

    # --- round robin -------------------------------------------------------

    def next_cursor(self, team: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT cursor FROM round_robin WHERE team=?", (team,)).fetchone()
            cur = row[0] if row else 0
            self._conn.execute(
                "INSERT INTO round_robin(team, cursor) VALUES (?, ?) ON CONFLICT(team) DO UPDATE SET cursor=excluded.cursor",
                (team, cur + 1),
            )
            self._conn.commit()
        return cur

    # --- idempotency -------------------------------------------------------

    def claim(self, key: str, lead_id: str) -> bool:
        """Return True if this key is new (we own it), False if already processed."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO idempotency(key, lead_id, created_at) VALUES (?, ?, ?)",
                    (key, lead_id, datetime.now(timezone.utc).isoformat()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # --- metrics -----------------------------------------------------------

    def response_times(self) -> list[float]:
        out: list[float] = []
        for rec in self.list(limit=10_000):
            if rec.sla and rec.sla.response_seconds is not None:
                out.append(rec.sla.response_seconds)
        return out

    def dump(self) -> list[dict]:
        return [json.loads(r.model_dump_json()) for r in self.list()]
