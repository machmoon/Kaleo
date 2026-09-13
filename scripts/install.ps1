<#
.SYNOPSIS
    One-command install for Windows (PowerShell 5.1+ / PowerShell 7).

.DESCRIPTION
    The Windows twin of scripts/install.sh: find Python 3.11+, create .venv,
    install the engine editable with its extras, and build the web bundle when
    Node 22+ is present. Re-running is safe -- an existing .venv is reused, and
    pip and npm are both idempotent. Nothing is installed outside the repo, so
    no elevation is ever required.

    Note the venv layout differs from POSIX: interpreters and console scripts
    live in .venv\Scripts, not .venv/bin.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoWeb -DryRun
#>
[CmdletBinding()]
param(
    [switch]$NoWeb,
    [switch]$DryRun
)

# Stop on the first failure rather than limping onward: a half-installed tree
# fails later, somewhere far from the real cause.
$ErrorActionPreference = 'Stop'

$MinPyMinor   = 11  # pyproject: requires-python >= 3.11
$MinNodeMajor = 22  # CI pins 22; Vite 8 refuses to start on anything older

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Venv = Join-Path $Root '.venv'
$Py   = Join-Path $Venv 'Scripts\python.exe'

function Write-Step([string]$Text) { Write-Host "`n==> $Text" }

function Stop-Install([string]$Problem, [string]$Fix) {
    Write-Host ""
    Write-Host "error: $Problem" -ForegroundColor Red
    if ($Fix) { Write-Host "try:   $Fix" -ForegroundColor Yellow }
    exit 1
}

# Every invocation goes through here so -DryRun covers the whole script, and so
# a non-zero exit code from a child process is never silently swallowed (native
# commands do not trip $ErrorActionPreference on their own).
function Invoke-Step {
    param([string]$Exe, [string[]]$Arguments, [string]$Problem, [string]$Fix)
    if ($DryRun) {
        Write-Host "  would run: $Exe $($Arguments -join ' ')"
        return
    }
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Stop-Install $Problem $Fix }
}

if (-not (Test-Path (Join-Path $Root 'pyproject.toml'))) {
    Stop-Install "no pyproject.toml at $Root -- this script must stay in the repo's scripts\ directory" `
                 "clone the repository again and run scripts\install.ps1 from inside it"
}

Write-Host "Silkscreen installer"
Write-Host "repository: $Root"
if ($DryRun) { Write-Host "mode:       dry run (nothing will be written)" }

# ------------------------------------------------------------------ python --
Write-Step "Locating Python >= 3.$MinPyMinor"

# Ask each candidate for its own version instead of trusting its name. Windows
# also ships an App Execution Alias called python.exe that is not Python at all
# and merely opens the Store; a version probe rejects it like any other miss.
function Test-Python([string]$Exe) {
    try {
        & $Exe -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, $MinPyMinor) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$Python = $null
$candidates = @()
# The py launcher is the reliable way to reach a specific version on Windows.
if (Get-Command 'py' -ErrorAction SilentlyContinue) {
    foreach ($v in '3.13', '3.12', '3.11') {
        $out = (& py "-$v" -c "import sys; print(sys.executable)" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $out) { $candidates += $out.Trim() }
    }
}
foreach ($name in 'python3.13', 'python3.12', 'python3.11', 'python3', 'python') {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
}

foreach ($candidate in $candidates) {
    if ((Test-Path $candidate) -and (Test-Python $candidate)) { $Python = $candidate; break }
}

if (-not $Python) {
    Stop-Install "no Python 3.$MinPyMinor or newer on PATH" `
                 "install it from https://python.org/downloads (tick 'Add python.exe to PATH'), then re-run"
}
$pyVersion = (& $Python -c "import platform; print(platform.python_version())").Trim()
Write-Host "found: $Python ($pyVersion)"

