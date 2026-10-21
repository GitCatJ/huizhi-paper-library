"""Per-paper transactions, immutable input snapshots and persistent resumption."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from .database import connect, latest_status, writer_lock
from .parser import PARSER_VERSION, parse_paper
from .scanner import scan
from .source_archive import file_hash, snapshot_sample
from .utils import canonical_json, stable_id


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_html(path: Path) -> None:
    with path.open("rb") as stream:
        head = stream.read(8192).lstrip(b"\xef\xbb\xbf \r\n\t").lower()
    if head.startswith((b"\xd0\xcf\x11\xe0", b"PK\x03\x04", b"%PDF")) or not any(
        tag in head for tag in (b"<html", b"<body", b"<div", b"<p>", b"<!doctype", b"<h2")
    ):
        raise ValueError(f"不支持的文档内容（当前支持 HTML 格式的 .doc/.html/.htm）：{path.name}")


def _image_refs(value, base_path: Path):
    if isinstance(value, list):
        for item in value:
            yield from _image_refs(item, base_path)
    elif isinstance(value, dict):
        if value.get("type") == "image":
            url = urljoin(base_path.as_uri(), value.get("remote_url", ""))
            value["asset_key"] = stable_id("asset", url)
            yield value["asset_key"], url
        else:
            for item in value.values():
                yield from _image_refs(item, base_path)


def register_images(conn, parsed, sample):
    truth = Path(sample["components"]["真题"])
    analysis = Path(sample["components"].get("解析", str(truth)))
    refs = []
    for material in parsed["materials"]:
        refs.extend(_image_refs(material, truth))
    for question in parsed["questions"]:
        refs.extend(_image_refs(question["stem"], truth))
        refs.extend(_image_refs(question["options"], truth))
        refs.extend(_image_refs(question["explanations"], analysis))
    conn.executemany("INSERT OR IGNORE INTO image_asset(id,source_url) VALUES(?,?)", refs)


def _search_text(question: dict) -> str:
    return "\n".join([question["stem"]["plain_text"],
                       *(o["content"]["plain_text"] for o in question["options"]),
                       *(a["answer_value"] for a in question["answer_claims"]),
                       *(e["plain_text"] for e in question["explanations"])])


def store_paper(conn, parsed: dict, sample: dict, fingerprint: str) -> None:
    """Caller owns the transaction; all replacements roll back together."""
    paper = parsed["paper"]
    old = conn.execute("SELECT question_count,material_count FROM paper WHERE id=?", (paper["id"],)).fetchone()
    stamp = now()
    register_images(conn, parsed, sample)
    conn.execute("DELETE FROM question_search WHERE question_id IN (SELECT id FROM question WHERE paper_id=?)", (paper["id"],))
    conn.execute("DELETE FROM question WHERE paper_id=?", (paper["id"],))
    conn.execute("DELETE FROM material WHERE paper_id=?", (paper["id"],))
    conn.execute("DELETE FROM source_file WHERE paper_id=?", (paper["id"],))
    conn.execute("""INSERT INTO paper VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET source_root=excluded.source_root,relative_path=excluded.relative_path,
        title=excluded.title,year=excluded.year,region=excluded.region,exam_type=excluded.exam_type,
        category=excluded.category,fingerprint=excluded.fingerprint,parser_version=excluded.parser_version,
        question_count=excluded.question_count,material_count=excluded.material_count,
        flags_json=excluded.flags_json,imported_at=excluded.imported_at""",
        (paper["id"], sample["source_root"], sample["relative_path"], paper["title"], paper["year"],
         paper["region"], paper["exam_type"], paper["category"], fingerprint, PARSER_VERSION,
         len(parsed["questions"]), len(parsed["materials"]), canonical_json(paper["quality_flags"]), stamp))
    for source in parsed["source_files"]:
        conn.execute("INSERT INTO source_file VALUES(?,?,?,?,?,?,?)",
                     (source["id"], paper["id"], source["kind"], source["path"],
                      source["archive"]["archive_path"], source["sha256"], source["size_bytes"]))
    for material in parsed["materials"]:
        conn.execute("INSERT INTO material VALUES(?,?,?,?,?)",
                     (material["id"], paper["id"], material["ordinal"], material["label"], canonical_json(material["content"])))
    for question in parsed["questions"]:
        conn.execute("INSERT INTO question VALUES(?,?,?,?,?,?,?,?,?)",
                     (question["id"], paper["id"], question["ordinal"], str(question["source_no"]),
                      question["section_title"], question["question_kind"], question["stem"]["plain_text"],
                      canonical_json(question["quality_flags"]), canonical_json(question)))
        scope = question["material_scope"]
        for material_id in scope.get("material_ids", []):
            conn.execute("INSERT INTO question_material VALUES(?,?,?,?)",
                         (question["id"], material_id, scope["scope_type"], scope["confidence"]))
        conn.execute("INSERT INTO question_search VALUES(?,?,?)",
                     (question["id"], paper["title"], _search_text(question)))
    conn.execute("INSERT OR IGNORE INTO paper_revision(paper_id,fingerprint,parsed_json,imported_at) VALUES(?,?,?,?)",
                 (paper["id"], fingerprint, canonical_json(parsed), stamp))
    for key, delta in (("papers", 0 if old else 1),
                       ("questions", len(parsed["questions"]) - (old[0] if old else 0)),
                       ("materials", len(parsed["materials"]) - (old[1] if old else 0))):
        conn.execute("UPDATE library_meta SET value=CAST(value AS INTEGER)+? WHERE key=?", (delta, key))


def run_import(db: Path, source: Path | None = None, *, limit: int = 0,
               resume: str | None = None, force: bool = False, callback=None) -> dict:
    db = db.expanduser().resolve()
    with writer_lock(db):
        conn = connect(db, writable=True)
        try:
            if resume:
                job = conn.execute("SELECT * FROM import_job WHERE id=?", (resume,)).fetchone()
                if not job:
                    raise ValueError(f"导入任务不存在：{resume}")
                job_id, root = resume, job["source_root"]
                scan_info = json.loads(job["scan_json"])
                force = scan_info.get("force", False)
                log = Path(job["log_path"])
                started_at = job["started_at"]
            else:
                if source is None:
                    raise ValueError("请提供 --source 目录或 --resume 任务ID")
                plan = scan(source, limit)
                if not plan["samples"]:
                    raise ValueError(f"没有可导入的试卷；扫描问题：{canonical_json(plan['issues'][:5])}")
                job_id, root = "import_" + uuid.uuid4().hex[:16], plan["source_root"]
                log = db.parent / "logs" / (job_id + ".jsonl")
                started_at = now()
                scan_info = {k: v for k, v in plan.items() if k != "samples"}
                scan_info["force"] = force
                with conn:
                    conn.execute("INSERT INTO import_job VALUES(?,?,?,?,?,?,?,?)",
                                 (job_id, root, "running", started_at, started_at, "{}", canonical_json(scan_info), str(log)))
                    conn.executemany("INSERT INTO import_item(job_id,paper_id,sample_json) VALUES(?,?,?)",
                                     [(job_id, s["id"], canonical_json({**s, "source_root": root})) for s in plan["samples"]])
            log.parent.mkdir(parents=True, exist_ok=True)
            counts = Counter(dict(conn.execute("SELECT status,COUNT(*) FROM import_item WHERE job_id=? GROUP BY status", (job_id,))))
            total = sum(counts.values())
            progress = {"total": total, "succeeded": counts["success"], "failed": counts["failed"],
                        "skipped": counts["skipped"], "phase": "preparing", "current_file": "",
                        "eta_seconds": None, "model_calls": 0}
            clock_start, completed_this_run = time.monotonic(), 0

            def emit(phase, current="", error=None, status="running"):
                progress.update(phase=phase, current_file=current)
                progress["done"] = sum(progress[k] for k in ("succeeded", "failed", "skipped"))
                remaining = total - progress["succeeded"] - progress["skipped"]
                progress["eta_seconds"] = (0 if phase == "completed" else
                    round((time.monotonic() - clock_start) / completed_this_run * remaining) if completed_this_run else None)
                with conn:
                    conn.execute("UPDATE import_job SET status=?,updated_at=?,progress_json=? WHERE id=?",
                                 (status, now(), canonical_json(progress), job_id))
                event = {**progress, "job_id": job_id, "status": status, "started_at": started_at,
                         "updated_at": now(), "log_path": str(log)}
                if error:
                    event["error"] = error
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(canonical_json(event) + "\n")
                if callback:
                    callback(event)

            emit("preparing")
            rows = conn.execute("SELECT * FROM import_item WHERE job_id=? AND status IN ('pending','running','failed') ORDER BY rowid", (job_id,)).fetchall()
            try:
                for row in rows:
                    sample = json.loads(row["sample_json"])
                    current = sample["relative_path"]
                    if row["status"] == "failed":
                        progress["failed"] -= 1
                    with conn:
                        conn.execute("UPDATE import_item SET status='running',error=NULL WHERE job_id=? AND paper_id=?", (job_id, row["paper_id"]))
                    emit("parsing", current)
                    try:
                        hashes = {kind: file_hash(Path(path)) for kind, path in sample["components"].items()}
                        fingerprint = hashlib.sha256(canonical_json({"components": hashes, "parser": PARSER_VERSION,
                            "metadata": {k: sample[k] for k in ("year", "region", "exam", "category", "title")}}).encode()).hexdigest()
                        old = conn.execute("SELECT fingerprint FROM paper WHERE id=?", (sample["id"],)).fetchone()
                        if old and old[0] == fingerprint and not force:
                            outcome = "skipped"
                        else:
                            for path in sample["components"].values():
                                check_html(Path(path))
                            snapshot = snapshot_sample(sample, db.parent / "sources", job_id)
                            if snapshot["component_hashes"] != hashes:
                                raise ValueError("源文件在导入期间变化；已保留原有题库，请重新导入该卷")
                            parsed = parse_paper(snapshot)
                            if not parsed["questions"]:
                                raise ValueError("没有识别出题目，需检查源文件结构")
                            with conn:
                                store_paper(conn, parsed, sample, fingerprint)
                                conn.execute("UPDATE import_item SET status='success',error=NULL WHERE job_id=? AND paper_id=?", (job_id, row["paper_id"]))
                            outcome = "success"
                        if outcome == "skipped":
                            with conn:
                                conn.execute("UPDATE import_item SET status='skipped',error=NULL WHERE job_id=? AND paper_id=?", (job_id, row["paper_id"]))
                        progress["succeeded" if outcome == "success" else "skipped"] += 1
                        completed_this_run += 1
                        emit("importing", current)
                    except Exception as exc:
                        message = f"{type(exc).__name__}: {exc}"
                        with conn:
                            conn.execute("UPDATE import_item SET status='failed',error=? WHERE job_id=? AND paper_id=?", (message, job_id, row["paper_id"]))
                        progress["failed"] += 1
                        completed_this_run += 1
                        emit("importing", current, error=message)
                final = "completed_with_errors" if progress["failed"] or scan_info["issues"] else "completed"
                progress["eta_seconds"] = 0
                emit("completed", status=final)
            except BaseException:
                emit("interrupted", status="interrupted")
                raise
            return latest_status(conn, job_id)
        finally:
            conn.close()
