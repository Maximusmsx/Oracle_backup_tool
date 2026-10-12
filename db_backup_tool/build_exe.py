import PyInstaller.__main__
import os

current_dir = os.path.abspath(os.path.dirname(__file__))
main_file = os.path.join(current_dir, "main.py")

params = [
    main_file,
    "--onefile",
    "--noconsole",
    "--clean",
    "--noconfirm",
    "--name=数据库备份工具",
    f"--distpath={os.path.join(current_dir, 'dist')}",
    f"--workpath={os.path.join(current_dir, 'build_pack')}",
    f"--specpath={current_dir}",
    "--collect-all=cryptography",
    "--collect-all=oracledb",
    "--hidden-import=uuid",
    "--hidden-import=cffi",
    "--hidden-import=cryptography",
    "--hidden-import=cryptography.hazmat.primitives",
    "--hidden-import=cryptography.hazmat.backends",
    "--hidden-import=cryptography.hazmat.backends.openssl.backend",
]

# 如果本目录下存在 oracle_client 文件夹且包含 oci.dll，则一并打包进 exe
oracle_client_dir = os.path.join(current_dir, "oracle_client")
if os.path.isfile(os.path.join(oracle_client_dir, "oci.dll")):
    params.append(f"--add-data={oracle_client_dir}{os.pathsep}oracle_client")
    print(f"[INFO] 检测到 oracle_client/ 目录，Oracle Client 将内嵌到 exe 中")
else:
    print("[INFO] 未检测到 oracle_client/ 目录，exe 不内嵌 Oracle Client")
    print("       如需支持 Oracle 11g 且目标机器无客户端，请将 Instant Client Basic Lite")
    print("       的所有文件解压到 db_backup_tool/oracle_client/ 后重新打包")

if __name__ == "__main__":
    print(f"正在开始打包: {main_file}")
    PyInstaller.__main__.run(params)
    print("打包完成！请在 dist 目录下查找 EXE 文件。")
