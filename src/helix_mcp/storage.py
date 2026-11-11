"""SQLite artifacts and single-delivery worker jobs.

Expired leases become unknown, never automatically queued again: a lost result
is not evidence that a desktop action did not execute.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class StoreError(ValueError):
    pass


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class Store:
    def __init__(self, data_dir: Path):
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "helix.sqlite3"
        with self.connect() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise StoreError("Database schema is newer than this application. Restore a matching backup or version.")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
                    content TEXT NOT NULL, sha256 TEXT NOT NULL,
                    metadata TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, created_at REAL NOT NULL,
                    queue_deadline REAL NOT NULL, lease_deadline REAL,
                    lease_hash TEXT, result TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_available ON jobs(worker_id,status,created_at);
                CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, last_seen REAL NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def add_artifact(self, kind: str, name: str, content: str, metadata: dict) -> dict:
        if kind not in {"template", "candidate"}:
            raise StoreError("Unknown artifact type.")
        artifact_id = uuid.uuid4().hex
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        created = time.time()
        with self.connect() as conn:
            conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
                         (artifact_id, kind, name, content, digest, json_text(metadata), created))
        return {"id": artifact_id, "kind": kind, "name": name, "sha256": digest, "created_at": created}

    def get_artifact(self, artifact_id: str, kind: str | None = None) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
            raise StoreError("Invalid artifact ID.")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None or (kind is not None and row["kind"] != kind):
            raise StoreError("Artifact not found.")
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        return result

    def list_templates(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id,kind,name,sha256,created_at FROM artifacts WHERE kind='template' ORDER BY created_at DESC LIMIT 200").fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def expire(conn, now):
        conn.execute("UPDATE jobs SET status='unknown',error='Lease expired; execution may have occurred. Inspect Windows before creating another request.' WHERE status='leased' AND lease_deadline<?", (now,))
        conn.execute("UPDATE jobs SET status='expired',error='Worker did not claim this job before its queue deadline.' WHERE status='queued' AND queue_deadline<?", (now,))

    def enqueue(self, worker_id: str, kind: str, payload: dict, request_id: str, queue_seconds=600) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
            raise StoreError("request_id must be 1-128 letters/digits/_.:- and reused for a retry.")
        encoded = json_text(payload)
        fingerprint = hashlib.sha256(json.dumps([worker_id, kind, payload], sort_keys=True,
                                                ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        now, job_id = time.time(), uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.expire(conn, now)
            existing = conn.execute("SELECT id,fingerprint FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise StoreError("request_id already identifies different job contents.")
                job_id = existing["id"]
            else:
                count = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','leased')").fetchone()[0]
                if count >= 100:
                    raise StoreError("Job queue is full.")
                conn.execute("INSERT INTO jobs(id,worker_id,kind,payload,fingerprint,request_id,status,created_at,queue_deadline) VALUES(?,?,?,?,?,?,'queued',?,?)",
                             (job_id, worker_id, kind, encoded, fingerprint, request_id, now, now + queue_seconds))
        return self.get_job(job_id)

    def claim(self, worker_id: str, lease_seconds=180) -> dict | None:
        now, token = time.time(), secrets.token_urlsafe(32)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.expire(conn, now)
            conn.execute("INSERT INTO workers VALUES(?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen", (worker_id, now))
            # One interactive desktop job at a time, even if two worker processes poll.
            if conn.execute("SELECT 1 FROM jobs WHERE worker_id=? AND status='leased'", (worker_id,)).fetchone():
                return None
            row = conn.execute("SELECT * FROM jobs WHERE worker_id=? AND status='queued' ORDER BY created_at,id LIMIT 1", (worker_id,)).fetchone()
            if row is None:
                return None
            deadline = now + lease_seconds
            conn.execute("UPDATE jobs SET status='leased',lease_deadline=?,lease_hash=? WHERE id=?",
                         (deadline, hashlib.sha256(token.encode()).hexdigest(), row["id"]))
            return {"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"]),
                    "lease_token": token, "expires_at": deadline}

    def complete(self, job_id: str, worker_id: str, lease_token: str, success: bool,
                 result: dict, error: str | None) -> dict:
        now = time.time()
        encoded = json_text(result)
        if len(encoded.encode()) > 262144 or (error is not None and len(error) > 2048):
            raise StoreError("Worker result is too large.")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.expire(conn, now)
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND worker_id=?", (job_id, worker_id)).fetchone()
            digest = hashlib.sha256(lease_token.encode()).hexdigest()
            if row is None or not row["lease_hash"] or not secrets.compare_digest(row["lease_hash"], digest):
                raise StoreError("Job lease not found.")
            if row["status"] in {"succeeded", "failed"}:
                # A lost HTTP acknowledgement must not cause another desktop action.
                if json.loads(row["result"]) != result or row["error"] != error or (row["status"] == "succeeded") != success:
                    raise StoreError("A different result was already recorded.")
            elif row["status"] == "leased":
                conn.execute("UPDATE jobs SET status=?,result=?,error=? WHERE id=?",
                             ("succeeded" if success else "failed", encoded, error, job_id))
            else:
                raise StoreError("Lease expired. Job remains unknown; inspect Windows before retrying.")
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise StoreError("Invalid job ID.")
        with self.connect() as conn:
            self.expire(conn, time.time())
            row = conn.execute("SELECT id,worker_id,kind,status,created_at,queue_deadline,lease_deadline,result,error FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise StoreError("Job not found.")
        result = dict(row)
        result["result"] = json.loads(result["result"]) if result["result"] else None
        result["hardware_applied"] = False  # No automatic import adapter in this release.
        return result

    def worker_status(self, worker_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT last_seen FROM workers WHERE id=?", (worker_id,)).fetchone()
        seen = row[0] if row else None
        return {"worker_id": worker_id, "last_seen": seen,
                "recently_seen": seen is not None and time.time() - seen < 30,
                "automatic_hardware_import": False}

    def backup(self, destination: Path) -> Path:
        destination = Path(destination).resolve()
        if destination == self.path.resolve() or destination.exists():
            raise StoreError("Choose a new backup filename.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Reserve exclusively before SQLite opens the backup file.
        with destination.open("xb"):
            pass
        try:
            with self.connect() as source:
                target = sqlite3.connect(destination)
                try:
                    source.backup(target)
                finally:
                    target.close()
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination
