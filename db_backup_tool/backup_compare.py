"""Read-only SQL snapshot comparison and local Markdown reports."""
import datetime
import difflib
import os
from pathlib import Path, PurePosixPath
import tempfile

from dulwich.client import get_transport_and_path, HTTPUnauthorized
from dulwich.config import ConfigDict
from dulwich.object_store import iter_tree_contents
from dulwich.repo import Repo

from db_logic import OBJECT_CATEGORIES
from git_backup import collect_backup_files, create_git_pool, reveal_secret, validate_git_config


class CompareError(Exception):
    pass


def _text(data, name):
    try:
        return data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        raise CompareError(f"无法读取 UTF-8 SQL 文件：{name}，未生成不完整报告") from None


def _read_snapshot(directory):
    return {name: _text((directory / name).read_bytes(), name)
            for name in collect_backup_files(directory)}


def _check_snapshot(snapshot):
    for name, content in snapshot.items():
        if "-- Error getting DDL" in content or "-- Error getting table extras" in content:
            raise CompareError(f"备份文件包含导出错误：{name}，请重新备份后比较")


def _dated_pair(root):
    dated = []
    for directory in root.iterdir():
        if (not directory.is_dir() or directory.is_symlink()
                or not directory.resolve().is_relative_to(root)):
            continue
        try:
            date = datetime.date.fromisoformat(directory.name)
        except ValueError:
            continue
        if date.isoformat() == directory.name:
            dated.append(directory)
    dated.sort(key=lambda directory: directory.name)
    if len(dated) < 2:
        raise CompareError("无法对比：至少需要两个日期备份目录，才能比较最新目录和上一个日期目录")
    previous, current = dated[-2:]
    if not collect_backup_files(previous) or not collect_backup_files(current):
        raise CompareError("无法对比：最新或上一个日期目录没有备份 SQL 文件，请检查备份是否完成")
    return previous, current


def _git_snapshot(repo, commit_id, prefix):
    folders = {kind.replace(" ", "_") for _, kind in OBJECT_CATEGORIES} | {"DATA"}
    contents = {}
    for entry in iter_tree_contents(repo.object_store, repo[commit_id].tree):
        path = entry.path.decode("utf-8")
        if not path.startswith(prefix):
            continue
        name = path[len(prefix):]
        relative = PurePosixPath(name)
        if len(relative.parts) != 2 or relative.parts[0] not in folders or relative.suffix.lower() != ".sql":
            continue
        if (relative.is_absolute() or relative.as_posix() != name or entry.mode not in (0o100644, 0o100755)
                or any(c in name for c in '\\:*?"<>|') or any(ord(c) < 32 for c in name)
                or any(part in (".", "..") or part.endswith((".", " ")) for part in relative.parts)
                or relative.stem.upper().split(".")[0] in
                {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                 *[f"LPT{i}" for i in range(1, 10)]}):
            raise CompareError("Git 备份包含不合法的 SQL 文件路径，未写入本地")
        contents[name] = repo[entry.sha].data
    return contents


