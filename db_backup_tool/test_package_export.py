import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
import db_logic
import main
from backup_compare import generate_comparison
from git_backup import collect_backup_files


class PackageExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_no_body_selection_and_legacy_all_selection_becomes_package(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(main, "CONFIG_FILE", str(Path(folder) / "config.json")):
            window = main.MainWindow()
            window.profiles = {"old": {"user": "X", "selected_objects": {
                "PACKAGE": ["X.P"], "PACKAGE BODY": "__ALL__", "TABLE": ["T"],
            }}}
            window._load_profile("old")
            kinds = [window.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                     for i in range(window.tree.topLevelItemCount())]
            self.assertNotIn("PACKAGE BODY", kinds)
            self.assertEqual(window.profiles["old"]["selected_objects"]["PACKAGE"], "__ALL__")
            self.assertNotIn("PACKAGE BODY", window.profiles["old"]["selected_objects"])
            self.assertEqual(window.profiles["old"]["selected_objects"]["TABLE"], ["T"])
            package = next(window.tree.topLevelItem(i) for i in range(window.tree.topLevelItemCount())
                           if window.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == "PACKAGE")
            self.assertEqual(package.checkState(0), Qt.CheckState.Checked)
            window.close()

    def test_legacy_body_only_and_combined_selections_export_package_once(self):
        for selection, expected in (({"PACKAGE BODY": ["P"]}, ["P"]),
                                    ({"PACKAGE": ["P"], "PACKAGE BODY": ["P", "Q"]}, ["P", "Q"]),
                                    ({"PACKAGE": ["X.P"], "PACKAGE BODY": ["P", "Q"]}, ["P", "Q"]),
                                    ({"PACKAGE BODY": None}, ["P", "Q"])):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as folder:
                db = Mock()
                db.connect.return_value = (True, "OK")
                db.get_objects_by_type.return_value = ["P", "Q"]
                combined = "CREATE OR REPLACE PACKAGE P AS END;\n/\nCREATE OR REPLACE PACKAGE BODY P AS END;\n/\n"
                db.get_ddl.return_value = combined
                with patch.object(db_logic, "DatabaseManager", return_value=db):
                    db_logic.export_ddl_task({"user": "X", "password": "test", "conn_str": "test"},
                                             folder, selection, Mock(), Mock())
                self.assertEqual([call.args for call in db.get_ddl.call_args_list],
                                 [("PACKAGE", name) for name in expected])
                self.assertFalse((Path(folder) / "PACKAGE_BODY").exists())
                self.assertEqual((Path(folder) / "PACKAGE" / "P.sql").read_text(encoding="utf-8"), combined)

    def test_default_export_does_not_query_or_export_body_category(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Mock()
            db.connect.return_value = (True, "OK")
            db.get_objects_by_type.side_effect = lambda kind: ["P"] if kind == "PACKAGE" else []
            db.get_ddl.return_value = "package spec and body"
            with patch.object(db_logic, "DatabaseManager", return_value=db):
                db_logic.export_ddl_task({"user": "X", "password": "test", "conn_str": "test"},
                                         folder, {}, Mock(), Mock())
            self.assertNotIn(("PACKAGE BODY",), [call.args for call in db.get_objects_by_type.call_args_list])
            db.get_ddl.assert_called_once_with("PACKAGE", "P")

    def test_get_ddl_uses_combined_package_metadata_type_unchanged(self):
        db = db_logic.DatabaseManager("X", "test", "test")
        cursor = Mock()
        combined = "CREATE PACKAGE P AS END;\nCREATE PACKAGE BODY P AS END;\n"
        cursor.fetchone.return_value = (combined,)
        db.conn = Mock()
        db.conn.cursor.return_value = cursor
        with patch.object(db, "_set_transform_params"):
            self.assertEqual(db.get_ddl("PACKAGE", "P"), combined)
        self.assertEqual(cursor.execute.call_args.args[1], ["PACKAGE", "P"])
        cursor.close.assert_called_once()

    def test_old_backup_body_duplicates_are_ignored_and_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for date, number in (("2026-10-13", 1), ("2026-10-14", 2)):
                for kind in ("PACKAGE", "PACKAGE_BODY"):
                    path = root / date / kind / "TAX_UNTAX.sql"
                    path.parent.mkdir(parents=True)
                    path.write_text(f"line\nvalue := {number};\n", encoding="utf-8")
            before = {str(path): path.read_bytes() for path in root.rglob("*.sql")}
            self.assertEqual(collect_backup_files(root / "2026-10-14"), ["PACKAGE/TAX_UNTAX.sql"])
            report_path = generate_comparison(root, True, {}, Mock())
            report = Path(report_path).read_text(encoding="utf-8")
            self.assertEqual(report.count("## TAX_UNTAX【修改】"), 1)
            self.assertNotIn("PACKAGE_BODY", report)
            self.assertIn("修改 1 个对象", report)
            self.assertEqual(before, {str(path): path.read_bytes() for path in root.rglob("*.sql")})


if __name__ == "__main__":
    unittest.main()
