import os
import unittest
from unittest.mock import Mock, patch

import main


class ComparisonOpenTest(unittest.TestCase):
    def test_open_report_and_handle_failures(self):
        path = os.path.abspath("中文目录/此次修改说明.md")
        for success, opened in ((True, True), (True, False), (False, False)):
            with self.subTest(success=success, opened=opened), \
                    patch.object(main.QDesktopServices, "openUrl", return_value=opened) as opener, \
                    patch.object(main.QMessageBox, "warning") as warning:
                window = Mock()
                message = path if success else "生成失败"
                main.MainWindow.on_comparison_finished(window, success, message)
                if success:
                    self.assertEqual(opener.call_args.args[0].toLocalFile(), path.replace("\\", "/"))
                    window.log_output.append.assert_not_called()
                else:
                    opener.assert_not_called()
                    window.log_output.append.assert_called_once_with(message)
                self.assertEqual(warning.call_count, int(not success or not opened))
                if warning.called:
                    self.assertIn(message, warning.call_args.args[2])
                for button in (window.btn_compare, window.btn_export,
                               window.btn_all_export, window.btn_git_upload):
                    button.setEnabled.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
