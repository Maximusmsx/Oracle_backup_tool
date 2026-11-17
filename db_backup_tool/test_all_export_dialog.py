import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import Qt, QTimer
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
        dialog = main.AllExportDialog(self.profiles())
        self.assertEqual(set(states for states in self.states(dialog).values()),
                         {Qt.CheckState.Checked})

    def test_select_all_and_invert(self):
        dialog = main.AllExportDialog(self.profiles())
        dialog.tree.topLevelItem(1).setCheckState(0, Qt.CheckState.Unchecked)
        dialog._invert_selection()
        self.assertEqual(dialog.selected_names(), ["配置2"])
        dialog._set_all(Qt.CheckState.Checked)
        self.assertEqual(len(dialog.selected_names()), 4)
        dialog._invert_selection()
        self.assertEqual(dialog.selected_names(), [])
        dialog._invert_selection()
        self.assertEqual(len(dialog.selected_names()), 4)

    def test_commit_message_is_stripped(self):
        dialog = main.AllExportDialog(self.profiles())
        dialog.message_edit.setPlainText("  更新说明  \n")
        self.assertEqual(dialog.commit_message(), "更新说明")


class ExportSelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_confirm_closes_dialog_and_starts_only_selected_profiles(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(main, "CONFIG_FILE", os.path.join(folder, "config.json")):
            window = main.MainWindow()
            window.profiles = {"A": make_profile(folder, git_enabled=True),
                               "B": make_profile(folder, git_enabled=True)}
            dialog = main.AllExportDialog(window.profiles, window)
            dialog.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Unchecked)
            QTimer.singleShot(0, dialog.accept)
            with patch.object(main, "AllExportDialog", return_value=dialog), \
                 patch.object(main, "AllProfilesWorker") as factory, \
                 patch.object(window, "_prompt_git_message") as prompt:
                factory.return_value.isRunning.return_value = False
                window.start_all_export()
                self.assertFalse(dialog.isVisible())
                profiles = factory.call_args.args[0]
                self.assertEqual(list(profiles), ["B"])
                self.assertEqual(profiles["B"]["git"]["commit_message"], "")
                factory.return_value.start.assert_called_once()
                prompt.assert_not_called()
            window.close()

    def test_cancel_or_empty_selection_does_not_start_backup(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(main, "CONFIG_FILE", os.path.join(folder, "config.json")):
            window = main.MainWindow()
            window.profiles = {"A": make_profile(folder)}
            for result in (main.QDialog.DialogCode.Rejected, main.QDialog.DialogCode.Accepted):
                dialog = Mock()
                dialog.exec.return_value = result
                dialog.selected_names.return_value = []
                with patch.object(main, "AllExportDialog", return_value=dialog), \
                     patch.object(main, "AllProfilesWorker") as factory, \
                     patch.object(main.QMessageBox, "warning"):
                    window.start_all_export()
                    factory.assert_not_called()
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

    def test_git_batch_generates_after_upload_without_opening(self):
        profiles = {"A": make_profile("/tmp/a", git_enabled=True),
                    "B": make_profile("/tmp/b", git_enabled=True)}

        def fake_export(*args, **kwargs):
            kwargs["on_git_uploaded"]()
            return "上传成功"

        with patch.object(main, "export_ddl_task", side_effect=fake_export), \
             patch.object(main, "generate_comparison", return_value="/tmp/report.md") as compare, \
             patch.object(main.QDesktopServices, "openUrl") as opener:
            _, _, results = self.run_worker(profiles)
        self.assertEqual(compare.call_count, 2)
        opener.assert_not_called()
        self.assertTrue(results[0][0])

    def test_git_cancel_does_not_generate_report(self):
        with patch.object(main, "export_ddl_task", return_value="已取消上传"), \
             patch.object(main, "generate_comparison") as compare:
            self.run_worker({"A": make_profile("/tmp/a", daily=True, git_enabled=True)})
        compare.assert_not_called()


class CurrentProfileWorkerTest(unittest.TestCase):
    def test_upload_then_report_signal_and_comparison_failure(self):
        def fake_export(*args, **kwargs):
            kwargs["on_git_uploaded"]()
            return "上传成功"

        for error in (None, Exception("没有历史版本")):
            worker = main.BackupWorker({}, "/tmp/a", {}, git_config={"enabled": True})
            reports, results, logs = [], [], []
            worker.report_ready.connect(reports.append)
            worker.finished.connect(lambda ok, msg: results.append(ok))
            worker.log.connect(logs.append)
            with patch.object(main, "export_ddl_task", side_effect=fake_export), \
                 patch.object(main, "generate_comparison", return_value="/tmp/report.md", side_effect=error):
                worker.run()
            self.assertEqual(reports, [] if error else ["/tmp/report.md"])
            self.assertEqual(results, [True])
            if error:
                self.assertTrue(any("修改说明生成失败" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
