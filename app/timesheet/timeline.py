#!/usr/bin/env python3
"""Attribute intervals to a line key, share overlapping time, round up.

A key is what one proposed timesheet line is about:

  ("task", id)       a known Odoo task — pushable
  ("project", id)    a project, task unknown — pushable once a task is picked
  ("bucket", name)   meeting / support / internal / admin — shown, never pushed

Overlapping intervals on different keys share their time equally (two Claude
sessions running side by side on two tickets give each half of the hour), so
the day is never counted twice. There is no daily cap: the total is whatever
was worked.
"""
import collections
import datetime as dt
import math

from ts_config import BUCKETS


def project_for_repo(repo, repo_projects):
    """Longest `repo` / `repo/worktree-prefix` key of repo_projects matching."""
    if not repo:
        return None
    best, best_len = None, -1
    for key, project_id in repo_projects.items():
        if repo == key or repo.startswith(key + "/") or (
                "/" in key and repo.startswith(key)):
            if len(key) > best_len:
                best, best_len = project_id, len(key)
    return best


def resolve(ref, source, cfg, tasks):
    """The key for one interval's Ref, or None when it is not work evidence.

    `tasks` is {task_id: {...}} for the ids Odoo confirmed exist; an id it does
    not know (a stray number in a prompt) falls back to the repo.
    """
    if ref.task_id and ref.task_id in tasks:
        return ("task", ref.task_id)
    project_id = ref.project_id or project_for_repo(ref.repo, cfg["repo_projects"])
    if project_id:
        return ("project", project_id)
    if ref.bucket in BUCKETS:
        return ("bucket", ref.bucket)
    if source == "claude":
        return ("bucket", cfg["claude_default_bucket"])
    return None


def share_time(spans, split=True):
    """{key: seconds} for [(start, end, key)]. A key never counts the same
    moment twice; with `split`, a moment shared by several keys is divided
    equally between them, otherwise each of them gets all of it."""
    points = sorted({t for s, e, _ in spans for t in (s, e)})
    out = collections.defaultdict(float)
    # Sweep elementary segments; spans are few hundred a day, O(n*m) is fine.
    ordered = sorted(spans, key=lambda x: x[0])
    for a, b in zip(points, points[1:]):
        keys = {k for s, e, k in ordered if s <= a and e >= b}
        if not keys:
            continue
        share = (b - a).total_seconds() / (len(keys) if split else 1)
        for k in keys:
            out[k] += share
    return dict(out)


def round_up(minutes, step):
    """Hours, rounded UP to `step` minutes: 5 min -> 0.25 h at step 15."""
    if minutes <= 0:
        return 0.0
    return math.ceil(round(minutes, 6) / step) * step / 60.0


def describe(tags, texts):
    """Default description in the house style ("Dev", "Analysis + Dev",
    "Review", "Push to prod"). The /timesheet skill refines it from the
    evidence; this is what you get without that pass."""
    tags = set(tags)
    words = " ".join(texts).lower()
    parts = []
    if "skill:tsc-tech-analysis:tech-analysis" in tags or "analys" in words or "estimat" in words:
        parts.append("Analysis")
    if tags & {"edit", "commit", "test"}:
        parts.append("Dev")
    if "review" in tags and "Dev" not in parts:
        parts.append("Review")
    if "push" in tags and not parts:
        parts.append("Push")
    if "meeting" in tags:
        parts.append("Meeting")
    if not parts:
        parts.append("Analysis")
    return " + ".join(parts)


def build_lines(intervals, cfg, tasks, day_start, day_end):
    """The day's proposed lines, most hours first."""
    spans, by_key = [], collections.defaultdict(list)
    for iv in intervals:
        key = resolve(iv.ref, iv.source, cfg, tasks)
        if key is None:
            continue
        start, end = max(iv.start, day_start), min(iv.end, day_end)
        if end <= start:
            continue
        spans.append((start, end, key))
        by_key[key].append(iv)
    seconds = share_time(spans, split=cfg["overlap"] == "split")
    step = cfg["rounding_minutes"]
    tz = day_start.tzinfo
    lines = []
    for key, ivs in by_key.items():
        minutes = seconds.get(key, 0.0) / 60.0
        if minutes <= 0 or minutes < cfg["min_minutes"]:
            continue
        ivs.sort(key=lambda x: x.start)
        tags = [t for iv in ivs for t in iv.tags]
        texts = [iv.text for iv in ivs if iv.text and iv.kind == "prompt"]
        evidence, seen = [], set()
        for iv in ivs:
            if not iv.text or (iv.source, iv.text) in seen:
                continue
            seen.add((iv.source, iv.text))
            evidence.append({"time": iv.start.astimezone(tz).strftime("%H:%M"),
                             "source": iv.source, "kind": iv.kind, "text": iv.text})
        kind, ident = key
        line = {
            "id": f"{kind}-{ident}",
            "key_type": kind,
            "task_id": ident if kind == "task" else None,
            "project_id": ident if kind == "project" else None,
            "bucket": ident if kind == "bucket" else None,
            "minutes": round(minutes, 1),
            "hours": round_up(minutes, step),
            "description": describe(tags, texts),
            "first": ivs[0].start.astimezone(tz).strftime("%H:%M"),
            "last": max(iv.end for iv in ivs).astimezone(tz).strftime("%H:%M"),
            "sources": dict(collections.Counter(iv.source for iv in ivs)),
            "evidence": evidence[:40],
        }
        if kind == "task":
            line["project_id"] = tasks[ident].get("project_id")
        lines.append(line)
    lines.sort(key=lambda x: (-x["minutes"], x["id"]))
    return lines


def day_bounds(day, tz=None):
    """[start, end) of a calendar day, as aware datetimes — in `tz`, else in
    the machine's local zone (each bound gets its own offset, so a DST change
    day is 23 or 25 hours long, as it should be)."""
    nxt = day + dt.timedelta(days=1)
    if tz is not None:
        return (dt.datetime.combine(day, dt.time.min, tzinfo=tz),
                dt.datetime.combine(nxt, dt.time.min, tzinfo=tz))
    return (dt.datetime.combine(day, dt.time.min).astimezone(),
            dt.datetime.combine(nxt, dt.time.min).astimezone())
