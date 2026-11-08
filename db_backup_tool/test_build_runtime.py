import os
from pathlib import Path
import runpy
import ssl
import tempfile
import unittest
from unittest.mock import patch

import PyInstaller.__main__


class BuildRuntimeTest(unittest.TestCase):
    def test_ssl_runtime_certificate_is_bundled_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            certificate = Path(directory) / "codex-rejected-cas.pem"
            certificate.write_text("runtime certificate", encoding="ascii")
            with patch.object(ssl, "__file__", str(Path(directory) / "ssl.py")):
                result = runpy.run_path(str(Path(__file__).with_name("build_exe.py")))
        self.assertIn(f"--add-data={certificate}{os.pathsep}.", result["params"])

    def test_standard_ssl_runtime_does_not_require_extra_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(ssl, "__file__", str(Path(directory) / "ssl.py")):
                result = runpy.run_path(str(Path(__file__).with_name("build_exe.py")))
        self.assertFalse(any("codex-rejected-cas.pem" in p for p in result["params"]))

    def test_only_oci_runtime_and_license_are_bundled(self):
        with patch("os.path.isfile", return_value=True):
            result = runpy.run_path(str(Path(__file__).with_name("build_exe.py")))
        files = [Path(p.removeprefix("--add-data=").split(os.pathsep)[0]).name
                 for p in result["params"] if p.endswith(os.pathsep + "oracle_client")]
        self.assertEqual(files, ["oci.dll", "oraociei19.dll", "orannzsbb19.dll", "oraons.dll",
                                 "BASIC_LICENSE", "BASIC_README"])

    def test_incomplete_or_wrong_client_is_rejected(self):
        with patch("os.path.isfile", side_effect=lambda p: Path(p).name == "oci.dll"):
            with self.assertRaisesRegex(FileNotFoundError, "oraociei19.dll"):
                runpy.run_path(str(Path(__file__).with_name("build_exe.py")))


if __name__ == "__main__":
    unittest.main()
