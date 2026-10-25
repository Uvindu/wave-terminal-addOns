#!/usr/bin/env bash
# install.sh — install waveGitEditor (wv cockpit suite) into ~/.local.
#
# Idempotent: re-running upgrades in place (files are overwritten from this
# repo copy; lazygit config keys are merged, never duplicated).
#
# Usage:  ./install.sh [--uninstall] [--no-lazygit]
#         curl -fsSL <repo>/install.sh | bash    (from repo root: bash install.sh)
#
# Installs:
#   ~/.local/bin/{wv,wvopen,wvdiff,wvdiff-ensure}   0755
#   ~/.local/share/wvdiff/assets/diff2html*          vendored, MIT
#   ~/.config/lazygit/{wv-cockpit.yml,wv-keys.yml}   overlays, cockpit-only
#   lazygit custom keys O + <enter> merged into ~/.config/lazygit/config.yml
#     (only when the file exists and lacks them; user config is otherwise
#      never touched — inside the cockpit the overlays provide the keys)
#
# Optional: --no-lazygit skips fetching the lazygit binary when missing.
set -euo pipefail

PKG=$(cd "$(dirname "${0}")" && pwd)
BIN_DIR="$HOME/.local/bin"
DATA_DIR="$HOME/.local/share/wvdiff"
LG_DIR="$HOME/.config/lazygit"
LG_USER_CONFIG="$LG_DIR/config.yml"
LAZYGIT_VERSION="0.66.2"

UNINSTALL=0
FETCH_LAZYGIT=1
for arg in "$@"; do
    case "$arg" in
        --uninstall)  UNINSTALL=1 ;;
        --no-lazygit) FETCH_LAZYGIT=0 ;;
        *)            echo "install.sh: unknown arg: $arg" >&2; exit 1 ;;
    esac
done

say()  { printf 'install: %s\n' "$*"; }
warn() { printf 'install: WARNING: %s\n' "$*" >&2; }
die()  { printf 'install: ERROR: %s\n' "$*" >&2; exit 1; }


# ---------------------------------------------------------------- config merge
merge_config_keys() {
    # Insert the O + <enter> custom commands into the user's config.yml so
    # standalone lazygit also has them. Idempotent: keyed on the wvopen marker.
    [ -f "$LG_USER_CONFIG" ] || { say "no user lazygit config — cockpit overlays cover it (wv --use-config-file)"; return 0; }
    if grep -q 'wvopen file' "$LG_USER_CONFIG"; then
        say "lazygit O/enter keys already present"
        return 0
    fi
    if ! grep -q '^customCommands:' "$LG_USER_CONFIG"; then
        printf '\ncustomCommands:\n' >> "$LG_USER_CONFIG"
        append_keys_block
        say "lazygit O/enter keys added (new customCommands block)"
        return 0
    fi
    # existing customCommands: insert after the last top-level item of that
    # block (before the next top-level key or EOF)
    python3 - "$LG_USER_CONFIG" <<'PYEOF'
import sys
path = sys.argv[1]
lines = open(path).read().splitlines(keepends=True)
keys_block = """  - key: "O"
    context: "files"
    command: "~/.local/bin/wvopen file {{.SelectedFile.Name}}"
    description: "Open selected file diff in CR web view"
    output: none
  - key: <enter>
    context: commits, subCommits
    description: Open commit in Gerrit-CR web view
    command: ~/.local/bin/wvopen commit {{.SelectedCommit.Hash}}
    output: none
    loadingText: opening CR view
"""
# find start of customCommands block
start = next(i for i, l in enumerate(lines) if l.startswith("customCommands:"))
end = len(lines)
for i in range(start + 1, len(lines)):
    l = lines[i]
    if l.strip() and not l.startswith((" ", "\t", "-")):
        end = i
        break
# last non-blank line inside block
last = end - 1
while last > start and not lines[last].strip():
    last -= 1
if not lines[last].endswith("\n"):
    lines[last] += "\n"
lines.insert(last + 1, keys_block)
open(path, "w").write("".join(lines))
PYEOF
    say "lazygit O/enter keys merged into config.yml"
}

append_keys_block() {
    cat >> "$LG_USER_CONFIG" <<'EOF'
  - key: "O"
    context: "files"
    command: "~/.local/bin/wvopen file {{.SelectedFile.Name}}"
    description: "Open selected file diff in CR web view"
    output: none
  - key: <enter>
    context: commits, subCommits
    description: Open commit in Gerrit-CR web view
    command: ~/.local/bin/wvopen commit {{.SelectedCommit.Hash}}
    output: none
    loadingText: opening CR view
EOF
}

remove_config_keys() {
    # Strip the merged O/enter block from config.yml (uninstall). Best-effort:
    # removes the 5-line O entry and 6-line enter entry that contain wvopen.
    [ -f "$LG_USER_CONFIG" ] || return 0
    python3 - "$LG_USER_CONFIG" <<'PYEOF'
import sys
path = sys.argv[1]
lines = open(path).read().splitlines(keepends=True)
out, i, removed = [], 0, 0
while i < len(lines):
    l = lines[i]
    if l.startswith("  - key:"):
        # collect the full entry: this line + following lines indented deeper
        # than the "  - " list marker (4+ spaces or nested lists)
        j = i + 1
        while j < len(lines) and lines[j].startswith("    "):
            j += 1
        entry = "".join(lines[i:j])
        if "wvopen" in entry:
            removed += 1
            i = j
            continue
    out.append(l)
    i += 1
if removed:
    open(path, "w").write("".join(out))
    print(f"install: removed {removed} wv key(s) from config.yml")
PYEOF
}
# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" = 1 ]; then
    say "removing wv cockpit suite"
    rm -f "$BIN_DIR/wv" "$BIN_DIR/wvopen" "$BIN_DIR/wvdiff" "$BIN_DIR/wvdiff-ensure"
    rm -rf "$DATA_DIR"
    rm -f "$LG_DIR/wv-cockpit.yml" "$LG_DIR/wv-keys.yml"
    remove_config_keys
    say "uninstalled (state kept: ~/.local/state/wv; remove manually to purge)"
    exit 0
