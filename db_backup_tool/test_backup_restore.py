import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
import backup_restore as restore
import main


class RestoreTest(unittest.TestCase):
    def write_sql(self, root, kind, name, text):
        path = Path(root) / kind / (name + ".sql")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_split_package_literals_comments_and_table_extras(self):
        text = '''-- header
CREATE OR REPLACE PACKAGE "OLD"."P" AS PROCEDURE run; END;
/
CREATE OR REPLACE PACKAGE BODY "OLD"."P" AS
PROCEDURE run IS x varchar2(50) := q'[a;'b
/
c]'; BEGIN x := 'it''s;ok'; /* ; / */ NULL; END; END;
/
CREATE TABLE "OLD"."T" (x varchar2(10) DEFAULT ';');
COMMENT ON COLUMN "OLD"."T".x IS 'text;with;semicolons';
CREATE INDEX "OLD"."IX" ON "OLD"."T"(x);
'''
        parts = restore.split_sql(text)
        self.assertEqual(len(parts), 5)
        self.assertTrue(parts[0].endswith("END;"))
        self.assertIn("q'[a;'b\n/\nc]'", parts[1])
        self.assertTrue(parts[2].endswith("DEFAULT ';')"))
        changed, key = restore.prepare_statement(parts[1], "NEW")
        self.assertEqual(key, ("PACKAGE BODY", "P"))
        self.assertIn('PACKAGE BODY "NEW"."P"', changed)
        self.assertIn("q'[a;'b\n/\nc]'", changed)
        self.assertEqual(len(restore.split_sql("CREATE TABLE T(x nvarchar2(40) DEFAULT nq'[it';s]'); COMMENT ON TABLE T IS 'x';")), 2)

    def test_remap_qualifiers_preserves_strings_comments_and_external_schema(self):
        sql = '''CREATE PROCEDURE "OLD"."P" AS BEGIN "OLD"."Q"; OTHER.Q; x := '"OLD"."Q"'; -- "OLD"."Q"
END;'''
        changed, key = restore.prepare_statement(sql, "NEW")
        self.assertIn('CREATE OR REPLACE PROCEDURE "NEW"."P"', changed)
        self.assertIn('BEGIN "NEW"."Q"; OTHER.Q', changed)
        self.assertIn("'\"OLD\".\"Q\"'", changed)
        self.assertIn('-- "OLD"."Q"', changed)
        comment, _ = restore.prepare_statement('COMMENT ON COLUMN "OLD"."T"."C" IS \'x\'', "NEW")
        self.assertIn('"NEW"."T"."C"', comment)

    def test_reject_dangerous_or_unsupported_input(self):
        for sql in ("DROP TABLE T", "DELETE FROM T", "BEGIN NULL; END;", "CREATE PUBLIC SYNONYM S FOR X.T", "CREATE USER X IDENTIFIED BY x", "SET DEFINE OFF"):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                restore.prepare_statement(sql, "NEW")
        for sql in ("CREATE VIEW V AS SELECT 'oops FROM DUAL;", "/* unfinished", "q'[unfinished"):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                restore.split_sql(sql)

    def test_scan_excludes_data_and_duplicate_legacy_bodies(self):
        with tempfile.TemporaryDirectory() as root:
            for kind in ("PACKAGE", "PACKAGE_BODY", "DATA", "JOB"):
                self.write_sql(root, kind, "P", "dummy")
            self.write_sql(root, "PACKAGE_BODY", "Q", "dummy")
            found = restore.scan_backup(root)
            self.assertEqual(list(found), ["PACKAGE", "PACKAGE BODY"])
            self.assertEqual([p.stem for p in found["PACKAGE BODY"]], ["Q"])

    def fake_db(self, valid=True, fail=None):
        db = Mock()
        db.connect.return_value = True, "ok"
        cursor = db.conn.cursor.return_value
        def execute(sql, params=None):
            if fail and sql.startswith(fail):
                raise RuntimeError("ORA-00955: name already used")
            if sql.startswith("SELECT USER"):
                cursor.fetchone.return_value = ("NEW", "NEW")
            elif sql.startswith("SELECT status"):
                cursor.fetchone.return_value = ("VALID" if valid else "INVALID",)
        cursor.execute.side_effect = execute
        cursor.fetchall.return_value = [(3, 5, "PLS-00201 missing dependency")]
        return db, cursor

    def run_restore(self, root, kinds, db):
        with patch.object(restore, "DatabaseManager", return_value=db):
            return restore.restore_backup({"user": "new", "password": "x", "conn_str": "target"},
                                          root, kinds, Mock(), Mock())

    def test_selected_types_only_and_all_specs_before_bodies(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("A", "B"):
                self.write_sql(root, "PACKAGE", name,
                    f'CREATE PACKAGE "OLD"."{name}" AS END;\n/\nCREATE PACKAGE BODY "OLD"."{name}" AS END;\n/\n')
            self.write_sql(root, "FUNCTION", "F", "DROP TABLE T;")
            db, cursor = self.fake_db()
            message = self.run_restore(root, ["PACKAGE"], db)
            ddls = [call.args[0] for call in cursor.execute.call_args_list if call.args[0].startswith("CREATE")]
            self.assertEqual(len(ddls), 4)
            self.assertTrue(all('PACKAGE "NEW"' in sql for sql in ddls[:2]))
            self.assertTrue(all('PACKAGE BODY "NEW"' in sql for sql in ddls[2:]))
            self.assertIn("恢复完成", message)
            cursor.close.assert_called_once()
            db.close.assert_called_once()

    def test_preflight_errors_do_not_connect(self):
        for text in ("-- Error getting DDL for PACKAGE P", "-- comments only", "DROP TABLE T;", "CREATE TABLE T (x number);"):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as root:
                self.write_sql(root, "PACKAGE", "P", text)
                with patch.object(restore, "DatabaseManager") as factory, self.assertRaises(ValueError):
                    restore.restore_backup({"user": "NEW"}, root, ["PACKAGE"], Mock(), Mock())
                factory.assert_not_called()

    def test_failure_stops_and_reports_partial_ddl(self):
        with tempfile.TemporaryDirectory() as root:
            self.write_sql(root, "TABLE", "A", "CREATE TABLE A (x number); CREATE INDEX IX ON A(x);")
            self.write_sql(root, "TABLE", "B", "CREATE TABLE B (x number);")
            db, cursor = self.fake_db(fail="CREATE TABLE B")
            with self.assertRaisesRegex(RuntimeError, "已执行 1/3"):
                self.run_restore(root, ["TABLE"], db)
            self.assertFalse(any("CREATE INDEX" in call.args[0] for call in cursor.execute.call_args_list))
            db.close.assert_called_once()

    def test_invalid_object_is_not_success(self):
        with tempfile.TemporaryDirectory() as root:
            self.write_sql(root, "PROCEDURE", "P", "CREATE PROCEDURE P AS BEGIN MISSING; END;\n/\n")
            db, cursor = self.fake_db(valid=False)
            with self.assertRaisesRegex(RuntimeError, "未通过编译"):
                self.run_restore(root, ["PROCEDURE"], db)
            self.assertTrue(any(call.args[0].startswith("ALTER PROCEDURE") for call in cursor.execute.call_args_list))
            self.assertTrue(any("user_errors" in call.args[0] for call in cursor.execute.call_args_list))

    def test_table_and_selected_indexes_do_not_execute_index_twice(self):
        with tempfile.TemporaryDirectory() as root:
            self.write_sql(root, "TABLE", "T", "CREATE TABLE T(x number); CREATE INDEX IX ON T(x);")
            self.write_sql(root, "INDEX", "IX", "CREATE INDEX IX ON T(x);")
            db, cursor = self.fake_db()
            self.run_restore(root, ["TABLE", "INDEX"], db)
            self.assertEqual(sum(call.args[0].startswith("CREATE INDEX") for call in cursor.execute.call_args_list), 1)

    def test_metadata_modifiers_and_trigger_enable_order(self):
        for sql, kind in (("CREATE UNIQUE INDEX IX ON T(x)", "INDEX"),
                          ("CREATE GLOBAL TEMPORARY TABLE T(x number)", "TABLE"),
                          ("CREATE OR REPLACE FORCE EDITIONABLE VIEW V AS SELECT 1 x FROM DUAL", "VIEW")):
            with self.subTest(sql=sql):
                self.assertEqual(restore.prepare_statement(sql, "NEW")[1][0], kind)
        with tempfile.TemporaryDirectory() as root:
            self.write_sql(root, "TRIGGER", "TR", 'CREATE TRIGGER "OLD"."TR" BEFORE INSERT ON "OLD"."T" BEGIN NULL; END;\n/\nALTER TRIGGER "OLD"."TR" ENABLE;')
            db, cursor = self.fake_db()
            self.run_restore(root, ["TRIGGER"], db)
            calls = [c.args[0] for c in cursor.execute.call_args_list]
            create = next(i for i, sql in enumerate(calls) if sql.startswith("CREATE"))
            enable = next(i for i, sql in enumerate(calls) if sql.endswith("ENABLE"))
            self.assertLess(create, enable)

    def test_constraint_generated_index_is_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            self.write_sql(root, "TABLE", "T", "CREATE TABLE T(x number PRIMARY KEY); CREATE UNIQUE INDEX PK_T ON T(x);")
            db, cursor = self.fake_db()
            execute = cursor.execute.side_effect
            def with_constraint(sql, params=None):
                execute(sql, params)
                if "FROM user_constraints" in sql:
                    cursor.fetchone.return_value = ("T",)
            cursor.execute.side_effect = with_constraint
            self.run_restore(root, ["TABLE"], db)
            self.assertFalse(any(c.args[0].startswith("CREATE UNIQUE INDEX") for c in cursor.execute.call_args_list))

    def test_ui_confirmed_uses_current_configuration_and_restores_controls(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as root, patch.object(main, "CONFIG_FILE", str(Path(root) / "config.json")):
            self.write_sql(root, "FUNCTION", "F", "CREATE FUNCTION F RETURN NUMBER AS BEGIN RETURN 1; END;\n/")
            window = main.MainWindow()
            window.u_name.setText("NEW")
            window.u_pwd.setText("x")
            window.conn_str.setText("target")
            with patch.object(main.QFileDialog, "getExistingDirectory", return_value=root), \
                    patch.object(main, "RestoreDialog") as dialog, \
                    patch.object(main.QMessageBox, "warning", return_value=main.QMessageBox.StandardButton.Yes), \
                    patch.object(main, "RestoreWorker") as worker:
                dialog.return_value.exec.return_value = main.QDialog.DialogCode.Accepted
                dialog.return_value.selected_types.return_value = ["FUNCTION"]
                window.start_restore()
                self.assertEqual(worker.call_args.args, ({"user": "NEW", "password": "x", "conn_str": "target"}, root, ["FUNCTION"]))
                worker.return_value.start.assert_called_once()
                self.assertFalse(window.u_name.isEnabled())
                self.assertFalse(window.btn_export.isEnabled())
                with patch.object(main.QMessageBox, "information"):
                    window.on_restore_finished(True, "ok")
                self.assertTrue(window.u_name.isEnabled())
                self.assertTrue(window.btn_export.isEnabled())
                worker.return_value.isRunning.return_value = False
            window.close()

    def test_restore_ui_type_selection_and_cancel_never_starts_worker(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as root, patch.object(main, "CONFIG_FILE", str(Path(root) / "config.json")):
            self.write_sql(root, "PACKAGE", "P", "CREATE PACKAGE P AS END;\n/\n")
            dialog = main.RestoreDialog(root, restore.scan_backup(root))
            self.assertEqual(dialog.selected_types(), [])
            dialog.types.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
            self.assertEqual(dialog.selected_types(), ["PACKAGE"])
            window = main.MainWindow()
            window.u_name.setText("NEW")
            window.u_pwd.setText("x")
            window.conn_str.setText("target")
            with patch.object(main.QFileDialog, "getExistingDirectory", return_value=root), \
                    patch.object(main.RestoreDialog, "exec", return_value=main.QDialog.DialogCode.Rejected), \
                    patch.object(main, "RestoreWorker") as worker:
                window.start_restore()
                worker.assert_not_called()
            dialog.close()
            window.close()


if __name__ == "__main__":
    unittest.main()
