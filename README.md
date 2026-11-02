# wave-terminal-addOns

Add-on packages for [Wave Terminal](https://waveterm.dev) (WSL side).
Each subfolder is self-contained with its own `install.sh`.

| Package | What it gives you |
|---|---|
| [waveGitEditor](waveGitEditor/) | `wv` — one command opens a full git-review cockpit: directory preview + lazygit column + web diff/stage/commit view |
| [ompPicker](ompPicker/) | `ompc` / Wave widget — fzf picker over past omp conversations: resume (same tab or new), star, rename, fork, delete/undo, live status, project filter |

## Install any package

```sh
git clone <this-repo-url>
./<package>/install.sh
```

Uninstall: `./<package>/install.sh --uninstall`
