@echo off
setlocal EnableExtensions
REM Phase 2 path hardening: legacy wrapper for backward compatibility.

set "ROOT=%~dp0"
call "%ROOT%launchers\run_trade_server.cmd" %*
exit /b %ERRORLEVEL%
