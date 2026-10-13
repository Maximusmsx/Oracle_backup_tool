"""Git backup over HTTP(S), without Git executables or credential helpers."""
import base64
import ctypes
from ctypes import wintypes
import datetime
from pathlib import Path, PurePosixPath
import tempfile
import time
from urllib.parse import urlsplit

from dulwich.client import get_transport_and_path, HTTPUnauthorized
from dulwich.config import ConfigDict
from dulwich.index import commit_tree
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit
from dulwich.refs import check_ref_format
from dulwich.repo import Repo
from urllib3 import PoolManager, Timeout


class GitBackupError(Exception):
    pass


def _crypt_secret(value, decrypt=False):
    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = DataBlob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise GitBackupError("无法读取或保存 Git 凭据，请在当前 Windows 用户下重新填写密码／Token")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def protect_secret(value):
    return base64.b64encode(_crypt_secret(value.encode("utf-8"))).decode("ascii") if value else ""


def reveal_secret(value):
    if not value:
        return ""
    try:
        return _crypt_secret(base64.b64decode(value, validate=True), decrypt=True).decode("utf-8")
    except Exception:
        raise GitBackupError("Git 凭据无法解密，请在当前 Windows 用户下重新填写密码／Token") from None


def validate_git_config(options):
    if not options.get("enabled"):
        return
    url = urlsplit(options.get("url", "").strip())
    if url.scheme not in ("http", "https") or not url.hostname or not url.path.strip("/"):
        raise GitBackupError("请填写 HTTP／HTTPS Git 仓库地址")
    if url.username is not None or url.password is not None or url.query or url.fragment:
        raise GitBackupError("仓库地址不能包含账号、密码、查询参数或片段，请使用单独的凭据输入框")
    branch = options.get("branch", "main").strip()
    if not branch or not check_ref_format(("refs/heads/" + branch).encode("utf-8")):
        raise GitBackupError("Git 分支名不合法")
    folder = options.get("folder", "").strip()
    parts = folder.split("/")
    if (not folder or "\\" in folder or ":" in folder or
            any(part in ("", ".", "..") or part.lower() == ".git" for part in parts)):
        raise GitBackupError("请填写仓库子目录，例如 backups/学校A；不能使用绝对路径或 ..")
    if ":" in options.get("username", "") or any(ord(c) < 32 for c in folder + branch):
        raise GitBackupError("Git 用户名或路径包含不合法字符")
    if url.hostname.lower() == "github.com" and "@" in options.get("username", ""):
        raise GitBackupError("GitHub 用户名请填写账号名，不是登录邮箱；密码／Token 请填写有仓库写入权限的 Token")


def collect_backup_files(export_dir):
    # Only the application's object folders are eligible, never arbitrary files/configs.
    from db_logic import OBJECT_CATEGORIES
    folders = [kind.replace(" ", "_") for _, kind in OBJECT_CATEGORIES] + ["DATA"]
    root = Path(export_dir).resolve()
    files = []
    for folder in folders:
        directory = root / folder
        if directory.is_symlink():
            continue
        for path in sorted(directory.glob("*.sql")):
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
                files.append(path.relative_to(root).as_posix())
    return files


