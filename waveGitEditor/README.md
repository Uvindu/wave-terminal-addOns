# waveGitEditor — `wv` cockpit for Wave Terminal

A one-command installer for the `wv` git-review cockpit inside
[Wave Terminal](https://waveterm.dev)'s WSL integration:

```
wv <repo>
```

opens, in one Wave window:

| Block | What it does |
|---|---|
| Directory preview | `wsh view` of the repo |
| Narrow `lazygit` column | full lazygit, panels auto-stacked for narrow blocks |
| Wide **CR web view** | `wvdiff` server: working tree + vs-upstream + log, side-by-side diffs, stage / unstage / discard, commit box, reviewed marks, path filter, `j`/`k` keyboard nav |
| optional `--review` | live `aidiff` diff vs upstream (needs your own `aidiff`) |
| optional `--omp` | local-LLM block (needs your own `omp` + credentials) |

`wv --clean <repo>` closes the cockpit: deletes its blocks, stops its
`wvdiff` server, clears state.

## Requirements

- Wave Terminal installed on Windows, with this WSL distro enabled in its
  settings (`wsh` then appears at `~/.waveterm/bin/wsh` — the installer
  checks, never installs it)
- bash, git, python3 (stdlib only), curl
- `lazygit` — auto-fetched to `~/.local/bin` if missing (pinned release), or
  skip with `--no-lazygit`

## Install

From this repo:

```sh
./install.sh
```

From a remote host with static file access (one command in a fresh WSL):

```sh
curl -fsSL <repo-url>/raw/main/waveGitEditor/install.sh | bash -s -- <repo-root-tarball-or-clone>
```

The simplest supported remote form clones first (upgrade = re-run):

```sh
git clone <repo-url> ~/wave-terminal-addOns && ~/wave-terminal-addOns/waveGitEditor/install.sh
```

## What gets installed

| Target | Content |
|---|---|
| `~/.local/bin/{wv,wvopen,wvdiff,wvdiff-ensure}` | the four scripts, 0755 |
| `~/.local/share/wvdiff/assets/` | vendored diff2html 3.4.51 (MIT) |
| `~/.config/lazygit/wv-cockpit.yml` | cockpit-only lazygit layout overlay |
| `~/.config/lazygit/wv-keys.yml` | cockpit-only `O` / `<enter>` key overlay |
| `~/.config/lazygit/config.yml` | **merged**: `O` (open file diff in web view) and `<enter>` on commits (open commit view) — only inserted if absent, never removed otherwise |
| `~/.local/bin/lazygit` | only if missing and not `--no-lazygit` |

Runtime state lives in `~/.local/state/wv/` and regenerates; it is never
touched by install or uninstall.

## Uninstall

```sh
./install.sh --uninstall
```

Removes the scripts, assets, overlays, and the merged lazygit keys. Keeps
`~/.local/state/wv` (delete manually to purge).

## How it works

- `wv` resolves the repo, ensures a `wvdiff` server (`wvdiff-ensure`), and
  spawns the Wave blocks via `wsh`. Block ids are recorded per repo, so a
  second `wv` focuses the existing cockpit instead of duplicating it.
- `wvdiff` is a Python-stdlib HTTP server bound to `127.0.0.1`, one per
  repo, that serves the review page and JSON/diff APIs. It exits after an
  idle timeout and re-spawns on demand. When the `wvdiff` script is newer
  than a running server, the next `wv` transparently restarts it
  (self-upgrade after re-install).
- Inside the cockpit, lazygit loads three config files: your own
  `config.yml`, then `wv-cockpit.yml` (layout), then `wv-keys.yml`
  (commands). Later files win on key conflicts, so standalone lazygit is
  unaffected.
- `wvopen file <path>` / `wvopen commit <sha>` (what the lazygit keys call)
  deep-link the existing web block to that file's diff or that commit.

## Troubleshooting

- `wv: wsh not found` — run `wv` from a Wave Terminal WSL tab, not a plain
  terminal. If Wave is installed and the error persists, enable the distro
  in Wave settings and open a new tab.
- `wv: not a git repository` — pass a path inside a git work tree.
- Web view empty — check `~/.local/state/wv/wvdiff.log`.
- `?selftest=1` — append to any `wvdiff` URL to run its built-in 19-check
  self-test in the page.

## License

- The scripts in this package: see `LICENSE`.
- diff2html (vendored under `share/wvdiff/assets/`) is
  [MIT](https://github.com/rtfpessoa/diff2html), © rtfpessoa.
