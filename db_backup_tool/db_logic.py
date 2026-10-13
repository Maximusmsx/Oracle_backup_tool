import oracledb
import os
import shutil
import sys
import glob
import datetime
import decimal

# ── Thick 模式（读取 tnsnames.ora / TNS 别名）──────────────────
_thick_initialized = False


def _is_64bit_oci(path):
    """检查 oci.dll 是否为 64-bit (x86_64)，避免加载 32-bit dll 导致崩溃。"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            dos = f.read(0x40)
            if len(dos) < 0x40:
                return False
            pe_offset = int.from_bytes(dos[0x3C:0x40], "little")
            f.seek(pe_offset + 4)
            machine = int.from_bytes(f.read(2), "little")
        return machine == 0x8664   # x86_64
    except Exception:
        return False


def _find_oracle_client():
    r"""搜索第一个包含 64-bit oci.dll 的目录，跳过所有 32-bit 的 dll。

    优先级：
      1. exe 同目录下的 oracle_client\ 文件夹（便携部署）
      2. ORACLE_HOME 环境变量
      3. C:/D: 盘下的 oracle/instantclient_* 或 oracle/ora* 目录
      4. 常见第三方工具（Navicat、DataGrip 等）附带的 Instant Client
      5. PATH 中的目录
    """
    candidates = []

    # 0. 内嵌在 exe 中的 oracle_client（PyInstaller --add-data 解压到 _MEIPASS）
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "oracle_client"))

    # 1. exe/脚本 旁边的 oracle_client\ 目录（便于分发时手动放置）
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
    else:
        exe_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(exe_dir, "oracle_client"))

    # 2. ORACLE_HOME 环境变量
    oracle_home = os.environ.get("ORACLE_HOME", "")
    if oracle_home:
        candidates.append(os.path.join(oracle_home, "bin"))
        candidates.append(oracle_home)

    # 3. 常见 Oracle Instant Client 目录（最新版优先）
    for root in ["C:\\oracle", "D:\\oracle", "C:\\instantclient", "D:\\instantclient"]:
        try:
            # 匹配 instantclient_xx_x  和  oraXX  两种命名
            for p in sorted(
                glob.glob(os.path.join(root, "instantclient_*")) +
                glob.glob(os.path.join(root, "ora*")),
                reverse=True
            ):
                candidates.append(p)
                candidates.append(os.path.join(p, "bin"))
        except Exception:
            pass

    # 4. 常见第三方工具自带的 Instant Client
    for pattern in [
        r"C:\Program Files\PremiumSoft\Navicat*\instantclient*",
        r"C:\Program Files\JetBrains\*\jbr\bin",
        r"C:\Program Files\*\instantclient*",
    ]:
        try:
            for p in glob.glob(pattern):
                candidates.append(p)
        except Exception:
            pass

    # 5. PATH 中的每个目录
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d:
            candidates.append(d)

    for d in candidates:
        if _is_64bit_oci(os.path.join(d, "oci.dll")):
            return d

    return None


def init_thick_mode():
    """初始化 oracledb Thick 模式（连接 Oracle 11g 所必需）。"""
    global _thick_initialized
    if _thick_initialized:
        return True, ""
    lib_dir = _find_oracle_client()
    if lib_dir is None:
        return False, (
            "未找到 64-bit Oracle 客户端库 (oci.dll)。\n\n"
            "解决方法：\n"
            "① 从 Oracle 官网下载 Windows x64 Instant Client Basic：\n"
            "   https://www.oracle.com/database/technologies/instant-client/winx64-64-downloads.html\n"
            "② 解压后将文件夹（如 instantclient_21_3）放到 C:\\oracle\\ 下，\n"
            "   或直接放在本程序 exe 同目录的 oracle_client\\ 文件夹中\n"
            "③ 重新启动本程序后再连接"
        )
    try:
        oracledb.init_oracle_client(lib_dir=lib_dir)
        _thick_initialized = True
        return True, ""
    except Exception as e:
        return False, str(e)

# Hardcoded object categories: (display_name, oracle_type)
# oracle_type matches user_objects.object_type (or special scheduler views)
OBJECT_CATEGORIES = [
    ("Functions",          "FUNCTION"),
    ("Procedures",         "PROCEDURE"),
    ("Packages",           "PACKAGE"),
    ("Package bodies",     "PACKAGE BODY"),
    ("Types",              "TYPE"),
    ("Type bodies",        "TYPE BODY"),
    ("Triggers",           "TRIGGER"),
    ("Java sources",       "JAVA SOURCE"),
    ("Java classes",       "JAVA CLASS"),
    ("Tables",             "TABLE"),
    ("Indexes",            "INDEX"),
    ("Views",              "VIEW"),
    ("Materialized views", "MATERIALIZED VIEW"),
    ("Sequences",          "SEQUENCE"),
    ("Synonyms",           "SYNONYM"),
    ("Database links",     "DATABASE LINK"),
    ("Directories",        "DIRECTORY"),
    ("Libraries",          "LIBRARY"),
    ("Queues",             "QUEUE"),
    ("Queue tables",       "QUEUE TABLE"),
    ("Clusters",           "CLUSTER"),
    ("Jobs",               "JOB"),
    ("Schedules",          "SCHEDULE"),
    ("Programs",           "PROGRAM"),
    ("Chains",             "CHAIN"),
    ("Windows",            "WINDOW"),
    ("Window groups",      "WINDOW GROUP"),
    ("Job classes",        "JOB CLASS"),
]

# Maps oracle_type → DBMS_METADATA.GET_DDL type parameter (where they differ)
META_TYPE_MAP = {
    "PACKAGE BODY":     "PACKAGE_BODY",
    "TYPE BODY":        "TYPE_BODY",
    "JAVA SOURCE":      "JAVA_SOURCE",
    "JAVA CLASS":       "JAVA_CLASS",
    "DATABASE LINK":    "DB_LINK",
    "MATERIALIZED VIEW": "MATERIALIZED_VIEW",
    "QUEUE":            "AQ_QUEUE",
    "QUEUE TABLE":      "AQ_QUEUE_TABLE",
    "WINDOW GROUP":     "WINDOW_GROUP",
    "JOB CLASS":        "JOB_CLASS",
}

# Scheduler/special types that are NOT in user_objects, need their own query
SPECIAL_QUERIES = {
    "JOB":          "SELECT job_name FROM user_scheduler_jobs ORDER BY job_name",
    "SCHEDULE":     "SELECT schedule_name FROM user_scheduler_schedules ORDER BY schedule_name",
    "PROGRAM":      "SELECT program_name FROM user_scheduler_programs ORDER BY program_name",
    "CHAIN":        "SELECT chain_name FROM user_scheduler_chains ORDER BY chain_name",
}

OBJECT_LIST_QUERIES = {
    "FUNCTION": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'FUNCTION'
            UNION
            SELECT object_name AS name FROM user_procedures
             WHERE object_type = 'FUNCTION' AND procedure_name IS NULL
            UNION
            SELECT name FROM user_source WHERE type = 'FUNCTION'
        ) ORDER BY name
    """,
    "PROCEDURE": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'PROCEDURE'
            UNION
            SELECT object_name AS name FROM user_procedures
             WHERE object_type = 'PROCEDURE' AND procedure_name IS NULL
            UNION
            SELECT name FROM user_source WHERE type = 'PROCEDURE'
        ) ORDER BY name
    """,
    "PACKAGE": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'PACKAGE'
            UNION
            SELECT object_name AS name FROM user_procedures
             WHERE object_type = 'PACKAGE' AND procedure_name IS NULL
            UNION
            SELECT name FROM user_source WHERE type = 'PACKAGE'
        ) ORDER BY name
    """,
    "PACKAGE BODY": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'PACKAGE BODY'
            UNION
            SELECT name FROM user_source WHERE type = 'PACKAGE BODY'
        ) ORDER BY name
    """,
    "TYPE": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'TYPE'
            UNION
            SELECT type_name AS name FROM user_types
            UNION
            SELECT name FROM user_source WHERE type = 'TYPE'
        ) ORDER BY name
    """,
    "TYPE BODY": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'TYPE BODY'
            UNION
            SELECT name FROM user_source WHERE type = 'TYPE BODY'
        ) ORDER BY name
    """,
    "TRIGGER": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'TRIGGER'
            UNION
            SELECT trigger_name AS name FROM user_triggers
            UNION
            SELECT name FROM user_source WHERE type = 'TRIGGER'
        ) ORDER BY name
    """,
    "JAVA SOURCE": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'JAVA SOURCE'
            UNION
            SELECT name FROM user_source WHERE type = 'JAVA SOURCE'
        ) ORDER BY name
    """,
    "TABLE": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'TABLE'
            UNION
            SELECT table_name AS name FROM user_tables
        ) ORDER BY name
    """,
    "INDEX": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'INDEX'
            UNION
            SELECT index_name AS name FROM user_indexes
        ) ORDER BY name
    """,
    "VIEW": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'VIEW'
            UNION
            SELECT view_name AS name FROM user_views
        ) ORDER BY name
    """,
    "MATERIALIZED VIEW": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'MATERIALIZED VIEW'
            UNION
            SELECT mview_name AS name FROM user_mviews
        ) ORDER BY name
    """,
    "SEQUENCE": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'SEQUENCE'
            UNION
            SELECT sequence_name AS name FROM user_sequences
        ) ORDER BY name
    """,
    "SYNONYM": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'SYNONYM'
            UNION
            SELECT synonym_name AS name FROM user_synonyms
        ) ORDER BY name
    """,
    "DATABASE LINK": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'DATABASE LINK'
            UNION
            SELECT db_link AS name FROM user_db_links
        ) ORDER BY name
    """,
    "LIBRARY": """
        SELECT name FROM (
            SELECT object_name AS name FROM user_objects WHERE object_type = 'LIBRARY'
            UNION
            SELECT library_name AS name FROM user_libraries
        ) ORDER BY name
    """,
}


