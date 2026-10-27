#!/bin/sh
# hermes-xmpp installer — sets up the bridge on a fresh host.
#
#   ./install.sh                                  # interactive, installs into ~/.hermes-xmpp
#   ./install.sh --jid hermes@example.org --owner you@example.org
#   ./install.sh --service --jid … --owner …      # also install + start the systemd unit
#
# The Hermes side (API server, guest profile, gateway) is NOT touched: this
# script prints the exact lines to paste there. Run again to upgrade.
set -eu

DIR="${HERMES_XMPP_DIR:-$HOME/.hermes-xmpp}"
VENV="${HERMES_XMPP_VENV:-$HOME/.hermes-xmpp/venv}"
BIN="${HERMES_XMPP_BIN:-$HOME/.local/bin}"
PACKAGE=""
JID=""
OWNER=""
MODEL="${HERMES_XMPP_MODEL:-qwen3:8b}"
PROVIDER="${HERMES_XMPP_PROVIDER:-custom}"
UPLOAD=""
ROOM=""
DO_INIT=1
DO_SERVICE=0
DO_CHECK=1

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need_value() { [ $# -ge 2 ] && [ -n "$2" ] || die "$1 needs a value"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) need_value "$@"; DIR="$2"; shift 2 ;;
        --venv) need_value "$@"; VENV="$2"; shift 2 ;;
        --bin) need_value "$@"; BIN="$2"; shift 2 ;;
        --package) need_value "$@"; PACKAGE="$2"; shift 2 ;;
        --jid) need_value "$@"; JID="$2"; shift 2 ;;
        --owner) need_value "$@"; OWNER="$2"; shift 2 ;;
        --model) need_value "$@"; MODEL="$2"; shift 2 ;;
        --provider) need_value "$@"; PROVIDER="$2"; shift 2 ;;
        --upload-service) need_value "$@"; UPLOAD="$2"; shift 2 ;;
        --room) need_value "$@"; ROOM="$2"; shift 2 ;;
        --service) DO_SERVICE=1; shift ;;
        --skip-init) DO_INIT=0; shift ;;
        --no-check) DO_CHECK=0; shift ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# 1. python ------------------------------------------------------------------
PY=python3
command -v "$PY" >/dev/null 2>&1 || die "python3 not found"
"$PY" - <<'EOF' || die "python 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
EOF
say "python      $("$PY" -V 2>&1)"

# 2. venv + package ----------------------------------------------------------
if [ -z "$PACKAGE" ]; then
    if ls "$here"/dist/hermes_xmpp-*.whl >/dev/null 2>&1; then
        PACKAGE=$(ls "$here"/dist/hermes_xmpp-*.whl | sort | tail -1)
    elif [ -f "$here/pyproject.toml" ]; then
        PACKAGE="$here"
    else
        die "no package found; pass --package <wheel|sdist|git-url|path>"
    fi
fi
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
say "installing  $PACKAGE"
"$VENV/bin/pip" install --quiet --upgrade "$PACKAGE"
say "installed   $("$VENV/bin/hermes-xmpp" --version)"

mkdir -p "$BIN"
ln -sf "$VENV/bin/hermes-xmpp" "$BIN/hermes-xmpp"
say "cli         $BIN/hermes-xmpp"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) say "note: add $BIN to PATH to run 'hermes-xmpp' directly" ;;
esac

# 3. deployment directory ----------------------------------------------------
CONFIG="$DIR/hermes.toml"
if [ -f "$CONFIG" ]; then
    say "config      keeping existing $CONFIG"
    DO_INIT=0
fi

if [ "$DO_INIT" = 1 ]; then
    if [ -z "$JID" ]; then
        printf 'bot JID (e.g. hermes@example.org): '
        read -r JID
    fi
    if [ -z "$OWNER" ]; then
        printf 'authorized operator JID (you): '
        read -r OWNER
    fi
    if [ -z "${HERMES_XMPP_PASSWORD:-}" ]; then
        printf 'XMPP password for %s: ' "$JID"
        stty -echo 2>/dev/null || true
        read -r HERMES_XMPP_PASSWORD
        stty echo 2>/dev/null || true
        printf '\n'
    fi
    export HERMES_XMPP_PASSWORD
    set -- init --dir "$DIR" --jid "$JID" --owner "$OWNER" --model "$MODEL" --provider "$PROVIDER"
    if [ -n "$UPLOAD" ]; then set -- "$@" --upload-service "$UPLOAD"; fi
    if [ -n "$ROOM" ]; then set -- "$@" --room "$ROOM"; fi
    "$VENV/bin/hermes-xmpp" "$@" 
fi

# 4. service -----------------------------------------------------------------
if [ "$DO_SERVICE" = 1 ]; then
    unit_dir="$HOME/.config/systemd/user"
    mkdir -p "$unit_dir"
    name=$(basename "$DIR")
    "$VENV/bin/hermes-xmpp" unit --config "$CONFIG" --name "$name" \
        > "$unit_dir/hermes-xmpp-$name.service"
    systemctl --user daemon-reload
    systemctl --user enable --now "hermes-xmpp-$name"
    say "service     hermes-xmpp-$name started"
else
    say ""
    say "To run it as a service:"
    say "  hermes-xmpp unit --config $CONFIG > ~/.config/systemd/user/hermes-xmpp-$(basename "$DIR").service"
    say "  systemctl --user daemon-reload && systemctl --user enable --now hermes-xmpp-$(basename "$DIR")"
fi

# 5. verification ------------------------------------------------------------
if [ "$DO_CHECK" = 1 ] && [ -f "$CONFIG" ]; then
    say ""
    "$VENV/bin/hermes-xmpp" check --config "$CONFIG" || true
fi
