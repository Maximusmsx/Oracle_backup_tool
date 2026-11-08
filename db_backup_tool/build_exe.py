import PyInstaller.__main__
import os
import ssl

current_dir = os.path.abspath(os.path.dirname(__file__))
main_file = os.path.join(current_dir, "main.py")

params = [
    main_file,
    "--onefile",
    "--noconsole",
    "--clean",
    "--noconfirm",
    "--name=数据库备份工具",
    f"--icon={os.path.join(current_dir, 'assets', 'backup.ico')}",
    f"--add-data={os.path.join(current_dir, 'assets')}{os.pathsep}assets",
    f"--distpath={os.path.join(current_dir, 'dist')}",
    f"--workpath={os.path.join(current_dir, 'build_pack')}",
    f"--specpath={current_dir}",
    "--collect-all=cryptography",
    "--collect-all=oracledb",
    "--collect-all=dulwich",
    "--hidden-import=uuid",
    "--hidden-import=cffi",
    "--hidden-import=cryptography",
    "--hidden-import=cryptography.hazmat.primitives",
    "--hidden-import=cryptography.hazmat.backends",
    "--hidden-import=cryptography.hazmat.backends.openssl.backend",
]

# Some Python runtimes require this certificate beside their bundled ssl module.
ssl_certificate = os.path.join(os.path.dirname(ssl.__file__), "codex-rejected-cas.pem")
if os.path.isfile(ssl_certificate):
    params.append(f"--add-data={ssl_certificate}{os.pathsep}.")

# 只打包 Client 19 Basic 的 OCI 运行库及许可文件，避免带入工具、符号和备用库。
oracle_client_dir = os.path.join(current_dir, "oracle_client")
if os.path.isfile(os.path.join(oracle_client_dir, "oci.dll")):
    runtime_files = ("oci.dll", "oraociei19.dll", "orannzsbb19.dll", "oraons.dll",
                     "BASIC_LICENSE", "BASIC_README")
    for name in runtime_files:
        path = os.path.join(oracle_client_dir, name)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Oracle Client 19 Basic 运行文件缺失：{name}")
        params.append(f"--add-data={path}{os.pathsep}oracle_client")
    print("[INFO] 将内嵌 Oracle Client 19 Basic 的 OCI 运行库及许可文件")
else:
    print("[INFO] 未检测到 oracle_client/ 目录，exe 不内嵌 Oracle Client")
    print("       如需支持 Oracle 11g 且目标机器无客户端，请将 Instant Client 19 Basic")
    print("       的所有文件解压到 db_backup_tool/oracle_client/ 后重新打包")

if __name__ == "__main__":
    print(f"正在开始打包: {main_file}")
    PyInstaller.__main__.run(params)
    print("打包完成！请在 dist 目录下查找 EXE 文件。")
