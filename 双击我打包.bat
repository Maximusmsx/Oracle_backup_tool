@echo off
title DB_Backup_Tool_Builder
echo Starting build process...
echo Please wait, this may take a few minutes...

cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File .\run_build.ps1

echo.
echo ======================================================
echo Done. Please check the "dist" folder for your EXE.
echo ======================================================
pause
