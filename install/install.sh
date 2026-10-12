#!/usr/bin/env sh
# Install the zkshizen harness for the current user (Linux / macOS).
#
#   ./install/install.sh               install from this checkout
#   ./install/install.sh <git-url>     install from a git repository (e.g. https://github.com/zkshizen-Technology/zkshizen-harness.git)
#
# Uses pipx when available, otherwise a private venv in
# ~/.local/share/zkshizen/venv with `zks` symlinked into ~/.local/bin.
set -eu

SOURCE="${1:-}"
if [ -z "$SOURCE" ]; then
    SOURCE="$(cd "$(dirname "$0")/.." && pwd)"
    [ -f "$SOURCE/pyproject.toml" ] || { echo "error: run from a checkout or pass a git URL" >&2; exit 1; }
else
    case "$SOURCE" in
        git+*) ;;
        *) SOURCE="git+$SOURCE" ;;
    esac
fi

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "error: python3 not found; install Python 3.11 or newer" >&2; exit 1; }
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "error: Python 3.11 or newer is required (found $("$PY" -V 2>&1))" >&2
    exit 1
fi

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

if command -v pipx >/dev/null 2>&1; then
    echo "installing with pipx from $SOURCE"
    pipx install --force --python "$PY" "$SOURCE"
else
    VENV="${XDG_DATA_HOME:-$HOME/.local/share}/zkshizen/venv"
    echo "creating venv in $VENV"
    "$PY" -m venv "$VENV"
    "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
    "$VENV/bin/python" -m pip install --upgrade "$SOURCE"
    ln -sf "$VENV/bin/zks" "$BIN_DIR/zks"
    echo "linked $BIN_DIR/zks"
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: add $BIN_DIR to your PATH, e.g. echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile" ;;
esac

cat <<'EOF'

Installed. Next steps:
  zks init
  zks config set anthropic --provider anthropic --api-key - --default
  zks config test
  zks user passwd admin
  zks user totp admin
  zks serve                 # or: zks service install
EOF
