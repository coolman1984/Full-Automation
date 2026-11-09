"""Durable run receipts. Store metadata and error classes, never action payloads."""
from contextlib import closing, contextmanager
import sqlite3
import time

from .common import AutomationError, slug


class RunHistory:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def database(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys = ON')
            db.execute('''CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL,
                status TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
                steps INTEGER NOT NULL DEFAULT 0, error_type TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS steps (
                run_id TEXT NOT NULL REFERENCES runs(run_id), sequence INTEGER NOT NULL,
                location TEXT NOT NULL, tool TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL, elapsed_ms INTEGER NOT NULL, error_type TEXT,
                PRIMARY KEY (run_id, sequence))''')
            db.execute('CREATE INDEX IF NOT EXISTS runs_recent ON runs(started_at DESC)')
            yield db

    def start(self, run_id, name, sha256):
        with self.database() as db:
            db.execute('INSERT INTO runs (run_id,name,sha256,status,started_at) VALUES (?,?,?,?,?)',
                       (run_id, name, sha256, 'running', time.time()))

    def step(self, run_id, sequence, location, tool, status, attempts, elapsed_ms, error_type):
        with self.database() as db:
            db.execute('''INSERT INTO steps VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,sequence) DO UPDATE SET status=excluded.status,
                attempts=excluded.attempts, elapsed_ms=excluded.elapsed_ms, error_type=excluded.error_type''',
                       (run_id, sequence, location, tool, status, attempts, elapsed_ms, error_type))
            db.execute('UPDATE runs SET steps=MAX(steps,?) WHERE run_id=?', (sequence, run_id))

    def finish(self, run_id, status, steps, error_type=None):
        with self.database() as db:
            db.execute('UPDATE runs SET status=?,finished_at=?,steps=?,error_type=? WHERE run_id=?',
                       (status, time.time(), steps, error_type, run_id))

    def list(self, name=None, limit=20):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise AutomationError('History limit must be 1 to 100.')
        query = 'SELECT * FROM runs'
        args = ()
        if name is not None:
            query += ' WHERE name=?'
            args = (slug(name),)
        with self.database() as db:
            return [dict(row) for row in db.execute(query+' ORDER BY started_at DESC, run_id LIMIT ?', (*args, limit))]

    def prune(self, older_than_days, name=None):
        """Explicit retention: delete finished receipts older than the given age. Running receipts are kept."""
        if isinstance(older_than_days, bool) or not isinstance(older_than_days, (int, float)) or not 1 <= older_than_days <= 3650:
            raise AutomationError('older_than_days must be between 1 and 3650.')
        cutoff = time.time() - older_than_days * 86400
        query = "SELECT run_id FROM runs WHERE status != 'running' AND started_at < ?"
        args = [cutoff]
        if name is not None:
            query += ' AND name=?'
            args.append(slug(name))
        with self.database() as db:
            ids = [row['run_id'] for row in db.execute(query, args)]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ','.join('?' * len(chunk))
                db.execute(f'DELETE FROM steps WHERE run_id IN ({marks})', chunk)
                db.execute(f'DELETE FROM runs WHERE run_id IN ({marks})', chunk)
        return {'deleted_runs': len(ids), 'older_than_days': older_than_days}

    def info(self, run_id, cursor=0, limit=100):
        if (isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0 or
                isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500):
            raise AutomationError('Invalid run step cursor or limit.')
        with self.database() as db:
            run = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
            if run is None:
                raise AutomationError('Unknown workflow run.')
            rows = [dict(row) for row in db.execute(
                'SELECT * FROM steps WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?', (run_id, cursor, limit+1))]
            page = rows[:limit]
            return {'run': dict(run), 'steps': page, 'next_cursor': page[-1]['sequence'] if page else cursor,
                    'has_more': len(rows) > limit}
