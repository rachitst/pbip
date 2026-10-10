@echo off
REM PBIP Developer Command Runner for Windows CMD
set SCRIPT_DIR=%~dp0
where uv >nul 2>nul
if %ERRORLEVEL% equ 0 (
    uv run python "%SCRIPT_DIR%scripts\dev.py" %*
    exit /b %ERRORLEVEL%
)
where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    python "%SCRIPT_DIR%scripts\dev.py" %*
    exit /b %ERRORLEVEL%
)
echo Neither 'uv' nor 'python' was found in PATH.
exit /b 1
