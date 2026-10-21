"""Read-only local browser interface; no source HTML or remote images execute."""
from __future__ import annotations

import html
import json
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .database import connect, latest_status, summary
from .search import papers, question_detail, questions

PAGE_SIZE = 30

STYLE = """
:root{font-family:system-ui,'Microsoft YaHei',sans-serif;color:#19324a;background:#f4f6f8;line-height:1.7}
*{box-sizing:border-box}body{margin:0}header{background:#143c59;color:#fff;padding:24px max(24px,calc((100% - 1120px)/2))}
header a{color:#fff;text-decoration:none}header h1{font-size:25px;margin:0 0 7px}nav{display:flex;gap:24px}
main{max-width:1168px;margin:24px auto;padding:0 24px}a{color:#17678f;text-underline-offset:3px}
.muted,small{color:#657b8c}.card{background:#fff;border:1px solid #dce4ea;border-radius:12px;padding:22px;margin:14px 0;overflow-wrap:anywhere}
.metrics{display:flex;gap:16px;flex-wrap:wrap}.metric{flex:1;min-width:140px}.metric strong{display:block;font-size:30px}
form{display:flex;flex-wrap:wrap;gap:12px;align-items:end}label{display:flex;flex-direction:column;font-size:14px;gap:4px}
input,select,button{font:inherit;padding:9px 12px;border:1px solid #bccdd8;border-radius:7px;min-width:0}input[name=query]{width:min(340px,75vw)}
button{background:#17678f;color:#fff;cursor:pointer}h2{font-size:22px}h3{font-size:18px;margin-top:0}.tag{display:inline-block;font-size:12px;border-radius:5px;background:#e9f1f6;padding:2px 8px;margin:2px}
.warn{color:#88601b;background:#fff5db;padding:10px 14px;border-radius:7px}.rich{white-space:pre-wrap;overflow-wrap:anywhere}.rich img{max-width:100%;height:auto;vertical-align:middle}
.pending{display:inline-block;border:1px dashed #a5b8c5;padding:4px 9px;font-size:13px;color:#6b7b88}.blank{border-bottom:1px solid;display:inline-block;min-width:5em}
.pager{display:flex;gap:24px;padding:20px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}details{margin:12px 0}dt{font-weight:600}dd{margin:0 0 12px}progress{width:100%}
@media(max-width:540px){main{padding:0 12px}header{padding:20px}.card{padding:16px}form label{max-width:100%}}
"""


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def layout(title, body):
    return (f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)} · 试卷本地题库</title><style>{STYLE}</style><body><header><h1>试卷本地题库</h1>'
            '<nav><a href="/">题库概览</a><a href="/papers">试卷</a><a href="/questions">搜题</a><a href="/status">导入进度</a></nav>'
            f'</header><main>{body}</main></body></html>')


def rich(conn, content):
    parts = []
    keys = list({n.get("asset_key", "") for n in content.get("nodes", []) if n.get("type") == "image"})
    assets = {}
    for offset in range(0, len(keys), 500):
        batch = keys[offset:offset + 500]
        assets.update({r["id"]: r for r in conn.execute(
            "SELECT id,status,local_path FROM image_asset WHERE id IN (" + ','.join('?' for _ in batch) + ')', batch)})
    for node in content.get("nodes", []):
        kind = node.get("type")
        if kind == "text":
            text, cursor = node.get("text", ""), 0
            for mark in node.get("marks", []):
                start, end = mark["start"], mark["end"]
                parts.extend((esc(text[cursor:start]), "<u>" + esc(text[start:end]) + "</u>"))
                cursor = end
            parts.append(esc(text[cursor:]))
        elif kind == "break":
            parts.append("<br>")
        elif kind == "blank":
            parts.append('<span class="blank" aria-label="填写位置">&nbsp;</span>')
        elif kind == "image":
            row = assets.get(node.get("asset_key", ""))
            if row and row["status"] == "ready" and Path(row["local_path"]).is_file():
                width = node.get("declared_width")
                width_attr = f' width="{min(max(width, 1), 1600)}"' if type(width) is int else ""
                parts.append(f'<img src="/assets/{esc(row["id"])}" loading="lazy" alt="{esc(node.get("alt") or "试题图片")}"{width_attr}>')
            else:
                parts.append('<span class="pending">图片待本地化' + ('（下载失败）' if row and row["status"] == "failed" else '') + '</span>')
    return '<div class="rich">' + "".join(parts) + '</div>'


