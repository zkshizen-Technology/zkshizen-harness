# Install the zkshizen harness for the current user (Windows).
#
#   powershell -ExecutionPolicy Bypass -File install\install.ps1             from this checkout
#   powershell -ExecutionPolicy Bypass -File install\install.ps1 <git-url>   from a git repository
#
# Uses pipx when available, otherwise a private venv in
# %LOCALAPPDATA%\zkshizen\venv plus a zks.cmd shim in %LOCALAPPDATA%\zkshizen\bin.
param([string]$Source = "")

$ErrorActionPreference = "Stop"

if (-not $Source) {
    $Source = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    if (-not (Test-Path (Join-Path $Source "pyproject.toml"))) {
        Write-Error "Run from a checkout or pass a git URL."
    }
} elseif (-not $Source.StartsWith("git+")) {
    $Source = "git+$Source"
}

# Find Python 3.11+ (the py launcher first, then python on PATH).
$Py = $null
foreach ($candidate in @(@("py", "-3"), @("python"))) {
    $exe = $candidate[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $pyArgs = @($candidate | Select-Object -Skip 1)
    & $exe @pyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -eq 0) { $Py = $candidate; break }
}
if (-not $Py) { Write-Error "Python 3.11 or newer is required. Install it from python.org." }
$PyExe = $Py[0]
$PyArgs = @($Py | Select-Object -Skip 1)

if (Get-Command pipx -ErrorAction SilentlyContinue) {
    Write-Host "installing with pipx from $Source"
    pipx install --force $Source
    if ($LASTEXITCODE -ne 0) { Write-Error "pipx install failed" }
} else {
    $Root = Join-Path $env:LOCALAPPDATA "zkshizen"
    $Venv = Join-Path $Root "venv"
    $BinDir = Join-Path $Root "bin"
    Write-Host "creating venv in $Venv"
    & $PyExe @PyArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Write-Error "venv creation failed" }
    $VenvPy = Join-Path $Venv "Scripts\python.exe"
    & $VenvPy -m pip install --upgrade pip | Out-Null
    & $VenvPy -m pip install --upgrade $Source
    if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed" }

    New-Item -ItemType Directory -Force $BinDir | Out-Null
    $Shim = Join-Path $BinDir "zks.cmd"
    $ZksExe = Join-Path $Venv "Scripts\zks.exe"
    Set-Content -Path $Shim -Encoding ascii -Value "@echo off`r`n`"$ZksExe`" %*"
    Write-Host "wrote $Shim"

    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $UserPath) { $UserPath = "" }
    if (($UserPath -split ";") -notcontains $BinDir) {
        [Environment]::SetEnvironmentVariable("Path", ($UserPath.TrimEnd(";") + ";" + $BinDir).TrimStart(";"), "User")
        Write-Host "added $BinDir to your user PATH (open a new terminal)"
    }
}

Write-Host ""
Write-Host "Installed. Next steps:"
Write-Host "  zks init"
Write-Host "  zks config set anthropic --provider anthropic --api-key - --default"
Write-Host "  zks config test"
Write-Host "  zks user passwd admin"
Write-Host "  zks user totp admin"
Write-Host "  zks serve                 # or: zks service install"
