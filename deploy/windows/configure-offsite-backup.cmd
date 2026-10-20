@echo off
setlocal
title OSS encrypted backup secure setup
powershell.exe -NoLogo -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0configure-offsite-backup.ps1" %*
exit /b %errorlevel%
