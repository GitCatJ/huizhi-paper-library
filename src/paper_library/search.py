"""Bounded result pages. Chinese substring search uses SQLite FTS5 trigram."""
import json


def papers(conn, *, query="", year=None, region="", category="", page=1, page_size=30):
    where, args = [], []
    for name, value in (("year", year), ("region", region), ("category", category)):
        if value not in (None, ""):
            where.append(f"{name}=?")
            args.append(value)
    if query:
        where.append("title LIKE ? ESCAPE '\\'")
        args.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    clause = " WHERE " + " AND ".join(where) if where else ""
    return [dict(r) for r in conn.execute("SELECT * FROM paper" + clause + " ORDER BY year DESC,title,id LIMIT ? OFFSET ?",
                                          (*args, page_size + 1, (page - 1) * page_size))]


def questions(conn, *, query="", year=None, region="", category="", page=1, page_size=30):
    where, args = [], []
    for name, value in (("year", year), ("region", region), ("category", category)):
        if value not in (None, ""):
            where.append(f"p.{name}=?")
            args.append(value)
    join = ""
    tokenizer = conn.execute("SELECT value FROM library_meta WHERE key='search_tokenizer'").fetchone()[0]
    if query:
        if tokenizer == "trigram" and len(query) >= 3:
            join = " JOIN question_search s ON s.question_id=q.id"
            where.append("question_search MATCH ?")
            args.append('"' + query.replace('"', '""') + '"')
        else:
            join = " JOIN question_search s ON s.question_id=q.id"
            where.append("(s.body LIKE ? ESCAPE '\\' OR s.title LIKE ? ESCAPE '\\')")
            needle = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.extend((needle, needle))
    clause = " WHERE " + " AND ".join(where) if where else ""
    sql = "SELECT q.id,q.paper_id,q.ordinal,q.source_no,q.stem_text,q.kind,q.flags_json,p.title,p.year,p.category,p.region FROM question q JOIN paper p ON p.id=q.paper_id" + join + clause
    return [dict(r) for r in conn.execute(sql + " ORDER BY p.year DESC,p.id,q.ordinal LIMIT ? OFFSET ?",
                                         (*args, page_size + 1, (page - 1) * page_size))]


def question_detail(conn, question_id):
    row = conn.execute("SELECT content_json,paper_id FROM question WHERE id=?", (question_id,)).fetchone()
    if not row:
        return None
    result = json.loads(row[0])
    result["paper"] = dict(conn.execute("SELECT * FROM paper WHERE id=?", (row[1],)).fetchone())
    result["materials"] = [{**dict(r), "content": json.loads(r["content_json"])} for r in conn.execute(
        "SELECT m.*,qm.scope_type,qm.confidence FROM question_material qm JOIN material m ON m.id=qm.material_id WHERE qm.question_id=? ORDER BY m.ordinal", (question_id,))]
    return result
