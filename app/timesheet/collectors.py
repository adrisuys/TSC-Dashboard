#!/usr/bin/env python3
"""Read the day's local traces and turn them into attributed time intervals.

Three sources, all read-only and all local:

- Claude Code transcripts  ~/.claude/projects/*/*.jsonl
- Chrome history           <profile>/History (SQLite, copied first: Chrome locks it)
- git commits              custom/<version>/<repo>/.bare (or .git)

Each collector returns `Interval`s: a time span plus a `Ref` saying what it was
about (a task id, a repo, a project, a non-ticket bucket). Turning a Ref into an
Odoo task or project is attribution.py's job; splitting overlapping intervals
into hours is timeline.py's.
"""
import dataclasses
import datetime as dt
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.parse

UTC = dt.timezone.utc


@dataclasses.dataclass
class Ref:
    task_id: int = None
    repo: str = None        # "european-schools" or "european-schools/dev-30581"
    project_id: int = None
    bucket: str = None

    def empty(self):
        return not (self.task_id or self.repo or self.project_id or self.bucket)


@dataclasses.dataclass
class Interval:
    start: dt.datetime
    end: dt.datetime
    source: str             # claude | chrome | git
    ref: Ref
    kind: str = "work"      # prompt | edit | commit | push | review | browse | work
    text: str = ""          # evidence shown on the review page
    tags: tuple = ()        # every kind seen in the span, for the description


class SourceUnavailable(Exception):
    """A source exists but cannot be read (permission, missing profile...)."""


# --- task id / repo extraction -------------------------------------------------

# Explicit references: an Odoo task URL, a branch or worktree name, a #id.
_TASK_URL = re.compile(
    r"(?:project\.task/|/tasks/|my-tasks/|[?&#]id=(?=\d+&model=project\.task))(\d{3,6})\b"
    r"|model=project\.task&(?:[^\s\"']*&)?id=(\d{3,6})\b")
_BRANCH = re.compile(r"(?<![A-Za-z])(?:dev|fix|feat|feature|hotfix|bug)[-/_](\d{4,6})(?!\d)")
# `(?!\w)`, not `(?!\d)`: a CSS colour like #3987e5 must not read as task 3987.
_HASH = re.compile(r"(?<![\w&])#(\d{4,6})(?!\w)")
# Bare ticket numbers are only trusted in what the user typed, and are checked
# against Odoo before they count.
_BARE = re.compile(r"(?<![\w./:#=-])([23]\d{4})(?![\w])")
_REPO_PATH = re.compile(r"/custom/(?:\d+\.0|utilities)/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?")


def task_ids_in(text, bare=False):
    """Task ids in `text`, explicit references first, in order of appearance."""
    if not text:
        return []

    def scan(patterns):
        found = []
        for rx in patterns:
            for m in rx.finditer(text):
                found.append((m.start(), int(next(g for g in m.groups() if g))))
        return [v for _, v in sorted(found)]

    # URLs, branches and #ids outrank bare numbers even when they come later.
    out = []
    for value in scan((_TASK_URL, _BRANCH, _HASH)) + (scan((_BARE,)) if bare else []):
        if value not in out:
            out.append(value)
    return out


def repos_in(text):
    """['repo' or 'repo/worktree', ...] for every custom/ path in `text`."""
    out = []
    for m in _REPO_PATH.finditer(text or ""):
        repo, sub = m.group(1), m.group(2)
        out.append(f"{repo}/{sub}" if sub else repo)
    return out


def compile_path_tasks(path_tasks):
    """[(regex, task id)] from {folder: task id}, the longest folder first."""
    rules = []
    for path, task in sorted((path_tasks or {}).items(), key=lambda kv: -len(kv[0])):
        folder = os.path.expanduser(path).rstrip("/")
        rules.append((re.compile(re.escape(folder) + r"(?![\w.-])"), int(task)))
    return rules


