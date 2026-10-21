from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any


SPACE_RE = re.compile(r"\s+")
PUNCT_RE = re.compile(r"[\s，。；：、？！,.!?;:\"“”‘’（）()《》〈〉\[\]【】_]+")


def stable_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.blake2b(raw, digest_size=10).hexdigest()
    return f"{prefix}_{digest}"


def normalize_display_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return SPACE_RE.sub(" ", value).strip()


def normalize_match_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    return PUNCT_RE.sub("", value)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Each writer owns its temporary file. Readers see either complete version,
    # and concurrent writers cannot replace or truncate another writer's data.
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temp = Path(stream.name)
            stream.write(payload)
        for attempt in range(8):
            try:
                temp.replace(path)
                break
            except PermissionError as exc:
                # Windows readers/security scanners can briefly deny rename.
                # Do not hide a persistent ACL error or retry other I/O errors.
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 7:
                    raise
                time.sleep(min(0.02 * 2 ** attempt, 0.2))
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
