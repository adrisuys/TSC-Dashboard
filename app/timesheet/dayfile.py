#!/usr/bin/env python3
"""Build, store and edit one day's proposal.

The day file (DATA_DIR/days/<date>.json) is the single source of truth shared by
the CLI, the /timesheet skill and the review page. Rebuilding a day re-reads
the traces but keeps what a person decided: edited hours and descriptions, a
task picked for a project line, ticks, and above all what was already pushed.
"""
import datetime as dt
import json
import math
import os

import collectors
import odoo_sync
import timeline
import ts_config

# Fields a person (or the skill) may set; a rebuild never overwrites them.
EDITABLE = ("hours", "description", "selected", "project_id", "task_id")


def collect(cfg, start, end):
    """All intervals of [start, end) plus a per-source status."""
    intervals, status = [], {}
    for name, fn in (("claude", collectors.collect_claude),
                     ("chrome", collectors.collect_chrome),
                     ("git", collectors.collect_git)):
        try:
            found, info = fn(cfg, start, end)
        except collectors.SourceUnavailable as exc:
            status[name] = {"ok": False, "error": str(exc)}
            continue
        except Exception as exc:  # one broken source must not cost the day
            status[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            continue
        intervals.extend(found)
        status[name] = {"ok": True, "intervals": len(found), **info}
    return intervals, status


def load(day):
    try:
        with open(ts_config.day_path(day)) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def save(data):
    path = ts_config.day_path(data["date"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def default_selected(line, cfg):
    if line.get("pushed"):
        return False
    if line["key_type"] == "bucket" or not line.get("task_id"):
        return False
    # Ticked only while tracked time remains beyond what was booked by hand.
    return line.get("hours", 0) > 0


def reopen_if_grown(line):
    """A pushed line whose traces kept growing (work went on after the push)
    goes back to the proposal for the top-up; its odoo_ids keep the history."""
    if not line.get("pushed") or line.get("vanished"):
        return
    if line["hours"] > line.get("logged_hours", 0.0):
        line["pushed"] = False
        line["edited"] = [f for f in line.get("edited", []) if f not in ("hours", "selected")]


def deduct_logged(line):
    """Propose only what the day's Odoo lines on that task do not cover yet:
    1 h tracked with 0.5 h booked by hand leaves a 0.5 h line to push."""
    if line.get("pushed") or line["key_type"] == "bucket" or "hours" in line.get("edited", []):
        return
    line["tracked_hours"] = line["hours"]
    rest = round(line["hours"] - line.get("logged_hours", 0.0), 4)
    line["hours"] = max(0.0, math.ceil(rest * 4) / 4)


def merge(new_lines, previous):
    """Carry decisions over from the previous build of the same day."""
    prev = {l["id"]: l for l in (previous or {}).get("lines", [])}
    out = []
    for line in new_lines:
        old = prev.pop(line["id"], None)
        if old:
            for field in old.get("edited", []):
                if field in EDITABLE:
                    line[field] = old[field]
            line["edited"] = list(old.get("edited", []))
            for field in ("pushed", "odoo_ids", "push_error"):
                if old.get(field):
                    line[field] = old[field]
        out.append(line)
    # A pushed line whose traces vanished (a transcript deleted) stays visible.
    out.extend({**l, "vanished": True} for l in prev.values() if l.get("pushed"))
    return out


def enrich(lines, tasks, projects, logged):
    for line in lines:
        task = tasks.get(line.get("task_id")) if line.get("task_id") else None
        if task:
            line["task_name"] = task["name"]
            # A task picked by hand may sit in another project than the repo's.
            line["project_id"] = task["project_id"]
        line["project_name"] = (projects.get(line.get("project_id")) or {}).get("name", "") \
            or (task or {}).get("project_name", "")
    set_logged(lines, logged)


def set_logged(lines, logged):
    """Hours already in Odoo for each line's task (or task-less project)."""
    for line in lines:
        if line.get("task_id"):
            line["logged_hours"] = sum(l["hours"] for l in logged if l["task_id"] == line["task_id"])
        elif line.get("project_id"):
            line["logged_hours"] = sum(l["hours"] for l in logged
                                       if l["project_id"] == line["project_id"] and not l["task_id"])
        else:
            line["logged_hours"] = 0.0


def build(cfg, day, kw, uid, tz=None):
    start, end = timeline.day_bounds(day, tz)
    intervals, status = collect(cfg, start, end)
    tasks = odoo_sync.fetch_tasks(kw, [iv.ref.task_id for iv in intervals if iv.ref.task_id])
    lines = timeline.build_lines(intervals, cfg, tasks, start, end)
    previous = load(str(day))
    lines = merge(lines, previous)
    # Tasks picked by hand on project lines need their names too.
    extra = [l["task_id"] for l in lines if l.get("task_id") and l["task_id"] not in tasks]
    tasks.update(odoo_sync.fetch_tasks(kw, extra))
    projects = odoo_sync.fetch_projects(kw, [l.get("project_id") for l in lines])
    logged = odoo_sync.logged_lines(kw, uid, day)
    enrich(lines, tasks, projects, logged)
    for line in lines:
        reopen_if_grown(line)
        deduct_logged(line)
        if "selected" not in line.get("edited", []):
            line["selected"] = default_selected(line, cfg)
    data = {
        "date": str(day),
        "built_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": status,
        "lines": lines,
        "logged": logged,
    }
    save(data)
    return data


def apply_edits(data, edits):
    """edits: [{"id", hours?, description?, selected?, project_id?, task_id?}]."""
    by_id = {l["id"]: l for l in data["lines"]}
    for edit in edits:
        line = by_id.get(edit.get("id"))
        if not line or line.get("pushed"):
            continue
        for field in EDITABLE:
            if field not in edit:
                continue
            value = edit[field]
            if field == "hours":
                value = max(0.0, round(float(value) * 4) / 4)
            elif field in ("task_id", "project_id"):
                value = int(value) if value else None
            elif field == "selected":
                value = bool(value)
            elif field == "description":
                value = str(value).strip()
            if line.get(field) != value:
                line[field] = value
                if field not in line.setdefault("edited", []):
                    line["edited"].append(field)
    return data


def refresh_tasks(data, kw):
    """Names and projects for a task or project changed by hand, after an edit."""
    ids = [l["task_id"] for l in data["lines"] if l.get("task_id")]
    tasks = odoo_sync.fetch_tasks(kw, ids)
    projects = odoo_sync.fetch_projects(
        kw, [l.get("project_id") for l in data["lines"] if not tasks.get(l.get("task_id"))])
    for line in data["lines"]:
        task = tasks.get(line.get("task_id"))
        if task:
            line["task_name"] = task["name"]
            line["project_id"] = task["project_id"]
            line["project_name"] = task["project_name"]
            continue
        line.pop("task_name", None)
        if line.get("project_id") in projects:
            line["project_name"] = projects[line["project_id"]]["name"]
    set_logged(data["lines"], data.get("logged", []))
    return data


def pushable(line):
    return (line.get("selected") and not line.get("pushed")
            and line["key_type"] in ("task", "project")
            and line.get("task_id") and line.get("project_id") and line.get("hours", 0) > 0)


def record_push(data, results):
    by_id = {l["id"]: l for l in data["lines"]}
    for res in results:
        line = by_id.get(res["id"])
        if not line:
            continue
        if res["odoo_id"]:
            line["pushed"] = True
            line["odoo_ids"] = line.get("odoo_ids", []) + [res["odoo_id"]]
            line["selected"] = False
            line.pop("push_error", None)
        else:
            line["push_error"] = res["error"]
    return data
