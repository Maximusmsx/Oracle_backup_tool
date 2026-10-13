# Oracle_backup_tool

Windows 上的 Oracle 数据库备份工具，使用 Python、PyQt6 和 python-oracledb。

支持多份连接配置、数据库对象按需加载与筛选、DDL 导出、表数据 SQL 导出、按 `yyyy-mm-dd` 建立备份子目录，以及备份完成后提交并推送到 Git。

对象列表与导出范围限定为当前连接用户自有的 schema（使用 `USER_*` 字典视图）。以 LXD 登录时不显示 APEX、CWBS 等其他 schema 的对象；旧配置保存的跨 schema 勾选也会被过滤。数据库链接名称自身包含的域名点号保留。

## Git 备份

每份配置可独立设置 Git 上传开关、HTTP／HTTPS 仓库地址、分支、仓库子目录、用户名和密码／Token。GitHub 使用 Token，自建 Git 服务可使用其支持的密码或 Token。Git 功能使用 Dulwich，分发的 EXE 无需目标机器安装 Git、Python 或 SSH 客户端。

“按日建目录”仅影响本地归档：SQL 保存到 `备份目录/yyyy-mm-dd/对象类型/`。“上传 Git”独立生成或同步 `备份目录/对象类型/` 下的 SQL，并从根目录提交上传。两项同时开启时，本次导出的 SQL 在日期目录和根目录各保留一份，两份内容完全相同，使用同一次数据库导出生成的数据。

各配置应使用不同的仓库子目录，例如 `backups/学校A`。Git 路径始终为 `backups/学校A/TABLE/表名.sql`，不受本地日期选项影响。同一对象类型下已删除的 SQL 会在新提交中移除；历史日期目录不上传，也不会因上传而删除。

内容未变化时不提交；远端分支在上传过程中发生变化时拒绝覆盖。上传失败不删除本地 SQL，可点击“上传现有备份”重试，该按钮始终上传备份根目录中的 SQL，与日期选项无关。取消备份、导出失败或 SQL 含错误时不会自动上传。新分支基于远端默认分支创建，空仓库直接创建首个提交。

Git 密码／Token 通过 Windows DPAPI 加密保存，绑定当前 Windows 用户；把配置复制到其他用户或机器后，需要重新输入 Git 凭据。数据库连接密码仍沿用原有配置格式。上传范围仅为对象类型目录和 DATA 目录的 SQL，不上传 `config.json`、凭据或其他文件。

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
.\venv\Scripts\python.exe -m unittest discover -p "test_*.py" -v
```

测试使用模拟数据库、临时目录及本地 HTTP Git 服务，不连接实际 Oracle。HTTP 测试包含用户名／密码认证、无外部进程、远端冲突、历史目录保留、自动上传和上传失败后本地文件保留。

## 打包

在项目根目录运行 `run_build.ps1`，或在安装依赖后运行 `db_backup_tool/build_exe.py`。EXE 输出到 `db_backup_tool/dist`。

如需支持 Oracle 11g，在 `db_backup_tool/oracle_client` 放入适合目标数据库的 Oracle Instant Client（包括 `oci.dll`）后打包。打包脚本会将客户端嵌入 EXE；客户端文件和生成文件不提交到仓库。
