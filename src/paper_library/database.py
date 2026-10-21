"""A separate SQLite store, guarded by an application identifier."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

APPLICATION_ID = 0x48504C31
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE library_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE paper(
 id TEXT PRIMARY KEY, source_root TEXT NOT NULL, relative_path TEXT NOT NULL,
 title TEXT NOT NULL, year INTEGER, region TEXT NOT NULL, exam_type TEXT NOT NULL,
 category TEXT NOT NULL, fingerprint TEXT NOT NULL, parser_version TEXT NOT NULL,
 question_count INTEGER NOT NULL, material_count INTEGER NOT NULL,
 flags_json TEXT NOT NULL, imported_at TEXT NOT NULL
);
CREATE INDEX paper_filters ON paper(category, year, region);
CREATE TABLE paper_revision(
 id INTEGER PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES paper(id),
 fingerprint TEXT NOT NULL, parsed_json TEXT NOT NULL, imported_at TEXT NOT NULL,
 UNIQUE(paper_id, fingerprint)
);
CREATE TABLE source_file(
 id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES paper(id), kind TEXT NOT NULL,
 original_path TEXT NOT NULL, archive_path TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL
);
CREATE INDEX source_paper ON source_file(paper_id);
CREATE TABLE material(
 id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES paper(id), ordinal INTEGER NOT NULL,
 label TEXT NOT NULL, content_json TEXT NOT NULL
);
CREATE INDEX material_paper ON material(paper_id, ordinal);
CREATE TABLE question(
 id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES paper(id), ordinal INTEGER NOT NULL,
 source_no TEXT NOT NULL, section_title TEXT NOT NULL, kind TEXT NOT NULL,
 stem_text TEXT NOT NULL, flags_json TEXT NOT NULL, content_json TEXT NOT NULL
);
CREATE INDEX question_paper ON question(paper_id, ordinal);
CREATE TABLE question_material(
 question_id TEXT NOT NULL REFERENCES question(id) ON DELETE CASCADE,
 material_id TEXT NOT NULL REFERENCES material(id), scope_type TEXT NOT NULL,
 confidence REAL NOT NULL, PRIMARY KEY(question_id, material_id)
);
CREATE TABLE image_asset(
 id TEXT PRIMARY KEY, source_url TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
 sha256 TEXT, local_path TEXT, size_bytes INTEGER, mime_type TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 error TEXT, updated_at TEXT
);
CREATE INDEX image_status ON image_asset(status, id);
CREATE TABLE import_job(
 id TEXT PRIMARY KEY, source_root TEXT NOT NULL, status TEXT NOT NULL,
 started_at TEXT NOT NULL, updated_at TEXT NOT NULL, progress_json TEXT NOT NULL,
 scan_json TEXT NOT NULL, log_path TEXT NOT NULL
);
CREATE TABLE import_item(
 job_id TEXT NOT NULL REFERENCES import_job(id), paper_id TEXT NOT NULL,
 sample_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT,
 PRIMARY KEY(job_id, paper_id)
);
CREATE INDEX item_status ON import_item(job_id, status);
"""


def connect(path: Path, *, writable: bool = False) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if writable:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise ValueError(f"题库不存在，请先执行 import：{path}")
    conn = sqlite3.connect(str(path) if writable else path.as_uri() + "?mode=ro",
                           uri=not writable, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if not tables and not app_id and writable:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
            try:
                conn.execute("CREATE VIRTUAL TABLE question_search USING fts5(question_id UNINDEXED, title, body, tokenize='trigram')")
                tokenizer = "trigram"
            except sqlite3.OperationalError:
                conn.execute("CREATE VIRTUAL TABLE question_search USING fts5(question_id UNINDEXED, title, body)")
                tokenizer = "unicode61"
            conn.executemany("INSERT INTO library_meta VALUES(?,?)",
                             [("search_tokenizer", tokenizer), ("papers", "0"), ("questions", "0"), ("materials", "0")])
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        elif app_id != APPLICATION_ID:
            raise ValueError("该文件不是试卷本地题库数据库；为保护已有数据，拒绝使用")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("数据库版本不受支持，请使用对应版本的程序")
        return conn
    except BaseException:
        conn.close()
        raise


@contextmanager
def writer_lock(path: Path):
    """OS releases the lock on process exit, including forced termination."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".import.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("该题库已有导入进程；请等待原进程结束后再继续") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if __import__("os").name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def summary(conn: sqlite3.Connection) -> dict:
    values = dict(conn.execute("SELECT key,value FROM library_meta"))
    return {key: int(values[key]) for key in ("papers", "questions", "materials")}


def latest_status(conn: sqlite3.Connection, job_id: str | None = None) -> dict:
    row = conn.execute("SELECT * FROM import_job WHERE id=?", (job_id,)).fetchone() if job_id else conn.execute(
        "SELECT * FROM import_job ORDER BY started_at DESC,rowid DESC LIMIT 1").fetchone()
    if not row:
        return {"status": "no_jobs"}
    return {**json.loads(row["progress_json"]), "job_id": row["id"], "status": row["status"],
            "started_at": row["started_at"], "updated_at": row["updated_at"], "log_path": row["log_path"],
            "source_root": row["source_root"], "scan": json.loads(row["scan_json"])}