def _download_previous(root, options, current, log_callback):
    validate_git_config(options)
    password = reveal_secret(options.get("password_protected", ""))
    pool = create_git_pool(options["url"].strip())
    try:
        with tempfile.TemporaryDirectory(prefix="oracle_compare_repo_") as directory:
            with Repo.init_bare(directory) as repo:
                client, remote_path = get_transport_and_path(
                    options["url"].strip(), config=ConfigDict(),
                    username=options.get("username") or None, password=password or None,
                    pool_manager=pool,
                )
                log_callback("对比：正在读取 Git 远端分支（只读取，不提交或推送）...")
                fetched = client.fetch(remote_path, repo)
                ref = ("refs/heads/" + options.get("branch", "main").strip()).encode("utf-8")
                commit_id = fetched.refs.get(ref)
                if not commit_id:
                    raise CompareError("无法对比：Git 远端配置分支不存在或尚无提交")
                prefix = options["folder"].strip() + "/"
                contents = _git_snapshot(repo, commit_id, prefix)
                snapshot = {name: _text(data, name) for name, data in contents.items()}
                # After auto-upload the branch tip is the current snapshot. Walk only
                # first parents, skipping commits that did not change this profile.
                if snapshot == current:
                    while snapshot == current:
                        parents = repo[commit_id].parents
                        if not parents:
                            raise CompareError("无法对比：Git 中只有当前备份版本，没有上一个不同的备份版本")
                        commit_id = parents[0]
                        contents = _git_snapshot(repo, commit_id, prefix)
                        snapshot = {name: _text(data, name) for name, data in contents.items()}
                if not snapshot:
                    raise CompareError("无法对比：Git 上一个版本中没有该仓库子目录的备份 SQL 文件")
                _check_snapshot(snapshot)
                compare_root = root / "git_compare"
                if compare_root.is_symlink() or not compare_root.resolve().is_relative_to(root):
                    raise CompareError("git_compare 目录不能链接到备份目录以外的位置")
                compare_root.mkdir(exist_ok=True)
                target = Path(tempfile.mkdtemp(prefix=commit_id.decode()[:12] + "_", dir=compare_root))
                for name, data in contents.items():
                    destination = target / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                log_callback(f"对比：Git 历史 SQL 已下载到 {target}")
                label = f"Git 分支 {options.get('branch', 'main')}，提交 {commit_id.decode()}，子目录 {options['folder']}"
                return snapshot, label, target
    except CompareError:
        raise
    except Exception as error:
        if isinstance(error, HTTPUnauthorized):
            raise CompareError("Git 对比认证失败，请检查用户名及密码／Token") from None
        # Network exception messages can contain credentials; never show them.
        raise CompareError(f"Git 对比失败（{type(error).__name__}），请检查网络、仓库地址和凭据") from None
    finally:
        pool.clear()


def _range(start, end):
    return str(start + 1) if end == start + 1 else f"{start + 1}—{end}"


def _code(lines):
    if not lines:
        return "（无）\n"
    content = "".join(lines)
    fence = "```"
    while fence in content:
        fence += "`"
    suffix = "\n（该片段最后一行在原文件中没有末尾换行符。）\n" if not content.endswith("\n") else ""
    displayed = content if content.endswith("\n") else content + "\n"
    note = f"（该片段为 {len(lines)} 行空白行，以下保留原始内容。）\n\n" if not content.strip() else ""
    return f"{note}{fence}sql\n{displayed}{fence}\n{suffix}"