fi

# ---------------------------------------------------------------- preflight
for tool in bash git python3 curl md5sum; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool not found — install it and re-run"
done
[ -f "$PKG/bin/wvdiff" ] || die "run from the waveGitEditor repo root (bin/wvdiff missing)"

if ! command -v wsh >/dev/null 2>&1 && [ ! -x "$HOME/.waveterm/bin/wsh" ]; then
    warn "wsh not found — wv needs Wave Terminal's WSL integration."
    warn "Install Wave on Windows, enable this distro in Wave settings, open a Wave tab, then re-run wv."
fi

# ---------------------------------------------------------------- install
install -d "$BIN_DIR" "$DATA_DIR/assets" "$LG_DIR"
install -m 0755 "$PKG/bin/wv"             "$BIN_DIR/wv"
install -m 0755 "$PKG/bin/wvopen"         "$BIN_DIR/wvopen"
install -m 0755 "$PKG/bin/wvdiff"         "$BIN_DIR/wvdiff"
install -m 0755 "$PKG/bin/wvdiff-ensure"  "$BIN_DIR/wvdiff-ensure"
install -m 0644 "$PKG/share/wvdiff/assets/diff2html.min.css"     "$DATA_DIR/assets/"
install -m 0644 "$PKG/share/wvdiff/assets/diff2html-ui.min.js"   "$DATA_DIR/assets/"
install -m 0644 "$PKG/config/lazygit/wv-cockpit.yml" "$LG_DIR/wv-cockpit.yml"
install -m 0644 "$PKG/config/lazygit/wv-keys.yml"    "$LG_DIR/wv-keys.yml"
say "scripts + assets + lazygit overlays installed"

# ---------------------------------------------------------------- lazygit
if ! command -v lazygit >/dev/null 2>&1 && [ ! -x "$BIN_DIR/lazygit" ]; then
    if [ "$FETCH_LAZYGIT" = 1 ]; then
        say "lazygit not found — fetching v$LAZYGIT_VERSION to $BIN_DIR"
        arch=$(uname -m)
        case "$arch" in
            x86_64)  arch=x86_64 ;;
            aarch64) arch=arm64 ;;
            *)       arch="" ;;
        esac
        [ -n "$arch" ] || warn "unsupported arch $(uname -m) — install lazygit manually"
        if [ -n "${arch:-}" ]; then
            url="https://github.com/jesseduffield/lazygit/releases/download/v${LAZYGIT_VERSION}/lazygit_${LAZYGIT_VERSION}_linux_${arch}.tar.gz"
            t=$(mktemp -d)
            if curl -fsSL --max-time 120 -o "$t/lg.tgz" "$url"; then
                tar -xzf "$t/lg.tgz" -C "$t" lazygit \
                    || die "could not extract lazygit tarball"
                install -m 0755 "$t/lazygit" "$BIN_DIR/lazygit"
                say "lazygit v$LAZYGIT_VERSION installed"
            else
                warn "lazygit download failed — install it manually (brew, go, or GitHub releases)"
            fi
            rm -rf "$t"
        fi
    else
        warn "lazygit not found (--no-lazygit) — the cockpit lazygit block will fail until you install it"
    fi
fi


merge_config_keys

# ---------------------------------------------------------------- PATH
if ! case ":$PATH:" in *":$BIN_DIR:"*) true ;; *) false ;; esac; then
    say "$BIN_DIR not on PATH in this shell — open a new shell or: export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
if [ -f "$HOME/.profile" ] && ! grep -q '\.local/bin' "$HOME/.profile"; then
    say "note: ~/.profile does not add ~/.local/bin — add it if a new shell cannot find wv"
fi

# ---------------------------------------------------------------- verify
say "verifying install"
for s in wv wvopen wvdiff-ensure; do bash -n "$BIN_DIR/$s"; done
[ -x "$BIN_DIR/lazygit" ] || command -v lazygit >/dev/null 2>&1 \
    || warn "lazygit missing — cockpit lazygit block will fail"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
PYTHONPYCACHEPREFIX="$tmp/pyc" python3 -m py_compile "$BIN_DIR/wvdiff"
git init -q "$tmp/repo" && git -C "$tmp/repo" commit -q --allow-empty -m init 2>/dev/null || true
port=$("$BIN_DIR/wvdiff-ensure" "$tmp/repo") || die "wvdiff-ensure failed"
curl -fsS -m 3 "http://127.0.0.1:${port}/healthz" >/dev/null || die "wvdiff healthz failed"
# stop the verification server again
pid=$(curl -fsS -m 2 "http://127.0.0.1:${port}/healthz" | python3 -c 'import json,sys;print(json.load(sys.stdin)["pid"])')
kill "$pid" 2>/dev/null || true
say "verification passed (wvdiff served healthz on port $port)"

echo
say "done. Run:  wv <repo-path>   inside a Wave Terminal WSL tab"
