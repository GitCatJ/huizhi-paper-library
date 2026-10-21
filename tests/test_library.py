import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image
from lxml import html

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from paper_library.database import connect, latest_status, summary, writer_lock
from paper_library.images import run_images
from paper_library.importer import run_import, store_paper
from paper_library.parser import extract_options
from paper_library.scanner import scan
from paper_library.search import question_detail, questions
from paper_library.source_archive import file_hash
from paper_library.web import make_server, render


def objective(root, name="2024_测试_行测_合成试卷", stem="基层治理测试题", image=False):
    folder = root / "行测"
    folder.mkdir(parents=True, exist_ok=True)
    blocks = f'<div class="row"><div class="left">1</div><div class="right"><p>{stem}</p>'
    blocks += '<img src="picture.png" width="120">' if image else ''
    blocks += '<p>A、第一项</p><div>B、第二项</div><p>C、第三项</p><p>D、第四项</p></div></div>'
    path = folder / (name + "_真题.doc")
    path.write_text('<html><body><div>' + blocks + '</div></body></html>', encoding="utf-8-sig")
    (folder / (name + "_答案.doc")).write_text('<html><body><div><p>1、B</p></div></body></html>', encoding="utf-8")
    (folder / (name + "_解析.doc")).write_text('<html><body><div><div class="row"><div class="left">1</div><div class="right"><p>合成解析：第二项正确。</p></div></div></div></body></html>', encoding="utf-8")
    if image:
        Image.new("RGB", (10, 10), "blue").save(folder / "picture.png")
    return path


