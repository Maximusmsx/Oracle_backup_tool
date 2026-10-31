"""Restore this tool's DDL snapshots (never execute DATA scripts or DROP objects)."""
import re
from pathlib import Path

from db_logic import DatabaseManager


RESTORE_TYPES = (
    "TYPE", "PACKAGE", "FUNCTION", "PROCEDURE", "TYPE BODY", "PACKAGE BODY",
    "TABLE", "VIEW", "MATERIALIZED VIEW", "SEQUENCE", "SYNONYM",
    "INDEX", "TRIGGER", "LIBRARY", "CLUSTER",
)
REPLACE_TYPES = {"TYPE", "TYPE BODY", "PACKAGE", "PACKAGE BODY", "FUNCTION",
                 "PROCEDURE", "VIEW", "SYNONYM", "TRIGGER", "LIBRARY"}
COMPILE_TYPES = {"TYPE", "TYPE BODY", "PACKAGE", "PACKAGE BODY", "FUNCTION",
                 "PROCEDURE", "VIEW", "TRIGGER"}


def sql_tokens(text):
    """Yield (text, start, end); preserve literals/comments, including Oracle q quotes."""
    i = 0
    while i < len(text):
        start = i
        if text[i].isspace():
            i += 1
            continue
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("SQL 块注释未闭合")
            i = end + 2
        elif (text[i:i + 2].lower() == "q'" or text[i:i + 3].lower() == "nq'"):
            prefix = 3 if text[i:i + 3].lower() == "nq'" else 2
            if i + prefix >= len(text):
                raise ValueError("SQL q 字符串未闭合")
            opening = text[i + prefix]
            closing = {"[": "]", "(": ")", "{": "}", "<": ">"}.get(opening, opening)
            end = text.find(closing + "'", i + prefix + 1)
            if end < 0:
                raise ValueError("SQL q 字符串未闭合")
            i = end + 2
        elif text[i] in "'\"":
            quote = text[i]
            i += 1
            while i < len(text):
                if text[i] == quote:
                    i += 1
                    if i < len(text) and text[i] == quote:
                        i += 1
                        continue
                    break
                i += 1
            else:
                raise ValueError("SQL 字符串或标识符未闭合")
        elif text[i].isalnum() or text[i] in "_$#":
            i += 1
            while i < len(text) and (text[i].isalnum() or text[i] in "_$#"):
                i += 1
        else:
            i += 1
        token = text[start:i]
        if not token.startswith(("--", "/*")):
            yield token, start, i


def split_sql(text):
    statements = []
    start = 0
    head = []
    plsql = False
    for token, a, b in sql_tokens(text):
        # SQL*Plus slash terminates PL/SQL, but is never sent to cursor.execute.
        slash = token == "/" and not text[text.rfind("\n", 0, a) + 1:a].strip() and not text[b:text.find("\n", b) if "\n" in text[b:] else len(text)].strip()
        if slash or (token == ";" and not plsql):
            statement = text[start:a].strip()
            if head:
                statements.append(statement)
            start, head, plsql = b, [], False
            continue
        head.append(token.upper())
        if head[0] in ("BEGIN", "DECLARE"):
            plsql = True
        if head[0] == "CREATE" and any(t in head[:8] for t in
                ("PACKAGE", "FUNCTION", "PROCEDURE", "TRIGGER", "TYPE")):
            plsql = True
    if head:
        statements.append(text[start:].strip())
    return statements


def scan_backup(directory):
    root = Path(directory)
    if not root.is_dir():
        raise ValueError("请选择包含对象类型子目录的备份日期目录")
    result = {}
    for kind in RESTORE_TYPES:
        folder = root / kind.replace(" ", "_")
        files = sorted(folder.glob("*.sql")) if folder.is_dir() else []
        files = [p for p in files if p.is_file() and not p.is_symlink()]
        # Older snapshots can contain duplicate bodies as well as combined packages.
        if kind == "PACKAGE BODY":
            files = [p for p in files if not (root / "PACKAGE" / p.name).exists()]
        if files:
            result[kind] = files
    return result


