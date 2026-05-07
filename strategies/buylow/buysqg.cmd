@echo off
setlocal EnableExtensions
REM Legacy orchestration compatibility: delegate to the root multi-window buysqg launcher.

set "THIS_DIR=%~dp0"
pushd "%THIS_DIR%..\.." || exit /b 1
call "%CD%\buysqg.cmd" %*
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%
