@echo off
setlocal EnableExtensions
REM Legacy orchestration compatibility: delegate to the root multi-window sellall launcher.

set "THIS_DIR=%~dp0"
pushd "%THIS_DIR%..\.." || exit /b 1
call "%CD%\sellall.cmd" %*
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%
