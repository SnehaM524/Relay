"""SQLite persistence for experiments."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .models import Experiment, ExperimentStatus


class Store:
    def __init__(self, database_url: str = "sqlite:///autopilot.db"):
        path = ":memory:" if database_url in (":memory:", "sqlite://") else database_url.removeprefix("sqlite:///")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("""CREATE TABLE IF NOT EXISTS experiments (
                id TEXT PRIMARY KEY, funnel_id TEXT, key TEXT, status TEXT, created_at TEXT, payload TEXT NOT NULL)""")
            self._conn.commit()

    def save(self, exp: Experiment) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO experiments (id, funnel_id, key, status, created_at, payload) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status, payload=excluded.payload""",
                (exp.id, exp.funnel_id, exp.proposal.key, exp.status.value, exp.created_at.isoformat(), exp.model_dump_json()),
            )
            self._conn.commit()

    def get(self, exp_id: str) -> Experiment | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM experiments WHERE id=?", (exp_id,)).fetchone()
        return Experiment.model_validate_json(row[0]) if row else None

    def list(self, funnel_id: str | None = None, status: ExperimentStatus | list[ExperimentStatus] | None = None) -> list[Experiment]:
        q, args = "SELECT payload FROM experiments WHERE 1=1", []
        if funnel_id:
            q += " AND funnel_id=?"; args.append(funnel_id)
        if status:
            sts = status if isinstance(status, list) else [status]
            q += f" AND status IN ({','.join('?' * len(sts))})"; args += [s.value for s in sts]
        q += " ORDER BY created_at"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [Experiment.model_validate_json(r[0]) for r in rows]

    def keys_for(self, funnel_id: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT key FROM experiments WHERE funnel_id=?", (funnel_id,)).fetchall()
        return {r[0] for r in rows}
