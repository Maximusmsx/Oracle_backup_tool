$env:HTTP_PROXY=$null
$env:HTTPS_PROXY=$null
$env:ALL_PROXY=$null

cd "db_backup_tool"

Write-Host "Creating Virtual Environment..."
python -m venv venv

Write-Host "Updating Pip..."
.\venv\Scripts\python -m pip install --upgrade pip

Write-Host "Installing Requirements from pypi.org..."
.\venv\Scripts\python -m pip install -r requirements.txt

Write-Host "Starting PyInstaller Building Process..."
.\venv\Scripts\python build_exe.py

Write-Host "Build finished. Please check the dist folder."
