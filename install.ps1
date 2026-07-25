# Argus installer (Windows PowerShell).
#
#   irm https://raw.githubusercontent.com/apollo-orbit-dev/argus-agent/main/install.ps1 | iex
#
# Clones the repo (unless you're already inside it), creates a virtualenv, installs
# Argus, and copies .env.example -> .env so you're one API key away from running.
# macOS/Linux users: use install.sh instead.

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/apollo-orbit-dev/argus-agent"
$DefaultDirName = "argus"
$DirName = $DefaultDirName

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
    # Install folder name - Enter keeps the default "argus". Prompting here makes running a
    # second Argus instance (e.g. a daily one and a dev one) next to the first easy: each
    # install picks its own folder.
    $attempts = 0
    $maxAttempts = 5
    while ($true) {
        if ($attempts -eq 0) { $reply = Read-Host "  Install folder name [$DefaultDirName]" }
        else { $reply = Read-Host "  Install folder name (or press Enter/q to cancel)" }

        # Empty/q only means "cancel" once we're re-prompting after a collision - on the very
        # first ask it just means "keep the default", same as the port prompt.
        if ($attempts -gt 0 -and ([string]::IsNullOrEmpty($reply) -or $reply -eq "q")) { Fail "Install cancelled." }

        $candidate = $DefaultDirName
        if ($reply) {
            if ($reply -match '^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$') { $candidate = $reply }
            else { Warn "'$reply' is not a valid folder name - using $DefaultDirName" }
        }

        # Refuse to clobber an existing install. That directory may hold the operator's .env
        # (API keys) and data/ (sessions, tables, memory) - overwriting or half-upgrading it is
        # real data loss, and the whole point of this prompt is installing a second instance
        # NEXT TO a first one, not into it. Re-prompt for a different name rather than aborting
        # outright - bounded, so it can't spin forever.
        if ((Test-Path $candidate) -and ((Test-Path (Join-Path $candidate ".env")) -or (Test-Path (Join-Path $candidate "data") -PathType Container))) {
            Warn "$(Join-Path (Get-Location).Path $candidate) already looks like an existing Argus install (found .env or data/) - it will not be touched."
            $attempts++
            if ($attempts -ge $maxAttempts) { Fail "Too many attempts choosing a folder name - re-run and pick one that isn't an existing install." }
            continue
        }
        $DirName = $candidate
        break
    }

    if (Test-Path $DirName) { Warn "./$DirName already exists - using it instead of cloning again." }
    else {
        Info "Cloning $RepoUrl ..."; git clone $RepoUrl $DirName; Ok "cloned into ./$DirName"
        # Pin to the latest released version (a stable tag), not the moving main branch.
        $LatestTag = (git -C $DirName tag -l 'v*' --sort=-v:refname | Select-Object -First 1)
        if ($LatestTag) { git -C $DirName -c advice.detachedHead=false checkout -q $LatestTag; Ok "checked out latest release $LatestTag" }
    }
    Set-Location $DirName
    if ($DirName -ne $DefaultDirName) {
        Info "Using a non-default folder name - if you enable the sandbox, also set SANDBOX_INSTANCE in .env so this instance gets its own podman namespace."
    }
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
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"; Ok "created .env from .env.example"
    # Dashboard port - Enter keeps the default. Lets you run several Argus instances on one
    # machine, each on its own port.
    $DefaultPort = 8700
    $Port = $DefaultPort
    $reply = Read-Host "  Dashboard port [$DefaultPort]"
    if ($reply) {
        $n = 0
        if ([int]::TryParse($reply, [ref]$n) -and $n -ge 1 -and $n -le 65535) { $Port = $n }
        else { Warn "'$reply' is not a valid port (1-65535) - using $DefaultPort" }
    }
    # Set PORT in the fresh .env (replace .env.example's PORT= line, or append if absent).
    $lines = @(Get-Content ".env" | Where-Object { $_ -notmatch '^PORT=' })
    ($lines + "PORT=$Port") | Set-Content ".env"
    Ok "dashboard port set to $Port"
} else { Info ".env already exists - leaving it as-is" }

# Port for the closing message (read back from .env; default 8700).
$PortMsg = (Select-String -Path ".env" -Pattern '^PORT=(.+)$' | Select-Object -Last 1).Matches.Groups[1].Value
if (-not $PortMsg) { $PortMsg = 8700 }

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
Write-Host "    4) open http://localhost:$PortMsg"
Write-Host ""
Write-Host "  Optional features that need native prereqs (skip unless you want them):"
Write-Host "    PDF export:  .venv\Scripts\python -m pip install -e '.[pdf]'   (needs GTK)"
Write-Host "    OCR:         .venv\Scripts\python -m pip install -e '.[ocr]'   (needs Tesseract)"
Write-Host ""
