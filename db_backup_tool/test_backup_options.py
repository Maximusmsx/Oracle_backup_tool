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
                "B": {"user": "B", "password": "test", "conn_str": "db-b", "selected_objects": {"TABLE": ["B_TABLE"]}},
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
            window.save_current_profile()
            window.combo_profile.setEditText("plain")
            window.daily_subdir.setChecked(False)
            window.save_current_profile()
            window.close()
            reloaded = main.MainWindow()
            self.assertFalse(reloaded.daily_subdir.isChecked())
            reloaded._load_profile("daily")
            self.assertTrue(reloaded.daily_subdir.isChecked())
            self.assertFalse(reloaded._migrate_old_config({"user": "old"})["profiles"]["old"]["daily_subdir"])
            with patch.object(main, "export_ddl_task", return_value="OK") as export:
                worker = main.BackupWorker({}, folder, {}, daily_subdir=True)
                worker.run()
                self.assertTrue(export.call_args.kwargs["daily_subdir"])
                batch = main.AllProfilesWorker(reloaded.profiles)
                batch.run()
                self.assertEqual([call.kwargs["daily_subdir"] for call in export.call_args_list], [True, True, False])
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


if __name__ == "__main__":
    unittest.main()
