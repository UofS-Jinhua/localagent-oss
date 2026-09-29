<#
.SYNOPSIS
Set up LocalAgent on this computer: a virtualenv, the dependencies, and a shortcut that opens the window.

.EXAMPLE
.\scripts\setup.ps1
.EXAMPLE
.\scripts\setup.ps1 -BaseUrl http://<your-lm-studio-host>:1234 -Dev
#>
[CmdletBinding()]
param(
    # Address of your model server (LM Studio, Ollama, ...); registers it and lists its models.
    [string]$BaseUrl = "",
    # Also install the test dependencies.
    [switch]$Dev,
    # Do not create the shortcut.
    [switch]$NoShortcut,
    # Also put a copy on the Desktop and in the Start menu.
    [switch]$Desktop,
    # Where to put the shortcut (default: this project folder).
    [string]$ShortcutDir = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $repo ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$pythonw = Join-Path $venv "Scripts\pythonw.exe"

function Write-Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Write-Note($text) { Write-Host "    $text" -ForegroundColor DarkGray }

# ---- 1. find a Python interpreter --------------------------------------------------------------
function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $candidates += ,@("py", @("-3"))
    }
    foreach ($name in @("python3", "python")) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { $candidates += ,@($found.Source, @()) }
    }
    foreach ($candidate in $candidates) {
        $exe, $prefix = $candidate
        $version = & $exe @prefix -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $version) {
            $parts = $version.Trim().Split(".")
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12) {
                return @{ Exe = $exe; Prefix = $prefix; Version = $version.Trim() }
            }
            Write-Note "skipping Python $($version.Trim()) at $exe (3.12 or newer is needed)"
        }
    }
    return $null
}

Write-Step "Looking for Python 3.12+"
$interpreter = Find-Python
if (-not $interpreter) {
    Write-Host "No suitable Python found." -ForegroundColor Red
    Write-Host "Install Python 3.12 or newer from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this script again."
    exit 1
}
Write-Note "using Python $($interpreter.Version) ($($interpreter.Exe))"

# ---- 2. virtualenv and dependencies ------------------------------------------------------------
if (Test-Path $python) {
    Write-Step "Reusing the existing virtualenv ($venv)"
} else {
    Write-Step "Creating the virtualenv ($venv)"
    & $interpreter.Exe @($interpreter.Prefix) -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "could not create the virtualenv" }
}

$extras = "desktop"
if ($Dev) { $extras = "dev,desktop" }
Write-Step "Installing LocalAgent and its dependencies [$extras]"
Write-Note "this downloads a few packages the first time; later runs are quick"
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -e "$repo[$extras]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
& $python -c "import localagent, fastapi, httpx, pypdf; print('    installed LocalAgent', localagent.__version__)"

# ---- 3. register a model server ----------------------------------------------------------------
$registry = Join-Path $repo "data\models.yaml"
$legacy = Join-Path $env:USERPROFILE ".localagent\models.yaml"
if ($BaseUrl) {
    Write-Step "Registering $BaseUrl"
    & (Join-Path $venv "Scripts\la.exe") init --base-url $BaseUrl
} elseif (-not (Test-Path $registry) -and -not (Test-Path $legacy)) {
    Write-Note "no model server registered yet: run  .venv\Scripts\la init --base-url http://<host>:1234"
}

# ---- 4. shortcuts -------------------------------------------------------------------------------
if (-not $NoShortcut) {
    Write-Step "Creating the shortcut"
    $icon = Join-Path $repo "assets\localagent.ico"
    # The shortcut lives in the project folder; copy it wherever you like (-Desktop does that for you).
    $targets = @()
    if ($ShortcutDir) {
        $targets += $ShortcutDir
    } else {
        $targets += $repo
        if ($Desktop) {
            $targets += [Environment]::GetFolderPath("Desktop")
            $targets += Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
        }
    }
    $shell = New-Object -ComObject WScript.Shell
    foreach ($directory in $targets) {
        if (-not (Test-Path $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
        $path = Join-Path $directory "LocalAgent.lnk"
        $link = $shell.CreateShortcut($path)
        # pythonw keeps the console window away; the module is the same entry point as the `la` command.
        $link.TargetPath = $pythonw
        $link.Arguments = "-m localagent.cli.main ui"
        $link.WorkingDirectory = $repo
        $link.Description = "LocalAgent: a coding agent for local LLMs"
        if (Test-Path $icon) { $link.IconLocation = $icon }
        $link.Save()
        Write-Note "created $path"
    }
}

# ---- 5. what to do next -------------------------------------------------------------------------
Write-Host "`nDone." -ForegroundColor Green
Write-Host "  Open the window:   double-click LocalAgent.lnk in this folder (copy it to the Desktop if you like),"
Write-Host "                     or run  .\LocalAgent.cmd"
Write-Host "  Command line:      .venv\Scripts\la run -w <folder> `"fix the failing test`""
Write-Host "  Add a model:       .venv\Scripts\la init --base-url http://<host>:1234   (or the gear in the window)"
if ($Dev) { Write-Host "  Tests:             .venv\Scripts\python -m pytest" }