class DatabaseManager:
    def __init__(self, user, password, dsn, use_thick=False):
        self.user = user
        self.password = password
        self.dsn = dsn
        self.use_thick = use_thick
        self.conn = None

    def connect(self):
        if self.use_thick:
            ok, msg = init_thick_mode()
            if not ok:
                return False, (
                    f"无法加载 Oracle 客户端库: {msg}\n"
                    "请确认本机已安装 Oracle Client，且 ORACLE_HOME 或 PATH 配置正确。"
                )
        try:
            self.conn = oracledb.connect(user=self.user, password=self.password, dsn=self.dsn)
            return True, "连接成功"
        except Exception as e:
            err = str(e)
            # Oracle 11g/10g 不支持 thin 模式，自动切换 Thick 模式重试
            if not self.use_thick and "DPY-3010" in err:
                return self._connect_thick_fallback()
            return False, err

    def _connect_thick_fallback(self):
        """当 thin 模式报 DPY-3010（Oracle 版本过低）时，自动尝试 Thick 模式。"""
        ok, msg = init_thick_mode()
        if not ok:
            return False, (
                "检测到 Oracle 11g/10g，Thin 模式不支持该版本，需要 Oracle 客户端库。\n\n"
                f"{msg}"
            )
        try:
            self.conn = oracledb.connect(user=self.user, password=self.password, dsn=self.dsn)
            self.use_thick = True
            return True, "连接成功 (已自动切换为 Thick 模式)"
        except Exception as e:
            return False, str(e)

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def is_connected(self):
        if not self.conn:
            return False
        try:
            self.conn.ping()
            return True
        except Exception:
            self.conn = None
            return False

    def get_objects_by_type(self, oracle_type):
        """Return list of object names for the given oracle_type."""
        if not self.conn:
            return []
        cursor = self.conn.cursor()
        try:
            if oracle_type in SPECIAL_QUERIES:
                cursor.execute(SPECIAL_QUERIES[oracle_type])
            elif oracle_type in OBJECT_LIST_QUERIES:
                cursor.execute(OBJECT_LIST_QUERIES[oracle_type])
            else:
                cursor.execute(
                    "SELECT object_name FROM user_objects "
                    "WHERE object_type = :1 ORDER BY object_name",
                    [oracle_type]
                )
            return [row[0] for row in cursor.fetchall()]
        except Exception:
            return []
        finally:
            cursor.close()

    def _set_transform_params(self, cursor):
        for param, val in [
            ("SQLTERMINATOR", "TRUE"),
            ("PRETTY", "TRUE"),
            ("SEGMENT_ATTRIBUTES", "TRUE"),
            ("STORAGE", "TRUE"),
            ("TABLESPACE", "TRUE"),
            ("CONSTRAINTS", "TRUE"),
            ("REF_CONSTRAINTS", "TRUE"),
        ]:
            try:
                cursor.execute(
                    f"BEGIN DBMS_METADATA.SET_TRANSFORM_PARAM("
                    f"DBMS_METADATA.SESSION_TRANSFORM, '{param}', {val}); END;"
                )
            except Exception:
                pass

    def split_object_name(self, obj_name):
        obj_name = (obj_name or "").strip()
        if "." not in obj_name:
            return None, obj_name
        owner, name = obj_name.split(".", 1)
        return owner.strip().upper(), name.strip().upper()

    def normalize_where_clause(self, where_clause):
        where_clause = (where_clause or "").strip().rstrip(";").strip()
        if not where_clause:
            return ""
        if where_clause[:5].lower() == "where":
            return "WHERE" + where_clause[5:]
        return "WHERE " + where_clause

    def validate_table_where(self, table_name, where_clause):
        normalized = self.normalize_where_clause(where_clause)
        owner, base_name = self.split_object_name(table_name)
        full_name = f"{owner}.{base_name}" if owner else base_name
        cursor = self.conn.cursor()
        try:
            if normalized:
                sql = f"SELECT 1 FROM {full_name} {normalized} AND ROWNUM = 1"
            else:
                sql = f"SELECT 1 FROM {full_name} WHERE ROWNUM = 1"
            cursor.execute(sql)
            cursor.fetchone()
            return True, normalized, ""
        except Exception as e:
            return False, normalized, str(e)
        finally:
            cursor.close()

    def get_table_columns(self, table_name):
        owner, base_name = self.split_object_name(table_name)
        cursor = self.conn.cursor()
        try:
            if owner:
                cursor.execute(
                    "SELECT column_name FROM all_tab_columns "
                    "WHERE owner = :1 AND table_name = :2 ORDER BY column_id",
                    [owner, base_name],
                )
            else:
                cursor.execute(
                    "SELECT column_name FROM user_tab_columns "
                    "WHERE table_name = :1 ORDER BY column_id",
                    [base_name],
                )
            return [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()

    def format_sql_value(self, value):
        if value is None:
            return "null"
        if hasattr(value, "read"):
            value = value.read()
        if isinstance(value, bytes):
            return "hextoraw('" + value.hex().upper() + "')"
        if isinstance(value, decimal.Decimal):
            return str(value)
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, datetime.datetime):
            text = value.strftime("%Y-%m-%d %H:%M:%S")
            return f"to_date('{text}', 'yyyy-mm-dd hh24:mi:ss')"
        if isinstance(value, datetime.date):
            text = value.strftime("%Y-%m-%d")
            return f"to_date('{text}', 'yyyy-mm-dd')"
        text = str(value).replace("'", "''")
        return f"'{text}'"

    def export_table_data_sql(self, table_name, where_clause, file_path, should_stop=None):
        columns = self.get_table_columns(table_name)
        if not columns:
            raise Exception(f"{table_name}: table not found or has no columns")

        ok, normalized_where, err = self.validate_table_where(table_name, where_clause)
        if not ok:
            raise Exception(f"{table_name} where condition error: {err}")

        cursor = self.conn.cursor()
        row_count = 0
        col_list = ", ".join(c.lower() for c in columns)
        select_sql = f"SELECT {', '.join(columns)} FROM {table_name}"
        if normalized_where:
            select_sql += f" {normalized_where}"
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("prompt PL/SQL Developer import file\n")
                f.write(f"prompt Created on {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
                f.write("set feedback off\n")
                f.write("set define off\n")
                f.write(f"prompt Creating {table_name}...\n")
                f.write(self.get_ddl("TABLE", table_name).rstrip() + "\n\n")
                f.write(f"prompt Disabling triggers for {table_name}...\n")
                f.write(f"alter table {table_name} disable all triggers;\n")
                f.write(f"prompt Deleting {table_name}...\n")
                f.write(f"delete from {table_name};\n")
                f.write("commit;\n")
                f.write(f"prompt Loading {table_name}...\n")

                cursor.execute(select_sql)
                while True:
                    if should_stop and should_stop():
                        break
                    rows = cursor.fetchmany(200)
                    if not rows:
                        break
                    for row in rows:
                        values = ", ".join(self.format_sql_value(v) for v in row)
                        f.write(f"insert into {table_name} ({col_list})\n")
                        f.write(f"values ({values});\n")
                        row_count += 1

                f.write("commit;\n")
                f.write(f"prompt {row_count} records loaded\n")
                f.write(f"prompt Enabling triggers for {table_name}...\n")
                f.write(f"alter table {table_name} enable all triggers;\n")
                f.write("set feedback on\n")
                f.write("set define on\n")
                f.write("prompt Done.\n")
            return row_count
        finally:
            cursor.close()

    def get_ddl(self, oracle_type, obj_name):
        """Fetch DDL text for a single object."""
        meta_type = META_TYPE_MAP.get(oracle_type, oracle_type)
        owner, base_name = self.split_object_name(obj_name)
        cursor = self.conn.cursor()
        ddl = ""
        try:
            self._set_transform_params(cursor)
            if owner:
                cursor.execute(
                    "SELECT DBMS_METADATA.GET_DDL(:1, :2, :3) FROM DUAL",
                    [meta_type, base_name, owner]
                )
            else:
                cursor.execute(
                    "SELECT DBMS_METADATA.GET_DDL(:1, :2) FROM DUAL",
                    [meta_type, base_name]
                )
            row = cursor.fetchone()
            if row:
                val = row[0]
                ddl = val.read() if hasattr(val, "read") else str(val)
        except Exception as e:
            # Fallback: try with original oracle_type if mapped type failed
            if meta_type != oracle_type:
                try:
                    c2 = self.conn.cursor()
                    if owner:
                        c2.execute(
                            "SELECT DBMS_METADATA.GET_DDL(:1, :2, :3) FROM DUAL",
                            [oracle_type, base_name, owner]
                        )
                    else:
                        c2.execute(
                            "SELECT DBMS_METADATA.GET_DDL(:1, :2) FROM DUAL",
                            [oracle_type, base_name]
                        )
                    row = c2.fetchone()
                    c2.close()
                    if row:
                        val = row[0]
                        ddl = val.read() if hasattr(val, "read") else str(val)
                except Exception as e2:
                    return f"-- Error getting DDL for {oracle_type} {obj_name}: {e2}\n"
            else:
                return f"-- Error getting DDL for {oracle_type} {obj_name}: {e}\n"
        finally:
            cursor.close()

        # For TABLE: append column/table comments and all indexes
        if oracle_type == "TABLE" and ddl.strip():
            extras = self._get_table_extras(obj_name)
            if extras:
                ddl = ddl.rstrip() + "\n\n" + extras + "\n"

        return ddl

    def _get_table_extras(self, table_name):
        """Append to table DDL: table comment, column comments, indexes (alpha order)."""
        parts = []
        owner, base_name = self.split_object_name(table_name)
        display_name = f"{owner}.{base_name}" if owner else base_name
        cursor = self.conn.cursor()
        try:
            # 1. Table-level comment
            if owner:
                cursor.execute(
                    "SELECT comments FROM all_tab_comments WHERE owner = :1 AND table_name = :2",
                    [owner, base_name]
                )
            else:
                cursor.execute(
                    "SELECT comments FROM user_tab_comments WHERE table_name = :1",
                    [base_name]
                )
            row = cursor.fetchone()
            if row and row[0]:
                escaped = row[0].replace("'", "''")
                parts.append(f"COMMENT ON TABLE {display_name} IS '{escaped}';")

            # 2. Column comments, ordered by column position in the table
            if owner:
                cursor.execute(
                    """SELECT tc.column_name, cc.comments
                       FROM all_tab_columns tc
                       JOIN all_col_comments cc
                         ON tc.owner = cc.owner
                        AND tc.table_name = cc.table_name
                        AND tc.column_name = cc.column_name
                       WHERE tc.owner = :1
                         AND tc.table_name = :2
                         AND cc.comments IS NOT NULL
                       ORDER BY tc.column_id""",
                    [owner, base_name]
                )
            else:
                cursor.execute(
                    """SELECT tc.column_name, cc.comments
                       FROM user_tab_columns tc
                       JOIN user_col_comments cc
                         ON tc.table_name = cc.table_name
                        AND tc.column_name = cc.column_name
                       WHERE tc.table_name = :1
                         AND cc.comments IS NOT NULL
                       ORDER BY tc.column_id""",
                    [base_name]
                )
            for col_name, comment in cursor.fetchall():
                escaped = comment.replace("'", "''")
                parts.append(
                    f"COMMENT ON COLUMN {display_name}.{col_name} IS '{escaped}';"
                )

            # 3. All indexes on this table, sorted alphabetically by index name
            if owner:
                cursor.execute(
                    "SELECT owner, index_name FROM all_indexes "
                    "WHERE table_owner = :1 AND table_name = :2 ORDER BY owner, index_name",
                    [owner, base_name]
                )
            else:
                cursor.execute(
                    "SELECT NULL AS owner, index_name FROM user_indexes "
                    "WHERE table_name = :1 ORDER BY index_name",
                    [base_name]
                )
            index_names = [
                f"{idx_owner}.{idx_name}" if idx_owner else idx_name
                for idx_owner, idx_name in cursor.fetchall()
            ]
            if index_names:
                parts.append("")           # blank separator
                parts.append("-- Indexes")
                for idx_name in index_names:
                    idx_ddl = self.get_ddl("INDEX", idx_name).strip()
                    if idx_ddl and not idx_ddl.startswith("-- Error"):
                        parts.append(idx_ddl)

            return "\n".join(parts)
        except Exception as e:
            return f"-- Error getting table extras for {table_name}: {e}"
        finally:
            cursor.close()


def own_object_name(user, name, oracle_type=""):
    """Discard legacy selections from another schema; retain dotted DB link names."""
    if "." not in name or oracle_type == "DATABASE LINK":
        return name
    owner, base_name = name.split(".", 1)
    return base_name if owner.upper() == user.strip().upper() else None


def validate_table_exports(config, table_exports):
    if not table_exports:
        return []
    db = DatabaseManager(config["user"], config["password"], config["conn_str"])
    ok, msg = db.connect()
    if not ok:
        raise Exception(f"Database connection failed: {msg}")
    normalized = []
    try:
        for item in table_exports:
            table_name = (item.get("table") or "").strip().upper()
            table_name = own_object_name(config["user"], table_name)
            if not table_name:
                continue
            ok, where_clause, err = db.validate_table_where(table_name, item.get("where", ""))
            if not ok:
                raise Exception(f"{table_name} where condition error: {err}")
            normalized.append({"table": table_name, "where": where_clause})
        return normalized
    finally:
        db.close()


def export_ddl_task(config, export_dir, selected_objects, progress_callback, log_callback,
                    should_stop=None, name_filter="", table_exports=None, daily_subdir=False,
                    git_config=None):
    """
    Export selected DDL objects and optional table data scripts.
    selected_objects: {oracle_type: list_of_names or None}
    table_exports: [{"table": "TABLE_NAME", "where": "WHERE ..."}]
    """
    date_folder = datetime.date.today().isoformat() if daily_subdir else ""
    if date_folder:
        export_dir = os.path.join(export_dir, date_folder)
    log_callback(f"备份目录：{export_dir}")
    db = DatabaseManager(config["user"], config["password"], config["conn_str"])
    ok, msg = db.connect()
    if not ok:
        raise Exception(f"Database connection failed: {msg}")

    def _apply_filter(names):
        if not name_filter:
            return names
        kw = name_filter.lower()
        return [n for n in names if kw in n.lower()]

    try:
        target_list = []
        if not selected_objects:
            for _, oracle_type in OBJECT_CATEGORIES:
                names = _apply_filter(db.get_objects_by_type(oracle_type))
                if names:
                    target_list.append((oracle_type, names))
        else:
            for oracle_type, names in selected_objects.items():
                if names is None:
                    names = _apply_filter(db.get_objects_by_type(oracle_type))
                else:
                    names = [own_object_name(config["user"], name, oracle_type) for name in names]
                    names = [name for name in names if name]
                if names:
                    target_list.append((oracle_type, names))

        table_exports = [dict(item, table=own_object_name(config["user"], item.get("table", "")))
                         for item in (table_exports or [])
                         if own_object_name(config["user"], item.get("table", ""))]
        total = sum(len(names) for _, names in target_list) + len(table_exports)
        if total == 0:
            return "没有发现待导出的对象或表数据"

        log_callback(f"找到 {total} 个对象/表待导出...")
        current = 0
        exported_files = []

        for oracle_type, names in target_list:
            folder_name = oracle_type.replace(" ", "_")
            type_dir = os.path.join(export_dir, folder_name)
            if os.path.exists(type_dir):
                shutil.rmtree(type_dir)
            os.makedirs(type_dir)
            log_callback(f"正在导出 {oracle_type} ({len(names)} 个)...")

            for name in names:
                if should_stop and should_stop():
                    log_callback("用户已取消，导出中止。")
                    return f"已中止，已完成 {current}/{total} 个对象/表。"
                ddl = db.get_ddl(oracle_type, name)
                with open(os.path.join(type_dir, f"{name}.sql"), "w", encoding="utf-8") as f:
                    f.write(ddl)
                exported_files.append(f"{folder_name}/{name}.sql")
                current += 1
                progress_callback(int(current / total * 100))

        if table_exports:
            data_dir = os.path.join(export_dir, "DATA")
            if os.path.exists(data_dir):
                shutil.rmtree(data_dir)
            os.makedirs(data_dir)
            log_callback(f"正在导出表数据 ({len(table_exports)} 张表)...")
            for item in table_exports:
                if should_stop and should_stop():
                    log_callback("用户已取消，导出中止。")
                    return f"已中止，已完成 {current}/{total} 个对象/表。"
                table_name = item["table"]
                where_clause = item.get("where", "")
                rows = db.export_table_data_sql(
                    table_name,
                    where_clause,
                    os.path.join(data_dir, f"{table_name}.sql"),
                    should_stop=should_stop,
                )
                if should_stop and should_stop():
                    return f"已中止，已完成 {current}/{total} 个对象/表；未上传 Git。"
                exported_files.append(f"DATA/{table_name}.sql")
                log_callback(f"  {table_name}: 已导出 {rows} 行")
                current += 1
                progress_callback(int(current / total * 100))

        message = f"导出成功，共导出 {total} 个对象/表。"
        if git_config and git_config.get("enabled"):
            if should_stop and should_stop():
                return "本地备份已完成；已取消 Git 上传。"
            from git_backup import upload_backup
            try:
                git_message = upload_backup(export_dir, git_config, log_callback,
                                            files=exported_files, date_folder=date_folder,
                                            should_stop=should_stop)
            except Exception as error:
                raise Exception(f"本地备份已完成；{error}") from None
            log_callback(git_message)
            message += "\n" + git_message
        return message
    finally:
        db.close()

