<#
.SYNOPSIS
    PBIP Developer Command Runner for Windows PowerShell.
.DESCRIPTION
    Native PowerShell entry point wrapping scripts/dev.py to provide a
    cross-platform developer experience matching the Makefile on Linux/macOS.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Prefer uv run python if uv is installed, otherwise fallback to system python
if (Get-Command uv -ErrorAction SilentlyContinue) {
    & uv run python "$ScriptDir\scripts\dev.py" @Arguments
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python "$ScriptDir\scripts\dev.py" @Arguments
} else {
    Write-Error "Neither 'uv' nor 'python' was found in PATH. Please install uv (winget install Astral-sh.uv) or Python 3.12+."
    exit 1
}
exit $LASTEXITCODE
