#!/usr/bin/env python3
"""Every timesheet line behind one row of "Par développeur — projet et tâche".

Served live by `dev_webapp.py` at `/lines`, because the drill-down the
row needs — one developer's lines on one task since 1 January — cannot be put
in an Odoo URL: since Odoo 17 an `/odoo/...` link opens a record or an action,
never a filtered list. Embedding every line in the page instead would carry the
whole team's year (thousands of lines) for the few rows anyone opens.

Query parameters, all built by `dev_timesheet_report.lines_url`:

    emp      comma-separated hr.employee ids (every record of the developer)
    name     the developer's name, for the title only
    from/to  the window, YYYY-MM-DD
    project  project id(s), comma-separated, 0 for "no project" — absent
             means every project
    task     task id, 0 for "no task" — absent means every task
    kind     internal | invest | nb (both) — the row's nature
    pairs    "pid:tid,pid:tid" — several project × task rows at once (an
             "N autres" row); 0 = none. Overrides project/task.
    leave    1 = only the lines whose label looks like leave (`LEAVE_RX`)

Read-only: one search_read per request. `context()` returns the data;
`dev_webapp.py` renders it with the Jinja templates under `webapp/`.
"""
import datetime
import os
import sys
from collections import defaultdict
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_timesheet_report as report  # noqa: E402

KINDS = {"internal": {"internal"}, "invest": {"invest"},
         "nb": {"internal", "invest"}}

def _one(params, key):
    values = params.get(key)
    return values[0] if values else None


def _optional_id(params, key):
    """None when the filter is absent, else the list of ids (0 = "none set")."""
    raw = _one(params, key)
    return None if raw is None or raw == "" else [int(x) for x in raw.split(",")]


def _in(field, ids):
    """`field` in ids, where 0 stands for "not set" — one Odoo domain term, or
    an OR of two when both kinds are asked for."""
    real = [i for i in ids if i]
    if real and len(real) < len(ids):
        return ["|", [field, "in", real], [field, "=", False]]
    return [[field, "in", real]] if real else [[field, "=", False]]


def fetch(kw, cfg, params):
    emp_ids = [int(x) for x in (_one(params, "emp") or "").split(",") if x]
    if not emp_ids:
        raise ValueError("paramètre `emp` manquant")
    date_from = datetime.date.fromisoformat(_one(params, "from"))
    date_to = datetime.date.fromisoformat(_one(params, "to"))
    domain = [["employee_id", "in", emp_ids],
              ["date", ">=", date_from.isoformat()],
              ["date", "<=", date_to.isoformat()]]
    project, task = _optional_id(params, "project"), _optional_id(params, "task")
    pairs = [tuple(int(x) for x in pair.split(":"))
             for pair in (_one(params, "pairs") or "").split(",") if pair]
    if pairs:
        # (p1 AND t1) OR (p2 AND t2) … in Odoo's prefix notation.
        project = task = None
        domain += ["|"] * (len(pairs) - 1)
        for pid, tid in pairs:
            domain += ["&", ["project_id", "=", pid or False],
                       ["task_id", "=", tid or False]]
    if project is not None:
        domain += _in("project_id", project)
    if task is not None:
        domain += _in("task_id", task)
    lines = kw("account.analytic.line", "search_read", [domain],
               {"fields": ["date", "unit_amount", "project_id", "task_id",
                           "timesheet_invoice_type", "name"],
                "order": "date desc, id desc"})
    # The same classification as the report, so the page's total is the row's.
    leave_ids = set(cfg.get("leave_project_ids") or [])
    wanted = KINDS.get(_one(params, "kind") or "nb", KINDS["nb"])
    lines = [ln for ln in lines if report.classify(ln, leave_ids) in wanted]
    if _one(params, "leave") == "1":
        lines = [ln for ln in lines if report.LEAVE_RX.search(ln["name"] or "")]
    return lines, date_from, date_to, project, task


def context(kw, cfg, query):
    """The template context for one drill-down: the lines, a summary by label,
    and what the page is about. Rendered by `webapp/templates/_lines.html`."""
    params = parse_qs(query)
    lines, date_from, date_to, project, task = fetch(kw, cfg, params)
    fixed_project, fixed_task = project is not None, task is not None
    first = lines[0] if lines else None

    # Several ids can still be one name ("Interne" exists per company, and a
    # row carries every id behind its label): title by the names seen.
    project_names = {ln["project_id"][1] if ln["project_id"] else "(sans projet)"
                     for ln in lines}
    task_names = {ln["task_id"][1] if ln["task_id"] else "(sans tâche)" for ln in lines}
    if len(project_names) == 1:
        project_label, fixed_project = next(iter(project_names)), True
    elif not fixed_project:
        project_label = "plusieurs projets"
    else:
        project_label = "(aucune ligne)"
    if len(task_names) == 1 and not fixed_task:
        fixed_task = True
    task_obj = None
    if fixed_task and first and first["task_id"] and len(task_names) == 1:
        task_obj = {"name": first["task_id"][1],
                    "url": f"{cfg['url']}/odoo/project.task/{first['task_id'][0]}"}
    task_label = (task_obj["name"] if task_obj
                  else next(iter(task_names)) if len(task_names) == 1
                  else "plusieurs tâches")

    total = sum(ln["unit_amount"] for ln in lines)
    by_label = defaultdict(lambda: [0, 0.0])
    rows = []
    for ln in lines:
        label = (ln["name"] or "").strip() or "/"
        by_label[label.lower()][0] += 1
        by_label[label.lower()][1] += ln["unit_amount"]
        day = datetime.date.fromisoformat(ln["date"])
        rows.append({
            "date": f"{report.date_fr(day)}/{day:%y}",
            "hours": ln["unit_amount"], "label": label,
            "project": ln["project_id"][1] if ln["project_id"] else "(sans projet)",
            "task": ln["task_id"][1] if ln["task_id"] else None,
            "task_url": (f"{cfg['url']}/odoo/project.task/{ln['task_id'][0]}"
                         if ln["task_id"] else None),
            "kind": ln["timesheet_invoice_type"] or "",
        })
    labels = [{"label": lab, "count": c, "hours": h,
               "share": h / total * 100.0 if total else 0.0}
              for lab, (c, h) in sorted(by_label.items(), key=lambda kv: -kv[1][1])]
    return {"name": _one(params, "name") or "Développeur",
            "project_label": project_label, "task_label": task_label,
            "task_obj": task_obj, "fixed_project": fixed_project,
            "fixed_task": fixed_task,
            "subtitle": ("congés pointés hors du projet Congés"
                         if _one(params, "leave") == "1" else "non facturable"),
            "date_from": f"{date_from:%d/%m/%Y}", "date_to": f"{date_to:%d/%m/%Y}",
            "total": total, "rows": rows, "labels": labels}