def subjective(root):
    folder = root / "深层目录" / "申论"
    folder.mkdir(parents=True)
    path = folder / "2023_测试_申论_合成申论_真题.html"
    path.write_text('<html><body><div><h2>给定材料</h2><p>材料1</p><p>合成材料：基层协商提升治理能力。</p>'
                    '<h2>作答要求</h2><p>一、根据材料1，概括治理措施。（20分）</p><p>要求：准确简洁。</p></div></body></html>', encoding="utf-8")
    return path


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "papers"
        self.source.mkdir()
        self.db = self.root / "data" / "library.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_recursive_scanning_and_orphan_reporting(self):
        objective(self.source)
        subjective(self.source)
        (self.source / "orphan_解析.doc").write_text("orphan", encoding="utf-8")
        (self.source / "不支持.pdf").write_bytes(b"%PDF")
        plan = scan(self.source)
        self.assertEqual(2, plan["paper_count"])
        self.assertEqual({"行测", "申论"}, {s["category"] for s in plan["samples"]})
        self.assertEqual({"missing_truth", "unsupported_format"}, {i["reason"] for i in plan["issues"]})
        self.assertFalse(self.db.exists())

    def test_duplicate_component_is_not_silently_selected(self):
        path = objective(self.source)
        path.with_suffix(".html").write_bytes(path.read_bytes())
        plan = scan(self.source)
        self.assertEqual(0, plan["paper_count"])
        self.assertIn("ambiguous_paper", {i["reason"] for i in plan["issues"]})

    def test_limit_and_flat_source_directory(self):
        path = objective(self.source)
        (self.source / path.name).write_bytes(path.read_bytes())
        self.assertEqual(2, scan(self.source)["paper_count"])
        self.assertEqual(1, scan(self.source, 1)["selected_count"])

    def test_import_search_and_source_snapshots(self):
        path = objective(self.source, image=True)
        subjective(self.source)
        result = run_import(self.db, self.source)
        self.assertEqual(("completed", 2), (result["status"], result["succeeded"]))
        with closing(connect(self.db)) as conn:
            self.assertEqual({"papers": 2, "questions": 2, "materials": 1}, summary(conn))
            matches = questions(conn, query="基层治理", year=2024, category="行测")
            self.assertEqual(1, len(matches))
            q = question_detail(conn, matches[0]["id"])
            self.assertEqual("B", q["answer_claims"][0]["answer_value"])
            self.assertIn("第二项正确", q["explanations"][0]["plain_text"])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM image_asset").fetchone()[0])
            for row in conn.execute("SELECT * FROM source_file"):
                self.assertEqual(row["sha256"], file_hash(Path(row["archive_path"])))
            subjective_q = conn.execute("SELECT id FROM question WHERE kind='subjective'").fetchone()[0]
            self.assertEqual(1, len(question_detail(conn, subjective_q)["materials"]))
            self.assertEqual([], conn.execute("PRAGMA foreign_key_check").fetchall())
        original = path.read_bytes()
        path.write_text("changed original", encoding="utf-8")
        with closing(connect(self.db)) as conn:
            archive = conn.execute("SELECT archive_path FROM source_file WHERE original_path=?", (str(path),)).fetchone()[0]
            self.assertEqual(original, Path(archive).read_bytes())

    def test_second_import_skips_unchanged_and_keeps_counts(self):
        objective(self.source)
        run_import(self.db, self.source)
        result = run_import(self.db, self.source)
        self.assertEqual((0, 1), (result["succeeded"], result["skipped"]))
        with closing(connect(self.db)) as conn:
            self.assertEqual(1, summary(conn)["questions"])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM paper_revision").fetchone()[0])

    def test_changed_source_updates_search_and_retains_revision(self):
        objective(self.source, stem="基层治理测试题")
        run_import(self.db, self.source)
        objective(self.source, stem="公共服务新题干")
        run_import(self.db, self.source)
        with closing(connect(self.db)) as conn:
            self.assertEqual(0, len(questions(conn, query="基层治理")))
            self.assertEqual(1, len(questions(conn, query="公共服务")))
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM paper_revision").fetchone()[0])
            self.assertEqual(1, summary(conn)["questions"])

    def test_failed_replacement_rolls_back_every_table(self):
        objective(self.source, stem="原有题干")
        run_import(self.db, self.source)
        objective(self.source, stem="新的题干")
        def broken(*args):
            store_paper(*args)
            raise RuntimeError("模拟提交前失败")
        with patch("paper_library.importer.store_paper", broken):
            result = run_import(self.db, self.source)
        self.assertEqual(1, result["failed"])
        with closing(connect(self.db)) as conn:
            self.assertEqual(1, len(questions(conn, query="原有题干")))
            self.assertEqual(0, len(questions(conn, query="新的题干")))
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM paper_revision").fetchone()[0])
            self.assertEqual(1, summary(conn)["questions"])
        retry = run_import(self.db, resume=result["job_id"])
        self.assertEqual((1, 0), (retry["succeeded"], retry["failed"]))

    def test_interrupt_then_resume_does_not_repeat_completed_papers(self):
        objective(self.source)
        objective(self.source, name="2025_测试_行测_另一份合成试卷")
        def interrupt(event):
            if event["phase"] == "importing" and event["succeeded"] == 1:
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            run_import(self.db, self.source, callback=interrupt)
        with closing(connect(self.db)) as conn:
            state = latest_status(conn)
            self.assertEqual("interrupted", state["status"])
            self.assertEqual(1, summary(conn)["papers"])
        resumed = run_import(self.db, resume=state["job_id"])
        self.assertEqual((2, 0, 0), (resumed["succeeded"], resumed["failed"], resumed["skipped"]))
        with closing(connect(self.db)) as conn:
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM paper_revision").fetchone()[0])

    def test_binary_word_fails_without_creating_a_fake_question(self):
        path = objective(self.source)
        path.write_bytes(b"\xd0\xcf\x11\xe0 binary word")
        result = run_import(self.db, self.source)
        self.assertEqual(("completed_with_errors", 1), (result["status"], result["failed"]))
        with closing(connect(self.db)) as conn:
            self.assertEqual(0, summary(conn)["questions"])

    def test_foreign_database_is_rejected_without_modification(self):
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as conn:
            with conn:
                conn.execute("CREATE TABLE production(secret TEXT)")
                conn.execute("INSERT INTO production VALUES('keep')")
        before = self.db.read_bytes()
        with self.assertRaisesRegex(ValueError, "不是试卷本地题库"):
            connect(self.db, writable=True)
        self.assertEqual(before, self.db.read_bytes())

    def test_writer_lock_prevents_concurrent_import_and_releases(self):
        with writer_lock(self.db):
            with self.assertRaises(ValueError):
                with writer_lock(self.db):
                    pass
        with writer_lock(self.db):
            pass

    def test_local_image_cache_is_verified_and_not_redownloaded(self):
        objective(self.source, image=True)
        run_import(self.db, self.source)
        result = run_images(self.db, workers=2)
        self.assertEqual((1, 0), (result["succeeded"], result["failed"]))
        self.assertEqual(0, run_images(self.db)["total"])
        with closing(connect(self.db)) as conn:
            row = conn.execute("SELECT * FROM image_asset").fetchone()
            self.assertEqual(row["sha256"], file_hash(Path(row["local_path"])))
            self.assertEqual("image/png", row["mime_type"])

    def test_missing_image_is_reported_and_retryable(self):
        path = objective(self.source, image=True)
        path.with_name("picture.png").unlink()
        run_import(self.db, self.source)
        self.assertEqual(1, run_images(self.db)["failed"])
        self.assertEqual(0, run_images(self.db)["total"])
        Image.new("RGB", (5, 5)).save(path.with_name("picture.png"))
        self.assertEqual(1, run_images(self.db, retry_failed=True)["succeeded"])

    def test_two_character_and_fts_search_and_literal_input(self):
        objective(self.source)
        run_import(self.db, self.source)
        with closing(connect(self.db)) as conn:
            self.assertEqual(1, len(questions(conn, query="治理")))
            self.assertEqual(1, len(questions(conn, query="基层治理")))
            self.assertEqual(0, len(questions(conn, query='" OR 1=1 --')))
            self.assertEqual(0, len(questions(conn, query="%")))

    def test_html_is_escaped_and_remote_images_are_not_embedded(self):
        objective(self.source, stem="&lt;script&gt;alert(1)&lt;/script&gt;", image=True)
        run_import(self.db, self.source)
        with closing(connect(self.db)) as conn:
            qid = conn.execute("SELECT id FROM question").fetchone()[0]
            result = render(conn, "/question/" + qid, {})
            self.assertIn("&lt;script&gt;", result)
            self.assertNotIn("<script>", result)
            self.assertNotIn("<img", result)
            self.assertIn("图片待本地化", result)

    def test_http_queries_health_errors_and_loopback_host(self):
        objective(self.source)
        run_import(self.db, self.source)
        server = make_server(self.db, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/api/health") as response:
                self.assertEqual(1, json.load(response)["papers"])
                self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
            for url, code in (("/questions?page=wrong", 400), ("/missing", 404)):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(base + url)
                self.assertEqual(code, caught.exception.code)
                caught.exception.close()
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(base, headers={"Host": "untrusted.example"}))
            self.assertEqual(403, caught.exception.code)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_option_parser_preserves_mixed_blocks_images_and_underlines(self):
        stem, options = extract_options(html.fromstring('<div><p>题干<u>关键字</u></p><p>A、甲</p><div>B、<img src="b.png"></div><p>C、丙</p><p>D、丁</p></div>'), "q")
        self.assertEqual(list("ABCD"), [o["label"] for o in options])
        self.assertEqual(1, options[1]["content"]["image_count"])
        self.assertEqual("关键字", stem["nodes"][0]["marks"][0]["quote"])


if __name__ == "__main__":
    unittest.main()
