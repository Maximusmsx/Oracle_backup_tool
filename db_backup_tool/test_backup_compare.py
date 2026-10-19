import datetime
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication
import backup_compare
from backup_compare import CompareError, generate_comparison, render_report
from git_backup import upload_backup
import main
import test_git_backup


class LocalComparisonTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def sql(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def test_latest_dates_report_layout_line_numbers_and_no_sql_writes(self):
        self.sql("2026-10-12/TABLE/IGNORE.sql", "older\n")
        self.sql("2026-10-13/FUNCTION/F.sql", "start\nv := 1;\nend\n")
        self.sql("2026-10-14/FUNCTION/F.sql", "start\nv := 2;\nend\n")
        self.sql("2026-10-13/TABLE/SAME.sql", "\ufeffsame\r\n")
        self.sql("2026-10-14/TABLE/SAME.sql", "same\n")
        self.sql("2026-10-13/TYPE_BODY/OLD.sql", "old\n")
        self.sql("2026-10-14/TYPE_BODY/NEW.sql", "new\n")
        self.sql("FUNCTION/F.sql", "root must not be compared\n")
        self.sql("git_compare/TABLE/IGNORED.sql", "download\n")
        before = {str(p): p.read_bytes() for p in self.root.rglob("*.sql")}
        # Daily mode takes precedence even if the Git options are invalid.
        with patch.object(backup_compare, "_download_previous", side_effect=AssertionError("No Git in daily mode")):
            output = Path(generate_comparison(str(self.root), True, {"enabled": True}, Mock()))
        self.assertEqual(output, self.root / "2026-10-14" / "此次修改说明.md")
        report = output.read_text(encoding="utf-8")
        self.assertIn("# FUNCTION\n\n## F【修改】", report)
        self.assertIn("修改前第 2 行 → 修改后第 2 行，替换 1 行为 1 行代码", report)
        self.assertIn("```sql\nv := 1;\n```", report)
        self.assertIn("```sql\nv := 2;\n```", report)
        self.assertIn("## OLD【当前目录缺失】", report)
        self.assertIn("## NEW【新增】", report)
        self.assertNotIn("SAME", report)
        self.assertNotIn("IGNORE", report)
        self.assertNotIn("# TABLE", report)
        self.assertIn("修改 1 个对象，新增 1 个对象，当前目录缺失 1 个对象", report)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*.sql")})

    def test_insert_delete_multiple_changes_and_trailing_newline(self):
        old = "a\nremove\nb\nold\nc\n"
        new = "a\nb\nnew\nc\nadded\n"
        report = render_report({"PACKAGE/P.sql": old}, {"PACKAGE/P.sql": new}, "old", "new", datetime.datetime.now())
        self.assertIn("共 3 处差异", report)
        self.assertIn("删除修改前第 2 行", report)
        self.assertIn("修改前第 4 行 → 修改后第 3 行", report)
        self.assertIn("新增修改后第 5 行", report)
        report = render_report({"TABLE/T.sql": "x"}, {"TABLE/T.sql": "x\n"}, "old", "new", datetime.datetime.now())
        self.assertIn("没有末尾换行符", report)
        # SQL containing Markdown fences must remain inside one code block.
        report = render_report({}, {"TABLE/T.sql": "-- ```\n"}, "old", "new", datetime.datetime.now())
        self.assertIn("````sql", report)
        report = render_report({}, {"TABLE/T.sql": "x\n\n\n"}, "old", "new", datetime.datetime.now())
        self.assertIn("```sql\nx\n\n\n```", report)

    def test_identical_snapshots_have_no_object_headings_and_daily_report_can_repeat(self):
        for date in ("2026-10-13", "2026-10-14"):
            self.sql(f"{date}/TABLE/T.sql", "same\n")
        output = Path(generate_comparison(self.root, True, {}, Mock()))
        first = output.read_bytes()
        self.assertIn("未发现差异", first.decode())
        self.assertNotIn("# TABLE", first.decode())
        self.assertEqual(Path(generate_comparison(self.root, True, {}, Mock())), output)
        self.assertFalse(list(output.parent.glob("*.tmp")))

    def test_deleted_code_shows_old_content_without_after_section(self):
        for deleted in ("  RETURN 'old';\n", "  RETURN 'old';\n\n  RETURN 'other';\n"):
            with self.subTest(deleted=repr(deleted)):
                report = render_report(
                    {"FUNCTION/F.sql": "BEGIN\n" + deleted + "END;\n"},
                    {"FUNCTION/F.sql": "BEGIN\nEND;\n"},
                    "old", "new", datetime.datetime.now())
                change = report.split("**差异 1：", 1)[1]
                self.assertIn("删除修改前第 2", change)
                self.assertIn("修改前：\n", change)
                self.assertIn("```sql\n" + deleted + "```", change)
                self.assertNotIn("修改后：", change)
                self.assertNotIn("（无）", change)

        report = render_report({"FUNCTION/F.sql": "old\n"}, {},
                               "old", "new", datetime.datetime.now())
        change = report.split("## F【当前目录缺失】", 1)[1]
        self.assertIn("修改前：\n```sql\nold\n```", change)
        self.assertNotIn("修改后：", change)

    def test_blank_line_changes_do_not_count_as_modified_objects(self):
        for old, new in (("\nBEGIN\n\n  \nEND;\n\n", "BEGIN\nEND;\n"),
                         ("BEGIN\nEND;\n", "\nBEGIN\n\t\nEND;\n\n"),
                         (" \n\t\n", "")):
            with self.subTest(old=repr(old), new=repr(new)):
                report = render_report({"FUNCTION/F.sql": old}, {"FUNCTION/F.sql": new},
                                       "old", "new", datetime.datetime.now())
                self.assertIn("未发现差异", report)
                self.assertIn("修改 0 个对象", report)
                self.assertNotIn("## F", report)

    def test_blank_lines_are_ignored_when_matching_but_original_line_numbers_are_kept(self):
        report = render_report(
            {"FUNCTION/F.sql": "\nBEGIN\n\n  v := 1;\n\nEND;\n"},
            {"FUNCTION/F.sql": "BEGIN\n  v := 2;\nEND;\n\n"},
            "old", "new", datetime.datetime.now())
        self.assertIn("共 1 处差异", report)
        self.assertIn("修改前第 4 行 → 修改后第 2 行，替换 1 行为 1 行代码", report)
        self.assertIn("```sql\n  v := 1;\n```", report)
        self.assertIn("```sql\n  v := 2;\n```", report)
        report = render_report({"FUNCTION/F.sql": "BEGIN\n  v := 1;\nEND;\n"},
                               {"FUNCTION/F.sql": "BEGIN\n v := 1;\nEND;\n"},
                               "old", "new", datetime.datetime.now())
        self.assertIn("共 1 处差异", report)

    def test_invalid_modes_dates_empty_and_bad_sql_preserve_existing_report(self):
        with self.assertRaisesRegex(CompareError, "无法对比"):
            generate_comparison(self.root, False, {}, Mock())
        with self.assertRaisesRegex(CompareError, "两个日期"):
            generate_comparison(self.root, True, {}, Mock())
        self.sql("2026-10-13/TABLE/T.sql", "old\n")
        (self.root / "2026-10-14").mkdir()
        with self.assertRaisesRegex(CompareError, "没有备份 SQL"):
            generate_comparison(self.root, True, {}, Mock())
        self.sql("2026-10-14/TABLE/T.sql", "-- Error getting DDL: failure\n")
        existing = self.root / "2026-10-14" / "此次修改说明.md"
        existing.write_text("keep existing report", encoding="utf-8")
        with self.assertRaisesRegex(CompareError, "导出错误"):
            generate_comparison(self.root, True, {}, Mock())
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep existing report")
        (self.root / "2026-10-14" / "TABLE" / "T.sql").write_bytes(b"\xff")
        with self.assertRaisesRegex(CompareError, "UTF-8"):
            generate_comparison(self.root, True, {}, Mock())
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep existing report")


class GitComparisonTest(unittest.TestCase):
    # Reuse the existing authenticated HTTP Git fixture, without inheriting its tests.
    setUp = test_git_backup.GitBackupTest.setUp
    tearDown = test_git_backup.GitBackupTest.tearDown
    seed = test_git_backup.GitBackupTest.seed
    remote_files = test_git_backup.GitBackupTest.remote_files

    def report(self):
        return Path(generate_comparison(self.backup, False, self.options, Mock()))

    def test_compare_before_upload_downloads_head_without_push_or_git_process(self):
        prefix = self.options["folder"] + "/"
        self.seed({prefix + "TABLE/旧表.sql": b"select 0;",
                   "backups/other/TABLE/OTHER.sql": b"private-other-profile",
                   prefix + "config.json": b"private-config"})
        head = self.repo.refs[b"refs/heads/main"]
        local_before = {p.name: p.read_bytes() for p in self.backup.glob("TABLE/*.sql")}
        with patch("subprocess.Popen", side_effect=AssertionError("No external Git")), patch.dict(os.environ, {"PATH": ""}):
            output = self.report()
        self.assertEqual(output.parent, self.backup)
        self.assertRegex(output.name, r"^GIT差异描述\d{8}-\d{2}-\d{2}-\d{2}\.md$")
        report = output.read_text(encoding="utf-8")
        self.assertIn("select 0;", report)
        self.assertIn("select 1;", report)
        self.assertIn(head.decode(), report)
        downloaded = list((self.backup / "git_compare").glob("*/TABLE/旧表.sql"))
        self.assertEqual(len(downloaded), 1)
        self.assertEqual(downloaded[0].read_bytes(), b"select 0;")
        self.assertFalse(list((self.backup / "git_compare").rglob("OTHER.sql")))
        self.assertFalse(list((self.backup / "git_compare").rglob("config.json")))
        self.assertEqual(head, self.repo.refs[b"refs/heads/main"])
        self.assertEqual(local_before, {p.name: p.read_bytes() for p in self.backup.glob("TABLE/*.sql")})

    def test_after_upload_skips_same_profile_commits_and_compares_previous(self):
        prefix = self.options["folder"] + "/"
        self.seed({prefix + "TABLE/旧表.sql": b"select 0;"})
        previous = self.repo.refs[b"refs/heads/main"]
        upload_backup(self.backup, self.options, Mock())
        # An unrelated profile's newer commit must not become this profile's baseline.
        other = self.root / "other"
        (other / "TABLE").mkdir(parents=True)
        (other / "TABLE" / "X.sql").write_text("other")
        upload_backup(other, dict(self.options, folder="backups/other"), Mock())
        head = self.repo.refs[b"refs/heads/main"]
        report = self.report().read_text(encoding="utf-8")
        self.assertIn(previous.decode(), report)
        self.assertIn("select 0;", report)
        self.assertNotIn("## X", report)
        self.assertEqual(head, self.repo.refs[b"refs/heads/main"])

    def test_no_previous_version_missing_branch_and_auth_error_do_not_write_report(self):
        upload_backup(self.backup, self.options, Mock())
        with self.assertRaisesRegex(CompareError, "没有上一个"):
            self.report()
        self.options["branch"] = "missing"
        with self.assertRaisesRegex(CompareError, "分支不存在"):
            self.report()
        self.options["branch"] = "main"
        self.options["username"] = "invalid"
        with self.assertRaisesRegex(CompareError, "认证失败") as error:
            self.report()
        self.assertNotIn("example-secret", str(error.exception))
        self.assertFalse(list(self.backup.glob("GIT差异描述*.md")))

    def test_absent_profile_and_remote_path_traversal_do_not_download(self):
        self.seed({"backups/other/TABLE/X.sql": b"other"})
        with self.assertRaisesRegex(CompareError, "没有该仓库子目录"):
            self.report()
        self.seed({self.options["folder"] + "/TABLE/..\\escape.sql": b"unsafe"})
        with self.assertRaisesRegex(CompareError, "不合法"):
            self.report()
        self.assertFalse((self.backup / "git_compare").exists())

    def test_deleted_remote_object_and_added_local_object(self):
        prefix = self.options["folder"] + "/"
        self.seed({prefix + "TABLE/旧表.sql": b"select 0;", prefix + "FUNCTION/GONE.sql": b"old"})
        (self.backup / "TABLE" / "新表.sql").write_text("new", encoding="utf-8")
        report = self.report().read_text(encoding="utf-8")
        self.assertIn("## GONE【当前目录缺失】", report)
        self.assertIn("## 新表【新增】", report)
        self.assertIn("## 旧表【修改】", report)

    def test_legacy_git_body_duplicate_is_not_downloaded_or_reported(self):
        prefix = self.options["folder"] + "/"
        old = b"CREATE PACKAGE P AS END;\nCREATE PACKAGE BODY P AS old;\n"
        new = b"CREATE PACKAGE P AS END;\nCREATE PACKAGE BODY P AS new;\n"
        self.seed({prefix + "TABLE/旧表.sql": b"select 1;",
                   prefix + "PACKAGE/P.sql": old, prefix + "PACKAGE_BODY/P.sql": old})
        previous = self.repo.refs[b"refs/heads/main"]
        (self.backup / "PACKAGE").mkdir()
        (self.backup / "PACKAGE" / "P.sql").write_bytes(new)
        upload_backup(self.backup, self.options, Mock())
        head = self.repo.refs[b"refs/heads/main"]
        report = self.report().read_text(encoding="utf-8")
        self.assertIn(previous.decode(), report)
        self.assertEqual(report.count("## P【修改】"), 1)
        self.assertNotIn("PACKAGE_BODY", report)
        self.assertFalse(list((self.backup / "git_compare").rglob("PACKAGE_BODY")))
        self.assertEqual(head, self.repo.refs[b"refs/heads/main"])


class ComparisonUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_button_works_without_database_or_valid_git_and_restores_controls(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "CONFIG_FILE", str(Path(directory) / "config.json")):
            root = Path(directory)
            for date, text in (("2026-10-13", "old\n"), ("2026-10-14", "new\n")):
                path = root / date / "FUNCTION" / "F.sql"
                path.parent.mkdir(parents=True)
                path.write_text(text, encoding="utf-8")
            window = main.MainWindow()
            window.dir_path.setText(directory)
            window.daily_subdir.setChecked(True)
            window.git_group.setChecked(True)
            with patch.object(main, "DatabaseManager", side_effect=AssertionError("No database")), \
                    patch.object(window, "_collect_git_config", side_effect=AssertionError("No Git config needed")), \
                    patch.object(main.QMessageBox, "information") as complete:
                window.btn_compare.click()
                self.assertFalse(window.btn_export.isEnabled())
                self.assertFalse(window.btn_compare.isEnabled())
                deadline = time.monotonic() + 10
                while not complete.called and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                window.compare_worker.wait(1000)
                self.app.processEvents()
                complete.assert_called_once()
            self.assertTrue((root / "2026-10-14" / "此次修改说明.md").is_file())
            self.assertTrue(window.btn_export.isEnabled())
            self.assertTrue(window.btn_compare.isEnabled())
            self.assertTrue(window.btn_all_export.isEnabled())
            self.assertTrue(window.btn_git_upload.isEnabled())
            window.close()

    def test_unchecked_busy_and_failure_messages(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "CONFIG_FILE", str(Path(directory) / "config.json")):
            window = main.MainWindow()
            with patch.object(main.QMessageBox, "warning") as warning, patch.object(main, "CompareWorker") as worker:
                window.btn_compare.click()
                self.assertIn("无法对比", warning.call_args.args[2])
                worker.assert_not_called()
                window.daily_subdir.setChecked(True)
                window.dir_path.setText(directory)
                window.worker = Mock()
                window.worker.isRunning.return_value = True
                window.btn_compare.click()
                worker.assert_not_called()
                del window.worker
                window.on_comparison_finished(False, "无法对比：缺少历史目录")
                self.assertIn("缺少历史目录", warning.call_args.args[2])
                self.assertTrue(window.btn_compare.isEnabled())
            window.close()


if __name__ == "__main__":
    unittest.main()
