@echo off
REM PBIP Developer Command Runner for Windows CMD
REM Exit with an explicit code: a bare "exit /b" sets ERRORLEVEL but cmd.exe still exits 0 when
REM run as "cmd /c dev.cmd ..." or from PowerShell (seen on Windows 10). "%ERRORLEVEL%" inside
REM ( ) is expanded when the block is parsed (always 0), so "call" re-expands %%ERRORLEVEL%% at
REM run time. No setlocal EnableDelayedExpansion: it resets ERRORLEVEL and mangles "!" in args.
REM No goto labels: the repo stores this file with LF endings, where label lookup is fragile.
where uv >nul 2>nul
if not errorlevel 1 (
    uv run python "%~dp0scripts\dev.py" %*
    call exit /b %%ERRORLEVEL%%
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0scripts\dev.py" %*
    call exit /b %%ERRORLEVEL%%
)
echo Neither 'uv' nor 'python' was found in PATH.
exit /b 1