def path_task_in(text, rules):
    """The task of the most specific folder in `text`, else None."""
    if text:
        for rx, task in rules:
            if rx.search(text):
                return task
    return None


def parse_ts(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


# --- Claude Code transcripts -------------------------------------------------------

_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
_NOISE_PREFIXES = ("<system-reminder", "<local-command", "<command-message",
                   "[Request interrupted", "<task-notification")


def _prompt_text(content):
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content):
            return None
        text = " ".join(x.get("text", "") for x in content
                        if isinstance(x, dict) and x.get("type") == "text")
    else:
        return None
    text = text.strip()
    if not text or text.startswith(_NOISE_PREFIXES):
        return None
    return text


_READONLY = re.compile(
    r"^(?:cd|ls|cat|head|tail|grep|rg|find|wc|echo|pwd|which|file|stat|du|df|sort|uniq|cut|tr"
    r"|sed\s+-n|awk|jq|tree|diff|true|test|\[|curl\s+-s|date"
    r"|git\s+(?:-C\s+\S+\s+)?(?:log|show|diff|status|config|branch|rev-parse|remote|fetch"
    r"|ls-files|ls-remote|blame|describe|tag|worktree\s+list|for-each-ref|merge-base|cat-file))\b")


def _readonly_bash(cmd):
    """True when every part of a shell command only reads."""
    parts = [p.strip() for p in re.split(r"&&|\|\||;|\||\n", cmd) if p.strip()]
    return bool(parts) and all(_READONLY.match(p) for p in parts)


def _tool_hints(item):
    """(kind, text to scan for repos/ids) for one tool_use block."""
    name = item.get("name", "")
    args = item.get("input") or {}
    if name in _EDIT_TOOLS:
        return "edit", str(args.get("file_path") or args.get("notebook_path") or "")
    if name == "Bash":
        cmd = str(args.get("command", ""))
        if _readonly_bash(cmd):
            # Looking around (ls, git log, cat...) is not working there.
            return "work", ""
        if re.search(r"\bgit\s+push\b", cmd):
            kind = "push"
        elif re.search(r"\bgit\s+commit\b", cmd):
            kind = "commit"
        elif re.search(r"\bgh\s+pr\b", cmd):
            kind = "review"
        elif re.search(r"odoo-bin|start_odoo|--test-enable|--test-tags", cmd):
            kind = "test"
        else:
            kind = "work"
        return kind, cmd
    if name == "Skill":
        return "skill:" + str(args.get("skill", "")), str(args.get("args", ""))
    # Reads and searches say nothing about which repo we are *working* in:
    # looking at another client's code for a pattern must not move the clock.
    return None, ""


def read_claude_events(path, start, end, path_rules=()):
    """[(ts, kind, text, task_ids, repos, cwd)] for the main thread of one
    transcript, inside [start, end). An edit in a folder of `path_rules`
    (compile_path_tasks) is about that folder's task first."""
    events = []
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("isSidechain") or d.get("type") not in ("user", "assistant"):
                continue
            ts = d.get("timestamp")
            if not ts:
                continue
            ts = parse_ts(ts)
            if not (start <= ts < end):
                continue
            cwd = d.get("cwd") or ""
            content = (d.get("message") or {}).get("content")
            if d["type"] == "user":
                if d.get("isMeta"):
                    continue
                text = _prompt_text(content)
                if text is None:
                    # A tool result: still activity, nothing to attribute.
                    events.append((ts, "work", "", [], [], cwd))
                    continue
                events.append((ts, "prompt", text, task_ids_in(text, bare=True),
                               repos_in(text), cwd))
                continue
            if not isinstance(content, list):
                continue
            hits = [_tool_hints(x) for x in content
                    if isinstance(x, dict) and x.get("type") == "tool_use"]
            hits = [h for h in hits if h[0]]
            if not hits:
                events.append((ts, "work", "", [], [], cwd))
            for kind, text in hits:
                ids = task_ids_in(text)
                # Edits only: a command running a skill's script uses the skill.
                folder_task = path_task_in(text, path_rules) if kind == "edit" else None
                if folder_task:
                    ids = [folder_task] + [i for i in ids if i != folder_task]
                events.append((ts, kind, "", ids, repos_in(text), cwd))
    events.sort(key=lambda e: e[0])
    return events


