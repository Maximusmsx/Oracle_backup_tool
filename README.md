# Oracle_backup_tool

Windows 上的 Oracle 数据库备份工具，使用 Python、PyQt6 和 python-oracledb。

支持多份连接配置、数据库对象按需加载与筛选、DDL 导出、表数据 SQL 导出，以及按 `yyyy-mm-dd` 建立备份子目录。Git 上传功能尚未实现。

## 运行

```powershell
cd db_backup_tool
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe main.py
```

连接配置保存在程序旁的 `config.json`。配置文件可能包含数据库密码，不提交到仓库。

## 测试

```powershell
cd db_backup_tool
.\venv\Scripts\python.exe -m unittest test_backup_options -v
```

测试使用模拟数据库和临时目录，不连接实际 Oracle。

## 打包

在项目根目录运行 `run_build.ps1`，或在安装依赖后运行 `db_backup_tool/build_exe.py`。EXE 输出到 `db_backup_tool/dist`。

如需支持 Oracle 11g，在 `db_backup_tool/oracle_client` 放入适合目标数据库的 Oracle Instant Client（包括 `oci.dll`）后打包。打包脚本会将客户端嵌入 EXE；客户端文件和生成文件不提交到仓库。
