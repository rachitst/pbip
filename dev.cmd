@echo off
REM PBIP Developer Command Runner for Windows CMD
REM "exit /b" without a code keeps the ERRORLEVEL of the command that just ran. Do not write
REM "exit /b %ERRORLEVEL%" inside ( ): it is expanded when the block is parsed (always 0).
REM No goto labels: the repo stores this file with LF endings, where label lookup is fragile.
where uv >nul 2>nul
if not errorlevel 1 (
    uv run python "%~dp0scripts\dev.py" %*
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0scripts\dev.py" %*
    exit /b
)
echo Neither 'uv' nor 'python' was found in PATH.
exit /b 1
