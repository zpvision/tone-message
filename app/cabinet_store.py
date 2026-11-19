import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

ACTIVE = ("preparing", "warming", "running", "stopping", "exporting")


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in ("uploads", "jobs"):
            (self.root / directory).mkdir(exist_ok=True, mode=0o700)
        self.db = self.root / "cabinet.sqlite3"
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,name TEXT,created TEXT,size INTEGER,meta TEXT);
            CREATE TABLE IF NOT EXISTS prompts(id INTEGER PRIMARY KEY AUTOINCREMENT,created TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,created TEXT,updated TEXT,upload_id TEXT,
              status TEXT,snapshot TEXT,timing TEXT,error TEXT DEFAULT '',export_ready INTEGER DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs((1))
              WHERE status IN ('preparing','warming','running','stopping','exporting');
            CREATE TABLE IF NOT EXISTS rows(job_id TEXT,row_num INTEGER,text TEXT,status TEXT DEFAULT 'pending',
              attempts INTEGER DEFAULT 0,duration REAL DEFAULT 0,result TEXT DEFAULT '{}',PRIMARY KEY(job_id,row_num));
            CREATE INDEX IF NOT EXISTS pending_rows ON rows(job_id,status,row_num);
            CREATE TABLE IF NOT EXISTS attempts(job_id TEXT,row_num INTEGER,number INTEGER,started TEXT,
              status TEXT,duration REAL DEFAULT 0,error TEXT DEFAULT '',PRIMARY KEY(job_id,row_num,number));
            CREATE TABLE IF NOT EXISTS samples(job_id TEXT,elapsed REAL,data TEXT);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).rowcount

    def one(self, sql, args=()):
        with self.connect() as db:
            row = db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE attempts SET status='interrupted',error='duration_incomplete' WHERE status='running'")
            db.execute("UPDATE rows SET status='pending' WHERE status='processing'")
            db.execute("UPDATE jobs SET status='interrupted',error='executor_restarted',updated=? WHERE status IN (?,?,?,?,?)", (now(), *ACTIVE))

    def active(self):
        return self.one("SELECT id,status FROM jobs WHERE status IN (?,?,?,?,?)", ACTIVE)

    def job(self, job_id):
        row = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row:
            row["snapshot"] = json.loads(row["snapshot"])
            row["timing"] = json.loads(row["timing"])
        return row

    def add_job(self, job_id, snapshot):
        with self.connect() as db:
            db.execute("INSERT INTO jobs(id,created,updated,upload_id,status,snapshot,timing) VALUES(?,?,?,?,?,?,?)",
                       (job_id, now(), now(), snapshot["selection"]["upload_id"], "preparing", json.dumps(snapshot, ensure_ascii=False), "{}"))

    def set_job(self, job_id, **fields):
        fields["updated"] = now()
        for key in ("timing",):
            if key in fields:
                fields[key] = json.dumps(fields[key])
        self.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", (*fields.values(), job_id))

    def claim(self, job_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM rows WHERE job_id=? AND status='pending' ORDER BY row_num LIMIT 1", (job_id,)).fetchone()
            if row:
                db.execute("UPDATE rows SET status='processing' WHERE job_id=? AND row_num=?", (job_id, row["row_num"]))
            return dict(row) if row else None

    def begin_attempt(self, job_id, row_num):
        with self.connect() as db:
            db.execute("UPDATE rows SET attempts=attempts+1 WHERE job_id=? AND row_num=?", (job_id, row_num))
            number = db.execute("SELECT attempts FROM rows WHERE job_id=? AND row_num=?", (job_id, row_num)).fetchone()[0]
            db.execute("INSERT INTO attempts(job_id,row_num,number,started,status) VALUES(?,?,?,?,?)", (job_id, row_num, number, now(), "running"))
            return number

    def finish_attempt(self, job_id, row_num, number, duration, status, error):
        with self.connect() as db:
            db.execute("UPDATE attempts SET duration=?,status=?,error=? WHERE job_id=? AND row_num=? AND number=?",
                       (duration, status, error, job_id, row_num, number))
            db.execute("UPDATE rows SET duration=duration+? WHERE job_id=? AND row_num=?", (duration, job_id, row_num))

    def finish_row(self, job_id, row_num, status, result):
        self.execute("UPDATE rows SET status=?,result=? WHERE job_id=? AND row_num=?",
                     (status, json.dumps(result, ensure_ascii=False), job_id, row_num))