def filters(conn, params, action):
    fields = ['<label>关键词<input name="query" value="' + esc(params.get("query", "")) + '" placeholder="试卷名称或题目内容"></label>']
    for key, label in (("year", "年份"), ("region", "地区"), ("category", "科目")):
        options = [f'<option value="">全部{label}</option>']
        for row in conn.execute(f"SELECT DISTINCT {key} FROM paper WHERE {key} IS NOT NULL ORDER BY {key} DESC"):
            value = str(row[0])
            selected = ' selected' if value == params.get(key, "") else ''
            options.append(f'<option value="{esc(value)}"{selected}>{esc(value)}</option>')
        fields.append(f'<label>{label}<select name="{key}">' + ''.join(options) + '</select></label>')
    return f'<form action="{action}" method="get">' + ''.join(fields) + '<button>查询</button></form>'


def pager(path, params, page, more):
    links = []
    for number, label in ((page - 1, "上一页"), (page + 1, "下一页")):
        if number >= 1 and (number < page or more):
            links.append('<a href="' + esc(path + '?' + urlencode({**params, "page": number})) + '">' + label + '</a>')
    return '<div class="pager">' + ''.join(links) + f'<span>第 {page} 页</span></div>'


def render(conn, path, params):
    if path == "/":
        stats = summary(conn)
        metrics = ''.join(f'<div class="card metric"><span>{label}</span><strong>{stats[key]:,}</strong></div>' for key, label in (("papers", "试卷"), ("questions", "题目"), ("materials", "材料")))
        body = '<h2>你的本地试卷库</h2><div class="metrics">' + metrics + '</div>'
        body += '<div class="card"><h3>检索题目</h3>' + filters(conn, params, '/questions') + '</div>'
        assets = dict(conn.execute("SELECT status,COUNT(*) FROM image_asset GROUP BY status"))
        body += f'<div class="card">图片：已本地化 {assets.get("ready", 0):,} · 待处理 {assets.get("pending", 0):,} · 失败 {assets.get("failed", 0):,}<p class="muted">题目、答案、解析均保留来源。图片本地化后可离线显示。来源缺题、缺选项或材料范围不明确时，会保留待核对标记。</p></div>'
        return layout("概览", body)
    if path in ("/papers", "/questions"):
        page = max(1, min(int(params.get("page", "1")), 100000))
        kwargs = {key: params.get(key, "") for key in ("query", "year", "region", "category")}
        rows = (papers if path == "/papers" else questions)(conn, page=page, **kwargs)
        body = f'<h2>{"浏览试卷" if path == "/papers" else "搜索题目"}</h2><div class="card">' + filters(conn, params, path) + '</div>'
        for row in rows[:PAGE_SIZE]:
            if path == "/papers":
                body += f'<article class="card"><h3><a href="/paper/{esc(row["id"])}">{esc(row["title"])}</a></h3>{esc(row["year"] or "年份未知")} · {esc(row["region"])} · {esc(row["category"])} · {row["question_count"]} 题</article>'
            else:
                body += f'<article class="card"><h3><a href="/question/{esc(row["id"])}">第 {esc(row["source_no"])} 题 · {esc(row["title"])}</a></h3><p>{esc(row["stem_text"][:260])}</p><small>{esc(row["category"])} · {esc(row["region"])} · {esc(row["kind"])}</small></article>'
        if not rows:
            body += '<div class="card">没有找到符合条件的结果。可以缩短关键词或放宽筛选条件。</div>'
        return layout("查询", body + pager(path, params, page, len(rows) > PAGE_SIZE))
    if path.startswith("/paper/"):
        paper_id = path.removeprefix("/paper/")
        row = conn.execute("SELECT * FROM paper WHERE id=?", (paper_id,)).fetchone()
        if not row:
            return None
        body = f'<h2>{esc(row["title"])}</h2><p>{esc(row["year"] or "年份未知")} · {esc(row["region"])} · {esc(row["category"])} · {row["question_count"]} 题</p>'
        flags = json.loads(row["flags_json"])
        if flags:
            body += '<p class="warn">试卷待核对：' + esc('、'.join(flags)) + '</p>'
        body += '<details class="card"><summary>来源文件与原文归档</summary><dl>'
        for source in conn.execute("SELECT * FROM source_file WHERE paper_id=? ORDER BY kind", (paper_id,)):
            body += f'<dt>{esc(source["kind"])}</dt><dd>{esc(source["original_path"])}<br><small>SHA-256：{esc(source["sha256"])}</small></dd>'
        body += '</dl></details>'
        body += '<details class="card"><summary>本卷材料</summary>'
        for material in conn.execute("SELECT * FROM material WHERE paper_id=? ORDER BY ordinal", (paper_id,)):
            body += '<h3>' + esc(material["label"]) + '</h3>' + rich(conn, json.loads(material["content_json"]))
        body += '</details>'
        for q in conn.execute("SELECT id,source_no,section_title,stem_text FROM question WHERE paper_id=? ORDER BY ordinal", (paper_id,)):
            body += f'<article class="card"><a href="/question/{esc(q["id"])}">第 {esc(q["source_no"])} 题 · {esc(q["section_title"])}</a><p>{esc(q["stem_text"][:230])}</p></article>'
        return layout(row["title"], body)
    if path.startswith("/question/"):
        q = question_detail(conn, path.removeprefix("/question/"))
        if not q:
            return None
        body = f'<p><a href="/paper/{esc(q["paper_id"])}">{esc(q["paper"]["title"])}</a></p><h2>第 {esc(q["source_no"])} 题</h2>'
        if q["quality_flags"]:
            body += '<p class="warn">待核对：' + esc('、'.join(q["quality_flags"])) + '</p>'
        for material in q["materials"]:
            body += '<div class="card"><h3>' + esc(material["label"]) + '</h3>' + rich(conn, material["content"]) + '<small>关联依据：' + esc(material["scope_type"]) + '</small></div>'
        body += '<article class="card">' + rich(conn, q["stem"])
        for option in q["options"]:
            body += '<p><strong>' + esc(option["label"]) + '.</strong></p>' + rich(conn, option["content"])
        body += '</article><div class="card"><h3>来源答案与解析</h3>'
        if not q["answer_claims"] and not q["explanations"]:
            body += '<p class="muted">源文件中未识别到对应答案或解析。</p>'
        for answer in q["answer_claims"]:
            body += '<p>来源答案：<strong>' + esc(answer["answer_value"]) + '</strong></p>'
        for explanation in q["explanations"]:
            body += rich(conn, explanation)
        return layout("题目详情", body + '</div>')
    if path == "/status":
        state = latest_status(conn, params.get("job"))
        body = '<h2>导入进度</h2><div class="card">'
        if state["status"] == "no_jobs":
            body += '还没有导入任务。'
        else:
            body += f'<p>状态：{esc(state["status"])} · 阶段：{esc(state.get("phase"))}</p><progress value="{state.get("done", 0)}" max="{max(state.get("total", 0), 1)}"></progress>'
            body += f'<p>已完成 {state.get("done",0)} / {state.get("total",0)} · 成功 {state.get("succeeded",0)} · 跳过 {state.get("skipped",0)} · 失败 {state.get("failed",0)}</p>'
            body += '<dl>' + ''.join(f'<dt>{label}</dt><dd>{esc(state.get(key, ""))}</dd>' for key, label in (("job_id", "任务 ID"), ("current_file", "当前试卷"), ("started_at", "开始时间（UTC）"), ("updated_at", "最近更新（UTC）"), ("eta_seconds", "预计剩余秒数"), ("log_path", "日志位置"))) + '</dl>'
            issues = state.get("scan", {}).get("issues", [])
            if issues:
                body += f'<details><summary>扫描问题 {len(issues)} 项</summary><pre>' + esc(json.dumps(issues[:100], ensure_ascii=False, indent=2)) + '</pre></details>'
            failed = conn.execute("SELECT sample_json,error FROM import_item WHERE job_id=? AND status='failed' LIMIT 100", (state["job_id"],)).fetchall()
            if failed:
                body += '<details open><summary>失败记录（最多显示 100 项）</summary>' + ''.join('<p>' + esc(json.loads(r[0])["relative_path"]) + '<br>' + esc(r[1]) + '</p>' for r in failed) + '</details>'
        return layout("导入进度", body + '<p><a href="/status">刷新进度</a></p><small>进度来自本地持久任务账本。</small></div>')
    return None


