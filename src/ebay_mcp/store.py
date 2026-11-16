"""SQLite state: pending (previewed) changes, the audit log, and daily API call counts."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

PENDING_TTL_S = 15 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending (
  change_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  change_id TEXT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  item_id TEXT NOT NULL,
  field TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT,
  reason TEXT,
  status TEXT NOT NULL,
  detail TEXT,
  undone_by INTEGER
);
CREATE INDEX IF NOT EXISTS audit_item ON audit(item_id);
CREATE TABLE IF NOT EXISTS api_calls (
  day TEXT NOT NULL, api TEXT NOT NULL, count INTEGER NOT NULL,
  PRIMARY KEY (day, api)
);
"""


class Store:
    def __init__(self, path: str = ":memory:", clock=time.time):
        if path != ":memory:":
            p = Path(path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = str(p)
            if not p.exists() and os.name == "posix":
                p.touch(mode=0o600)  # the audit log is private to this user
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._clock = clock

    # ---- pending changes -------------------------------------------------
    def create_pending(self, kind: str, payload: dict, ttl_s: int = PENDING_TTL_S) -> tuple[str, float]:
        change_id = "chg_" + secrets.token_urlsafe(16)  # 128 random bits: cannot be guessed
        now = self._clock()
        self.db.execute("DELETE FROM pending WHERE expires_at < ?", (now - 7 * 86400,))
        self.db.execute(
            "INSERT INTO pending(change_id, kind, payload, created_at, expires_at) VALUES (?,?,?,?,?)",
            (change_id, kind, json.dumps(payload), now, now + ttl_s),
        )
        return change_id, now + ttl_s

    def claim_pending(self, change_id: str) -> dict:
        """Atomically move pending -> applying. A change can be confirmed only once."""
        if not isinstance(change_id, str) or not change_id.startswith("chg_") or len(change_id) > 64:
            raise LookupError("Unknown change_id. Run the preview again.")
        row = self.db.execute("SELECT * FROM pending WHERE change_id=?", (change_id,)).fetchone()
        if row is None:
            raise LookupError("Unknown change_id. Run the preview again.")
        if row["status"] != "pending":
            raise LookupError(f"Change {change_id} is already {row['status']}.")
        if row["expires_at"] < self._clock():
            self.db.execute("UPDATE pending SET status='expired' WHERE change_id=?", (change_id,))
            raise LookupError("This preview expired (15 minutes). Run the preview again.")
        cur = self.db.execute(
            "UPDATE pending SET status='applying' WHERE change_id=? AND status='pending'", (change_id,)
        )
        if cur.rowcount != 1:
            raise LookupError("Change was claimed by another request.")
        return {"kind": row["kind"], **json.loads(row["payload"])}

    def finish_pending(self, change_id: str, status: str):
        self.db.execute("UPDATE pending SET status=? WHERE change_id=?", (status, change_id))

    # ---- audit log -------------------------------------------------------
    def log(self, *, change_id, kind, item_id, field, old, new, reason, status, detail="") -> int:
        cur = self.db.execute(
            "INSERT INTO audit(change_id, ts, kind, item_id, field, old_value, new_value, reason, status, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (change_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, item_id, field,
             None if old is None else json.dumps(old), None if new is None else json.dumps(new),
             (reason or "")[:200], status, (detail or "")[:500]),
        )
        return cur.lastrowid

    def get_audit(self, audit_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM audit WHERE id=?", (audit_id,)).fetchone()
        return _audit_dict(row) if row else None

    def list_audit(self, item_id: str | None = None, limit: int = 20) -> list[dict]:
        if item_id:
            rows = self.db.execute("SELECT * FROM audit WHERE item_id=? ORDER BY id DESC LIMIT ?", (item_id, limit))
        else:
            rows = self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [_audit_dict(r) for r in rows]

    def mark_undone(self, audit_id: int, by_audit_id: int):
        self.db.execute("UPDATE audit SET undone_by=? WHERE id=?", (by_audit_id, audit_id))

    # ---- rate limiting ---------------------------------------------------
    def incr_calls(self, api: str) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.db.execute(
            "INSERT INTO api_calls(day, api, count) VALUES (?,?,1) "
            "ON CONFLICT(day, api) DO UPDATE SET count = count + 1", (day, api),
        )
        return self.db.execute("SELECT count FROM api_calls WHERE day=? AND api=?", (day, api)).fetchone()[0]


def _audit_dict(row) -> dict:
    d = dict(row)
    for k in ("old_value", "new_value"):
        if d[k] is not None:
            d[k] = json.loads(d[k])
    return d