def identifier(token):
    return token[1:-1].replace('""', '"') if token.startswith('"') else token.upper()


def quote_name(name):
    return '"' + name.replace('"', '""') + '"'


def prepare_statement(sql, target_user):
    """Only DDL emitted for supported objects; remap explicit owning-schema qualifiers."""
    tokens = list(sql_tokens(sql))
    words = [t[0].upper() for t in tokens]
    if not words:
        raise ValueError("备份 SQL 为空")
    object_key = None
    source_owner = None
    if words[0] == "CREATE":
        i = 1
        if words[i:i + 2] == ["OR", "REPLACE"]:
            i += 2
        while i < len(words) and words[i] in {
                "EDITIONABLE", "NONEDITIONABLE", "FORCE", "NOFORCE", "UNIQUE",
                "BITMAP", "GLOBAL", "TEMPORARY"}:
            i += 1
        kind = words[i] if i < len(words) else ""
        i += 1
        if words[i:i + 1] == ["BODY"] or kind == "MATERIALIZED":
            kind += " " + words[i]
            i += 1
        if kind not in RESTORE_TYPES or i >= len(tokens):
            raise ValueError("不支持的 CREATE 对象类型")
        name = identifier(tokens[i][0])
        if words[i + 1:i + 2] == ["."]:
            source_owner = name
            name = identifier(tokens[i + 2][0])
        object_key = (kind, name)
        if kind in REPLACE_TYPES and words[1:3] != ["OR", "REPLACE"]:
            sql = sql[:tokens[0][2]] + " OR REPLACE" + sql[tokens[0][2]:]
            tokens = list(sql_tokens(sql))
    elif words[:2] == ["COMMENT", "ON"] and words[2:3] in (["TABLE"], ["COLUMN"]):
        if words[4:5] == ["."] and (words[2] == "TABLE" or words[6:7] == ["."]):
            source_owner = identifier(tokens[3][0])
    elif words[:2] == ["ALTER", "TRIGGER"] and words[-1] in ("ENABLE", "DISABLE"):
        if words[3:4] == ["."]:
            source_owner = identifier(tokens[2][0])
    else:
        raise ValueError("仅允许备份对象 DDL（CREATE、COMMENT ON、ALTER TRIGGER ENABLE/DISABLE）；不执行数据或删除脚本")
    if source_owner:
        for index in range(len(tokens) - 2, -1, -1):
            token, a, b = tokens[index]
            if tokens[index + 1][0] == "." and identifier(token) == source_owner:
                sql = sql[:a] + quote_name(target_user) + sql[b:]
    return sql, object_key


