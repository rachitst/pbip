<#
.SYNOPSIS
    PBIP Developer Command Runner for Windows PowerShell.
.DESCRIPTION
    Native PowerShell entry point wrapping scripts/dev.py to provide a
    cross-platform developer experience matching the Makefile on Linux/macOS.
#>
# Deliberately a simple script (no [CmdletBinding()] / param()): an advanced script owns the common
# parameters and their prefixes, so "-p", "-v", "-d" or "-ErrorAction" were bound to
# -PipelineVariable/-Verbose/-Debug instead of reaching dev.py, and the run ended with exit 0.
# Every argument is forwarded unchanged via $args.
# No $ErrorActionPreference = "Stop": in Windows PowerShell 5.1 it turns each stderr line of the
# native command into a terminating error when the caller redirects (2>&1 | Tee-Object, *> file).
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Prefer uv run python if uv is installed, otherwise fallback to system python
if (Get-Command uv -ErrorAction SilentlyContinue) {
    & uv run python "$ScriptDir\scripts\dev.py" @args
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python "$ScriptDir\scripts\dev.py" @args
} else {
    Write-Error "Neither 'uv' nor 'python' was found in PATH. Please install uv (winget install --id astral-sh.uv -e) or Python 3.12+."
    exit 1
}
exit $LASTEXITCODE
