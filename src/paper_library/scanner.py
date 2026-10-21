"""Recursive discovery. Filename and component identity remain source evidence."""
from __future__ import annotations

import re
from pathlib import Path

from .utils import stable_id

EXTENSIONS = {".doc", ".html", ".htm"}
DOCUMENTS = EXTENSIONS | {".docx", ".pdf", ".rtf", ".txt"}
PART = re.compile(r"^(.*)_(真题|答案|解析)$")
META = re.compile(r"^(\d{4})_([^_]+)_([^_]+)_(.+)$")
CATEGORIES = {"公共基础", "申论", "行测", "职测", "综合应用"}


def scan(source: Path, limit: int = 0) -> dict:
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"试卷目录不存在：{source}")
    if limit < 0:
        raise ValueError("limit 不能小于 0")
    groups: dict[tuple[str, str], dict] = {}
    issues: list[dict] = []
    files = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in DOCUMENTS:
            continue
        files += 1
        relative = path.relative_to(source)
        if path.suffix.lower() not in EXTENSIONS:
            issues.append({"path": str(relative), "reason": "unsupported_format"})
            continue
        match = PART.fullmatch(path.stem)
        base, kind = match.groups() if match else (path.stem, "真题")
        key = (str(relative.parent), base)
        group = groups.setdefault(key, {"base": base, "parent": relative.parent,
                                        "components": {}, "ambiguous": False})
        if kind in group["components"]:
            group["ambiguous"] = True
            issues.append({"path": str(relative), "reason": "duplicate_component"})
        group["components"][kind] = str(path)
    samples = []
    for group in groups.values():
        components = group["components"]
        if "真题" not in components or group["ambiguous"]:
            issues.append({"path": str(group["parent"] / group["base"]),
                           "reason": "missing_truth" if "真题" not in components else "ambiguous_paper"})
            continue
        match = META.fullmatch(group["base"])
        year, region, exam, title = match.groups() if match else (None, "未分类", "未分类", group["base"])
        parent = group["parent"]
        category = next((part for part in parent.parts if part in CATEGORIES),
                        parent.name if parent.name else source.name)
        identity = str(parent / group["base"])
        samples.append({"id": stable_id("paper", str(source), identity),
                        "truth_path": components["真题"], "components": components,
                        "year": int(year) if year else None, "region": region,
                        "exam": exam, "category": category, "title": title,
                        "base_name": group["base"], "relative_path": identity})
    total = len(samples)
    return {"source_root": str(source), "document_files": files, "paper_count": total,
            "selected_count": min(limit, total) if limit else total,
            "samples": samples[:limit] if limit else samples, "issues": issues}