def restore_backup(config, directory, selected_types, progress, log):
    available = scan_backup(directory)
    if not selected_types or any(kind not in available for kind in selected_types):
        raise ValueError("请选择备份目录中存在的对象类型")
    # Read and validate the entire selection before connecting or executing any DDL.
    prepared = []
    for kind in RESTORE_TYPES:
        if kind not in selected_types:
            continue
        for path in available[kind]:
            text = path.read_text(encoding="utf-8-sig")
            if re.search(r"^\s*--\s*Error\b", text, re.I | re.M):
                raise ValueError(f"备份包含导出错误：{path.name}")
            statements = split_sql(text)
            if not statements:
                raise ValueError(f"备份 SQL 为空：{path.name}")
            for statement in statements:
                raw_user = config["user"].strip()
                target_user = identifier(raw_user)
                sql, key = prepare_statement(statement, target_user)
                if key and key[0] != kind and not (kind == "PACKAGE" and key[0] == "PACKAGE BODY") and not (kind == "TABLE" and key[0] == "INDEX"):
                    raise ValueError(f"备份内容与对象目录不符：{path.name}")
                prepared.append((path, sql, key))
    # Package declarations precede all package bodies, even across different files.
    order = {kind: index for index, kind in enumerate(("CLUSTER", "SEQUENCE", "TYPE", "TABLE",
        "SYNONYM", "PACKAGE", "FUNCTION", "PROCEDURE", "TYPE BODY", "PACKAGE BODY",
        "VIEW", "MATERIALIZED VIEW", "INDEX", "TRIGGER", "LIBRARY"))}
    # Keep comments and table extras with the preceding CREATE TABLE statement.
    prepared.sort(key=lambda item: order.get(item[2][0] if item[2] else item[0].parent.name.replace("_", " "), 99))
    db = DatabaseManager(config["user"], config["password"], config["conn_str"])
    completed = 0
    try:
        ok, message = db.connect()
        if not ok:
            raise ValueError(f"数据库连接失败：{message}")
        cursor = db.conn.cursor()
        try:
            cursor.execute("SELECT USER, SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM DUAL")
            user, schema = cursor.fetchone()
            if user != schema or user != identifier(config["user"].strip()):
                raise ValueError("实际连接用户/当前 schema 与恢复目标不一致")
            log(f"恢复目标：{user}@{config['conn_str']}；共 {len(prepared)} 条 DDL")
            restored = set()
            executed_keys = set()
            created_tables = set()
            processed = 0
            for path, sql, key in prepared:
                processed += 1
                # TABLE exports already include their indexes; do not create them twice.
                if key and key[0] == "INDEX" and key in executed_keys:
                    progress(int(processed / len(prepared) * 100))
                    continue
                if key and key[0] == "INDEX" and created_tables:
                    cursor.execute("SELECT table_name FROM user_constraints WHERE index_name = :1 "
                                   "AND constraint_type IN ('P', 'U')", [key[1]])
                    row = cursor.fetchone()
                    if row and row[0] in created_tables:
                        log(f"跳过建表约束已生成的索引：{key[1]}")
                        executed_keys.add(key)
                        progress(int(processed / len(prepared) * 100))
                        continue
                log(f"正在恢复：{path.parent.name}/{path.name}" )
                try:
                    cursor.execute(sql)
                except Exception as error:
                    # Oracle may return success-with-compilation-errors as DPY-7000.
                    if not any(code in str(error) for code in ("DPY-7000", "ORA-24344")) or not key or key[0] not in COMPILE_TYPES:
                        raise
                completed += 1
                if key:
                    executed_keys.add(key)
                    if key[0] == "TABLE":
                        created_tables.add(key[1])
                if key and key[0] in COMPILE_TYPES:
                    restored.add(key)
                progress(int(processed / len(prepared) * 100))
            invalid = set(restored)
            for _ in range(len(restored) + 1):
                pending = set()
                for kind, name in sorted(restored):
                    cursor.execute("SELECT status FROM user_objects WHERE object_type = :1 AND object_name = :2", [kind, name])
                    row = cursor.fetchone()
                    if not row or row[0] != "VALID":
                        pending.add((kind, name))
                if not pending:
                    break
                if pending == invalid and _ > 1:
                    break
                invalid = pending
                for kind, name in sorted(pending):
                    base = kind.replace(" BODY", "")
                    suffix = " BODY" if kind.endswith(" BODY") else " SPECIFICATION" if kind in ("PACKAGE", "TYPE") else ""
                    try:
                        cursor.execute(f"ALTER {base} {quote_name(name)} COMPILE{suffix}")
                    except Exception as error:
                        log(f"编译 {kind} {name}：{error}")
            else:
                pending = invalid
            if pending:
                for kind, name in sorted(pending):
                    cursor.execute("SELECT line, position, text FROM user_errors WHERE type = :1 AND name = :2 ORDER BY sequence", [kind, name])
                    log(f"未通过编译：{kind} {name}")
                    for line, position, text in cursor.fetchall():
                        log(f"  {line}:{position} {text}")
                raise ValueError(f"DDL 已执行，但 {len(pending)} 个对象未通过编译，详情见日志")
        finally:
            cursor.close()
    except Exception as error:
        raise RuntimeError(f"恢复未完成：{error}\n已执行 {completed}/{len(prepared)} 条 DDL；Oracle DDL 自动提交，已执行部分不能回滚。") from error
    finally:
        db.close()
    return f"恢复完成，共执行 {completed} 条 DDL。对象编译检查通过；业务运行结果需另行验证。"
