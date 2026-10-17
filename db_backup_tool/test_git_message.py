import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication
import main


class GitMessageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = patch.object(main, "CONFIG_FILE", os.path.join(self.temp.name, "config.json"))
        self.config.start()
        self.window = main.MainWindow()
        self.window.dir_path.setText(self.temp.name)
        self.window.u_name.setText("test")
        self.window.u_pwd.setText("test")
        self.window.conn_str.setText("test")

    def tearDown(self):
        for name in ("worker", "all_worker", "git_worker"):
            worker = getattr(self.window, name, None)
            if worker:
                worker.isRunning.return_value = False
        self.window.close()
        self.config.stop()
        self.temp.cleanup()

    def test_current_and_manual_upload_pass_message_and_cancel_before_worker(self):
        for action, worker_name in (("start_export", "BackupWorker"), ("start_git_upload", "GitUploadWorker")):
            for accepted in (True, False):
                with self.subTest(action=action, accepted=accepted), \
                        patch.object(self.window, "_collect_git_config", return_value={"enabled": True}), \
                        patch.object(self.window, "_ensure_table_data_loaded", return_value=True), \
                        patch.object(main.QInputDialog, "getMultiLineText", return_value=(" 更新说明\n第二行 ", accepted)) as dialog, \
                        patch.object(main, worker_name) as factory:
                    getattr(self.window, action)()
                    dialog.assert_called_once()
                    if accepted:
                        options = (factory.call_args.kwargs["git_config"] if action == "start_export"
                                   else factory.call_args.args[1])
                        self.assertEqual(options["commit_message"], "更新说明\n第二行")
                        factory.return_value.start.assert_called_once()
                    else:
                        factory.assert_not_called()

    def test_batch_asks_once_and_does_not_save_message_in_profiles(self):
        self.window.profiles = {"A": {"export_dir": self.temp.name, "git": {"enabled": True}},
                                "B": {"export_dir": self.temp.name, "git": {"enabled": True}}}
        for accepted in (True, False):
            with self.subTest(accepted=accepted), \
                    patch.object(main.QInputDialog, "getMultiLineText", return_value=("本批次更新", accepted)) as dialog, \
                    patch.object(main, "AllProfilesWorker") as factory:
                self.window.start_all_export()
                dialog.assert_called_once()
                if accepted:
                    for p in factory.call_args.args[0].values():
                        self.assertEqual(p["git"]["commit_message"], "本批次更新")
                else:
                    factory.assert_not_called()
                for p in self.window.profiles.values():
                    self.assertNotIn("commit_message", p["git"])

    def test_local_backup_does_not_prompt_and_icon_loads(self):
        with patch.object(main.QInputDialog, "getMultiLineText") as dialog:
            self.assertTrue(self.window._prompt_git_message({"enabled": False}))
            dialog.assert_not_called()
        self.assertFalse(self.window.windowIcon().isNull())


if __name__ == "__main__":
    unittest.main()
