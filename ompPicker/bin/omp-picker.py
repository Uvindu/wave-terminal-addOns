#!/usr/bin/env python3
"""Wave Terminal widget: pick a past omp conversation and resume it.

Modes:
  (no args)          interactive fzf picker (sort/star/live aware)
  --list [--raw]     print the listing; --raw = fzf feed lines (reload cmd)
  --header           print the one-line header (transform-header source)
  --sort-next        cycle sort mode date -> title -> project -> date
  --star-toggle P    star/unstar the session at path P
  --delete P [P...]  move session file(s) to the omp trash (never unlink)
  --undo             restore the newest trash entry to its original path
  --rename P        interactively rename the picker title of session P
  --fork P          copy session P to a new id in the same dir and resume it

Sessions live under ~/.omp/agent/sessions/<slugified-cwd>/*.jsonl, one JSON
object per line. Only real LLM conversations are listed: the file must
contain at least one assistant message with text. Name = title (fallback
first user prompt); order and "last edited" time come from the newest
record timestamp, falling back to file mtime.

Row head (fzf feed field 1, merged so tab stops never split it):
"★ ● Title" -- star, status dot, title. Dot green ● = live in another omp
process, yellow ○ = not live and last activity under RECENT_MINS (recently
closed), blank otherwise. The LIVE text itself stays in the meta field, and
the project is shown as [project] highlighted in bold yellow.

On-disk state (all JSON, created lazily, missing/corrupt = defaults):
  ~/.waveterm/omp-state.json   {"sort": "date"|"title"|"project",
                                "project": "all"|<project name>}
  ~/.waveterm/omp-stars.json   {"order": [session-id, ...]} oldest first
  ~/.omp/agent/trash/.manifest.json
      {"entries": [{"file": <trash name>, "orig": <original path>,
                    "sidecar"?: {"fork"?, "rename"?}}]}
      sidecar parks fork/rename entries while a session sits in trash, so
      undo restores them together with the file.
  ~/.waveterm/omp-forks.json
      {"<fork path>": {"base": <parent title at fork time>, "ts": <ISO>}}
      Composite fork titles (base+brief-first-prompt) are derived from this
      map at list time; session files themselves are never rewritten.
  ~/.waveterm/omp-renames.json
      {"<session path>": "<custom title>"} -- picker-list display names.
      Precedence: rename > fork composite > stored title. Session files
      are never rewritten here either.
"""
from contextlib import contextmanager
import datetime
import fcntl
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import threading
import unicodedata
import uuid

SESSIONS_GLOB = os.path.expanduser("~/.omp/agent/sessions/*/*.jsonl")
SESSIONS_ROOT = os.path.expanduser("~/.omp/agent/sessions")
TRASH_DIR = os.path.expanduser("~/.omp/agent/trash")
MANIFEST_PATH = os.path.join(TRASH_DIR, ".manifest.json")
STATE_PATH = os.path.expanduser("~/.waveterm/omp-state.json")
STARS_PATH = os.path.expanduser("~/.waveterm/omp-stars.json")
FORKS_PATH = os.path.expanduser("~/.waveterm/omp-forks.json")
RENAMES_PATH = os.path.expanduser("~/.waveterm/omp-renames.json")
LOCK_PATH = os.path.expanduser("~/.waveterm/.omp-picker.lock")
OMP_FALLBACK = os.path.expanduser("~/.local/bin/omp")
FZF = os.path.expanduser("~/.waveterm/bin/fzf")

SORT_MODES = ("date", "title", "project")
CLEANUP_DAYS = 40
TRASH_KEEP_DAYS = 30
MANIFEST_CAP = 200
RECENT_MINS = 60   # yellow status dot: not live, last activity < this
# Row column geometry (display cells, not chars). Fields are padded so
# the fzf tab between field 1 and 2 always lands on the same tab stop
# and every column starts at the same screen column in every row.
TITLE_W = 65    # title column: longest live title (55) + 10 headroom
TIME_W = 11     # longest relative_time() output: "Sep 30 2025"
PROJECT_W = 12  # bracketed project: "my_project" -> "[my_project]"
SID_W = 8       # session id prefix

BOLD, DIM, ITALIC, RESET = "\x1b[1m", "\x1b[2m", "\x1b[3m", "\x1b[0m"
CYAN, GREEN, YELLOW, MAGENTA = ("\x1b[36m", "\x1b[32m", "\x1b[33m",
                                 "\x1b[35m")
BRIGHT_CYAN = "\x1b[96m"
STAR_ON, STAR_OFF = YELLOW + "★" + RESET, " "
LIVE_ON, LIVE_OFF = GREEN + "●" + RESET, " "
RECENT_ON = YELLOW + "●" + RESET


_TAIL_BLOCK = 262144  # 256 KB: enough for many records, cheap to read
_TITLE_CACHE_PATH = os.path.expanduser("~/.waveterm/omp-title-cache.json")
_TITLE_CACHE_MAX = 500


def _read_title_cache():
    try:
        with open(_TITLE_CACHE_PATH) as fh:
            val = json.load(fh)
        if isinstance(val, dict):
            return val
    except (OSError, ValueError):
        pass
    return {}


def _write_title_cache(cache):
    """Persist at most the newest _TITLE_CACHE_MAX entries."""
    if len(cache) > _TITLE_CACHE_MAX:
        newest = sorted(cache.items(),
                        key=lambda kv: -kv[1].get("seen", 0))[:_TITLE_CACHE_MAX]
        cache = dict(newest)
    tmp = _TITLE_CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(cache, fh)
        os.replace(tmp, _TITLE_CACHE_PATH)
    except OSError:
        pass


def _scan_title_records(fh):
    """Latest title seen while reading fh byte lines in order."""
    title = ""
    for line in fh:
        if b'"title"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") in ("title", "title_change") and rec.get("title"):
            title = rec["title"]
    return title


