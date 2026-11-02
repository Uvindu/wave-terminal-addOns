# ompPicker — omp conversation picker for Wave Terminal

An fzf-driven picker over your past [omp](https://github.com/ohmypi/omp)
conversations. Pick one and resume it in place, or open it in a new Wave tab.

## Requirements

- **Wave Terminal** installed on Windows with this WSL distro enabled
  (`wsh` at `~/.waveterm/bin/wsh` — Wave installs it itself; this script
  never does)
- **python3** (stdlib only)
- **your own `omp` binary and its credentials** — the picker never installs
  or configures omp; it only lists and resumes conversations omp created
- bash
- **fzf** — v0.74.5 is auto-fetched to `~/.waveterm/bin/fzf` if missing
  (`--no-fzf` to skip; any fzf binary dropped at that path works too)

## Install

```sh
./install.sh
```

Uninstall:

```sh
./install.sh --uninstall
```

Reinstalling overwrites the `omp-chats` widget entry with defaults
(self-healing after a home/distro change) and upgrades both scripts in place.

## What gets installed

| Path | What |
|---|---|
| `~/.waveterm/omp-picker.py` | the widget (python3, 0755) |
| `~/.local/bin/ompc` | any-terminal launcher for the same picker |
| `~/.waveterm/bin/fzf` | fzf binary (only if missing) |
| `omp-chats` entry in Wave's Windows-side `widgets.json` | registers the widget |

## Keys

| Key | Action |
|---|---|
| Enter | resume |
| alt-enter | new Wave tab |
| ctrl-p | project filter cycle |
| ctrl-d | delete to trash |
| ctrl-u | undo |
| ctrl-s | star |
| ctrl-o | sort cycle |
| ctrl-r | rename |
| ctrl-n | new chat |
| ctrl-f | fork |
| tab | mark |

Live rows show a green dot + `LIVE`; recently-closed rows show a yellow
dot. `ompc` runs the same picker in any terminal.

## Runtime state

The picker keeps sidecar state in JSON under `~/.waveterm/`:

- `omp-state.json` — sort mode + project filter
- `omp-stars.json` — starred session ids
- `omp-forks.json` — fork relationships
- `omp-renames.json` — picker-list display names
- and the omp trash manifest at `~/.omp/agent/trash/.manifest.json`

Install/uninstall never touch these. Session files themselves are never
rewritten — stars, renames and forks are sidecar state only.

## Troubleshooting

- **`wsh not found`** — run the picker from a Wave WSL tab, or enable this
  distro in Wave's settings first.
- **Empty list** — no conversations under `~/.omp/agent/sessions` yet;
  start omp once in a project directory.
- **fzf missing** — the picker falls back to a numbered-list mode.
- **Resume in new tab doesn't work** — requires Wave Terminal
  (`wsh` makes the new tab).

## License

MIT — see [LICENSE](LICENSE).
