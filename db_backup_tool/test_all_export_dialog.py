import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
import main


def make_profile(export_dir, daily=False, git_enabled=False):
    return {
        "user": "U", "password": "P", "conn_str": "db",
        "export_dir": export_dir, "daily_subdir": daily,
        "git": {"enabled": git_enabled},
        "table_exports": [],
    }


class DialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def profiles(self):
        return {
            "配置1": make_profile("/tmp/a", daily=True),
            "配置2": make_profile("/tmp/b", git_enabled=True),
            "配置3": make_profile(""),
            "配置4": make_profile("/tmp/d"),
        }

    def states(self, dialog):
        return {dialog.tree.topLevelItem(i).text(0):
                dialog.tree.topLevelItem(i).checkState(0)
                for i in range(dialog.tree.topLevelItemCount())}

    def test_no_record_defaults_to_all_checked(self):
        dialog = main.AllExportDialog(self.profiles(), None)
        self.assertEqual(set(states for states in self.states(dialog).values()),
                         {Qt.CheckState.Checked})

    def test_last_selection_is_default_checked(self):
        dialog = main.AllExportDialog(self.profiles(), ["配置1", "配置3"])
        states = self.states(dialog)
        self.assertEqual(states["配置1"], Qt.CheckState.Checked)
        self.assertEqual(states["配置3"], Qt.CheckState.Checked)
        self.assertEqual(states["配置2"], Qt.CheckState.Unchecked)
        self.assertEqual(states["配置4"], Qt.CheckState.Unchecked)
        self.assertEqual(dialog.selected_names(), ["配置1", "配置3"])

    def test_select_all_and_none(self):
        dialog = main.AllExportDialog(self.profiles(), ["配置1"])
        dialog._set_all(Qt.CheckState.Unchecked)
        self.assertEqual(dialog.selected_names(), [])
        dialog._set_all(Qt.CheckState.Checked)
        self.assertEqual(len(dialog.selected_names()), 4)

    def test_commit_message_is_stripped(self):
        dialog = main.AllExportDialog(self.profiles(), None)
        dialog.message_edit.setPlainText("  更新说明  \n")
        self.assertEqual(dialog.commit_message(), "更新说明")


class SelectionPersistenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = patch.object(main, "CONFIG_FILE",
                                   os.path.join(self.temp.name, "config.json"))
        self.config.start()

    def tearDown(self):
        self.config.stop()
        self.temp.cleanup()

    def test_selection_roundtrip_through_config_file(self):
        window = main.MainWindow()
        window.profiles = {"A": make_profile("/tmp/a"), "B": make_profile("/tmp/b")}
        window.all_export_last_selected = ["B"]
        window._write_config()
        window.close()

        with open(main.CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["all_export_selected"], ["B"])

        window2 = main.MainWindow()
        self.assertEqual(window2.all_export_last_selected, ["B"])
        window2.close()

    def test_missing_key_means_no_record(self):
        window = main.MainWindow()
        self.assertIsNone(window.all_export_last_selected)
        window.close()


class AllProfilesWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def run_worker(self, profiles):
        worker = main.AllProfilesWorker(profiles)
        progress, logs, results = [], [], []
        worker.progress.connect(progress.append)
        worker.log.connect(logs.append)
        worker.finished.connect(lambda ok, msg: results.append((ok, msg)))
        worker.run()    # 同步执行，便于断言
        return progress, logs, results

    def test_progress_is_overall_and_comparison_runs_per_profile(self):
        profiles = {f"配置{i}": make_profile(f"/tmp/{i}", daily=True) for i in range(5)}

        def fake_export(db_cfg, export_dir, selected, progress_cb, log_cb, **kwargs):
            progress_cb(50)    # 模拟单个配置导出一半
            return "ok"

        with patch.object(main, "export_ddl_task", side_effect=fake_export) as exp, \
             patch.object(main, "generate_comparison", return_value="/tmp/report.md") as cmp:
            progress, logs, results = self.run_worker(profiles)
        self.assertEqual(exp.call_count, 5)
        self.assertEqual(cmp.call_count, 5)
        # 每个配置导出到一半时总进度前进一个份额的 50%：10、30、50、70、90
        self.assertEqual(progress, [10, 30, 50, 70, 90, 100])
        self.assertTrue(results[0][0])

    def test_comparison_skipped_without_daily_or_git(self):
        profiles = {"配置1": make_profile("/tmp/a")}
        with patch.object(main, "export_ddl_task", return_value="ok"), \
             patch.object(main, "generate_comparison") as cmp:
            _, logs, results = self.run_worker(profiles)
        cmp.assert_not_called()
        self.assertTrue(any("跳过修改说明" in line for line in logs))

    def test_comparison_failure_does_not_stop_following_profiles(self):
        profiles = {"配置1": make_profile("/tmp/a", daily=True),
                    "配置2": make_profile("/tmp/b", daily=True)}
        with patch.object(main, "export_ddl_task", return_value="ok"), \
             patch.object(main, "generate_comparison",
                          side_effect=[Exception("没有上一个版本"), None]) as cmp:
            _, logs, results = self.run_worker(profiles)
        self.assertEqual(cmp.call_count, 2)
        self.assertTrue(any("修改说明生成失败" in line for line in logs))
        self.assertTrue(results[0][0])   # 导出本身都成功

    def test_export_failure_counts_as_unsuccessful(self):
        profiles = {"配置1": make_profile("/tmp/a", daily=True)}
        with patch.object(main, "export_ddl_task", side_effect=Exception("连接失败")), \
             patch.object(main, "generate_comparison") as cmp:
            _, logs, results = self.run_worker(profiles)
        cmp.assert_not_called()
        self.assertFalse(results[0][0])


if __name__ == "__main__":
    unittest.main()
