@echo off
setlocal
title DeepSeek API secure setup
powershell.exe -NoLogo -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0configure-model-api.ps1"
exit /b %errorlevel%
