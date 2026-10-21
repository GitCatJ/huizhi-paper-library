from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from .database import connect, latest_status, summary
from .images import run_images
from .importer import run_import
from .scanner import scan
from .search import questions


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def progress(event):
    done, total = event.get("done", 0), event.get("total", 0)
    if sys.stdout.isatty() or done % 25 == 0 or done == total or event.get("error"):
        line = (f"[{event.get('phase')}] {done}/{total}  成功 {event.get('succeeded',0)}"
                f"  跳过 {event.get('skipped',0)}  失败 {event.get('failed',0)}"
                f"  ETA {event.get('eta_seconds')}秒")
        print(line, end="\r" if sys.stdout.isatty() else "\n", flush=True)


def make_parser():
    parser = argparse.ArgumentParser(description="试卷目录 → 本地 SQLite 题库（不调用 AI 模型）")
    parser.add_argument("--db", type=Path, default=Path("data/library.sqlite3"), help="独立题库数据库位置")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (("scan", "只扫描目录，不写入数据库"), ("import", "增量导入或续跑任务"),
                              ("status", "读取持久任务进度"), ("serve", "打开只读本地查询页面"),
                              ("search", "命令行检索题目"), ("images", "下载并验证图片，支持断点续跑"),
                              ("verify", "检查数据库一致性"), ("export", "导出单套试卷 JSON")):
        sub = commands.add_parser(name, help=description)
        sub.add_argument("--db", type=Path, default=argparse.SUPPRESS)
        if name in ("scan", "import"):
            sub.add_argument("--source", type=Path, required=name == "scan")
            sub.add_argument("--limit", type=int, default=0, help="最多处理的试卷数，0 为全部")
        if name == "import":
            sub.add_argument("--resume", help="继续指定任务，仅重试未完成或失败项")
            sub.add_argument("--force", action="store_true", help="重新解析未变化的试卷")
        elif name == "status":
            sub.add_argument("--job", help="指定任务 ID；默认最近一次导入")
        elif name == "serve":
            sub.add_argument("--port", type=int, default=8913)
        elif name == "search":
            sub.add_argument("query")
            sub.add_argument("--year", type=int)
            sub.add_argument("--region", default="")
            sub.add_argument("--category", default="")
            sub.add_argument("--page", type=int, default=1)
        elif name == "images":
            sub.add_argument("--limit", type=int, default=0)
            sub.add_argument("--workers", type=int, default=8)
            sub.add_argument("--retry-failed", action="store_true")
        elif name == "export":
            sub.add_argument("--paper", required=True)
            sub.add_argument("--output", type=Path, required=True)
    return parser


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        if args.command == "scan":
            plan = scan(args.source, args.limit)
            output({**{k: v for k, v in plan.items() if k != "samples"},
                    "examples": [{k: s[k] for k in ("id", "title", "year", "category", "relative_path")} for s in plan["samples"][:10]],
                    "model_calls": 0})
            return 2 if plan["issues"] else 0
        if args.command == "import":
            if args.resume and (args.source or args.limit or args.force):
                raise ValueError("--resume 使用原任务清单，不能同时指定 --source、--limit 或 --force")
            if not args.resume and not args.source:
                raise ValueError("请提供 --source 目录")
            print("本机解析与数据库写入；不调用模型。扫描/准备任务中…", flush=True)
            result = run_import(args.db, args.source, limit=args.limit, resume=args.resume,
                                force=args.force, callback=progress)
            print()
            output(result)
            return 2 if result["status"] == "completed_with_errors" else 0
        if args.command == "images":
            result = run_images(args.db, limit=args.limit, workers=args.workers,
                                retry_failed=args.retry_failed, callback=progress)
            print()
            output(result)
            return 2 if result["failed"] else 0
        if args.command == "serve":
            from .web import make_server
            server = make_server(args.db, args.port)
            print(f"只读查询页面：http://127.0.0.1:{server.server_port}", flush=True)
            print(f"数据库：{args.db.resolve()}；按 Ctrl+C 停止服务。", flush=True)
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return 0
        with closing(connect(args.db)) as conn:
            if args.command == "status":
                state = latest_status(conn, args.job)
                image_state = args.db.resolve().parent / "logs" / "images-status.json"
                output({"library": summary(conn), "import": state,
                        "images": json.loads(image_state.read_text(encoding="utf-8")) if image_state.is_file() else None})
            elif args.command == "search":
                if args.page < 1:
                    raise ValueError("页码应从 1 开始")
                rows = questions(conn, query=args.query, year=args.year, region=args.region,
                                 category=args.category, page=args.page)
                output({"page": args.page, "has_more": len(rows) > 30, "results": rows[:30]})
            elif args.command == "verify":
                checks = [row[0] for row in conn.execute("PRAGMA quick_check")]
                foreign_keys = [list(row) for row in conn.execute("PRAGMA foreign_key_check")]
                counts = {key: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                          for key, table in (("papers", "paper"), ("questions", "question"), ("materials", "material"))}
                indexed = conn.execute("SELECT COUNT(*) FROM question_search").fetchone()[0]
                ok = checks == ["ok"] and not foreign_keys and counts == summary(conn) and indexed == counts["questions"]
                output({"ok": ok, "sqlite": checks, "foreign_keys": foreign_keys,
                        "counts": counts, "search_entries": indexed, "model_calls": 0})
                return 0 if ok else 2
            elif args.command == "export":
                row = conn.execute("SELECT r.parsed_json FROM paper_revision r JOIN paper p ON p.id=r.paper_id AND p.fingerprint=r.fingerprint WHERE p.id=?", (args.paper,)).fetchone()
                if not row:
                    raise ValueError("试卷不存在")
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as stream:
                    stream.write(json.dumps(json.loads(row[0]), ensure_ascii=False, indent=2))
                print(f"已导出：{args.output.resolve()}")
        return 0
    except KeyboardInterrupt:
        print("\n已中断；已完成的试卷保留。可用 status 查看任务 ID，再用 import --resume 继续。", file=sys.stderr)
        return 130
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
