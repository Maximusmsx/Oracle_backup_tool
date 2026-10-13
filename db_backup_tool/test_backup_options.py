import datetime
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
import main
import db_logic


class BackupOptionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_switch_profile_queries_new_connection(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(main, "CONFIG_FILE", os.path.join(folder, "config.json")):
            window = main.MainWindow()
            window.profiles = {
                "A": {"user": "A", "password": "test", "conn_str": "db-a"},
                "B": {"user": "B", "password": "test", "conn_str": "db-b",
                      "selected_objects": {"TABLE": ["B.B_TABLE", "CWBS.OTHER"]},
                      "table_exports": [{"table": "CWBS.OTHER"}, {"table": "B.B_TABLE"}]},
            }
            window._populate_combo()
            window._load_profile("A")
            old = Mock()
            old.is_connected.return_value = True
            window.db_manager = old
            window.on_profile_activated(1)
            old.close.assert_called_once()
            self.assertIsNone(window.db_manager)
            self.assertIn("未连接", window.lbl_status.text())
            self.assertEqual(window.profiles["B"]["selected_objects"]["TABLE"], ["B_TABLE"])
            self.assertEqual(window.table_data_grid.rowCount(), 1)
            self.assertEqual(window.table_data_grid.item(0, 0).text(), "B_TABLE")
            new = Mock(use_thick=False)
            new.connect.return_value = (True, "OK")
            new.get_objects_by_type.return_value = ["B_TABLE"]
            with patch.object(main, "DatabaseManager", return_value=new) as factory:
                item = next(window.tree.topLevelItem(i) for i in range(window.tree.topLevelItemCount())
                            if window.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == "TABLE")
                window.on_item_expanded(item)
            factory.assert_called_once_with("B", "test", "db-b")
            self.assertEqual(item.child(0).text(0), "B_TABLE")
            self.assertEqual(item.child(0).checkState(0), Qt.CheckState.Checked)
            old.get_objects_by_type.assert_not_called()
            window.close()

    def test_option_persistence_and_both_workers(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(main, "CONFIG_FILE", os.path.join(folder, "config.json")):
            window = main.MainWindow()
            window.combo_profile.setEditText("daily")
            window.dir_path.setText(folder)
            window.daily_subdir.setChecked(True)
            window.git_group.setChecked(True)
            window.git_url.setText("https://example.com/team/backup.git")
            window.git_folder.setText("backups/daily")
            window.git_username.setText("backup")
            window.git_password.setText("example-secret")
            window.save_current_profile()
            window.combo_profile.setEditText("plain")
            window.daily_subdir.setChecked(False)
            window.git_group.setChecked(False)
            window.save_current_profile()
            window.close()
            reloaded = main.MainWindow()
            self.assertFalse(reloaded.daily_subdir.isChecked())
            reloaded._load_profile("daily")
            self.assertTrue(reloaded.daily_subdir.isChecked())
            self.assertTrue(reloaded.git_group.isChecked())
            self.assertEqual(reloaded.git_password.text(), "example-secret")
            self.assertEqual(reloaded.git_folder.text(), "backups/daily")
            with open(main.CONFIG_FILE, encoding="utf-8") as stream:
                self.assertNotIn("example-secret", stream.read())
            self.assertFalse(reloaded._migrate_old_config({"user": "old"})["profiles"]["old"]["daily_subdir"])
            with patch.object(main, "export_ddl_task", return_value="OK") as export:
                worker = main.BackupWorker({}, folder, {}, daily_subdir=True,
                                           git_config=reloaded.profiles["daily"]["git"])
                worker.run()
                self.assertTrue(export.call_args.kwargs["daily_subdir"])
                batch = main.AllProfilesWorker(reloaded.profiles)
                batch.run()
                self.assertEqual([call.kwargs["daily_subdir"] for call in export.call_args_list], [True, True, False])
                self.assertEqual([call.kwargs["git_config"]["enabled"] for call in export.call_args_list], [True, True, False])
            reloaded.close()

    def test_date_directory_and_existing_backup_preserved(self):
        for daily in (False, True):
            with self.subTest(daily=daily), tempfile.TemporaryDirectory() as folder:
                previous = os.path.join(folder, "2000-01-01", "TABLE")
                os.makedirs(previous)
                sentinel = os.path.join(previous, "OLD.sql")
                with open(sentinel, "w") as stream:
                    stream.write("old")
                db = Mock()
                db.connect.return_value = (True, "OK")
                db.get_ddl.return_value = "create table T (id number);"
                def export_data(table, where, path, **kwargs):
                    with open(path, "w") as stream:
                        stream.write("insert into T values (1);")
                    return 1
                db.export_table_data_sql.side_effect = export_data
                with patch.object(db_logic, "DatabaseManager", return_value=db):
                    db_logic.export_ddl_task(
                        {"user": "test", "password": "test", "conn_str": "test"},
                        folder, {"TABLE": ["T"]}, Mock(), Mock(),
                        table_exports=[{"table": "T"}], daily_subdir=daily,
                    )
                target = os.path.join(folder, datetime.date.today().isoformat()) if daily else folder
                self.assertTrue(os.path.isfile(os.path.join(target, "TABLE", "T.sql")))
                self.assertTrue(os.path.isfile(os.path.join(target, "DATA", "T.sql")))
                self.assertTrue(os.path.isfile(sentinel))
                db.close.assert_called_once()

    def test_daily_local_copy_and_git_root_copy_are_independent(self):
        for daily in (False, True):
            for git_enabled in (False, True):
                with self.subTest(daily=daily, git=git_enabled), tempfile.TemporaryDirectory() as folder:
                    db = Mock()
                    db.connect.return_value = (True, "OK")
                    db.get_ddl.return_value = "create table T (id number);"
                    def data_sql(table, where, path, **kwargs):
                        with open(path, "w") as stream:
                            stream.write("insert into T values (1);")
                        return 1
                    db.export_table_data_sql.side_effect = data_sql
                    with patch.object(db_logic, "DatabaseManager", return_value=db), patch("git_backup.upload_backup", return_value="Git OK") as upload:
                        db_logic.export_ddl_task(
                            {"user": "test", "password": "test", "conn_str": "test"},
                            folder, {"TABLE": ["T"]}, Mock(), Mock(),
                            daily_subdir=daily, git_config={"enabled": git_enabled},
                            table_exports=[{"table": "T"}],
                        )
                    dated = os.path.join(folder, datetime.date.today().isoformat())
                    for kind in ("TABLE", "DATA"):
                        root_file = os.path.join(folder, kind, "T.sql")
                        dated_file = os.path.join(dated, kind, "T.sql")
                        self.assertEqual(os.path.isfile(root_file), git_enabled or not daily)
                        self.assertEqual(os.path.isfile(dated_file), daily)
                        if daily and git_enabled:
                            with open(root_file, "rb") as root_stream, open(dated_file, "rb") as dated_stream:
                                self.assertEqual(root_stream.read(), dated_stream.read())
                    if git_enabled:
                        self.assertEqual(upload.call_args.args[0], folder)
                        self.assertNotIn("date_folder", upload.call_args.kwargs)
                    else:
                        upload.assert_not_called()

    def test_manual_git_upload_always_uses_root(self):
        with patch.object(main, "upload_backup", return_value="OK") as upload:
            worker = main.GitUploadWorker("root", {"enabled": True})
            worker.run()
            self.assertEqual(upload.call_args.args[0], "root")
            self.assertNotIn("date_folder", upload.call_args.kwargs)

    def test_git_root_sql_snapshot_matches_date_without_stale_files(self):
        from pathlib import Path
        from git_backup import collect_backup_files
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "PROCEDURE").mkdir()
            (root / "PROCEDURE" / "OLD.sql").write_text("old")
            (root / "PROCEDURE" / "notes.txt").write_text("keep")
            old_date = root / "2000-01-01" / "TABLE"
            old_date.mkdir(parents=True)
            (old_date / "OLD.sql").write_text("archive")
            db = Mock()
            db.connect.return_value = (True, "OK")
            db.get_ddl.return_value = "create table T (id number);"
            with patch.object(db_logic, "DatabaseManager", return_value=db), patch("git_backup.upload_backup", return_value="OK"):
                db_logic.export_ddl_task({"user": "test", "password": "test", "conn_str": "test"},
                                         folder, {"TABLE": ["T"]}, Mock(), Mock(),
                                         daily_subdir=True, git_config={"enabled": True})
            date = root / datetime.date.today().isoformat()
            self.assertEqual(collect_backup_files(root), collect_backup_files(date))
            self.assertFalse((root / "PROCEDURE" / "OLD.sql").exists())
            self.assertTrue((root / "PROCEDURE" / "notes.txt").exists())
            self.assertTrue((old_date / "OLD.sql").exists())


if __name__ == "__main__":
    unittest.main()
