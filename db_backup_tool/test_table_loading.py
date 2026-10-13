import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
import main


class TableLoadingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = patch.object(main, "CONFIG_FILE", os.path.join(self.temp.name, "config.json"))
        self.config.start()
        self.window = main.MainWindow()
        self.window.profiles = {
            "A": {"user": "A", "password": "test", "conn_str": "db-a", "export_dir": self.temp.name, "table_exports": []},
            "B": {"user": "B", "password": "test", "conn_str": "db-b", "export_dir": self.temp.name, "table_exports": []},
        }
        self.window._populate_combo()
        self.window._load_profile("A")

    def tearDown(self):
        self.window.close()
        self.config.stop()
        self.temp.cleanup()

    def manager(self, names):
        db = Mock(use_thick=False)
        db.connect.return_value = (True, "OK")
        db.is_connected.return_value = True
        db.get_objects_by_type.return_value = names
        return db

    def test_open_filter_refresh_and_switch_load_correct_database(self):
        w = self.window
        first = self.manager(["REWARD_PZD2026", "OTHER"])
        second = self.manager(["B_TABLE"])
        with patch.object(main, "DatabaseManager", side_effect=[first, second]) as factory:
            w.tabs.setCurrentIndex(1)
            self.assertEqual(w.table_data_grid.rowCount(), 2)
            w.data_filter_input.setText("reward_pzd2026")
            w.btn_data_filter.click()
            self.assertEqual(sum(not w.table_data_grid.isRowHidden(i) for i in range(2)), 1)
            w.data_filter_input.clear()
            w.apply_data_filter()
            self.assertEqual(sum(not w.table_data_grid.isRowHidden(i) for i in range(2)), 2)
            first.get_objects_by_type.assert_called_once_with("TABLE")
            row = next(i for i in range(2) if w.table_data_grid.item(i, 0).text() == "REWARD_PZD2026")
            w.table_data_grid.item(row, 0).setCheckState(Qt.CheckState.Checked)
            w.table_data_grid.item(row, 1).setText("id > 3")
            w.btn_refresh_tables.click()
            self.assertEqual(w._collect_table_exports(), [{"table": "REWARD_PZD2026", "where": "id > 3"}])
            self.assertEqual(first.get_objects_by_type.call_count, 2)
            w.on_profile_activated(1)
            self.assertEqual(w.table_data_grid.rowCount(), 1)
            self.assertEqual(w.table_data_grid.item(0, 0).text(), "B_TABLE")
            first.close.assert_called_once()
            self.assertEqual(factory.call_args.args, ("B", "test", "db-b"))

    def test_direct_export_loads_list_without_selecting_all_data(self):
        db = self.manager(["REWARD_PZD2026", "OTHER"])
        worker = Mock()
        worker.isRunning.return_value = False
        with patch.object(main, "DatabaseManager", return_value=db), patch.object(main, "BackupWorker", return_value=worker) as factory:
            self.window.start_export()
            self.assertEqual(self.window.table_data_grid.rowCount(), 2)
            self.assertEqual(factory.call_args.kwargs["table_exports"], [])
            worker.start.assert_called_once()

    def test_filter_without_opening_tree_loads_and_reports_no_match(self):
        db = self.manager(["REWARD_PZD2026"])
        with patch.object(main, "DatabaseManager", return_value=db):
            self.window.data_filter_input.setText("missing")
            self.window.apply_data_filter()
            self.assertEqual(self.window.table_data_grid.rowCount(), 1)
            self.assertIn("无匹配", self.window.lbl_table_count.text())


if __name__ == "__main__":
    unittest.main()
