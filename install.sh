#!/usr/bin/env bash
# Argus installer.
#
#   curl -fsSL https://raw.githubusercontent.com/apollo-orbit-dev/argus-agent/main/install.sh | bash
#
# Clones the repo (unless you're already inside it), creates a virtualenv, installs
# Argus, and copies .env.example -> .env so you're one API key away from running.
#
# Windows users: use install.ps1 instead (irm ... | iex). Or run this script inside WSL,
# or follow the "Manual install" steps in README.md.
set -e

REPO_URL="https://github.com/apollo-orbit-dev/argus-agent"
DIR_NAME="argus"

info()  { printf '  %s\n' "$1"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$1"; }
fail()  { printf '  \033[31m✗\033[0m %s\n' "$1"; exit 1; }

echo ""
echo "=================================================="
echo "  Argus installer"
echo "=================================================="
echo ""

# --- python3 >= 3.11 -------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 not found. Install Python 3.11+ and re-run this script."
fi

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)' 2>/dev/null || echo 0)
PY_VER=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo "unknown")
if [ "$PY_OK" != "1" ]; then
    fail "Argus needs Python 3.11+, found $PY_VER. Install a newer Python and re-run."
fi
ok "python3 $PY_VER"

# --- git --------------------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
    fail "git not found. Install git and re-run this script."
fi
ok "git found"

# --- clone (skip if already inside the repo) --------------------------------
if [ -f "main.py" ] && [ -f "pyproject.toml" ]; then
    info "Already inside an Argus checkout — skipping clone."
    PROJECT_DIR="$(pwd)"
else
    if [ -d "$DIR_NAME" ]; then
        warn "./$DIR_NAME already exists — using it instead of cloning again."
    else
        info "Cloning $REPO_URL ..."
        git clone "$REPO_URL" "$DIR_NAME"
        ok "cloned into ./$DIR_NAME"
    fi
    cd "$DIR_NAME"
    PROJECT_DIR="$(pwd)"
fi

# --- virtualenv ---------------------------------------------------------------
if [ ! -d ".venv" ]; then
    info "Creating virtualenv (.venv) ..."
    python3 -m venv .venv
    ok "virtualenv created"
else
    info "Reusing existing .venv"
fi

# shellcheck disable=SC1091
. .venv/bin/activate

# --- install ------------------------------------------------------------------
info "Installing Argus and its dependencies (this can take a minute) ..."
pip install --upgrade pip >/dev/null
pip install -e .
ok "Argus installed"

# --- .env ------------------------------------------------------------------
if [ ! -f ".env" ]; then
    cp .env.example .env
    ok "created .env from .env.example"
else
    info ".env already exists — leaving it as-is"
fi

echo ""
echo "=================================================="
echo "  Install complete"
echo "=================================================="
echo ""
echo "  Next steps:"
echo "    1) add your model API key to $PROJECT_DIR/.env"
echo "       (the easiest default is OpenRouter: https://openrouter.ai)"
echo "    2) run: argus start"
echo "       (from $PROJECT_DIR, with the venv active: source .venv/bin/activate)"
echo "    3) open http://localhost:8700"
echo ""
echo "  Optional features that need native prereqs (skip unless you want them):"
echo "    PDF export:  .venv/bin/python -m pip install -e '.[pdf]'   (needs GTK/Pango/cairo)"
echo "    OCR:         .venv/bin/python -m pip install -e '.[ocr]'   (needs Tesseract)"
echo ""