def upload_backup(export_dir, options, log_callback, files=None, should_stop=None):
    validate_git_config(options)
    if should_stop and should_stop():
        return "Git：已取消上传，本地备份已保留"
    files = collect_backup_files(export_dir) if files is None else files
    if not files:
        raise GitBackupError("没有可上传的备份 SQL 文件")
    root = Path(export_dir).resolve()
    prefix = options["folder"].strip() + "/"
    # Resolve and validate all files before opening a network connection.
    contents = []
    for name in files:
        relative = PurePosixPath(name)
        source = root / name
        if (relative.is_absolute() or len(relative.parts) != 2 or ".." in relative.parts or
                relative.suffix.lower() != ".sql" or source.is_symlink() or
                not source.resolve().is_relative_to(root)):
            raise GitBackupError("备份文件路径不合法")
        data = source.read_bytes()
        if b"-- Error getting DDL" in data or b"-- Error getting table extras" in data:
            raise GitBackupError("备份 SQL 包含导出错误，未自动上传 Git，请检查 SQL 文件")
        contents.append(((prefix + relative.as_posix()).encode("utf-8"), data))
    ref = ("refs/heads/" + options.get("branch", "main").strip()).encode("utf-8")
    password = reveal_secret(options.get("password_protected", ""))
    pool = PoolManager(timeout=Timeout(connect=15, read=30), retries=False)
    try:
        with tempfile.TemporaryDirectory(prefix="oracle_git_") as directory:
            with Repo.init_bare(directory) as repo:
                client, remote_path = get_transport_and_path(
                    options["url"].strip(), config=ConfigDict(),
                    username=options.get("username") or None, password=password or None,
                    pool_manager=pool,
                )
                log_callback("Git：正在获取远端分支...")
                fetched = client.fetch(remote_path, repo)
                expected = fetched.refs.get(ref)
                parent = expected or fetched.refs.get(b"HEAD")
                old_tree = repo[parent].tree if parent else None
                entries = {entry.path: (entry.sha, entry.mode)
                           for entry in iter_tree_contents(repo.object_store, old_tree)}
                from db_logic import OBJECT_CATEGORIES
                folders = {(prefix + kind.replace(" ", "_") + "/").encode("utf-8")
                           for _, kind in OBJECT_CATEGORIES}
                folders.add((prefix + "DATA/").encode("utf-8"))
                entries = {path: value for path, value in entries.items()
                           if not (path.lower().endswith(b".sql") and
                                   any(path.startswith(folder) for folder in folders))}
                for path, data in contents:
                    blob = Blob.from_string(data)
                    repo.object_store.add_object(blob)
                    entries[path] = (blob.id, 0o100644)
                tree = commit_tree(repo.object_store, ((path, sha, mode)
                                                     for path, (sha, mode) in entries.items()))
                if tree == old_tree and expected:
                    return "Git：备份内容未变化，无需提交"
                if should_stop and should_stop():
                    return "Git：已取消上传，本地备份已保留"
                commit = Commit()
                commit.tree = tree
                commit.parents = [parent] if parent else []
                commit.author = commit.committer = b"Oracle Backup Tool <oracle-backup@localhost>"
                commit.author_time = commit.commit_time = int(time.time())
                commit.author_timezone = commit.commit_timezone = 0
                commit.message = ("Oracle backup " + datetime.datetime.now().isoformat(timespec="seconds")).encode()
                repo.object_store.add_object(commit)

                def update_refs(remote_refs):
                    if should_stop and should_stop():
                        raise GitBackupError("已取消上传，本地备份已保留")
                    if remote_refs.get(ref) != expected:
                        raise GitBackupError("远端分支已变化，未覆盖远端；请重新上传")
                    return {ref: commit.id}

                log_callback("Git：正在提交并推送备份...")
                result = client.send_pack(remote_path, update_refs, repo.generate_pack_data)
                if any((result.ref_status or {}).values()):
                    raise GitBackupError("远端拒绝推送，请检查分支保护和账号写入权限")
                return f"Git：上传成功，提交 {commit.id.decode()[:8]}，共 {len(contents)} 个文件"
    except GitBackupError:
        raise
    except Exception as error:
        if isinstance(error, HTTPUnauthorized):
            if urlsplit(options["url"]).hostname.lower() == "github.com":
                raise GitBackupError("GitHub 认证失败：请使用 GitHub 账号名＋有仓库写入权限的 Token，不能使用登录密码；本地备份已保留") from None
            raise GitBackupError("Git 服务认证失败，请检查用户名及密码／Token是否有效；本地备份已保留") from None
        # HTTP errors may contain authentication values; do not display their raw text.
        raise GitBackupError(f"Git 上传失败（{type(error).__name__}），请检查网络、仓库地址和凭据；本地备份已保留") from None
    finally:
        pool.clear()
