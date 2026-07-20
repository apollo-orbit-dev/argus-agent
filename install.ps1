# Argus installer (Windows PowerShell).
#
#   irm https://raw.githubusercontent.com/apollo-orbit-dev/argus-agent/main/install.ps1 | iex
#
# Clones the repo (unless you're already inside it), creates a virtualenv, installs
# Argus, and copies .env.example -> .env so you're one API key away from running.
# macOS/Linux users: use install.sh instead.

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/apollo-orbit-dev/argus-agent"
$DirName = "argus"

function Info($m) { Write-Host "  $m" }
function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [x] $m" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "=================================================="
Write-Host "  Argus installer (Windows)"
Write-Host "=================================================="
Write-Host ""

# --- python launcher + version (>= 3.11) ---
$py = $null
foreach ($cand in @("python", "py")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
}
if (-not $py) { Fail "Python not found. Install Python 3.11+ from https://python.org (tick 'Add to PATH') and re-run." }

$verOk = (& $py -c "import sys; print(1 if sys.version_info >= (3,11) else 0)").Trim()
$verStr = (& $py -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
if ($verOk -ne "1") { Fail "Argus needs Python 3.11+, found $verStr. Install a newer Python and re-run." }
Ok "python $verStr"

# --- git ---
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git not found. Install Git for Windows and re-run." }
Ok "git found"

# --- clone (skip if already inside the repo) ---
if ((Test-Path "main.py") -and (Test-Path "pyproject.toml")) {
    Info "Already inside an Argus checkout - skipping clone."
} else {
    if (Test-Path $DirName) { Warn "./$DirName already exists - using it instead of cloning again." }
    else {
        Info "Cloning $RepoUrl ..."; git clone $RepoUrl $DirName; Ok "cloned into ./$DirName"
        # Pin to the latest released version (a stable tag), not the moving main branch.
        $LatestTag = (git -C $DirName tag -l 'v*' --sort=-v:refname | Select-Object -First 1)
        if ($LatestTag) { git -C $DirName -c advice.detachedHead=false checkout -q $LatestTag; Ok "checked out latest release $LatestTag" }
    }
    Set-Location $DirName
}
$ProjectDir = (Get-Location).Path

# --- virtualenv ---
if (-not (Test-Path ".venv")) { Info "Creating virtualenv (.venv) ..."; & $py -m venv .venv; Ok "virtualenv created" }
else { Info "Reusing existing .venv" }
$venvPy = Join-Path ".venv" "Scripts\python.exe"

# --- install ---
Info "Installing Argus and its dependencies (this can take a minute) ..."
& $venvPy -m pip install --upgrade pip | Out-Null
& $venvPy -m pip install -e .
Ok "Argus installed"

# --- .env ---
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Ok "created .env from .env.example" }
else { Info ".env already exists - leaving it as-is" }

Write-Host ""
Write-Host "=================================================="
Write-Host "  Install complete"
Write-Host "=================================================="
Write-Host ""
Write-Host "  Next steps:"
Write-Host "    1) add your model API key to $ProjectDir\.env"
Write-Host "       (the easiest default is OpenRouter: https://openrouter.ai)"
Write-Host "    2) activate the venv:  .\.venv\Scripts\Activate.ps1"
Write-Host "    3) run:  argus start"
Write-Host "    4) open http://localhost:8700"
Write-Host ""
Write-Host "  Optional features that need native prereqs (skip unless you want them):"
Write-Host "    PDF export:  .venv\Scripts\python -m pip install -e '.[pdf]'   (needs GTK)"
Write-Host "    OCR:         .venv\Scripts\python -m pip install -e '.[ocr]'   (needs Tesseract)"
Write-Host ""
