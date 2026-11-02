#!/usr/bin/env bash
# install.sh — install ompPicker (omp conversation picker) into ~/.waveterm.
#
# Idempotent: re-running upgrades in place (files are overwritten from this
# repo copy; the omp-chats widget entry is overwritten with defaults).
#
# Usage:  ./install.sh [--uninstall] [--no-fzf]
#
# Installs:
#   ~/.waveterm/omp-picker.py                     0755 (Wave widget)
#   ~/.local/bin/ompc                             0755 (any-terminal launcher)
#   ~/.waveterm/bin/fzf                           0755 (only if missing)
#   omp-chats entry in Wave's Windows-side widgets.json
#
# Optional: --no-fzf skips fetching the fzf binary when missing.
set -euo pipefail

PKG=$(cd "$(dirname "${0}")" && pwd)
WAVETERM_DIR="$HOME/.waveterm"
BIN_DIR="$HOME/.local/bin"
FZF_VERSION="0.74.5"
WINCONFIG_ROOT="${WAVETERM_WINCONFIG_ROOT:-/mnt/c/Users}"
DISTRO="${WSL_DISTRO_NAME:-Ubuntu}"

UNINSTALL=0
FETCH_FZF=1
for arg in "$@"; do
    case "$arg" in
        --uninstall)  UNINSTALL=1 ;;
        --no-fzf)     FETCH_FZF=0 ;;
        *)            echo "install.sh: unknown arg: $arg" >&2; exit 1 ;;
    esac
done