def _current_title(path, size, head_title, cache):
    """Latest title for the session, incrementally.

    omp only ever APPENDS records, so a cached (offset -> title) pair
    stays valid for bytes below the offset; new title_change records
    can only appear in the delta between the cached offset and EOF.
    Cache miss = one full scan (same cost as the old parser), then the
    steady state is a zero-to-delta-byte scan. This is also what keeps
    displayed titles fresh as omp re-titles long-running sessions.
    """
    entry = cache.get(path)
    if entry and entry.get("offset") == size:
        return entry.get("title") or head_title
    title = head_title
    try:
        with open(path, "rb") as fh:
            start = entry.get("offset", 0) if entry else 0
            if start > size:
                start = 0  # truncated/rewritten file: rescan from the top
            fh.seek(start)
            scanned = _scan_title_records(fh)
            if scanned or start == 0:
                title = scanned or head_title
            elif entry:
                title = entry.get("title") or head_title
    except OSError:
        return head_title
    cache[path] = {"offset": size, "title": title,
                   "seen": time.time()}
    return title


def _tail_max_ts(path, size):
    """Newest record timestamp found in the file's tail block.

    Records are append-ordered, so the last timestamped line wins; the
    scan still takes the max defensively (mid-file clock jumps). The
    first line of the block may be cut in half -- json.loads rejects
    it, which is fine.
    """
    last_iso = ""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, size - _TAIL_BLOCK))
            for line in fh:
                if b'"timestamp"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = rec.get("timestamp")
                if isinstance(ts, str) and ts > last_iso:
                    last_iso = ts
    except OSError:
        return None
    return last_iso or None


def parse_session(path, _title_cache=None):
    """Return session dict, or None if the file is not a real conversation.

    Fast path: identity fields (session id, title, cwd, first user
    message) live at the TOP of the file, and both listing gates (first
    user text, first assistant text) are satisfied early, so the head
    scan stops as soon as all gates pass. The newest timestamp comes
    from a 256 KB tail block, and the latest title comes from an
    incremental delta scan backed by the title cache. Total cost is
    O(head records + tail block + delta), not O(file).


    _title_cache: shared dict for one load_rows() pass (one cache read
    + one cache write for the whole batch). None = private cache load,
    used by single-shot callers (rename, fork, resume).
    """
    own_cache = _title_cache is None
    if own_cache:
        _title_cache = _read_title_cache()
    sid, title, cwd, first_user = "", "", "", None
    has_asst_text = False
    try:
        st = os.stat(path)
    except OSError:
        return None
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                if rtype == "session":
                    sid = rec.get("id") or ""
                    if rec.get("title"):
                        title = rec["title"]
                    cwd = rec.get("cwd") or ""
                elif rtype == "message":
                    msg = rec.get("message")
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role")
                    content = msg.get("content")
                    text = ""
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text += part.get("text", "")
                    text = text.strip()
                    if role == "user" and text:
                        if first_user is None:
                            first_user = text
                    elif role == "assistant" and text:
                        has_asst_text = True
                if first_user is not None and has_asst_text:
                    break  # all gates passed; the rest is history
    except OSError:
        return None
    if not has_asst_text:
        return None
    mtime = st.st_mtime
    last_iso = _tail_max_ts(path, st.st_size)
    if last_iso:
        try:
            rec_ts = datetime.datetime.fromisoformat(
                last_iso.replace("Z", "+00:00")).timestamp()
            mtime = max(mtime, rec_ts)
        except ValueError:
            pass
    title = _current_title(path, st.st_size, title, _title_cache)
    if own_cache and _title_cache:
        _write_title_cache(_title_cache)
    return {"mtime": mtime, "id": sid, "title": title, "cwd": cwd,
            "first_user": first_user or "", "path": path}


# ---------------------------------------------------------------- state files

def _read_json(path, default):
    try:
        with open(path) as fh:
            val = json.load(fh)
        if not isinstance(val, dict):
            raise ValueError("not an object")
        return val
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


@contextmanager
def state_lock():
    """Cross-process lock for read-modify-write cycles on the state maps.

    The lock file is advisory (flock); holders never delete it. Multiple
    picker panes can star/rename/fork/delete concurrently without the
    last writer silently clobbering the other's update.
    """
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_state():
    raw = _read_json(STATE_PATH, {})
    mode = raw.get("sort")
    project = raw.get("project", "all")
    if project is True:  # legacy bool: "picker cwd's project"
        project = cwd_project()
    elif not isinstance(project, str) or not project:
        project = "all"
    return {"sort": mode if mode in SORT_MODES else "date",
            "project": project}


def write_state(mode, project=None):
    """Persist sort mode and (optionally) the project filter selection.

    project=None keeps the current selection (used by sort_next); the
    project cycle passes an explicit value ("all" or a project name).
    """
    with state_lock():
        cur = _read_json(STATE_PATH, {})
        if project is None:
            project = cur.get("project", "all")
            if project is True:  # legacy bool
                project = cwd_project()
            elif not isinstance(project, str) or not project:
                project = "all"
        _write_json(STATE_PATH, {"sort": mode, "project": project})


def read_stars():
    order = _read_json(STARS_PATH, {}).get("order")
    if not isinstance(order, list):
        order = []
    return [s for s in order if isinstance(s, str) and s]


def write_stars(order):
    _write_json(STARS_PATH, {"order": order})


def read_forks():
    """forks map: fork path -> {"base": str, "ts": ISO str}."""
    raw = _read_json(FORKS_PATH, {})
    out = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, dict) \
                and isinstance(v.get("base"), str) \
                and isinstance(v.get("ts"), str):
            out[k] = {"base": v["base"], "ts": v["ts"]}
    return out


def write_forks(forks):
    _write_json(FORKS_PATH, forks)


def record_fork(new_path, base_title):
    # ts in omp's record format (Z suffix): fork_brief compares ISO strings
    # lexicographically against record timestamps, so the format must match.
    now = datetime.datetime.now(datetime.timezone.utc)
    with state_lock():
        forks = read_forks()
        forks[new_path] = {
            "base": base_title,
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
        }
        write_forks(forks)


def read_renames():
    """renames map: absolute session path -> custom title."""
    raw = _read_json(RENAMES_PATH, {})
    return {k: v for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, str)}


def write_renames(renames):
    _write_json(RENAMES_PATH, renames)


