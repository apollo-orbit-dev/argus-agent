#!/usr/bin/env bash
# Set up the Argus container sandbox. Idempotent and re-runnable.
#
# Run it directly, or from the dashboard's Settings > Sandbox card. It never installs a container
# runtime for you: installing podman needs root, and a script that silently sudo-installs a runtime
# is not something anyone should run on a stranger's say-so. It tells you what to run instead.
set -euo pipefail

RUNTIME="${SANDBOX_RUNTIME:-podman}"
IMAGE="${SANDBOX_IMAGE:-argus-sandbox:local}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

info() { printf '  %s\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; exit 1; }

echo ""
echo "  Argus sandbox setup"
echo ""

command -v "$RUNTIME" >/dev/null 2>&1 || fail \
  "$RUNTIME not found. Install it first:
     Debian/Ubuntu  sudo apt install podman
     Fedora         sudo dnf install podman
     Arch           sudo pacman -S podman
     macOS          brew install podman && podman machine init && podman machine start
   then re-run this script."
ok "$RUNTIME $("$RUNTIME" --version | awk '{print $NF}')"

"$RUNTIME" info >/dev/null 2>&1 || fail \
  "$RUNTIME is installed but not usable. On Linux this is usually rootless setup:
     grep \"^$(id -un):\" /etc/subuid /etc/subgid    # both should print a line
     sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 \"$(id -un)\"
     $RUNTIME system migrate"
ok "$RUNTIME is usable (rootless as $(id -un))"

info "building $IMAGE (first run takes a minute) ..."
"$RUNTIME" build -t "$IMAGE" -f "$HERE/Containerfile" "$HERE" >/dev/null || fail "image build failed"
ok "built $IMAGE"

info "smoke test ..."
OUT="$("$RUNTIME" run --rm --network=none "$IMAGE" python -c 'print("sandbox-ok")')"
[ "$OUT" = "sandbox-ok" ] || fail "smoke test returned unexpected output: $OUT"
ok "container runs with no network"

echo ""
ok "sandbox ready — set ENABLE_SANDBOX=true (or flip it on the dashboard's Settings page)"
echo ""
