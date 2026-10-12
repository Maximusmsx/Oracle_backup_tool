# 强行清理环境变量中的代理
$env:HTTP_PROXY=$null
$env:HTTPS_PROXY=$null
$env:http_proxy=$null
$env:https_proxy=$null
$env:ALL_PROXY=$null
$env:all_proxy=$null

cd "db_backup_tool"

Write-Host "Step 1: Attempting to install requirements via Tsinghua mirror..."
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn

if ($LASTEXITCODE -eq 0) {
    Write-Host "Success! Building EXE..."
    python build_exe.py
} else {
    Write-Host "Tsinghua failed. Step 2: Trying Alibaba mirror..."
    python -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
    if ($LASTEXITCODE -eq 0) {
        python build_exe.py
    } else {
        Write-Host "CRITICAL ERROR: Environment network is completely blocked by proxy."
        exit 1
    }
}
