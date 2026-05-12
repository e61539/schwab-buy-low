@echo off
setlocal EnableExtensions
REM Phase 4A Trend Rider acceptance compatibility wrapper.

set "ROOT=%~dp0"
call "%ROOT%launchers\trend_accept.cmd" %*
exit /b %ERRORLEVEL%