def rename_session(path):
    """Interactive rename of one session's picker title via input().

    Empty input cancels. The map key is the absolute path; session files
    are never rewritten (omp owns them). Returns exit code.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        print(f"rename: no such session file: {path}", file=sys.stderr)
        return 3
    cur = displayed_title_of(path) or ""
    try:
        new = input(f"new title [{cur}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    if not new:
        print("rename cancelled")
        return 1
    with state_lock():
        renames = read_renames()
        renames[path] = " ".join(new.split())[:120]
        write_renames(renames)
    print(f"renamed: {renames[path]}")
    return 0


def _sidecar_delete(path, stars_order=None):
    """Park sidecar entries (forks, renames, stars) for a deleted path.

    Crash-safe ordering: the manifest entry carries the snapshot BEFORE
    the maps are stripped, so a crash between the two steps leaves the
    data reachable from the manifest instead of lost. All under
    state_lock; never call with the lock already held.
    """
    with state_lock():
        forks = read_forks()
        renames = read_renames()
        forks_info = forks.pop(path, None)
        rename = renames.pop(path, None)
        star_ids = [s for s in (stars_order or []) if s]
        if forks_info is None and rename is None and not star_ids:
            return
        entries = read_manifest()
        # attach to THIS path's newest manifest entry, not blindly [-1]:
        # crash recovery may interleave with deletes of other sessions
        for entry in reversed(entries):
            if entry.get("orig") == path:
                entry["sidecar"] = {
                    "fork": forks_info, "rename": rename,
                    "stars": star_ids or None}
                write_manifest(entries)
                break
        else:
            return  # no manifest entry (interrupted delete): keep maps
        if star_ids:
            order = read_stars()
            order = [s for s in order if s not in star_ids]
            write_stars(order)
        write_forks(forks)
        write_renames(renames)


def _sidecar_restore(path):
    """Restore sidecar entries parked by _sidecar_delete."""
    with state_lock():
        entries = read_manifest()
        if not entries:
            return
        # newest entry for this path
        for entry in reversed(entries):
            if entry.get("orig") == path:
                sidecar = entry.pop("sidecar", None)
                if sidecar:
                    write_manifest(entries)
                if isinstance(sidecar, dict):
                    forks = read_forks()
                    renames = read_renames()
                    if isinstance(sidecar.get("fork"), dict):
                        forks[path] = sidecar["fork"]
                    if isinstance(sidecar.get("rename"), str):
                        renames[path] = sidecar["rename"]
                    star_ids = sidecar.get("stars")
                    if isinstance(star_ids, list) and star_ids:
                        order = read_stars()
                        for sid in star_ids:
                            if isinstance(sid, str) and sid not in order:
                                order.append(sid)
                        write_stars(order)
                    write_forks(forks)
                    write_renames(renames)
                return


def fork_brief(path, fork_ts, max_words=5, max_chars=32):
    """Brief of the first user prompt sent to the fork (after fork_ts).

    Records are filtered by their ISO timestamp; resume does not inject
    synthetic user records, so the first user text after the fork moment
    is the prompt typed by the user into the forked session. Returns None
    when no such prompt exists yet (fresh fork).

    The brief is the first 3-5 words of that prompt's first line (image
    prefixes stripped): the composite fork title must stay short, so it is
    capped at max_words words and max_chars characters (hard ceiling).
    """
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("timestamp")
                if not (isinstance(ts, str) and ts > fork_ts):
                    continue
                if rec.get("type") != "message":
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "user":
                    continue
                text = _user_text(msg)
                if not text:
                    continue
                first_line = text.strip().splitlines()[0].strip()
                first_line = re.sub(r"^\[Image #\d+[^\]]*\]\s*", "", first_line)
                words = first_line.split()
                if not words:
                    continue
                brief = " ".join(words[:max_words])
                if len(brief) > max_chars:
                    # trim to the most content that fits the char ceiling,
                    # breaking at spaces AND hyphens -- else one long
                    # compound word ("fork-of-fork-title-thing") defeats it
                    orig = brief
                    pieces = re.split(r"(\s+|-)", brief)
                    out, dropped = "", False
                    for piece in pieces:
                        if not piece:
                            continue
                        if piece in (" ", "-") and not out:
                            continue  # no leading separator
                        cand = out + piece
                        if piece in (" ", "-"):
                            if len(cand) >= max_chars:
                                dropped = True
                                break
                            out = cand
                            continue
                        if len(cand) > max_chars:
                            dropped = True
                            break
                        out = cand
                    brief = out.rstrip(" -")
                    if dropped or not brief:
                        # single unbreakable token: hard-truncate the original
                        brief = (brief + "…") if brief \
                            else orig[:max_chars - 1] + "…"
                return brief
    except OSError:
        pass
    return None


def _user_text(msg):
    text = ""
    content = msg.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text += part.get("text", "")
    return text.strip()


def read_manifest():
    entries = _read_json(MANIFEST_PATH, {}).get("entries")
    if not isinstance(entries, list):
        entries = []
    out = []
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("file"), str) \
                and isinstance(e.get("orig"), str):
            entry = {"file": e["file"], "orig": e["orig"]}
            if isinstance(e.get("sidecar"), dict):
                entry["sidecar"] = e["sidecar"]
            out.append(entry)
    return out


def write_manifest(entries):
    _write_json(MANIFEST_PATH, {"entries": entries[-MANIFEST_CAP:]})


def manifest_append(trash_file, orig):
    entries = read_manifest()
    entries.append({"file": os.path.basename(trash_file), "orig": orig})
    write_manifest(entries)


# ------------------------------------------------------------------ helpers

def relative_time(ts):
    """Human relative time for the last-edited timestamp (local tz)."""
    now = datetime.datetime.now()
    dt = datetime.datetime.fromtimestamp(ts)
    secs = (now - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if now.date() - dt.date() == datetime.timedelta(days=1):
        return "yesterday"
    if secs < 7 * 86400:
        return f"{int(secs // 86400)}d ago"
    if secs < 365 * 86400:
        return dt.strftime("%b %d")
    return dt.strftime("%b %d %Y")


def project_of(path):
    """Project name = session dir name under the sessions root, '-' stripped."""
    return os.path.basename(os.path.dirname(os.path.abspath(path))).lstrip("-") or "-"


def cwd_project():
    """Project name for the picker's cwd, same shape as project_of().

    omp stores sessions under <root>/-<basename(cwd)> (verified against
    every existing session: 35/35 files agree). project_of() strips the
    leading '-', so this returns the bare basename to match. Only used
    to migrate the legacy boolean "project" state value; the filter
    itself now cycles through every project found on disk.
    """
    base = os.path.basename(os.getcwd().rstrip(os.sep))
    return base.lstrip("-") or "-"


def _fd_writable(pid, fd):
    """True if the process holds fd opened for writing (fdinfo flags)."""
    try:
        with open(f"/proc/{pid}/fdinfo/{fd}") as fh:
            for line in fh:
                if line.startswith("flags:"):
                    # O_WRONLY=1 / O_RDWR=2 in the octal flag field
                    bits = line.split(":", 1)[1].strip().split()
                    try:
                        return bool(int(bits[0], 8) & 3)
                    except ValueError:
                        return False
    except OSError:
        pass
    return False

_CLK_TCK = os.sysconf("SC_CLK_TCK") or 100


def _proc_start(pid):
    """Process start time as epoch seconds, or None if unreadable."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        ticks = int(fields[19])  # field 22 (starttime) of the full stat
        with open("/proc/uptime") as fh:
            uptime = float(fh.read().split()[0])
        return time.time() - uptime + ticks / _CLK_TCK
    except (OSError, ValueError, IndexError):
        return None