def _worktree_task(repo):
    """dev-30581 / fix-29715-shop-entry-date -> 30581 / 29715."""
    if repo and "/" in repo:
        ids = task_ids_in(repo.split("/", 1)[1])
        return ids[0] if ids else None
    return None


def move_to_repo(ctx, repo):
    """The context after work moves into `repo` ('repo' or 'repo/worktree').

    A worktree named after a ticket sets that ticket. Moving between worktrees
    of the same repo keeps the current ticket (merging dev-30581 into staging is
    still 30581); moving to another repo drops it.
    """
    if repo == ctx.repo:
        return ctx
    task = _worktree_task(repo)
    if task is None and ctx.repo and repo.split("/")[0] == ctx.repo.split("/")[0]:
        task = ctx.task_id
    return Ref(task_id=task, repo=repo)


def _root(repo):
    return repo.split("/")[0] if repo else None


def _fill_repo_tasks(states):
    """Give a task to stretches that know their repo but not their ticket.

    "Create the staging worktree" comes before "now branch dev/29320 off it":
    the setup is part of that ticket. A stretch takes the next task found in
    the same repo, else the previous one; a change of repo is a hard border.
    Stretches with no task on either side stay project-level.
    """
    n = len(states)
    nxt = [None] * n
    following = None
    for i in range(n - 1, -1, -1):
        ref = states[i][3]
        if following and _root(following.repo) != _root(ref.repo):
            following = None
        if ref.task_id:
            following = ref
        nxt[i] = following
    preceding = None
    for i in range(n):
        ts, kind, text, ref = states[i]
        if preceding and _root(preceding.repo) != _root(ref.repo):
            preceding = None
        if ref.task_id:
            preceding = ref
            continue
        if not ref.repo:
            continue
        donor = nxt[i] or preceding
        if donor and _root(donor.repo) == _root(ref.repo):
            states[i] = (ts, kind, text, Ref(task_id=donor.task_id, repo=ref.repo))


def session_intervals(events, idle, tail, path_rules=()):
    """Turn one session's events into intervals.

    The session carries a context — the task, else the repo — that each event
    may move: a task id switches the task; a path into another repo switches
    the repo (see move_to_repo). The working directory counts only for the
    first event — the folder the session was opened in. After that it follows
    every `cd`, including the ones that only look around.

    The span between two events belongs to the context at the first one; a
    silence longer than `idle` ends a block, which is credited `tail` of
    reading time. Events before the first hint inherit it — a session often
    starts with a generic prompt and only then moves into the worktree.
    """
    ctx = Ref()
    states = []
    for n, (ts, kind, text, ids, repos, cwd) in enumerate(events):
        if n == 0:
            repos = repos or repos_in(cwd)
            folder_task = path_task_in(cwd, path_rules)
            if folder_task and not ids:
                ids = [folder_task]
        if repos:
            ctx = move_to_repo(ctx, repos[0])
        if ids:
            ctx = Ref(task_id=ids[0], repo=ctx.repo)
        states.append((ts, kind, text, dataclasses.replace(ctx)))
    first_i = next((i for i, s in enumerate(states) if not s[3].empty()), None)
    if first_i:
        # Back-fill only the block the first hint sits in: an hour of tooling
        # in the morning is not part of the ticket opened after lunch.
        first = states[first_i][3]
        for i in range(first_i - 1, -1, -1):
            if states[i + 1][0] - states[i][0] > idle:
                break
            ts, kind, text, _ = states[i]
            states[i] = (ts, kind, text, dataclasses.replace(first))
    _fill_repo_tasks(states)

    out = []
    for i, (ts, kind, text, ref) in enumerate(states):
        nxt = states[i + 1][0] if i + 1 < len(states) else None
        end = nxt if nxt is not None and nxt - ts <= idle else ts + tail
        if end > ts:
            out.append(Interval(ts, end, "claude", ref, kind, text[:400], (kind,)))
        elif text:
            # Same-timestamp prompt: keep its words as evidence on a 1 s span.
            out.append(Interval(ts, ts + dt.timedelta(seconds=1), "claude", ref, kind,
                                text[:400], (kind,)))
    return out