# -------------------------------------------------------------------- venv --
Write-Step "Virtual environment"
if (Test-Path $Py) {
    Write-Host "reusing $Venv"
} elseif (Test-Path $Venv) {
    # Exists but has no interpreter: a half-created venv from an interrupted
    # run. Reusing it yields one Python's stdlib with another's packages.
    Stop-Install "$Venv exists but has no Scripts\python.exe (interrupted install?)" `
                 "Remove-Item -Recurse -Force '$Venv'; then re-run this script"
} else {
    Write-Host "creating $Venv"
    Invoke-Step $Python @('-m', 'venv', $Venv) `
        "could not create a virtual environment" `
        "re-run the Python installer and make sure the venv module is included"
}

Write-Step "Installing the engine (editable, with dev/agents/cloud/adk/cad extras)"
# `cad` is the build123d enclosure kernel, and it is what makes the case step
# work at all: without it that step refuses in words naming this extra rather
# than shipping a worse case, and the kernel tests skip.
$target = "$Root[dev,agents,cloud,adk,cad]"
Invoke-Step $Py @('-m', 'pip', 'install', '--upgrade', '--quiet', 'pip') `
    "could not upgrade pip" "run '$Py -m pip install --upgrade pip' by hand"
Invoke-Step $Py @('-m', 'pip', 'install', '-e', $target) `
    "pip install failed" `
    "re-read the output above; if ortools has no wheel for this platform, check '$Py -VV'"

# ---------------------------------------------------------------- frontend --
$WebBuilt = $false
$frontend = Join-Path $Root 'frontend'
$npm = Get-Command 'npm' -ErrorAction SilentlyContinue
$node = Get-Command 'node' -ErrorAction SilentlyContinue

if ($NoWeb) {
    Write-Step "Web UI: skipped (-NoWeb)"
} elseif (-not $node -or -not $npm) {
    Write-Step "Web UI: skipped"
    Write-Host "Node and npm were not found on PATH. The API and the CLI work without"
    Write-Host "them; only the browser UI needs a build. Install Node $MinNodeMajor+ from"
    Write-Host "https://nodejs.org and re-run this script to add it."
} else {
    $nodeMajor = [int](& node -p 'process.versions.node.split(".")[0]')
    if ($nodeMajor -lt $MinNodeMajor) {
        Write-Step "Web UI: skipped"
        Write-Host "Node v$nodeMajor is older than v$MinNodeMajor, which Vite requires."
        Write-Host "The API and the CLI work without it. Upgrade Node and re-run to add the UI."
    } else {
        Write-Step "Building the web UI (Node v$nodeMajor)"
        # npm.cmd is a batch file; call it through the resolved command object
        # so a space in the repo path does not re-split the arguments.
        $npmExe = $npm.Source
        if (Test-Path (Join-Path $frontend 'package-lock.json')) {
            Invoke-Step $npmExe @('--prefix', $frontend, 'ci') `
                "npm ci failed in $frontend" `
                "delete $frontend\node_modules and re-run this script"
        } else {
            Invoke-Step $npmExe @('--prefix', $frontend, 'install') `
                "npm install failed in $frontend" `
                "run it by hand in $frontend to see the full output"
        }
        Invoke-Step $npmExe @('--prefix', $frontend, 'run', 'build') `
            "the web build failed" "cd '$frontend'; npm run build"
        $WebBuilt = -not $DryRun
    }
}

# ------------------------------------------------------------- next steps --
if ($DryRun) {
    Write-Host ""
    Write-Host "Dry run complete. Nothing was written."
    exit 0
}

Write-Step "Installed"
Write-Host "  engine   $Venv"
if ($WebBuilt -or (Test-Path (Join-Path $frontend 'dist\index.html'))) {
    Write-Host "  web UI   $frontend\dist"
} else {
    Write-Host "  web UI   not built (API-only)"
}

Write-Host @"

Next steps:

  1. Configure a Gemini API key (writes .env, never echoes the key):

       .\.venv\Scripts\silkscreen.exe setup

  2. Start the app and open it in a browser:

       .\.venv\Scripts\silkscreen.exe serve

  Or generate a board straight from the command line:

       .\.venv\Scripts\silkscreen.exe "an stm32 stepper driver"

  Everything except the model calls runs offline; the test suite needs no key:

       .\.venv\Scripts\python.exe -m pytest -q
"@