def _session_exit_time(path):
    """Epoch seconds of the LAST `session_exit` record, or None.

    omp appends this custom record when a conversation ends: process
    exit, /exit, or an in-TUI switch to another session. A file can
    accumulate several over resume cycles, so the whole file is
    scanned; a byte-level prefilter keeps it cheap.
    """
    last = None
    try:
        with open(path, "rb") as fh:
            for line in fh:
                if b"session_exit" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if (rec.get("type") == "custom"
                        and rec.get("customType") == "session_exit"):
                    ts = rec.get("timestamp")
                    if ts:
                        last = ts
    except OSError:
        return None
    if not last:
        return None
    try:
        return datetime.datetime.fromisoformat(
            last.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _resume_claims(parts, root):
    """Session jsonl paths a cmdline's --resume arguments point at.

    Accepts the path form (picker launches), the bare-uuid form
    (manual `omp --resume <uuid>`), and the --resume=VALUE form.
    Unresolvable values are skipped, never guessed.
    """
    claims = []
    for i, arg in enumerate(parts[1:]):
        if arg == "--resume":
            if i + 2 > len(parts) - 1:
                continue
            val = parts[i + 2]
        elif arg.startswith("--resume="):
            val = arg[len("--resume="):]
        else:
            continue
        cand = os.path.abspath(val)
        if cand.startswith(root + os.sep) and cand.endswith(".jsonl"):
            claims.append(cand)
        else:
            # manual resume: bare session uuid -> locate the file
            claims.extend(os.path.abspath(hit) for hit in
                          glob.glob(os.path.join(root, "*", f"*{val}*.jsonl")))
    return claims


def live_paths():
    """jsonl paths of omp processes that own a live conversation.

    A session is live when an omp process claims it -- by holding the
    jsonl open for WRITING (fdinfo flags O_WRONLY/O_RDWR) or by naming
    it in a --resume argument -- AND the session did not die while this
    process was running it. Death is omp's `session_exit` record: if
    its timestamp is AFTER the process start, the conversation was
    closed or switched away inside this process (the fd and the cmdline
    stick around afterwards and must not keep the dot green). If the
    exit PREDATES the process start, the session was simply resumed
    after it had ended -- live again, even before the first new record
    is written (resume writes nothing until the first user message).

    Worker daemons (__omp_worker_*) are excluded: they may tail files
    without owning the conversation. Never raises; unreadable /proc ->
    empty set.
    """
    root = os.path.abspath(SESSIONS_ROOT)
    live = set()
    mine = str(os.getpid())
    try:
        pids = os.listdir("/proc")
    except OSError:
        return live
    for pid in pids:
        if not pid.isdigit() or pid == mine:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                parts = [p.decode(errors="replace")
                         for p in fh.read().split(b"\0") if p]
        except OSError:
            continue
        if not parts or os.path.basename(parts[0]) != "omp":
            continue
        if any(a.startswith("__omp_worker") for a in parts[1:]):
            continue
        claims = _resume_claims(parts, root)
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            fds = []
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.endswith(" (deleted)"):
                target = target[:-10]
            if target.startswith(root + os.sep) and target.endswith(".jsonl"):
                if _fd_writable(pid, fd):
                    claims.append(target)
        if not claims:
            continue
        started = _proc_start(pid)
        for path in claims:
            exited = _session_exit_time(path)
            if exited is None or started is None or exited < started:
                live.add(path)
    return live


def display_title(row):
    name = row.get("title_override") or row["title"] or row["first_user"] \
        or os.path.basename(row["path"])
    return " ".join(name.split())[:120]  # strip tabs/newlines, cap length

def _override_for(r, forks, renames):
    """title_override for row r, or None.

    Precedence: rename (explicit user intent) > fork composite > none.
    A rename wins over the fork composite, but the fork chain metadata
    (base/ts) stays intact underneath, so the composite returns if the
    rename is ever removed.
    """
    apath = os.path.abspath(r["path"])
    renamed = renames.get(apath)
    if renamed:
        return renamed
    info = forks.get(apath)
    if info:
        base = info["base"]
        if base.endswith(" (fork)"):
            base = base[:-len(" (fork)")]
        brief = fork_brief(r["path"], info["ts"])
        if brief:
            return f"{base}+{brief}"
        return f"{base} (fork)"
    return None


# ------------------------------------------------------------- row building

def load_rows():
    """Sorted rows: starred block first (most-recently-starred first),
    then unstarred rows in the current sort mode."""
    title_cache = _read_title_cache()
    rows = [r for r in (parse_session(p, title_cache)
                        for p in glob.glob(SESSIONS_GLOB)) if r]
    if title_cache:
        _write_title_cache(title_cache)
    star_order = read_stars()
    starred_ids = set(star_order)
    live = live_paths()
    for r in rows:
        r["project"] = project_of(r["path"])
        r["starred"] = r["id"] in starred_ids
        r["live"] = os.path.abspath(r["path"]) in live
        r["recent"] = (not r["live"]
                       and time.time() - r["mtime"] < RECENT_MINS * 60)
    forks = read_forks()
    renames = read_renames()
    for r in rows:
        r["title_override"] = _override_for(r, forks, renames)
    state = read_state()
    mode = state["sort"]
    if state["project"] != "all":
        rows = [r for r in rows if r["project"] == state["project"]]
    if mode == "title":
        keyfn = lambda r: display_title(r).casefold()
    elif mode == "project":
        keyfn = lambda r: (r["project"].casefold(), -r["mtime"])
    else:
        keyfn = lambda r: -r["mtime"]
    unstarred = sorted((r for r in rows if not r["starred"]), key=keyfn)
    # rank from reversed order: most-recently-starred = highest rank;
    # unknown ids (deleted star entries) sink to the bottom of the block
    rank = {sid: len(star_order) - i for i, sid in enumerate(star_order)}
    starred = sorted((r for r in rows if r["starred"]),
                     key=lambda r: (-rank.get(r["id"], 0), -r["mtime"]))
    return starred + unstarred


def _dw(ch):
    """Display width of one character (0 combining, 2 wide/fullwidth)."""
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _dwidth(s):
    """Terminal display width of s, in cells.

    Matches fzf/go-runewidth closely enough for column padding:
    a base char followed by U+FE0F (VS16, emoji presentation) renders
    2 cells in the terminal even when the base is narrow (e.g. '▶'),
    so the pair counts as 2.
    """
    w, prev = 0, 0
    for ch in s:
        cw = _dw(ch)
        if ch == "\ufe0f" and prev == 1:
            cw = 1  # base+VS16 pair -> 2 cells total
        w += cw
        prev = cw
    return w


def _pad(s, width):
    """Pad s with spaces to the given display width."""
    return s + " " * max(0, width - _dwidth(s))


def _truncate(s, width):
    """Truncate to display width, marking the cut with a trailing '..'."""
    if _dwidth(s) <= width:
        return s
    out, w = "", 0
    for ch in s:
        cw = _dw(ch)
        if w + cw > width - 2:
            break
        out += ch
        w += cw
    return out + ".."


def feed_line(row):
    """3 tab fields: prefix+title, meta, path.

    Column layout (display cells; fzf joins --with-nth fields with a
    tab that advances to the next 8-col stop, so every field is padded
    to a fixed width and the tab lands on a constant stop):
      prefix 4 | title TITLE_W | time TIME_W | project PROJECT_W
      | sid SID_W | LIVE
    Title longer than TITLE_W is cut and ends with '..'.
    F3 (path) must stay ANSI-free: it is used raw in fzf placeholders,
    --id-nth and output parsing (fields[-1]).
    """
    star = "★" if row["starred"] else " "
    if row["live"]:
        dot = "●"
    elif row.get("recent"):
        dot = "○"
    else:
        dot = " "
    if row["starred"]:
        star = YELLOW + star + RESET
    if row["live"]:
        dot = GREEN + dot + RESET
    elif row.get("recent"):
        dot = YELLOW + dot + RESET
    title = _truncate(display_title(row), TITLE_W)
    head = (f"{star} {dot} {BOLD}{title}{RESET}"
            f"{' ' * (TITLE_W - _dwidth(title))}")
    sid = row["id"][:SID_W] if row["id"] else "-" * SID_W
    meta = (f"{DIM}{_pad(relative_time(row['mtime']), TIME_W)}"
            f"{BOLD}{YELLOW}{_pad('[' + row['project'] + ']', PROJECT_W)}"
            f"{RESET}{DIM}{sid}{RESET}")
    if row["live"]:
        meta += f" {GREEN}LIVE{RESET}"
    return f"{head}\t{meta}\t{row['path']}"


def print_list(rows):
    for row in rows:
        print(feed_line(row).replace("\t", "  |  "))


# ------------------------------------------------------------------ actions

def delete_session(path):
    """Move session file into the omp trash directory; never unlink.

    Returns (status, dst): status in {"ok", "refused", "failed"}.
    Appends to the trash manifest on success.
    """
    path = os.path.abspath(path)
    root = os.path.abspath(SESSIONS_ROOT)
    if not path.startswith(root + os.sep):
        print(f"refusing to delete outside the sessions root: {path}",
              file=sys.stderr)
        return "refused", None
    if path in live_paths():
        print(f"refusing to delete a live session: {path}", file=sys.stderr)
        return "refused", None
    os.makedirs(TRASH_DIR, exist_ok=True)
    dst = os.path.join(TRASH_DIR, os.path.basename(path))
    if os.path.exists(dst):
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        dst = os.path.join(TRASH_DIR, f"{os.path.basename(path)}.{stamp}")
    row = parse_session(path)
    star_ids = []
    if row and row["id"]:
        order = read_stars()
        if row["id"] in order:
            star_ids = [row["id"]]
    try:
        shutil.move(path, dst)
    except OSError as e:
        print(f"delete failed: {e}", file=sys.stderr)
        return "failed", None
    with state_lock():
        manifest_append(dst, path)
    _sidecar_delete(path, stars_order=star_ids)
    print(f"moved to trash: {dst}")
    return "ok", dst


def undo_delete():
    """Restore the newest manifest entry; pop it from the manifest."""
    entries = read_manifest()
    if not entries:
        return 0
    entry = entries[-1]
    trash_file = os.path.join(TRASH_DIR, entry["file"])
    if not os.path.exists(trash_file):
        write_manifest(entries[:-1])
        print(f"undo: trash file missing: {trash_file}", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(entry["orig"]), exist_ok=True)
    if os.path.exists(entry["orig"]):
        print(f"undo: target already exists, not overwriting: {entry['orig']}",
              file=sys.stderr)
        return 1
    try:
        shutil.move(trash_file, entry["orig"])
    except OSError as e:
        print(f"undo failed: {e}", file=sys.stderr)
        return 1
    _sidecar_restore(entry["orig"])
    write_manifest(entries[:-1])
    print(f"restored: {entry['orig']}")
    return 0


def star_toggle(path):
    row = parse_session(path)
    sid = row["id"] if row else ""
    if not sid:
        return  # no-op: not a session or no id
    with state_lock():
        order = read_stars()
        if sid in order:
            order.remove(sid)
        else:
            order.append(sid)
        write_stars(order)


def sort_next():
    state = read_state()
    write_state(SORT_MODES[(SORT_MODES.index(state["sort"]) + 1)
                           % len(SORT_MODES)])


def _available_projects():
    """Projects that actually list rows, newest activity first.

    Derived from the parsed session set (same data load_rows uses), so
    a project joins the cycle the moment a conversation in it renders:
    empty dirs (leftover subdirs, never-replied sessions) never enter
    the cycle. Recency = the project's newest row mtime.
    """
    title_cache = _read_title_cache()
    newest = {}
    for path in glob.glob(SESSIONS_GLOB):
        row = parse_session(path, title_cache)
        if not row:
            continue
        proj = project_of(path)
        if row["mtime"] > newest.get(proj, 0):
            newest[proj] = row["mtime"]
    if title_cache:
        _write_title_cache(title_cache)
    return [proj for proj, _ in sorted(newest.items(),
                                       key=lambda kv: -kv[1])]


def project_toggle():
    """Cycle the project filter: all -> newest project -> ... -> all.

    Project order is by latest activity, so recent work surfaces early.
    A stored selection whose project no longer has any sessions jumps
    to the newest live project (or stays on "all" if none exist).
    """
    state = read_state()
    projects = _available_projects()
    seq = ["all"] + projects
    current = state["project"]
    try:
        idx = seq.index(current)
    except ValueError:
        idx = 0  # stale selection: re-enter the cycle at the newest project
    write_state(state["sort"], seq[(idx + 1) % len(seq)])


def prune_trash():
    """Delete trash files older than TRASH_KEEP_DAYS; keep manifest exact.

    Under state_lock with crash-safe ordering: the manifest entry is
    dropped BEFORE the file is unlinked. A crash between the two leaves
    an orphan trash file (dead weight, never data loss); the reverse
    order could lose the undo record while the file lives on.
    Returns (pruned, reclaimed_bytes).
    """
    cutoff = datetime.datetime.now().timestamp() - TRASH_KEEP_DAYS * 86400
    pruned = 0
    reclaimed = 0
    with state_lock():
        entries = read_manifest()
        keep = []
        for entry in entries:
            trash_file = os.path.join(TRASH_DIR, entry["file"])
            try:
                st = os.stat(trash_file)
            except OSError:
                keep.append(entry)  # file already gone; entry stays for undo msg
                continue
            if st.st_mtime < cutoff:
                reclaimed += st.st_size
                pruned += 1
                # entry intentionally NOT kept: dropped from manifest first
            else:
                keep.append(entry)
        if pruned:
            write_manifest(keep)
        # unlink AFTER the manifest write so a crash never strands a
        # manifest entry pointing at a missing file (undo drops those
        # anyway, but the manifest stays exact).
        for entry in entries:
            if entry not in keep:
                try:
                    os.unlink(os.path.join(TRASH_DIR, entry["file"]))
                except OSError:
                    pass  # already gone; manifest no longer references it
    return pruned, reclaimed


def prune_stars(live_ids):
    """Drop star entries whose session no longer exists on disk.

    Runs once per picker boot. Deleted sessions keep their stars via the
    trash-manifest sidecar (restored on undo), so pruning here cannot
    lose a star that undo would bring back.
    """
    with state_lock():
        order = read_stars()
        pruned = [s for s in order if s in live_ids]
        if len(pruned) != len(order):
            write_stars(pruned)


def displayed_title_of(path):
    """Display title of an arbitrary session path (rename/fork aware).

    Used at rename time (show current) and fork time (capture the source's
    current composite title as the base for the new fork's chain:
    (A, A+B -> forking A+B yields A+B+C).
    """
    row = parse_session(path)
    if not row:
        return os.path.basename(path)
    return display_title({**row, "title_override":
                          _override_for(row, read_forks(), read_renames())})


def fork_session(path):
    """Line-identical copy with a new filename uuid, then resume the copy.

    The fork's identity is the filename uuid; omp --resume <path>
    re-derives state from the records, so no record is rewritten.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        print(f"fork: no such session file: {path}", file=sys.stderr)
        sys.exit(3)
    directory = os.path.dirname(path)
    for _ in range(2):  # one retry on uuid collision, then fail
        now = datetime.datetime.now(datetime.timezone.utc)
        name = (now.strftime("%Y-%m-%dT%H-%M-%S-")
                + f"{now.microsecond // 1000:03d}Z_"
                + str(uuid.uuid4()) + ".jsonl")
        new_path = os.path.join(directory, name)
        if not os.path.exists(new_path):
            break
    else:
        print("fork: could not generate a free filename", file=sys.stderr)
        sys.exit(3)
    base_title = displayed_title_of(path)
    try:
        shutil.copyfile(path, new_path)
    except OSError as e:
        print(f"fork failed: {e}", file=sys.stderr)
        sys.exit(3)
    record_fork(new_path, base_title)
    if os.environ.get("OMPICKER_TEST_NOEXEC"):
        print(new_path)  # test hook: report instead of exec
        return
    launch_omp(["--resume", new_path])


def new_chat(cwd):
    """Start a fresh omp in cwd (no --resume)."""
    if cwd and os.path.isdir(cwd):
        os.chdir(cwd)
    if os.environ.get("OMPICKER_TEST_NOEXEC"):
        print(f"NEWCHAT {os.getcwd()}")  # test hook: report instead of exec
        return
    launch_omp([])


def cleanup_candidates(rows):
    cutoff = datetime.datetime.now().timestamp() - CLEANUP_DAYS * 86400
    return [r for r in rows
            if r["mtime"] < cutoff and not r["starred"] and not r["live"]]


def prompt_cleanup(cands):
    """Ask once about moving stale conversations to trash. Default N."""
    print(f"{len(cands)} conversation(s) untouched for {CLEANUP_DAYS}+ days:")
    for r in cands:
        print(f"  {display_title(r)}  ({relative_time(r['mtime'])})")
    try:
        answer = input("Move to trash? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer != "y":
        print("skipped")
        return 0
    n = 0
    for r in cands:
        status, _ = delete_session(r["path"])
        if status == "ok":
            n += 1
    return n


# ------------------------------------------------------------------ pickers

def pick_fzf(rows):
    """Run fzf. Returns (key, path) or None.

    key: "" (Enter -> resume), "ctrl-n" (new chat), "ctrl-f" (fork).
    alt-enter and the ctrl-* actions never reach here: their binds run
    execute-silent children inside fzf and the picker stays open.
    path comes from the first output row (highlighted, or first marked).
    """
    feed = "\n".join(feed_line(r) for r in rows)
    script = shlex.quote(os.path.abspath(__file__))
    reload_cmd = f"python3 {script} --list --raw"
    binds = [
        # header always shows current state; re-fires after every reload-sync
        f"load:transform-header(python3 {script} --header)",
        f"ctrl-d:execute-silent(python3 {script} --delete {{+3}})"
        f"+reload-sync({reload_cmd})",
        f"ctrl-u:execute-silent(python3 {script} --undo)"
        f"+reload-sync({reload_cmd})",
        f"ctrl-s:execute-silent(python3 {script} --star-toggle {{3}})"
        f"+reload-sync({reload_cmd})",
        f"ctrl-o:execute-silent(python3 {script} --sort-next)"
        f"+reload-sync({reload_cmd})",
        f"ctrl-p:execute-silent(python3 {script} --project-toggle)"
        f"+reload-sync({reload_cmd})",
        # execute(): the rename child gets the tty so input() works;
        # reload-sync (not reload) preserves multi marks via --id-nth 3
        f"ctrl-r:execute(python3 {script} --rename {{3}})"
        f"+reload-sync({reload_cmd})",
        # new Wave tab: spawn and stay in the picker; the dot of the
        # resumed session turns green on the next reload
        f"alt-enter:execute-silent(python3 {script} --resume-tab {{3}})"
        f"+reload-sync({reload_cmd})",
    ]
    proc = subprocess.run(
        [FZF,
         "--delimiter", "\t",
         "--with-nth", "1,2",     # display: head/meta, hide path
         "--nth", "1,2",          # search scope: head (prefix+title) + meta
         "--id-nth", "3",         # identity across reload-sync: the path
         "--multi",
         "--gap",
         "--ansi",
         # header text base: italic magenta (title); line 2 overrides
         # to dim via its own SGR, brackets dim via theirs
         "--color", "header:italic:magenta",
         "--expect=ctrl-n,ctrl-f",
         "--header-lines", "0",
         "--bind", ",".join(binds),
         "--layout", "reverse",
         "--info", "inline-right",
         "--no-preview"],
        input=feed, stdout=subprocess.PIPE, text=True)
    if proc.returncode != 0 or not proc.stdout:
        return None
    lines = proc.stdout.splitlines()
    if not lines:
        return None
    key = lines[0]
    if key not in ("", "ctrl-n", "ctrl-f"):
        return None
    for line in lines[1:]:
        fields = line.split("\t")
        path = fields[-1] if fields else ""
        if path:
            return key, path
    return None


def pick_numbered(rows):
    print_list(rows)
    try:
        choice = input("resume which conversation? (number, empty = cancel): ").strip()
    except EOFError:
        return None
    if not choice.isdigit() or not (1 <= int(choice) <= len(rows)):
        return None
    return rows[int(choice) - 1]["path"]


# ------------------------------------------------------------------ launch

def interactive_env():
    """Env from an interactive bash (sources ~/.bashrc incl. RAPID_CLIENT_TOKEN).

    stdin/stdout must be redirected away from the tty: interactive bash does
    job-control ioctls that corrupt the widget terminal, making the exec'd
    omp fail raw-mode setup (EIO) and exit on SIGHUP.
    """
    try:
        proc = subprocess.run(
            ["bash", "-ic", "printenv"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10,
            start_new_session=True)
        if proc.returncode == 0:
            env = {}
            for line in proc.stdout.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    env[k] = v
            return env
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


_ENV_CACHE = None
_ENV_THREAD = None


def _env_prefetch():
    """Fill _ENV_CACHE off the critical path.

    interactive_env() runs `bash -ic printenv` (up to 10s on a slow
    ~/.bashrc). It used to run lazily at first launch_omp, stalling the
    first resume. Now a daemon thread starts at picker boot; launch_omp
    joins it (bounded) only when it actually needs the env.
    """
    global _ENV_CACHE
    _ENV_CACHE = interactive_env()


def _ensure_env_thread():
    global _ENV_THREAD
    if _ENV_THREAD is None or not _ENV_THREAD.is_alive():
        _ENV_THREAD = threading.Thread(target=_env_prefetch, daemon=True)
        _ENV_THREAD.start()
    return _ENV_THREAD


def launch_omp(args):
    """Run omp interactively as a child; return its exit code.

    Runs in the foreground process group of the picker (no new session),
    so omp owns the terminal raw mode. Ctrl+C is swallowed while waiting:
    the user exiting omp (Ctrl+C, /exit) must return to the picker, not
    kill it. Credentials env is merged once per picker run and cached.
    """
    omp = shutil.which("omp") or (OMP_FALLBACK if os.path.exists(OMP_FALLBACK) else None)
    if omp is None:
        print("omp executable not found in PATH or ~/.local/bin", file=sys.stderr)
        return 1
    env = dict(os.environ)
    thread = _ensure_env_thread()
    thread.join(timeout=10)
    if _ENV_CACHE:
        for key in ("RAPID_CLIENT_TOKEN", "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_AUTH_TOKEN"):
            if key in _ENV_CACHE:
                env[key] = _ENV_CACHE[key]
    # Ctrl+C must not kill the picker while omp runs in the foreground.
    # The picker ignores SIGINT so it survives and reopens the list when
    # the user exits omp with Ctrl+C. The child must still receive it:
    # CPython 3.12's posix_spawn path does NOT reset an inherited
    # SIG_IGN to SIG_DFL in the child, so force SIG_DFL via preexec_fn
    # (do NOT use pthread_sigmask: the child would inherit the blocked
    # mask and lose Ctrl+C entirely).
    old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        proc = subprocess.run(
            [omp] + args, env=env,
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        return proc.returncode
    except OSError as e:
        print(f"failed to run omp: {e}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, old_int)


def resume(path):
    row = parse_session(path)
    cwd = row["cwd"] if row else ""
    if cwd and os.path.isdir(cwd):
        os.chdir(cwd)
    return launch_omp(["--resume", path])


def resume_tab(path):
    """Resume the session in a NEW Wave terminal block via `wsh run`.

    The picker stays open (the bind does not exit the loop). The block
    command runs through Wave's connserver, which starts with a bare
    env -- without RAPID_CLIENT_TOKEN / ANTHROPIC_* the new omp fails
    every request with 401, so the cached credentials are passed
    explicitly. A leading `true;` stops Wave's parser from swallowing
    the first statement (same trick as ~/.local/bin/wv).
    Returns exit code.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        print(f"resume-tab: no such session file: {path}", file=sys.stderr)
        return 3
    row = parse_session(path)
    cwd = row["cwd"] if row else ""
    if not cwd or not os.path.isdir(cwd):
        cwd = os.getcwd()
    if os.environ.get("OMPICKER_TEST_NOEXEC"):
        print(f"RESUMETAB {cwd} {path}")
        return 0
    wsh = shutil.which("wsh")
    if wsh is None:
        wsh = os.path.expanduser("~/.waveterm/bin/wsh")
        if not os.path.exists(wsh):
            print("resume-tab: wsh not found (Wave Terminal required)",
                  file=sys.stderr)
            return 3
    omp = shutil.which("omp") or (OMP_FALLBACK if os.path.exists(OMP_FALLBACK) else None)
    if omp is None:
        print("resume-tab: omp executable not found", file=sys.stderr)
        return 3
    thread = _ensure_env_thread()
    thread.join(timeout=10)
    env_prefix = ""
    if _ENV_CACHE:
        pairs = [f"{k}={shlex.quote(_ENV_CACHE[k])}"
                 for k in ("RAPID_CLIENT_TOKEN", "ANTHROPIC_BASE_URL",
                           "ANTHROPIC_AUTH_TOKEN")
                 if k in _ENV_CACHE]
        if pairs:
            env_prefix = "env " + " ".join(pairs) + " "
    cmd = f"true; {env_prefix}{shlex.quote(omp)} --resume {shlex.quote(path)}"
    try:
        proc = subprocess.run([wsh, "run", "--cwd", cwd, "-c", cmd],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        print(f"resume-tab: wsh run failed: {e}", file=sys.stderr)
        return 3
    if proc.returncode != 0:
        print(f"resume-tab: wsh run exited {proc.returncode}: "
              f"{proc.stdout.strip()[:200]}", file=sys.stderr)
        return proc.returncode
    return 0


# --------------------------------------------------------------------- main

def main():
    argv = sys.argv[1:]
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    if argv and argv[0] == "--list":
        raw = "--raw" in argv[1:]
        if raw:
            for row in load_rows():
                print(feed_line(row))
        else:
            print_list(load_rows())
        return
    if argv and argv[0] == "--header":
        state = read_state()
        projects = _available_projects()
        if state["project"] == "all":
            scope = (f"all projects ({len(projects)})" if projects
                     else "all projects")
        elif state["project"] in projects:
            pos = projects.index(state["project"]) + 1
            scope = f"project: {state['project']} [{pos}/{len(projects)}]"
        else:
            scope = f"project: {state['project']}"
        # Header layout (fzf strips raw color SGR from header text, so
        # color comes from --color=header:italic:magenta; our own SGR
        # only modulates attributes: bold title, dim keys/brackets):
        #   line 1: bold "omp conversations" + [sort | project] block
        #   line 2: dim keybinding summary in parentheses
        title = f"{BOLD}omp conversations{RESET}"
        block = (f"{DIM}[{RESET}sort: {state['sort']} | {scope}{DIM}]"
                 f"{RESET}")
        keys = (f"{RESET}{DIM}(Enter resume · alt-enter new tab"
                f" · ctrl-p project · ctrl-d del · ctrl-u undo"
                f" · ctrl-s star · ctrl-o sort · ctrl-r rename"
                f" · ctrl-n new · ctrl-f fork · tab mark){RESET}")
        print(f"{title}   {block}")
        print(keys)
        return
    if argv and argv[0] == "--sort-next":
        sort_next()
        return
    if argv and argv[0] == "--project-toggle":
        project_toggle()
        return
    if argv and argv[0] == "--star-toggle" and len(argv) > 1:
        star_toggle(argv[1])
        return
    if argv and argv[0] == "--delete" and len(argv) > 1:
        refused = failed = 0
        for path in argv[1:]:
            status, _ = delete_session(path)
            if status == "refused":
                refused += 1
            elif status == "failed":
                failed += 1
        if refused:
            sys.exit(2)
        if failed:
            sys.exit(3)
        return
    if argv and argv[0] == "--undo":
        sys.exit(undo_delete())
    if argv and argv[0] == "--rename" and len(argv) > 1:
        sys.exit(rename_session(argv[1]))
    if argv and argv[0] == "--fork" and len(argv) > 1:
        fork_session(argv[1])
        return
    if argv and argv[0] == "--resume-tab" and len(argv) > 1:
        sys.exit(resume_tab(argv[1]))
    if argv and argv[0] == "--cleanup":
        rows = load_rows()
        cands = cleanup_candidates(rows)
        if not cands:
            print(f"no conversations untouched for {CLEANUP_DAYS}+ days")
            return
        prompt_cleanup(cands)
        return


    if argv and argv[0] == "--prune-trash":
        pruned, reclaimed = prune_trash()
        if pruned:
            print(f"pruned {pruned} trash file(s), "
                  f"reclaimed {reclaimed / 1e6:.1f} MB")
        else:
            print("nothing to prune")
        return

    _ensure_env_thread()  # credentials fetch overlaps fzf render
    rows = load_rows()
    prune_trash()  # drop trash older than TRASH_KEEP_DAYS before rendering
    prune_stars({r["id"] for r in rows})
    if not rows:
        print("No omp conversations found under ~/.omp/agent/sessions")
        input("Press Enter to close: ")
        return
    # age cleanup before fzf renders: fzf's stdin is the feed pipe, so the
    # interactive prompt must happen before the subprocess starts
    cands = cleanup_candidates(rows)
    if cands and prompt_cleanup(cands):
        rows = load_rows()  # something was deleted; refresh once
        if not rows:
            return
    if os.path.exists(FZF):
        # picker loop: omp runs as a child; when it exits (Ctrl+C, /exit)
        # control returns here and the picker reopens. Esc leaves the loop.
        # rows is reused across iterations: fzf binds already reload the
        # feed themselves, so a full re-parse is only needed after a child
        # (resume/fork/new chat) actually changed the session set.
        while True:
            sel = pick_fzf(rows)
            if not sel:
                return
            key, path = sel
            if key == "ctrl-n":
                row = parse_session(path)
                new_chat(row["cwd"] if row else "")
            elif key == "ctrl-f":
                fork_session(path)
            else:
                resume(path)
            rows = load_rows()
    else:
        path = pick_numbered(rows)
        if path:
            resume(path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)  # Ctrl+C anywhere: clean exit, no traceback
