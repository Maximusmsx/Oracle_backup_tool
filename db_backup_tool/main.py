import sys
import json
import os
import datetime
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog, QTreeWidget,
    QTreeWidgetItem, QProgressBar, QMessageBox, QTextEdit, QComboBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox, QGroupBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from db_logic import DatabaseManager, export_ddl_task, OBJECT_CATEGORIES, validate_table_exports, own_object_name
from git_backup import protect_secret, reveal_secret, validate_git_config, upload_backup

PLACEHOLDER_DATA = "__placeholder__"


def get_config_path():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "config.json")


CONFIG_FILE = get_config_path()


class BackupWorker(QThread):
    progress = pyqtSignal(int)
    log = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, db_config, export_dir, selected_objects, name_filter="", table_exports=None,
                 daily_subdir=False, git_config=None):
        super().__init__()
        self.db_config = db_config
        self.export_dir = export_dir
        self.selected_objects = selected_objects
        self.name_filter = name_filter
        self.table_exports = table_exports or []
        self.daily_subdir = daily_subdir
        self.git_config = git_config

    def run(self):
        try:
            msg = export_ddl_task(
                self.db_config, self.export_dir, self.selected_objects,
                self.progress.emit, self.log.emit,
                should_stop=self.isInterruptionRequested,
                name_filter=self.name_filter,
                table_exports=self.table_exports,
                daily_subdir=self.daily_subdir,
                git_config=self.git_config,
            )
            self.finished.emit(True, msg)
        except Exception as e:
            self.finished.emit(False, str(e))


class AllProfilesWorker(QThread):
    progress = pyqtSignal(int)
    log = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, profiles):
        super().__init__()
        self.profiles = profiles   # dict {name: profile_dict}

    def run(self):
        names = list(self.profiles.keys())
        total = len(names)
        done = 0
        for idx, name in enumerate(names):
            if self.isInterruptionRequested():
                self.finished.emit(True, f"已中止，已完成 {done}/{total} 个配置")
                return
            p = self.profiles[name]
            export_dir = p.get("export_dir", "").strip()
            if not export_dir:
                self.log.emit(f"[{idx+1}/{total}] 跳过 [{name}]：未配置备份目录")
                continue
            self.log.emit(f"[{idx+1}/{total}] 开始导出配置：{name}")
            db_cfg = {
                "user": p.get("user", ""),
                "password": p.get("password", ""),
                "conn_str": p.get("conn_str", ""),
            }
            raw_sel = p.get("selected_objects", {})
            selected = {k: (None if v == "__ALL__" else v) for k, v in raw_sel.items()} if raw_sel else {}
            try:
                msg = export_ddl_task(
                    db_cfg, export_dir, selected,
                    self.progress.emit, self.log.emit,
                    should_stop=self.isInterruptionRequested,
                    name_filter=p.get("filter_text", "").strip(),
                    table_exports=p.get("table_exports", []),
                    daily_subdir=p.get("daily_subdir", False),
                    git_config=p.get("git", {}),
                )
                self.log.emit(f"  [{name}] {msg}")
                done += 1
            except Exception as e:
                self.log.emit(f"  [{name}] 导出失败：{e}")
        self.finished.emit(done == total, f"所有配置处理完毕，成功 {done}/{total} 个；失败或跳过 {total-done} 个，详情见日志")