def collect_claude(cfg, start, end):
    root = cfg["claude_projects_dir"]
    idle = dt.timedelta(minutes=cfg["idle_gap_minutes"])
    tail = dt.timedelta(minutes=cfg["tail_minutes"])
    rules = compile_path_tasks(cfg.get("path_tasks"))
    out, sessions = [], 0
    for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
        if os.path.getmtime(path) < start.timestamp():
            continue
        events = read_claude_events(path, start, end, rules)
        if not events:
            continue
        sessions += 1
        out.extend(session_intervals(events, idle, tail, rules))
    return out, {"sessions": sessions}


# --- Chrome history -------------------------------------------------------------

_CHROME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=UTC)


def chrome_time(us):
    return _CHROME_EPOCH + dt.timedelta(microseconds=us)


def to_chrome_time(when):
    return int((when - _CHROME_EPOCH).total_seconds() * 1_000_000)


def find_chrome_profile(chrome_dir, email):
    try:
        with open(os.path.join(chrome_dir, "Local State")) as fh:
            cache = json.load(fh)["profile"]["info_cache"]
    except PermissionError as exc:
        raise SourceUnavailable(
            "macOS refuses access to Chrome's data. Give Full Disk Access to the app "
            "running Claude Code (System Settings > Privacy & Security > Full Disk "
            "Access), then restart it.") from exc
    except (OSError, ValueError, KeyError) as exc:
        raise SourceUnavailable(f"Chrome profile list unreadable: {exc}") from exc
    for name, info in cache.items():
        if (info.get("user_name") or "").lower() == email.lower():
            return os.path.join(chrome_dir, name)
    raise SourceUnavailable(f"No Chrome profile signed in as {email}.")