def make_server(db: Path, port: int = 8913):
    db = db.resolve()
    with closing(connect(db)) as conn:
        summary(conn)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send(self, status, payload, content_type="text/html; charset=utf-8"):
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            host = self.headers.get("Host", "").split(":", 1)[0]
            if host not in ("127.0.0.1", "localhost"):
                self.send(403, "仅支持本机访问")
                return
            parsed = urlsplit(self.path)
            params = {k: v[-1][:500] for k, v in parse_qs(parsed.query).items()}
            conn = connect(db)
            try:
                if parsed.path == "/api/health":
                    self.send(200, json.dumps({"ok": True, **summary(conn)}), "application/json")
                elif parsed.path == "/api/status":
                    self.send(200, json.dumps(latest_status(conn, params.get("job")), ensure_ascii=False), "application/json; charset=utf-8")
                elif parsed.path.startswith("/assets/"):
                    row = conn.execute("SELECT * FROM image_asset WHERE id=? AND status='ready'", (parsed.path.removeprefix("/assets/"),)).fetchone()
                    if row and Path(row["local_path"]).is_relative_to(db.parent / "images") and Path(row["local_path"]).is_file():
                        self.send(200, Path(row["local_path"]).read_bytes(), row["mime_type"])
                    else:
                        self.send(404, "图片尚未本地化")
                else:
                    result = render(conn, parsed.path, params)
                    self.send(200 if result else 404, result or layout("未找到", '<div class="card">未找到对应内容。</div>'))
            except (ValueError, OverflowError):
                self.send(400, layout("参数错误", '<div class="card">查询参数不正确。</div>'))
            finally:
                conn.close()

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