def _changes(before, after):
    # Match nonblank lines, then map each changed range back to the original file.
    old_rows = [i for i, line in enumerate(before) if line.strip()] + [len(before)]
    new_rows = [i for i, line in enumerate(after) if line.strip()] + [len(after)]
    matcher = difflib.SequenceMatcher(None, [before[i] for i in old_rows[:-1]],
                                     [after[i] for i in new_rows[:-1]], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            yield (tag, old_rows[i1], old_rows[i2 - 1] + 1 if i2 > i1 else old_rows[i1],
                   new_rows[j1], new_rows[j2 - 1] + 1 if j2 > j1 else new_rows[j1])


def render_report(previous, current, previous_label, current_label, now):
    changes = {name: list(_changes(previous[name].splitlines(keepends=True),
                                  current[name].splitlines(keepends=True)))
               for name in previous.keys() & current.keys() if previous[name] != current[name]}
    changed = [name for name in sorted(previous.keys() | current.keys())
               if name not in previous or name not in current or changes.get(name)]
    added = sum(name not in previous for name in changed)
    missing = sum(name not in current for name in changed)
    modified = len(changed) - added - missing
    report = ["数据库对象修改说明\n", f"生成时间：{now:%Y-%m-%d %H:%M:%S}\n",
              f"修改前：{previous_label}\n", f"修改后：{current_label}\n",
              f"汇总：修改 {modified} 个对象，新增 {added} 个对象，当前目录缺失 {missing} 个对象。\n",
              "比较范围：对象类型及 DATA 目录内的 SQL 文件；忽略 BOM、换行格式及空白行增删差异。行号按原文件从 1 开始。\n"]
    if not changed:
        report.append("未发现差异。\n")
        return "\n".join(report)
    if missing:
        report.append("当前目录缺失仅表示文件缺失，可能由导出范围变化导致，不代表数据库对象已删除。\n")
    category = None
    for name in changed:
        folder, obj = name.split("/", 1)
        if folder != category:
            report.append(f"# {folder}\n")
            category = folder
        status = "新增" if name not in previous else "当前目录缺失" if name not in current else "修改"
        report.append(f"## {Path(obj).stem}【{status}】\n")
        report.append(f"文件：`{name}`\n")
        before = previous.get(name, "").splitlines(keepends=True)
        after = current.get(name, "").splitlines(keepends=True)
        if status != "修改":
            lines = after if status == "新增" else before
            report.append(f"{'新增' if status == '新增' else '历史文件'}共 {len(lines)} 行。\n")
            report.append("修改前：\n" + _code(before))
            if status == "新增":
                report.append("修改后：\n" + _code(after))
            continue
        opcodes = changes[name]
        report.append(f"共 {len(opcodes)} 处差异。\n")
        for index, (tag, i1, i2, j1, j2) in enumerate(opcodes, 1):
            if tag == "replace":
                description = f"修改前第 {_range(i1, i2)} 行 → 修改后第 {_range(j1, j2)} 行，替换 {i2-i1} 行为 {j2-j1} 行代码"
            elif tag == "delete":
                description = f"删除修改前第 {_range(i1, i2)} 行，共 {i2-i1} 行代码"
            else:
                description = f"新增修改后第 {_range(j1, j2)} 行，共 {j2-j1} 行代码"
            report.append(f"**差异 {index}：{description}。**\n")
            report.append("修改前：\n" + _code(before[i1:i2]))
            if tag != "delete":
                report.append("修改后：\n" + _code(after[j1:j2]))
    return "\n".join(report)


def generate_comparison(export_dir, daily_subdir, git_options, log_callback):
    if not daily_subdir and not git_options.get("enabled"):
        raise CompareError("无法对比：请勾选“按日建目录”或“备份完成后上传 Git”")
    if not str(export_dir).strip():
        raise CompareError("无法对比：请先选择备份目录")
    root = Path(export_dir).resolve()
    if not root.is_dir():
        raise CompareError("无法对比：备份目录不存在")
    now = datetime.datetime.now()
    if daily_subdir:
        previous_dir, current_dir = _dated_pair(root)
        previous, current = _read_snapshot(previous_dir), _read_snapshot(current_dir)
        previous_label, current_label = str(previous_dir), str(current_dir)
        report_path = current_dir / "此次修改说明.md"
        log_callback(f"对比：{previous_dir.name} → {current_dir.name}")
    else:
        current = _read_snapshot(root)
        if not current:
            raise CompareError("无法对比：备份根目录没有备份 SQL 文件，请先完成备份")
        _check_snapshot(current)
        previous, previous_label, downloaded = _download_previous(root, git_options, current, log_callback)
        previous_label += f"；下载目录 {downloaded}"
        current_label = str(root)
        report_path = root / f"GIT差异描述{now:%Y%m%d-%H-%M-%S}.md"
        if report_path.exists():
            raise CompareError("同一秒已生成 Git 差异报告，请稍后再试")
    _check_snapshot(previous)
    _check_snapshot(current)
    if report_path.is_symlink():
        raise CompareError("差异报告路径不能是符号链接")
    report = render_report(previous, current, previous_label, current_label, now)
    # Replace only the report, after the complete comparison succeeds.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=report_path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(report)
        os.replace(temporary, report_path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    log_callback(f"对比：修改说明已保存到 {report_path}")
    return str(report_path)
