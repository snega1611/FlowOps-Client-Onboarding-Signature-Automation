from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, full_name TEXT,
  company TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases(id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id),
  state TEXT NOT NULL, correlation_id TEXT NOT NULL, source TEXT, thread_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE IF NOT EXISTS requirements(id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id),
  kind TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL, due_at TEXT, reminder_count INTEGER DEFAULT 0,
  last_reminder_at TEXT, detail TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(case_id,name));
CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, requirement_id INTEGER,
  doc_type TEXT, filename TEXT, sha256 TEXT, path TEXT, status TEXT, ocr_confidence REAL, issues TEXT,
  text_excerpt TEXT, created_at TEXT NOT NULL, UNIQUE(case_id, sha256));
CREATE TABLE IF NOT EXISTS extracted_fields(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, name TEXT,
  raw_value TEXT, normalized_value TEXT, confidence REAL, evidence TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS signature_requests(id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, provider TEXT,
  external_id TEXT, status TEXT, verified INTEGER DEFAULT 0, sandbox INTEGER DEFAULT 0, letter_path TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_checked_at TEXT);
CREATE TABLE IF NOT EXISTS gmail_messages(id INTEGER PRIMARY KEY, message_id TEXT UNIQUE, thread_id TEXT,
  case_id INTEGER, sender TEXT, subject TEXT, direction TEXT, received_at TEXT, injection_flag INTEGER DEFAULT 0,
  status TEXT, attachment_count INTEGER DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS communications(id INTEGER PRIMARY KEY, case_id INTEGER, purpose TEXT, recipient TEXT,
  idem_key TEXT UNIQUE, status TEXT, attempt INTEGER DEFAULT 0, message_id TEXT, thread_id TEXT, error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_events(id INTEGER PRIMARY KEY, event_id TEXT UNIQUE, type TEXT, case_id INTEGER,
  payload TEXT, correlation_id TEXT, status TEXT, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS investigations(id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, blocker TEXT,
  diagnosis TEXT, confidence REAL, evidence TEXT, recommended_action TEXT, source TEXT, uncertain INTEGER,
  invalid_outputs INTEGER DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, investigation_id INTEGER,
  action TEXT, params TEXT, status TEXT, policy_reason TEXT, result TEXT, actor TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY, action_id INTEGER, case_id INTEGER, action TEXT,
  params TEXT, requested_by TEXT, status TEXT, expires_at TEXT, decided_by TEXT, decided_at TEXT,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY, result TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY, case_id INTEGER, actor TEXT, event TEXT,
  detail TEXT, correlation_id TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_req_case ON requirements(case_id);
CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_events(case_id);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat()


def parse_iso(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def hours_ago(h: float) -> str:
    return iso(now() - timedelta(hours=h))


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(signature_requests)")
        }
        
        if "simulated" in columns and "sandbox" not in columns:
            conn.execute(
                "ALTER TABLE signature_requests RENAME COLUMN simulated TO sandbox"
            )
        elif "sandbox" not in columns:
            conn.execute(
                "ALTER TABLE signature_requests ADD COLUMN sandbox INTEGER DEFAULT 0"
            )

        if "signing_url" not in columns:
            conn.execute(
                "ALTER TABLE signature_requests ADD COLUMN signing_url TEXT"
            )

        if "due_at" not in columns:
            conn.execute(
                "ALTER TABLE signature_requests ADD COLUMN due_at TEXT"
            )

        conn.commit()
    finally:
        conn.close()


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def one(conn, sql, args=()) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def dumps(o) -> str:
    return json.dumps(o, default=str, sort_keys=True)