class GitUploadWorker(QThread):
    log = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, export_dir, options, daily_subdir):
        super().__init__()
        self.export_dir = export_dir
        self.options = options
        self.date_folder = datetime.date.today().isoformat() if daily_subdir else ""

    def run(self):
        try:
            directory = os.path.join(self.export_dir, self.date_folder) if self.date_folder else self.export_dir
            message = upload_backup(directory, self.options, self.log.emit,
                                    date_folder=self.date_folder, should_stop=self.isInterruptionRequested)
            self.finished.emit(True, message)
        except Exception as error:
            self.finished.emit(False, str(error))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("数据库 DDL 备份工具")
        self.resize(820, 880)
        self.profiles = {}       # {name: {user, password, conn_str, export_dir, selected_objects}}
        self.current_profile = ""
        self.db_manager = None
        self._loading_profile = False
        self.init_ui()
        self.load_config()

    # ── UI 初始化 ────────────────────────────────────
    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(6)

        # 0. 配置管理行
        h0 = QHBoxLayout()
        h0.addWidget(QLabel("配置下拉框"))
        self.combo_profile = QComboBox()
        self.combo_profile.setEditable(True)
        self.combo_profile.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_profile.lineEdit().setPlaceholderText("输入或选择配置名")
        self.combo_profile.activated.connect(self.on_profile_activated)
        h0.addWidget(self.combo_profile, 1)
        self.btn_save_profile = QPushButton("保存当前配置")
        self.btn_save_profile.setFixedWidth(110)
        self.btn_save_profile.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold;"
        )
        self.btn_save_profile.clicked.connect(self.save_current_profile)
        h0.addWidget(self.btn_save_profile)
        self.btn_delete_profile = QPushButton("删除当前配置")
        self.btn_delete_profile.setFixedWidth(110)
        self.btn_delete_profile.setStyleSheet(
            "background-color: #f44336; color: white; font-weight: bold;"
        )
        self.btn_delete_profile.clicked.connect(self.delete_current_profile)
        h0.addWidget(self.btn_delete_profile)
        layout.addLayout(h0)

        # 1. 数据库连接
        layout.addWidget(QLabel("1. 数据库连接配置"))
        h1 = QHBoxLayout()
        self.u_name = QLineEdit()
        self.u_name.setPlaceholderText("用户名")
        self.u_pwd = QLineEdit()
        self.u_pwd.setPlaceholderText("密码")
        self.u_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        h1.addWidget(self.u_name)
        h1.addWidget(self.u_pwd)
        layout.addLayout(h1)

        self.conn_str = QLineEdit()
        self.conn_str.setPlaceholderText(
            "完整连接串，例如: localhost:1521/orcl.mshome.net  或  192.168.1.100:1521/orcl"
        )
        layout.addWidget(self.conn_str)

        h_conn = QHBoxLayout()
        self.btn_connect = QPushButton("连接数据库")
        self.btn_connect.setFixedWidth(120)
        self.btn_connect.clicked.connect(self.connect_database)
        self.lbl_status = QLabel("● 未连接")
        self.lbl_status.setStyleSheet("color: gray; font-weight: bold;")
        h_conn.addWidget(self.btn_connect)
        h_conn.addWidget(self.lbl_status)
        h_conn.addStretch()
        layout.addLayout(h_conn)

        # 2. 备份目录
        layout.addWidget(QLabel("2. 备份目录选择"))
        h2 = QHBoxLayout()
        self.dir_path = QLineEdit()
        self.dir_path.setPlaceholderText("选择备份目标目录")
        self.btn_browse = QPushButton("浏览...")
        self.btn_browse.setFixedWidth(70)
        self.btn_browse.clicked.connect(self.browse_directory)
        h2.addWidget(self.dir_path)
        h2.addWidget(self.btn_browse)
        layout.addLayout(h2)
        self.daily_subdir = QCheckBox("按日建目录（yyyy-mm-dd）")
        layout.addWidget(self.daily_subdir)

        self.git_group = QCheckBox("备份完成后上传 Git（HTTP／HTTPS，无需安装 Git）")
        self.git_group.setChecked(False)
        layout.addWidget(self.git_group)
        self.git_panel = QGroupBox()
        self.git_panel.setEnabled(False)
        self.git_group.toggled.connect(self.git_panel.setEnabled)
        git_layout = QVBoxLayout(self.git_panel)
        self.git_url = QLineEdit()
        self.git_url.setPlaceholderText("仓库地址，例如 https://git.example.com/team/backup.git")
        git_layout.addWidget(self.git_url)
        git_path_row = QHBoxLayout()
        git_path_row.addWidget(QLabel("分支"))
        self.git_branch = QLineEdit("main")
        self.git_branch.setMaximumWidth(140)
        git_path_row.addWidget(self.git_branch)
        git_path_row.addWidget(QLabel("仓库子目录"))
        self.git_folder = QLineEdit("backup")
        self.git_folder.setPlaceholderText("各配置使用不同目录，例如 backups/学校A")
        git_path_row.addWidget(self.git_folder)
        git_layout.addLayout(git_path_row)
        git_auth_row = QHBoxLayout()
        git_auth_row.addWidget(QLabel("用户名"))
        self.git_username = QLineEdit()
        git_auth_row.addWidget(self.git_username)
        git_auth_row.addWidget(QLabel("密码／Token"))
        self.git_password = QLineEdit()
        self.git_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.git_password.setPlaceholderText("GitHub 使用 Token；保存时绑定当前 Windows 用户加密")
        git_auth_row.addWidget(self.git_password)
        self.btn_git_upload = QPushButton("上传现有备份")
        self.btn_git_upload.clicked.connect(self.start_git_upload)
        git_auth_row.addWidget(self.btn_git_upload)
        git_layout.addLayout(git_auth_row)
        layout.addWidget(self.git_panel)

        layout.addWidget(QLabel("3. 导出对象选择"))
        self.tabs = QTabWidget()

        ddl_tab = QWidget()
        ddl_layout = QVBoxLayout(ddl_tab)
        ddl_layout.setContentsMargins(0, 0, 0, 0)
        h3 = QHBoxLayout()
        h3.addWidget(QLabel("数据库对象勾选（展开类型后按需加载；不勾选默认导出全部）"))
        h3.addStretch()
        h3.addWidget(QLabel("名称筛选"))
        self.filter_input = QLineEdit()
        self.filter_input.setFixedWidth(150)
        self.filter_input.setPlaceholderText("输入关键词")
        self.filter_input.returnPressed.connect(self.apply_filter)
        h3.addWidget(self.filter_input)
        self.btn_filter = QPushButton("筛选")
        self.btn_filter.setFixedWidth(50)
        self.btn_filter.clicked.connect(self.apply_filter)
        h3.addWidget(self.btn_filter)
        ddl_layout.addLayout(h3)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("数据库对象")
        self.tree.itemChanged.connect(self.on_item_changed)
        self.tree.itemExpanded.connect(self.on_item_expanded)
        ddl_layout.addWidget(self.tree)
        self.tabs.addTab(ddl_tab, "DDL")

        data_tab = QWidget()
        data_layout = QVBoxLayout(data_tab)
        data_layout.setContentsMargins(0, 0, 0, 0)
        data_top = QHBoxLayout()
        data_top.addWidget(QLabel("如需导出，请勾选表名。导出条件为空表示全部导出"))
        data_top.addStretch()
        data_top.addWidget(QLabel("名称筛选"))
        self.data_filter_input = QLineEdit()
        self.data_filter_input.setFixedWidth(150)
        self.data_filter_input.setPlaceholderText("输入关键词")
        self.data_filter_input.returnPressed.connect(self.apply_data_filter)
        data_top.addWidget(self.data_filter_input)
        self.btn_data_filter = QPushButton("筛选")
        self.btn_data_filter.setFixedWidth(50)
        self.btn_data_filter.clicked.connect(self.apply_data_filter)
        data_top.addWidget(self.btn_data_filter)
        data_layout.addLayout(data_top)

        self.table_data_grid = QTableWidget(0, 2)
        self.table_data_grid.setHorizontalHeaderLabels(["表名", "导出条件-空表示全部导出"])
        self.table_data_grid.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table_data_grid.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_data_grid.verticalHeader().setVisible(False)
        self.table_data_grid.setAlternatingRowColors(True)
        self.table_data_grid.itemChanged.connect(self.on_table_data_changed)
        data_layout.addWidget(self.table_data_grid)
        self.tabs.addTab(data_tab, "表数据")

        layout.addWidget(self.tabs)
        # 按钮行
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_export = QPushButton("导出当前配置")
        self.btn_export.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold; "
            "min-height: 30px; min-width: 120px;"
        )
        self.btn_export.clicked.connect(self.on_export_btn_clicked)
        btn_row.addWidget(self.btn_export)
        self.btn_all_export = QPushButton("导出所有配置")
        self.btn_all_export.setStyleSheet(
            "background-color: #2196F3; color: white; font-weight: bold; "
            "min-height: 30px; min-width: 120px;"
        )
        self.btn_all_export.clicked.connect(self.on_all_export_btn_clicked)
        btn_row.addWidget(self.btn_all_export)
        layout.addLayout(btn_row)

        # 进度 + 日志
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(110)
        layout.addWidget(self.log_output)

    # ── 树构建 ──────────────────────────────────────
    def _build_tree(self, saved_selections=None):
        self.tree.blockSignals(True)
        self.tree.clear()
        saved_selections = saved_selections or {}
        for display_name, oracle_type in OBJECT_CATEGORIES:
            item = QTreeWidgetItem(self.tree)
            item.setText(0, display_name)
            item.setData(0, Qt.ItemDataRole.UserRole, oracle_type)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            saved = saved_selections.get(oracle_type)
            if saved == "__ALL__":
                item.setCheckState(0, Qt.CheckState.Checked)
            elif isinstance(saved, list) and saved:
                item.setCheckState(0, Qt.CheckState.PartiallyChecked)
            else:
                item.setCheckState(0, Qt.CheckState.Unchecked)
            ph = QTreeWidgetItem(item)
            ph.setText(0, "点击展开加载对象...")
            ph.setData(0, Qt.ItemDataRole.UserRole, PLACEHOLDER_DATA)
            ph.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.tree.blockSignals(False)

    # ── 配置管理 ────────────────────────────────────
    def _migrate_old_config(self, raw):
        """将旧格式（单配置）迁移到 profiles 格式。"""
        name = raw.get("user", "") or "默认"
        profile = {
            "user": raw.get("user", ""),
            "password": raw.get("password", ""),
            "conn_str": raw.get("conn_str", ""),
            "export_dir": raw.get("export_dir", ""),
            "daily_subdir": raw.get("daily_subdir", False),
            "selected_objects": raw.get("selected_objects", {}),
            "table_exports": raw.get("table_exports", []),
        }
        return {"profiles": {name: profile}, "last_profile": name}

    def load_config(self):
        raw = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                raw = {}

        if "profiles" not in raw:
            raw = self._migrate_old_config(raw) if raw else {"profiles": {}, "last_profile": ""}

        self.profiles = raw.get("profiles", {})
        last = raw.get("last_profile", "")

        self._populate_combo()

        if last and last in self.profiles:
            self._load_profile(last)
        elif self.profiles:
            self._load_profile(next(iter(self.profiles)))
        else:
            self._build_tree()
            self._build_table_data_grid()

    def _populate_combo(self):
        self.combo_profile.blockSignals(True)
        self.combo_profile.clear()
        for name in self.profiles:
            self.combo_profile.addItem(name)
        self.combo_profile.blockSignals(False)

    def _load_profile(self, name):
        if name not in self.profiles:
            return
        self._loading_profile = True
        if self.db_manager:
            self.db_manager.close()
            self.db_manager = None
        self.lbl_status.setText("● 未连接")
        self.lbl_status.setStyleSheet("color: gray; font-weight: bold;")
        p = self.profiles[name]
        for kind, names in p.get("selected_objects", {}).items():
            if isinstance(names, list):
                p["selected_objects"][kind] = [own_object_name(p.get("user", ""), item, kind)
                                               for item in names
                                               if own_object_name(p.get("user", ""), item, kind)]
        self.current_profile = name
        self.u_name.setText(p.get("user", ""))
        self.u_pwd.setText(p.get("password", ""))
        self.conn_str.setText(p.get("conn_str", ""))
        self.dir_path.setText(p.get("export_dir", ""))
        self.daily_subdir.setChecked(p.get("daily_subdir", False))
        git = p.get("git", {})
        self.git_group.setChecked(git.get("enabled", False))
        self.git_url.setText(git.get("url", ""))
        self.git_branch.setText(git.get("branch", "main"))
        self.git_folder.setText(git.get("folder", name.replace("/", "_").replace("\\", "_").replace(":", "_")))
        self.git_username.setText(git.get("username", ""))
        try:
            self.git_password.setText(reveal_secret(git.get("password_protected", "")))
        except Exception as error:
            self.git_password.clear()
            self.log_output.append(str(error))
        self._build_tree(saved_selections=p.get("selected_objects", {}))
        self._build_table_data_grid(saved_exports=p.get("table_exports", []))
        self.filter_input.setText(p.get("filter_text", ""))
        self.apply_filter()
        idx = self.combo_profile.findText(name)
        if idx >= 0:
            self.combo_profile.setCurrentIndex(idx)
        else:
            self.combo_profile.setEditText(name)
        self._loading_profile = False

    def _build_table_data_grid(self, table_names=None, saved_exports=None):
        saved_exports = saved_exports or []
        saved_map = {
            own_object_name(self.u_name.text(), (item.get("table") or "").strip().upper()): item.get("where", "")
            for item in saved_exports
            if own_object_name(self.u_name.text(), (item.get("table") or "").strip().upper())
        }
        names = [n.upper() for n in (table_names or [])]
        for saved_name in saved_map:
            if saved_name not in names:
                names.append(saved_name)
        names = sorted(set(names))

        self.table_data_grid.blockSignals(True)
        self.table_data_grid.setRowCount(0)
        for row, table_name in enumerate(names):
            self.table_data_grid.insertRow(row)
            table_item = QTableWidgetItem(table_name)
            table_item.setFlags(
                (table_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                & ~Qt.ItemFlag.ItemIsEditable
            )
            table_item.setCheckState(
                Qt.CheckState.Checked if table_name in saved_map else Qt.CheckState.Unchecked
            )
            where_item = QTableWidgetItem(saved_map.get(table_name, ""))
            self.table_data_grid.setItem(row, 0, table_item)
            self.table_data_grid.setItem(row, 1, where_item)
        self.table_data_grid.blockSignals(False)
        self.apply_data_filter()

    def _load_table_data_from_db(self):
        saved = self._collect_table_exports(validate_where=False)
        if self.db_manager and self.db_manager.is_connected():
            names = self.db_manager.get_objects_by_type("TABLE")
            self._build_table_data_grid(names, saved)

    def _collect_table_exports(self, validate_where=False):
        result = []
        for row in range(self.table_data_grid.rowCount()):
            table_item = self.table_data_grid.item(row, 0)
            where_item = self.table_data_grid.item(row, 1)
            if not table_item or table_item.checkState() != Qt.CheckState.Checked:
                continue
            table_name = table_item.text().strip().upper()
            where_clause = where_item.text().strip() if where_item else ""
            result.append({"table": table_name, "where": where_clause})
        if validate_where and result:
            db_cfg = {
                "user": self.u_name.text(),
                "password": self.u_pwd.text(),
                "conn_str": self.conn_str.text(),
            }
            result = validate_table_exports(db_cfg, result)
            self._apply_normalized_table_exports(result)
        return result

    def _apply_normalized_table_exports(self, table_exports):
        where_map = {item["table"]: item.get("where", "") for item in table_exports}
        self.table_data_grid.blockSignals(True)
        for row in range(self.table_data_grid.rowCount()):
            table_item = self.table_data_grid.item(row, 0)
            where_item = self.table_data_grid.item(row, 1)
            if table_item and table_item.text().strip().upper() in where_map:
                if where_item is None:
                    where_item = QTableWidgetItem("")
                    self.table_data_grid.setItem(row, 1, where_item)
                where_item.setText(where_map[table_item.text().strip().upper()])
        self.table_data_grid.blockSignals(False)

    def apply_data_filter(self):
        text = self.data_filter_input.text().strip().lower()
        for row in range(self.table_data_grid.rowCount()):
            table_item = self.table_data_grid.item(row, 0)
            name = table_item.text().lower() if table_item else ""
            self.table_data_grid.setRowHidden(row, bool(text) and text not in name)

    def on_table_data_changed(self, item):
        pass

    def on_profile_activated(self, index):
        if self._loading_profile:
            return
        name = self.combo_profile.itemText(index)
        if name and name in self.profiles:
            self._load_profile(name)

    def save_current_profile(self):
        name = self.combo_profile.currentText().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请输入配置名")
            return
        try:
            table_exports = self._collect_table_exports(validate_where=True)
            git_config = self._collect_git_config()
        except Exception as e:
            QMessageBox.critical(self, "配置错误", str(e))
            return
        is_new = name not in self.profiles
        self.profiles[name] = {
            "user": self.u_name.text(),
            "password": self.u_pwd.text(),
            "conn_str": self.conn_str.text(),
            "export_dir": self.dir_path.text(),
            "daily_subdir": self.daily_subdir.isChecked(),
            "git": git_config,
            "selected_objects": self._collect_selections(),
            "table_exports": table_exports,
            "filter_text": self.filter_input.text(),
        }
        self.current_profile = name
        if is_new:
            self.combo_profile.blockSignals(True)
            self.combo_profile.addItem(name)
            self.combo_profile.setCurrentIndex(self.combo_profile.count() - 1)
            self.combo_profile.blockSignals(False)
        self._write_config()
        self.log_output.append(f"配置 [{name}] 已保存")

    def _write_config(self):
        data = {"profiles": self.profiles, "last_profile": self.current_profile}
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            self.log_output.append(f"保存配置失败: {e}")

    def _collect_selections(self):
        # 已保存的选择作为兜底（处理未展开类型的占位符情况）
        existing = {}
        if self.current_profile and self.current_profile in self.profiles:
            existing = self.profiles[self.current_profile].get("selected_objects", {})

        result = {}
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            cat = root.child(i)
            oracle_type = cat.data(0, Qt.ItemDataRole.UserRole)
            if not oracle_type:
                continue
            state = cat.checkState(0)
            if state == Qt.CheckState.Unchecked:
                continue
            if state == Qt.CheckState.Checked:
                result[oracle_type] = "__ALL__"
            elif state == Qt.CheckState.PartiallyChecked:
                real_children = [
                    cat.child(j) for j in range(cat.childCount())
                    if cat.child(j).data(0, Qt.ItemDataRole.UserRole) != PLACEHOLDER_DATA
                ]
                if real_children:
                    # 子节点已加载：从当前勾选状态收集
                    names = [c.text(0) for c in real_children
                             if c.checkState(0) == Qt.CheckState.Checked]
                    if names:
                        result[oracle_type] = names
                else:
                    # 子节点未加载（占位符）：沿用上次保存的具体选择
                    if oracle_type in existing:
                        result[oracle_type] = existing[oracle_type]
        return result

    # ── 筛选 ────────────────────────────────────────
    def apply_filter(self):
        text = self.filter_input.text().strip().lower()
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            type_item = root.child(i)
            type_item.setHidden(False)          # 类型行永不隐藏
            for j in range(type_item.childCount()):
                child = type_item.child(j)
                if child.data(0, Qt.ItemDataRole.UserRole) == PLACEHOLDER_DATA:
                    child.setHidden(False)
                    continue
                child.setHidden(bool(text) and text not in child.text(0).lower())

    # ── 数据库连接 ───────────────────────────────────
    def _try_connect(self):
        if not self.u_name.text() or not self.u_pwd.text() or not self.conn_str.text():
            return False, "连接配置不完整"
        if self.db_manager:
            self.db_manager.close()
        self.db_manager = DatabaseManager(
            self.u_name.text(), self.u_pwd.text(), self.conn_str.text()
        )
        ok, msg = self.db_manager.connect()
        if ok:
            mode = "Thick" if self.db_manager.use_thick else "Thin"
            self.lbl_status.setText(
                f"● 已连接 ({mode}): {self.u_name.text()}@{self.conn_str.text()}"
            )
            self.lbl_status.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.db_manager = None
            self.lbl_status.setText("● 连接失败")
            self.lbl_status.setStyleSheet("color: red; font-weight: bold;")
        return ok, msg

    def connect_database(self):
        if not self.u_name.text() or not self.u_pwd.text() or not self.conn_str.text():
            QMessageBox.critical(self, "错误", "请填写完整的数据库连接配置")
            return
        self.lbl_status.setText("● 连接中...")
        self.lbl_status.setStyleSheet("color: orange; font-weight: bold;")
        QApplication.processEvents()
        ok, err_msg = self._try_connect()
        if ok:
            self.log_output.append(f"数据库连接成功：{err_msg}")
            saved = self._collect_selections()
            self._build_tree(saved_selections=saved)
            self._load_table_data_from_db()
        else:
            self.db_manager = None
            QMessageBox.critical(self, "连接失败", err_msg)

    # ── 树事件 ──────────────────────────────────────
    def on_item_expanded(self, item):
        if item.childCount() != 1:
            return
        first = item.child(0)
        if first.data(0, Qt.ItemDataRole.UserRole) != PLACEHOLDER_DATA:
            return
        oracle_type = item.data(0, Qt.ItemDataRole.UserRole)
        if not oracle_type:
            return

        if not self.db_manager or not self.db_manager.is_connected():
            if not self.u_name.text() or not self.u_pwd.text() or not self.conn_str.text():
                QMessageBox.warning(self, "提示",
                    "请先填写数据库连接配置，然后点击 [连接数据库] 按钮")
                self.tree.collapseItem(item)
                return
            self.lbl_status.setText("● 连接中...")
            self.lbl_status.setStyleSheet("color: orange; font-weight: bold;")
            QApplication.processEvents()
            ok, err_msg = self._try_connect()
            if not ok:
                QMessageBox.critical(self, "连接失败", err_msg)
                self.tree.collapseItem(item)
                return
            self.log_output.append(f"数据库连接成功：{err_msg}")
            self._load_table_data_from_db()

        first.setText(0, "加载中...")
        QApplication.processEvents()
        names = self.db_manager.get_objects_by_type(oracle_type)

        self.tree.blockSignals(True)
        item.removeChild(first)

        saved_sel = self.profiles.get(self.current_profile, {}).get("selected_objects", {})
        saved_for_type = saved_sel.get(oracle_type, [])
        parent_checked = item.checkState(0) == Qt.CheckState.Checked

        for name in names:
            child = QTreeWidgetItem(item)
            child.setText(0, name)
            child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if parent_checked or saved_for_type == "__ALL__":
                child.setCheckState(0, Qt.CheckState.Checked)
            elif isinstance(saved_for_type, list) and name in saved_for_type:
                child.setCheckState(0, Qt.CheckState.Checked)
            else:
                child.setCheckState(0, Qt.CheckState.Unchecked)

        if not names:
            self.log_output.append(f"{item.text(0)}: 当前用户下没有此类型的对象")
        self.tree.blockSignals(False)

        # 展开后立即应用当前筛选词
        self.apply_filter()

    def on_item_changed(self, item, column):
        self.tree.blockSignals(True)
        try:
            state = item.checkState(0)
            oracle_type = item.data(0, Qt.ItemDataRole.UserRole)
            if oracle_type and oracle_type != PLACEHOLDER_DATA:
                filter_active = bool(self.filter_input.text().strip())
                for i in range(item.childCount()):
                    child = item.child(i)
                    if child.data(0, Qt.ItemDataRole.UserRole) != PLACEHOLDER_DATA:
                        # 取消勾选时必须传播到所有子节点（含隐藏），避免隐藏项残留勾选状态
                        # 勾选时若筛选激活，则只传播到可见子节点
                        if (state == Qt.CheckState.Unchecked
                                or not filter_active
                                or not child.isHidden()):
                            child.setCheckState(0, state)
            else:
                parent = item.parent()
                if parent:
                    real = [
                        parent.child(i) for i in range(parent.childCount())
                        if parent.child(i).data(0, Qt.ItemDataRole.UserRole) != PLACEHOLDER_DATA
                    ]
                    if real:
                        checked = sum(
                            1 for c in real if c.checkState(0) == Qt.CheckState.Checked
                        )
                        if checked == len(real):
                            parent.setCheckState(0, Qt.CheckState.Checked)
                        elif checked == 0:
                            parent.setCheckState(0, Qt.CheckState.Unchecked)
                        else:
                            parent.setCheckState(0, Qt.CheckState.PartiallyChecked)
        finally:
            self.tree.blockSignals(False)

        # 勾选变化时自动同步到当前配置（selected_objects）
        if self.current_profile:
            self.profiles.setdefault(self.current_profile, {})["selected_objects"] = (
                self._collect_selections()
            )
            self._write_config()

    # ── 浏览目录 / 导出 ─────────────────────────────
    def _collect_git_config(self):
        options = {
            "enabled": self.git_group.isChecked(),
            "url": self.git_url.text().strip(),
            "branch": self.git_branch.text().strip(),
            "folder": self.git_folder.text().strip(),
            "username": self.git_username.text().strip(),
            "password_protected": protect_secret(self.git_password.text()),
        }
        validate_git_config(options)
        return options

    def start_git_upload(self):
        try:
            options = self._collect_git_config()
            if not options["enabled"] or not self.dir_path.text().strip():
                raise ValueError("请启用 Git 上传并填写备份目录")
        except Exception as error:
            QMessageBox.critical(self, "Git 配置错误", str(error))
            return
        self.btn_git_upload.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.btn_all_export.setEnabled(False)
        self.git_worker = GitUploadWorker(self.dir_path.text().strip(), options, self.daily_subdir.isChecked())
        self.git_worker.log.connect(self.log_output.append)
        self.git_worker.finished.connect(self.on_git_upload_finished)
        self.git_worker.start()

    def on_git_upload_finished(self, success, message):
        self.btn_git_upload.setEnabled(True)
        self.btn_export.setEnabled(True)
        self.btn_all_export.setEnabled(True)
        self.log_output.append(message)
        if success:
            QMessageBox.information(self, "Git 上传完成", message)
        else:
            QMessageBox.critical(self, "Git 上传失败", message)

    def browse_directory(self):
        d = QFileDialog.getExistingDirectory(self, "选择备份目录")
        if d:
            self.dir_path.setText(d)

    def _collect_export_targets(self):
        """收集导出目标，尊重筛选隐藏状态：被隐藏的对象不导出。"""
        existing = {}
        if self.current_profile and self.current_profile in self.profiles:
            existing = self.profiles[self.current_profile].get("selected_objects", {})

        selected = {}
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            cat = root.child(i)
            oracle_type = cat.data(0, Qt.ItemDataRole.UserRole)
            state = cat.checkState(0)
            if state == Qt.CheckState.Unchecked:
                continue

            real_children = [
                cat.child(j) for j in range(cat.childCount())
                if cat.child(j).data(0, Qt.ItemDataRole.UserRole) != PLACEHOLDER_DATA
            ]
            has_hidden = any(c.isHidden() for c in real_children)

            if not real_children:
                # 子节点未展开（占位符）
                if state == Qt.CheckState.Checked:
                    selected[oracle_type] = None        # 全量，worker 侧应用筛选
                else:
                    # PartiallyChecked + 未展开 → 用已保存的具体名称
                    saved = existing.get(oracle_type)
                    if saved == "__ALL__":
                        selected[oracle_type] = None
                    elif isinstance(saved, list) and saved:
                        selected[oracle_type] = saved
            elif state == Qt.CheckState.Checked and not has_hidden:
                selected[oracle_type] = None            # 已展开且无隐藏 → 全量
            else:
                # PartiallyChecked 或 Checked 但有隐藏项 → 仅可见勾选项
                names = [
                    c.text(0) for c in real_children
                    if c.checkState(0) == Qt.CheckState.Checked and not c.isHidden()
                ]
                if names:
                    selected[oracle_type] = names

        return selected

    def on_export_btn_clicked(self):
        if hasattr(self, "worker") and self.worker.isRunning():
            self._stop_export()
        else:
            self.start_export()

    def _stop_export(self):
        self.worker.requestInterruption()
        self.btn_export.setEnabled(False)
        self.log_output.append("正在停止，请稍候...")

    def start_export(self):
        if not self.u_name.text() or not self.u_pwd.text() or not self.conn_str.text():
            QMessageBox.critical(self, "错误", "数据库连接配置不能为空")
            return
        if not self.dir_path.text():
            QMessageBox.critical(self, "错误", "备份目录不能为空")
            return

        selected = self._collect_export_targets()
        try:
            table_exports = self._collect_table_exports(validate_where=True)
            git_config = self._collect_git_config()
        except Exception as e:
            QMessageBox.critical(self, "导出配置错误", str(e))
            return
        self.btn_export.setText("停止导出")
        self.btn_export.setStyleSheet(
            "background-color: #f44336; color: white; font-weight: bold; "
            "min-height: 30px; min-width: 120px;"
        )
        self.btn_export.setEnabled(True)
        self.btn_all_export.setEnabled(False)
        self.btn_git_upload.setEnabled(False)
        self.progress_bar.setValue(0)
        self.log_output.clear()

        db_cfg = {
            "user": self.u_name.text(),
            "password": self.u_pwd.text(),
            "conn_str": self.conn_str.text(),
        }
        self.worker = BackupWorker(
            db_cfg, self.dir_path.text(), selected,
            name_filter=self.filter_input.text().strip(),
            table_exports=table_exports,
            daily_subdir=self.daily_subdir.isChecked(),
            git_config=git_config,
        )
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.log.connect(self.log_output.append)
        self.worker.finished.connect(self.on_export_finished)
        self.worker.start()

    def on_export_finished(self, success, msg):
        self.btn_git_upload.setEnabled(True)
        self.btn_export.setEnabled(True)
        self.btn_export.setText("导出当前配置")
        self.btn_export.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold; "
            "min-height: 30px; min-width: 120px;"
        )
        self.btn_all_export.setEnabled(True)
        if success:
            QMessageBox.information(self, "完成", msg)
        else:
            QMessageBox.critical(self, "导出失败", msg)

    # ── 导出所有配置 ─────────────────────────────────
    def on_all_export_btn_clicked(self):
        if hasattr(self, "all_worker") and self.all_worker.isRunning():
            self._stop_all_export()
        else:
            self.start_all_export()

    def _stop_all_export(self):
        self.all_worker.requestInterruption()
        self.btn_all_export.setEnabled(False)
        self.log_output.append("正在停止，请稍候...")

    def start_all_export(self):
        if not self.profiles:
            QMessageBox.warning(self, "提示", "没有任何已保存的配置")
            return
        missing = [n for n, p in self.profiles.items() if not p.get("export_dir", "").strip()]
        if missing:
            self.log_output.append(f"注意：以下配置未设置备份目录，将跳过：{', '.join(missing)}")
        self.btn_all_export.setText("停止")
        self.btn_all_export.setStyleSheet(
            "background-color: #f44336; color: white; font-weight: bold; "
            "min-height: 30px; min-width: 120px;"
        )
        self.btn_all_export.setEnabled(True)
        self.btn_export.setEnabled(False)
        self.btn_git_upload.setEnabled(False)
        self.progress_bar.setValue(0)
        self.log_output.clear()
        self.all_worker = AllProfilesWorker(dict(self.profiles))
        self.all_worker.progress.connect(self.progress_bar.setValue)
        self.all_worker.log.connect(self.log_output.append)
        self.all_worker.finished.connect(self.on_all_export_finished)
        self.all_worker.start()

    def on_all_export_finished(self, success, msg):
        self.btn_git_upload.setEnabled(True)
        self.btn_all_export.setEnabled(True)
        self.btn_all_export.setText("导出所有配置")
        self.btn_all_export.setStyleSheet(
            "background-color: #2196F3; color: white; font-weight: bold; "
            "min-height: 30px; min-width: 120px;"
        )
        self.btn_export.setEnabled(True)
        if success:
            QMessageBox.information(self, "完成", msg)
        else:
            QMessageBox.warning(self, "部分配置未完成", msg)

    # ── 删除配置 ─────────────────────────────────────
    def delete_current_profile(self):
        name = self.combo_profile.currentText().strip()
        if not name or name not in self.profiles:
            return
        del self.profiles[name]
        idx = self.combo_profile.findText(name)
        if idx >= 0:
            self.combo_profile.blockSignals(True)
            self.combo_profile.removeItem(idx)
            self.combo_profile.blockSignals(False)
        if self.current_profile == name:
            self.current_profile = ""
        self._write_config()
        self.log_output.append(f"配置 [{name}] 已删除")
        if self.profiles:
            self._load_profile(next(iter(self.profiles)))
        else:
            self.combo_profile.setEditText("")
            self._build_tree()
            self._build_table_data_grid()

    def closeEvent(self, event):
        if any(getattr(self, name, None) and getattr(self, name).isRunning()
               for name in ("worker", "all_worker", "git_worker")):
            QMessageBox.warning(self, "任务进行中", "请等待备份或上传结束后再关闭程序。")
            event.ignore()
            return
        if self.db_manager:
            self.db_manager.close()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

