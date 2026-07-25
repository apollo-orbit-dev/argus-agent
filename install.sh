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
DEFAULT_DIR_NAME="argus"
DIR_NAME="$DEFAULT_DIR_NAME"

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
    # Install folder name — Enter keeps the default "argus". Prompting here makes running
    # a second Argus instance (e.g. a daily one and a dev one) next to the first easy: each
    # install picks its own folder. Read from /dev/tty so it works even when the script is
    # piped in (curl ... | bash); falls back to the default with no terminal (non-interactive
    # install).
    if [ -r /dev/tty ]; then
        ATTEMPTS=0
        MAX_ATTEMPTS=5
        while :; do
            if [ "$ATTEMPTS" -eq 0 ]; then
                printf '  Install folder name [%s]: ' "$DEFAULT_DIR_NAME" > /dev/tty
            else
                printf '  Install folder name (or press Enter/Ctrl-D/q to cancel): ' > /dev/tty
            fi
            read -r REPLY_DIR < /dev/tty || REPLY_DIR=""

            # Empty/q/Ctrl-D only means "cancel" once we're re-prompting after a collision —
            # on the very first ask it just means "keep the default", same as the port prompt.
            if [ "$ATTEMPTS" -gt 0 ] && { [ -z "$REPLY_DIR" ] || [ "$REPLY_DIR" = "q" ]; }; then
                fail "Install cancelled."
            fi

            CANDIDATE="$DEFAULT_DIR_NAME"
            if [ -n "$REPLY_DIR" ]; then
                if printf '%s' "$REPLY_DIR" | grep -qE '^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$'; then
                    CANDIDATE="$REPLY_DIR"
                else
                    warn "'$REPLY_DIR' is not a valid folder name — using $DEFAULT_DIR_NAME"
                fi
            fi

            # Refuse to clobber an existing install. That directory may hold the operator's
            # .env (API keys) and data/ (sessions, tables, memory) — overwriting or
            # half-upgrading it is real data loss, and the whole point of this prompt is
            # installing a second instance NEXT TO a first one, not into it. Re-prompt for a
            # different name rather than aborting outright — bounded, so it can't spin forever.
            if [ -d "$CANDIDATE" ] && { [ -e "$CANDIDATE/.env" ] || [ -d "$CANDIDATE/data" ]; }; then
                warn "$(pwd)/$CANDIDATE already looks like an existing Argus install (found .env or data/) — it will not be touched."
                ATTEMPTS=$((ATTEMPTS + 1))
                if [ "$ATTEMPTS" -ge "$MAX_ATTEMPTS" ]; then
                    fail "Too many attempts choosing a folder name — re-run and pick one that isn't an existing install."
                fi
                continue
            fi
            DIR_NAME="$CANDIDATE"
            break
        done
    else
        # No terminal to prompt (e.g. curl ... | bash) — keep the default name, but still
        # refuse to touch an existing install. There is nobody to re-prompt on a piped /
        # non-interactive install, so on a collision we exit here rather than loop — do NOT
        # turn this into an interactive retry, it would hang forever with no tty to read from.
        if [ -d "$DIR_NAME" ] && { [ -e "$DIR_NAME/.env" ] || [ -d "$DIR_NAME/data" ]; }; then
            fail "$(pwd)/$DIR_NAME already looks like an existing Argus install (found .env or data/). Re-run interactively (not piped) and pick a different folder name."
        fi
    fi

    if [ -d "$DIR_NAME" ]; then
        warn "./$DIR_NAME already exists — using it instead of cloning again."
    else
        info "Cloning $REPO_URL ..."
        git clone "$REPO_URL" "$DIR_NAME"
        ok "cloned into ./$DIR_NAME"
        # Pin to the latest released version (a stable tag), not the moving main branch.
        LATEST_TAG="$(git -C "$DIR_NAME" tag -l 'v*' --sort=-v:refname | head -n 1)"
        if [ -n "$LATEST_TAG" ]; then
            git -C "$DIR_NAME" -c advice.detachedHead=false checkout -q "$LATEST_TAG"
            ok "checked out latest release $LATEST_TAG"
        fi
    fi
    cd "$DIR_NAME"
    PROJECT_DIR="$(pwd)"
    if [ "$DIR_NAME" != "$DEFAULT_DIR_NAME" ]; then
        info "Using a non-default folder name — if you enable the sandbox, also set SANDBOX_INSTANCE in .env so this instance gets its own podman namespace."
    fi
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
    # Dashboard port — Enter keeps the default. Prompting here makes running several Argus
    # instances on one machine easy: each install picks its own port. Read from /dev/tty so it
    # works even when the script is piped in (curl ... | bash); falls back to the default with
    # no terminal (non-interactive install).
    DEFAULT_PORT=8700
    PORT="$DEFAULT_PORT"
    if [ -r /dev/tty ]; then
        printf '  Dashboard port [%s]: ' "$DEFAULT_PORT" > /dev/tty
        read -r REPLY_PORT < /dev/tty || REPLY_PORT=""
        if [ -n "$REPLY_PORT" ]; then
            if printf '%s' "$REPLY_PORT" | grep -qE '^[0-9]+$' && [ "$REPLY_PORT" -ge 1 ] && [ "$REPLY_PORT" -le 65535 ]; then
                PORT="$REPLY_PORT"
            else
                warn "'$REPLY_PORT' is not a valid port (1-65535) — using $DEFAULT_PORT"
            fi
        fi
    fi
    # Set PORT in the fresh .env (replace .env.example's PORT= line, or append if absent).
    if grep -qE '^PORT=' .env; then
        tmp="$(mktemp)"; grep -vE '^PORT=' .env > "$tmp"; printf 'PORT=%s\n' "$PORT" >> "$tmp"; mv "$tmp" .env
    else
        printf 'PORT=%s\n' "$PORT" >> .env
    fi
    ok "dashboard port set to $PORT"
else
    info ".env already exists — leaving it as-is"
fi

# Port for the closing message (read back from .env; default 8700).
PORT_MSG="$(grep -E '^PORT=' .env 2>/dev/null | tail -n1 | cut -d= -f2)"
[ -z "$PORT_MSG" ] && PORT_MSG=8700

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
echo "    3) open http://localhost:$PORT_MSG"
echo ""
echo "  Optional features that need native prereqs (skip unless you want them):"
echo "    PDF export:  .venv/bin/python -m pip install -e '.[pdf]'   (needs GTK/Pango/cairo)"
echo "    OCR:         .venv/bin/python -m pip install -e '.[ocr]'   (needs Tesseract)"
echo ""
