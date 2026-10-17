import base64
import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from wsgiref.simple_server import make_server, WSGIRequestHandler

from dulwich.client import get_transport_and_path, HTTPUnauthorized
from dulwich.index import commit_tree
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit
from dulwich.repo import Repo
from dulwich.server import DictBackend
from dulwich.web import make_wsgi_chain
from git_backup import (GitBackupError, upload_backup, protect_secret, reveal_secret,
                        validate_git_config)
import git_backup
import db_logic


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class GitBackupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "remote").mkdir()
        self.repo = Repo.init_bare(str(self.root / "remote"))
        self.backup = self.root / "backup"
        (self.backup / "TABLE").mkdir(parents=True)
        (self.backup / "TABLE" / "旧表.sql").write_text("select 1;", encoding="utf-8")
        (self.backup / "config.json").write_text('{"password": "must-not-upload"}')
        app = make_wsgi_chain(DictBackend({"/repo.git": self.repo}))
        auth = "Basic " + base64.b64encode(b"backup:example-secret").decode()

        def authenticated(environ, start_response):
            if environ.get("HTTP_AUTHORIZATION") != auth:
                start_response("401 Unauthorized", [("WWW-Authenticate", 'Basic realm="test"')])
                return [b"Unauthorized"]
            return app(environ, start_response)

        self.server = make_server("127.0.0.1", 0, authenticated, handler_class=QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.options = {
            "enabled": True, "url": f"http://127.0.0.1:{self.server.server_port}/repo.git",
            "branch": "main", "folder": "backups/学校A", "username": "backup",
            "password_protected": protect_secret("example-secret"),
        }

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.repo.close()
        self.temp.cleanup()

    def seed(self, files):
        entries = []
        for path, content in files.items():
            blob = Blob.from_string(content)
            self.repo.object_store.add_object(blob)
            entries.append((path.encode(), blob.id, 0o100644))
        commit = Commit()
        commit.tree = commit_tree(self.repo.object_store, entries)
        commit.parents = []
        commit.author = commit.committer = b"Test <test@localhost>"
        commit.author_time = commit.commit_time = int(time.time())
        commit.author_timezone = commit.commit_timezone = 0
        commit.message = b"Existing content"
        self.repo.object_store.add_object(commit)
        self.repo.refs[b"refs/heads/main"] = commit.id
        self.repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")

    def remote_files(self, branch=b"refs/heads/main"):
        tree = self.repo[self.repo.refs[branch]].tree
        return {entry.path.decode(): self.repo[entry.sha].data
                for entry in iter_tree_contents(self.repo.object_store, tree)}

    def test_real_http_password_push_no_git_executable_and_noop(self):
        # Any attempt to run git/ssh/hooks is an error, even if Git is installed here.
        with patch("subprocess.Popen", side_effect=AssertionError("External process forbidden")), patch.dict(os.environ, {"PATH": ""}):
            message = upload_backup(str(self.backup), self.options, Mock())
            self.assertIn("上传成功", message)
            files = self.remote_files()
            self.assertEqual(list(files), ["backups/学校A/TABLE/旧表.sql"])
            head = self.repo.refs[b"refs/heads/main"]
            self.assertIn("未变化", upload_backup(str(self.backup), self.options, Mock()))
            self.assertEqual(head, self.repo.refs[b"refs/heads/main"])
        self.assertEqual(reveal_secret(self.options["password_protected"]), "example-secret")
        self.assertNotIn("example-secret", json.dumps(self.options))

    def test_git_pool_follows_system_proxy_and_bypass(self):
        proxies = {"https": "http://127.0.0.1:7890"}
        with patch("git_backup.getproxies", return_value=proxies), \
                patch("git_backup.proxy_bypass", return_value=False), \
                patch("git_backup.ProxyManager") as proxy, \
                patch("git_backup.PoolManager") as direct:
            git_backup.create_git_pool("https://github.com/example/repo.git")
            proxy.assert_called_once()
            self.assertEqual(proxy.call_args.args, (proxies["https"],))
            direct.assert_not_called()
        with patch("git_backup.getproxies", return_value=proxies), \
                patch("git_backup.proxy_bypass", return_value=True), \
                patch("git_backup.ProxyManager") as proxy, \
                patch("git_backup.PoolManager") as direct:
            git_backup.create_git_pool("https://internal.example/repo.git")
            direct.assert_called_once()
            proxy.assert_not_called()

    def test_git_pool_without_proxy_and_scheme_specific_selection(self):
        for proxies in ({}, {"https": "http://127.0.0.1:7890"}):
            with self.subTest(proxies=proxies), \
                    patch("git_backup.getproxies", return_value=proxies), \
                    patch("git_backup.proxy_bypass", return_value=False), \
                    patch("git_backup.PoolManager") as direct:
                git_backup.create_git_pool("http://internal.example/repo.git")
                direct.assert_called_once()

    def test_upload_uses_shared_git_pool(self):
        from urllib3 import PoolManager
        pool = PoolManager(retries=False)
        with patch("git_backup.create_git_pool", return_value=pool) as create:
            self.assertIn("上传成功", upload_backup(self.backup, self.options, Mock()))
        create.assert_called_once_with(self.options["url"].strip())

    def test_custom_commit_message_and_blank_fallback(self):
        options = dict(self.options, commit_message="  修复计税逻辑\n补充测试  ")
        upload_backup(self.backup, options, Mock())
        commit = self.repo[self.repo.refs[b"refs/heads/main"]]
        self.assertEqual(commit.message.decode("utf-8"), "修复计税逻辑\n补充测试")
        (self.backup / "TABLE" / "旧表.sql").write_text("select 2;", encoding="utf-8")
        upload_backup(self.backup, dict(self.options, commit_message="  "), Mock())
        commit = self.repo[self.repo.refs[b"refs/heads/main"]]
        self.assertTrue(commit.message.startswith(b"Oracle backup "))

    def test_preserve_other_folders_dates_and_create_branch(self):
        self.seed({"README.md": b"keep", "backups/学校A/2000-01-01/TABLE/OLD.sql": b"old",
                   "backups/学校A/PROCEDURE/STALE.sql": b"stale"})
        upload_backup(str(self.backup), self.options, Mock())
        files = self.remote_files()
        self.assertEqual(files["README.md"], b"keep")
        self.assertEqual(files["backups/学校A/2000-01-01/TABLE/OLD.sql"], b"old")
        self.assertNotIn("backups/学校A/PROCEDURE/STALE.sql", files)
        old_head = self.repo.refs[b"refs/heads/main"]
        (self.backup / "TABLE" / "旧表.sql").unlink()
        (self.backup / "TABLE" / "新表.sql").write_text("select 2;", encoding="utf-8")
        upload_backup(str(self.backup), self.options, Mock())
        files = self.remote_files()
        self.assertNotIn("backups/学校A/TABLE/旧表.sql", files)
        self.assertIn("backups/学校A/TABLE/新表.sql", files)
        self.assertEqual(self.repo[self.repo.refs[b"refs/heads/main"]].parents, [old_head])
        self.options["branch"] = "backups/test"
        upload_backup(str(self.backup), self.options, Mock())
        self.assertIn("README.md", self.remote_files(b"refs/heads/backups/test"))

    def test_invalid_config_wrong_password_and_remote_race(self):
        for change in ({"url": "ssh://server/repo"}, {"url": "https://user:secret@server/repo"},
                       {"folder": "../escape"}, {"folder": "/root"}, {"branch": "bad..name"}):
            with self.subTest(change=change), self.assertRaises(GitBackupError):
                validate_git_config(dict(self.options, **change))
        wrong = dict(self.options, password_protected=protect_secret("bad-password"))
        with self.assertRaises(GitBackupError) as error:
            upload_backup(str(self.backup), wrong, Mock())
        self.assertNotIn("bad-password", str(error.exception))
        self.assertFalse(b"refs/heads/main" in self.repo.refs)

        factory = get_transport_and_path

        def race(*args, **kwargs):
            client, path = factory(*args, **kwargs)
            client.send_pack = lambda path, update, generate: update({b"refs/heads/main": b"f" * 40})
            return client, path

        with patch("git_backup.get_transport_and_path", side_effect=race), self.assertRaisesRegex(GitBackupError, "远端分支已变化"):
            upload_backup(str(self.backup), self.options, Mock())
        self.assertFalse(b"refs/heads/main" in self.repo.refs)

    def test_github_authentication_guidance(self):
        options = dict(self.options, url="https://github.com/example/backup.git")
        with self.assertRaisesRegex(GitBackupError, "不是登录邮箱"):
            validate_git_config(dict(options, username="user@example.com"))
        client = Mock()
        client.fetch.side_effect = HTTPUnauthorized(None, "https://example.com/sensitive-error-text")
        with patch("git_backup.get_transport_and_path", return_value=(client, "/backup.git")):
            with self.assertRaisesRegex(GitBackupError, "不能使用登录密码") as error:
                upload_backup(str(self.backup), options, Mock())
            self.assertNotIn("sensitive-error-text", str(error.exception))

    def test_cancelled_or_invalid_ddl_never_autouploads(self):
        db = Mock()
        db.connect.return_value = (True, "OK")
        db.get_ddl.return_value = "-- Error getting DDL"
        with patch.object(db_logic, "DatabaseManager", return_value=db):
            with self.assertRaisesRegex(Exception, "未自动上传"):
                db_logic.export_ddl_task({"user": "test", "password": "test", "conn_str": "test"},
                                         str(self.backup), {"TABLE": ["T"]}, Mock(), Mock(), git_config=self.options)
            self.assertTrue((self.backup / "TABLE" / "T.sql").exists())
            self.assertFalse(b"refs/heads/main" in self.repo.refs)
            with patch("git_backup.upload_backup") as upload:
                message = db_logic.export_ddl_task({"user": "test", "password": "test", "conn_str": "test"},
                                                   str(self.backup), {"TABLE": ["T"]}, Mock(), Mock(),
                                                   git_config=self.options, should_stop=lambda: True)
                self.assertIn("已中止", message)
                upload.assert_not_called()

    def test_successful_backup_autoupload_and_failure_keeps_files(self):
        db = Mock()
        db.connect.return_value = (True, "OK")
        db.get_ddl.return_value = "create table T (id number);"
        config = {"user": "test", "password": "test", "conn_str": "test"}
        with patch.object(db_logic, "DatabaseManager", return_value=db):
            message = db_logic.export_ddl_task(config, str(self.backup), {"TABLE": ["T"]},
                                               Mock(), Mock(), daily_subdir=True, git_config=self.options)
            self.assertIn("上传成功", message)
            date = datetime.date.today().isoformat()
            self.assertIn("backups/学校A/TABLE/T.sql", self.remote_files())
            self.assertNotIn(f"backups/学校A/{date}/TABLE/T.sql", self.remote_files())
            self.assertEqual((self.backup / "TABLE" / "T.sql").read_bytes(),
                             (self.backup / date / "TABLE" / "T.sql").read_bytes())
            wrong = dict(self.options, password_protected=protect_secret("bad-password"))
            with self.assertRaisesRegex(Exception, "本地备份已完成"):
                db_logic.export_ddl_task(config, str(self.backup), {"TABLE": ["T"]},
                                         Mock(), Mock(), daily_subdir=True, git_config=wrong)
            self.assertTrue((self.backup / date / "TABLE" / "T.sql").exists())
            self.assertTrue((self.backup / "TABLE" / "T.sql").exists())


if __name__ == "__main__":
    unittest.main()