def classify_url(url, title, cfg):
    """(Ref, kind) for one visited page; an empty Ref means "not work evidence"."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    full = f"{parts.path}?{parts.query}#{parts.fragment}"

    def host_in(hosts):
        return any(host == h or host.endswith("." + h) for h in hosts)

    if host_in([cfg["odoo_host"]]):
        ids = task_ids_in(full)
        if ids:
            return Ref(task_id=ids[0]), "browse"
        if "helpdesk" in full:
            return Ref(bucket="support"), "browse"
        if re.search(r"timesheet|account\.analytic\.line|/odoo/(?:time-?off|expenses)", full):
            return Ref(bucket="admin"), "browse"
        return Ref(), "browse"
    if host == "github.com":
        segs = [s for s in parts.path.split("/") if s]
        if len(segs) >= 2 and segs[0] in cfg["github_orgs"]:
            ids = task_ids_in(parts.path) or task_ids_in(title or "")
            kind = "review" if len(segs) >= 3 and segs[2] == "pull" else "browse"
            return Ref(task_id=ids[0] if ids else None, repo=segs[1]), kind
        return Ref(), "browse"
    for bucket, hosts in cfg["bucket_hosts"].items():
        if host_in(hosts):
            return Ref(bucket=bucket), "meeting" if bucket == "meeting" else "browse"
    for suffix, project_id in cfg["domain_projects"].items():
        if host_in([suffix]):
            return Ref(project_id=project_id, task_id=(task_ids_in(full) or [None])[0]), "browse"
    return Ref(), "browse"


def read_chrome_visits(history_path, start, end):
    """[(datetime, url, title)] ordered, from a Chrome History SQLite file."""
    tmp = tempfile.mkdtemp(prefix="tsc-timesheet-")
    try:
        copy = os.path.join(tmp, "History")
        try:
            shutil.copy2(history_path, copy)
            for suffix in ("-wal", "-journal"):
                if os.path.exists(history_path + suffix):
                    shutil.copy2(history_path + suffix, copy + suffix)
        except PermissionError as exc:
            raise SourceUnavailable(
                "macOS refuses access to Chrome's history. Give Full Disk Access to "
                "the app running Claude Code, then restart it.") from exc
        con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT v.visit_time, u.url, u.title FROM visits v "
                "JOIN urls u ON u.id = v.url WHERE v.visit_time >= ? AND v.visit_time < ? "
                "ORDER BY v.visit_time",
                (to_chrome_time(start), to_chrome_time(end))).fetchall()
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return [(chrome_time(t), url, title or "") for t, url, title in rows]


def chrome_intervals(visits, cfg):
    cap = dt.timedelta(minutes=cfg["chrome_visit_cap_minutes"])
    out = []
    for i, (ts, url, title) in enumerate(visits):
        ref, kind = classify_url(url, title, cfg)
        if ref.empty():
            continue
        nxt = visits[i + 1][0] if i + 1 < len(visits) else ts + cap
        end = min(nxt, ts + cap)
        if end <= ts:
            continue
        out.append(Interval(ts, end, "chrome", ref, kind, (title or url)[:200], (kind,)))
    return out


def collect_chrome(cfg, start, end):
    profile = find_chrome_profile(cfg["chrome_dir"], cfg["chrome_profile_email"])
    visits = read_chrome_visits(os.path.join(profile, "History"), start, end)
    return chrome_intervals(visits, cfg), {"visits": len(visits),
                                           "profile": os.path.basename(profile)}


# --- git commits ----------------------------------------------------------------

def git_repos(workspace_root):
    """{repo name: git dir} for every repo under custom/<version>/."""
    out = {}
    for path in sorted(glob.glob(os.path.join(workspace_root, "custom", "*", "*"))):
        name = os.path.basename(path)
        if os.path.isdir(os.path.join(path, ".bare")):
            out[name] = os.path.join(path, ".bare")
        elif os.path.exists(os.path.join(path, ".git")):
            out[name] = os.path.join(path, ".git")
    return out


def parse_git_log(output, repo, lead):
    out = []
    for line in output.splitlines():
        try:
            when, source, subject = line.split("\x1f", 2)
        except ValueError:
            continue
        # A merge names the branches it joins, not the ticket being worked on;
        # the session that ran it attributes that time better.
        if subject.startswith("Merge "):
            continue
        ts = parse_ts(when)
        # Commit subjects carry ids bare: "[29320][FIX] ...", "[ADD] 30558: ...".
        ids = task_ids_in(subject, bare=True) or task_ids_in(source)
        out.append(Interval(ts - lead, ts, "git", Ref(task_id=ids[0] if ids else None, repo=repo),
                            "commit", subject[:200], ("commit",)))
    return out


def collect_git(cfg, start, end):
    lead = dt.timedelta(minutes=cfg["commit_lead_minutes"])
    out, seen = [], set()
    for repo, gitdir in git_repos(cfg["workspace_root"]).items():
        try:
            res = subprocess.run(
                ["git", f"--git-dir={gitdir}", "log", "--all", "--source",
                 f"--author={cfg['git_author']}", f"--since={start.isoformat()}",
                 f"--until={end.isoformat()}", "--format=%H\x1f%aI\x1f%S\x1f%s"],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        lines = []
        for line in res.stdout.splitlines():
            sha, rest = line.split("\x1f", 1) if "\x1f" in line else (None, None)
            if sha and sha not in seen:
                seen.add(sha)
                lines.append(rest)
        out.extend(parse_git_log("\n".join(lines), repo, lead))
    return out, {"commits": len(out)}