say()  { printf 'install: %s\n' "$*"; }
warn() { printf 'install: WARNING: %s\n' "$*" >&2; }
die()  { printf 'install: ERROR: %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------ widget JSON
# Merge the omp-chats widget entry into a Wave widgets.json.
# $1 = widgets.json path, $2 = home dir, $3 = distro
merge_widget_json() {
    python3 - "$1" "$2" "$3" <<'PYEOF'
import json, sys, os

path, home, distro = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f:
        data = json.load(f)
except FileNotFoundError:
    print(f"install: {path} missing, skipped", file=sys.stderr)
    sys.exit(0)
except (json.JSONDecodeError, OSError) as e:
    print(f"install: WARNING: cannot read {path}: {e} — skipped",
          file=sys.stderr)
    sys.exit(0)

if not isinstance(data, dict):
    print(f"install: WARNING: {path} is not a JSON object — skipped",
          file=sys.stderr)
    sys.exit(0)

existed = "omp-chats" in data
data["omp-chats"] = {
    "display:order": -7,
    "icon": "solid@comments",
    "color": "#c586c0",
    "label": "omp chats",
    "description": ("Pick a past omp conversation by title "
                    "(recent first) and resume it"),
    "blockdef": {
        "meta": {
            "view": "term",
            "controller": "cmd",
            "cmd": os.path.join(home, ".waveterm", "omp-picker.py"),
            "cmd:shell": True,
            "cmd:closeonexit": True,
            "connection": f"wsl://{distro}",
        },
    },
}
with open(path, "w") as f:
    json.dump(data, f, indent=1)
    f.write("\n")
print("install: {} omp-chats entry in {}".format(
    "updated" if existed else "added", path))
PYEOF
}

# ------------------------------------------------------------ uninstall
if [ "$UNINSTALL" = 1 ]; then
    say "removing ompPicker"
    rm -f "$WAVETERM_DIR/omp-picker.py" "$BIN_DIR/ompc"
    n=0
    shopt -s nullglob
    for d in "$WINCONFIG_ROOT"/*/.config/waveterm; do
        [ -d "$d" ] || continue
        [ -f "$d/widgets.json" ] || continue
        python3 - "$d/widgets.json" <<'PYEOF'
import json, sys

path = sys.argv[1]
try:
    with open(path) as f:
        data = json.load(f)
except (OSError, json.JSONDecodeError):
    sys.exit(0)
if not isinstance(data, dict) or "omp-chats" not in data:
    sys.exit(0)
data.pop("omp-chats", None)
with open(path, "w") as f:
    json.dump(data, f, indent=1)
    f.write("\n")
print(f"install: removed omp-chats entry from {path}")
PYEOF
        n=$((n + 1))
    done
    say "uninstalled (state kept: ~/.waveterm/omp-*.json, ~/.omp/agent/trash/;"
    say "remove $WAVETERM_DIR/bin/fzf manually if you want it gone)"
    exit 0
fi

# ------------------------------------------------------------ preflight
for tool in bash python3 curl; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool not found — install it and re-run"
done
[ -f "$PKG/bin/omp-picker.py" ] || die "run from the ompPicker repo root (bin/omp-picker.py missing)"
[ -f "$PKG/bin/ompc" ] || die "run from the ompPicker repo root (bin/ompc missing)"

if ! command -v wsh >/dev/null 2>&1 && [ ! -x "$WAVETERM_DIR/bin/wsh" ]; then
    warn "wsh not found — the widget needs Wave Terminal's WSL integration."
    warn "Install Wave on Windows, enable this distro in Wave settings, open a Wave tab, then re-run."
fi

# ------------------------------------------------------------ install
install -d "$WAVETERM_DIR" "$BIN_DIR" "$WAVETERM_DIR/bin"
install -m 0755 "$PKG/bin/omp-picker.py" "$WAVETERM_DIR/omp-picker.py"
install -m 0755 "$PKG/bin/ompc"          "$BIN_DIR/ompc"
say "widget + launcher installed"

# ------------------------------------------------------------ fzf fetch
if ! [ -x "$WAVETERM_DIR/bin/fzf" ] && [ "$FETCH_FZF" = 1 ]; then
    arch=$(uname -m)
    case "$arch" in
        x86_64)  arch=amd64 ;;
        aarch64) arch=arm64 ;;
        *)       arch="" ;;
    esac
    if [ -z "$arch" ]; then
        warn "unsupported arch $(uname -m) — install fzf manually to $WAVETERM_DIR/bin/fzf"
    else
        url="https://github.com/junegunn/fzf/releases/download/v${FZF_VERSION}/fzf-${FZF_VERSION}-linux_${arch}.tar.gz"
        say "fzf not found — fetching v$FZF_VERSION to $WAVETERM_DIR/bin/"
        t=$(mktemp -d)
        if curl -fsSL --max-time 120 -o "$t/fzf.tgz" "$url"; then
            tar -xzf "$t/fzf.tgz" -C "$t" fzf \
                || die "could not extract fzf tarball"
            install -m 0755 "$t/fzf" "$WAVETERM_DIR/bin/fzf"
            say "fzf v$FZF_VERSION installed"
        else
            warn "fzf download failed — the picker falls back to its numbered-list mode without it"
        fi
        rm -rf "$t"
    fi
fi
[ -x "$WAVETERM_DIR/bin/fzf" ] || warn "fzf missing — the picker falls back to its numbered-list mode"

# ------------------------------------------------------------ widget registration
say "registering omp-chats widget"
n=0
shopt -s nullglob
for d in "$WINCONFIG_ROOT"/*/.config/waveterm; do
    [ -d "$d" ] || continue
    if [ ! -f "$d/widgets.json" ]; then
        printf '{}\n' > "$d/widgets.json" || { warn "cannot create $d/widgets.json — skipped"; continue; }
    fi
    merge_widget_json "$d/widgets.json" "$HOME" "$DISTRO"
    n=$((n + 1))
done
if [ "$n" -eq 0 ]; then
    warn "no Wave config found under $WINCONFIG_ROOT/*/.config/waveterm"
    warn "paste the omp-chats entry (see README.md) into %USERPROFILE%\\.config\\waveterm\\widgets.json on the Windows side"
fi

# ------------------------------------------------------------ PATH
if ! case ":$PATH:" in *":$BIN_DIR:"*) true ;; *) false ;; esac; then
    say "$BIN_DIR not on PATH in this shell — open a new shell or: export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
if [ -f "$HOME/.profile" ] && ! grep -q '\.local/bin' "$HOME/.profile"; then
    say "note: ~/.profile does not add ~/.local/bin — add it if a new shell cannot find ompc"
fi

# ------------------------------------------------------------ verify
say "verifying install"
bash -n "$BIN_DIR/ompc" || die "ompc failed syntax check"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
PYTHONPYCACHEPREFIX="$tmp/pyc" python3 -m py_compile "$WAVETERM_DIR/omp-picker.py" \
    || die "omp-picker.py failed py_compile"
header=$(python3 "$WAVETERM_DIR/omp-picker.py" --header)
lines=$(printf '%s\n' "$header" | grep -c .)
[ "$lines" -ge 2 ] || die "omp-picker.py --header did not print 2 lines"
say "verification passed (header renders, $lines lines)"

echo
say "done. Open the \"omp chats\" widget in Wave Terminal, or run:  ompc"
