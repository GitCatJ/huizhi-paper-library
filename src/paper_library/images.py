"""Explicit, resumable image localization. Browsing never fetches source URLs."""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, url2pathname, urlopen

from PIL import Image

from .database import connect, writer_lock
from .importer import now
from .source_archive import file_hash
from .utils import canonical_json, write_json

MAX_BYTES = 20 * 1024 * 1024
TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp",
         "BMP": "image/bmp", "TIFF": "image/tiff"}


def _fetch(row: dict, target: Path, allowed_parents: set[Path]) -> dict:
    try:
        parts = urlsplit(row["source_url"])
        if parts.scheme == "file":
            if parts.netloc not in ("", "localhost"):
                raise ValueError("不支持网络共享图片路径")
            path = Path(url2pathname(parts.path)).resolve()
            if not any(path.is_relative_to(parent) for parent in allowed_parents):
                raise ValueError("本地图片必须位于试卷文件所在目录内")
            with path.open("rb") as stream:
                payload = stream.read(MAX_BYTES + 1)
        elif parts.scheme in ("http", "https"):
            request = Request(row["source_url"], headers={"User-Agent": "huizhi-paper-library/0.1"})
            with urlopen(request, timeout=25) as response:
                if urlsplit(response.geturl()).scheme not in ("http", "https"):
                    raise ValueError("图片重定向目标协议不受支持")
                payload = response.read(MAX_BYTES + 1)
        else:
            raise ValueError("图片地址协议不受支持")
        if len(payload) > MAX_BYTES:
            raise ValueError("图片超过 20 MiB 上限")
        with Image.open(io.BytesIO(payload)) as image:
            mime = TYPES.get(image.format)
            if not mime:
                raise ValueError("不支持的图片格式")
            image.verify()
        sha = hashlib.sha256(payload).hexdigest()
        destination = target / sha[:2] / (sha + ".img")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            fd, temp = tempfile.mkstemp(prefix=".image-", dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, destination)
            finally:
                Path(temp).unlink(missing_ok=True)
        if file_hash(destination) != sha:
            raise ValueError("本地图片哈希验证失败")
        return {"id": row["id"], "status": "ready", "sha256": sha, "local_path": str(destination),
                "size_bytes": len(payload), "mime_type": mime, "error": None}
    except Exception as exc:
        return {"id": row["id"], "status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def run_images(db: Path, *, limit: int = 0, workers: int = 8, retry_failed: bool = False, callback=None) -> dict:
    if not 1 <= workers <= 32 or limit < 0:
        raise ValueError("workers 应为 1～32，limit 不能小于 0")
    db = db.resolve()
    if not db.is_file():
        raise ValueError("请先导入试卷，再运行图片本地化")
    with writer_lock(db):
        conn = connect(db, writable=True)
        try:
            statuses = "('pending','failed')" if retry_failed else "('pending')"
            available = conn.execute(f"SELECT COUNT(*) FROM image_asset WHERE status IN {statuses}").fetchone()[0]
            total = min(limit, available) if limit else available
            parents = {Path(r[0]).parent.resolve() for r in conn.execute("SELECT DISTINCT original_path FROM source_file")}
            log = db.parent / "logs" / ("images-" + now().replace(":", "-") + ".jsonl")
            log.parent.mkdir(parents=True, exist_ok=True)
            progress = {"phase": "images", "total": total, "done": 0, "succeeded": 0, "failed": 0,
                        "started_at": now(), "updated_at": now(), "eta_seconds": None,
                        "log_path": str(log), "model_calls": 0}
            start, last = time.monotonic(), ""

            def emit():
                progress["updated_at"] = now()
                progress["eta_seconds"] = round((time.monotonic() - start) / progress["done"] * (total - progress["done"])) if progress["done"] else None
                write_json(db.parent / "logs" / "images-status.json", progress)
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(canonical_json(progress) + "\n")
                if callback:
                    callback(progress)

            emit()
            try:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    while progress["done"] < total:
                        batch = [dict(r) for r in conn.execute(
                            f"SELECT * FROM image_asset WHERE status IN {statuses} AND id>? ORDER BY id LIMIT ?",
                            (last, min(64, total - progress["done"])))]
                        if not batch:
                            break
                        last = batch[-1]["id"]
                        for result in pool.map(lambda row: _fetch(row, db.parent / "images", parents), batch):
                            with conn:
                                conn.execute("""UPDATE image_asset SET status=?,sha256=?,local_path=?,size_bytes=?,mime_type=?,
                                    error=?,attempts=attempts+1,updated_at=? WHERE id=?""",
                                    (result["status"], result.get("sha256"), result.get("local_path"), result.get("size_bytes"),
                                     result.get("mime_type"), result["error"], now(), result["id"]))
                            progress["done"] += 1
                            progress["succeeded" if result["status"] == "ready" else "failed"] += 1
                            emit()
                progress["phase"] = "completed_with_errors" if progress["failed"] else "completed"
                emit()
            except BaseException:
                progress["phase"] = "interrupted"
                emit()
                raise
            return progress
        finally:
            conn.close()
