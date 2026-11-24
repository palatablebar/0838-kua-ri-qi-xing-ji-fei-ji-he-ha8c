import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY,
              request_key TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL,
              profile TEXT NOT NULL,
              status TEXT NOT NULL,
              revision INTEGER NOT NULL,
              payload TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS corrections(
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              evidence_key TEXT NOT NULL,
              status TEXT NOT NULL,
              detail TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(run_id, evidence_key)
            );
            ''')

    def find_by_request(self, request_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE request_key=?", (request_key,)).fetchone()
        return dict(row) if row else None

    def create(self, request_key, profile, payload):
        existing = self.find_by_request(request_key)
        if existing:
            return existing
        run_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,request_key,created_at,profile,status,revision,payload) VALUES(?,?,?,?,?,?,?)",
                       (run_id, request_key, datetime.now(timezone.utc).isoformat(), profile, "DRAFT", 0,
                        json.dumps(payload, ensure_ascii=False)))
        return self.get(run_id)

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def review(self, run_id, decision, comment, expected_revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None, "missing"
            if row["revision"] != expected_revision:
                return None, "stale"
            db.execute("UPDATE runs SET status=?, revision=revision+1, comment=? WHERE id=?",
                       (decision, comment, run_id))
        return self.get(run_id), None

    def find_correction(self, run_id, evidence_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM corrections WHERE run_id=? AND evidence_key=?",
                             (run_id, evidence_key)).fetchone()
        return dict(row) if row else None

    def get_correction(self, correction_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM corrections WHERE id=?", (correction_id,)).fetchone()
        return dict(row) if row else None

    def create_correction(self, run_id, evidence_key, status, detail):
        existing = self.find_correction(run_id, evidence_key)
        if existing:
            return existing, False
        correction_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT INTO corrections(id,run_id,evidence_key,status,detail,created_at) VALUES(?,?,?,?,?,?)",
                       (correction_id, run_id, evidence_key, status, json.dumps(detail, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat()))
        return self.get_correction(correction_id), True

    def list_corrections(self, run_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM corrections WHERE run_id=? ORDER BY created_at, id",
                              (run_id,)).fetchall()
        return [dict(row) for row in rows]
