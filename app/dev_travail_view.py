#!/usr/bin/env python3
"""Tab 1 of the web app — "Le travail" as data for a template.

Two questions, in this order, as on the static tab:

  À prendre        the tickets nobody has claimed — Spec/Estim with no dev
                   analyst, To Do with none of the developers assigned
  Charge           what each developer already carries: tickets, estimate,
                   hours worked, hours left to burn, parked tickets

Inputs are the very objects the static tab is built from — the
`dev_quickstart_report.gather()` context (workload `data` + the 📌/⚡ `scope`)
and `dev_tasks_report.fetch("todo")` — and every figure goes through those
modules' own functions (`remaining`, `is_counted`, `is_parked`,
`overdue_days`), so the two pages cannot disagree.

What this tab deliberately does NOT say is whether someone has room: a stock
of ticket hours is not a capacity. That answer is tab 2's ("Temps allouable"),
and the note on the page says so.

Read-only, and no Odoo call of its own.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_tasks_report as tasks_report  # noqa: E402
import dev_workload_report as workload  # noqa: E402

# project.task.state → (label, css class). The default state is quiet.
STATES = {
    "01_in_progress": ("En cours", ""),
    "02_changes_requested": ("Modifs demandées", "warn"),
    "03_approved": ("Approuvé", "ok"),
    "04_waiting_normal": ("En attente", "muted"),
    "1_done": ("Terminé", "ok"),
    "1_canceled": ("Annulé", "muted"),
}


def _task_url(cfg, task_id):
    return f"{cfg['url']}/odoo/project.task/{task_id}"


def _project(scope, project):
    if not project:
        return {"name": "—", "planned": None}
    return {"name": project[1].strip(), "planned": scope.is_planned(project)}


def _keep(project, kind):
    return (kind == "all" or project["planned"] is None
            or project["planned"] == (kind == "planned"))


def a_prendre(todo, cfg, scope, kind):
    tasks, user_names, dev_ids = todo
    groups = {}
    for t in tasks:
        project = _project(scope, t["project_id"])
        if not _keep(project, kind):
            continue
        stage = tasks_report.stage_name(t)
        est, spent = t["dev_time"] or 0.0, t["effective_hours"] or 0.0
        state_label, state_cls = STATES.get(t["state"], (t["state"] or "—", ""))
        overdue = tasks_report.overdue_days(t["write_date"], stage)
        groups.setdefault(stage, []).append({
            "id": t["id"], "url": _task_url(cfg, t["id"]), "name": t["name"],
            "project": project, "state": state_label, "state_cls": state_cls,
            "analyst": t["dev_analyst_id"][1] if t["dev_analyst_id"] else None,
            "devs": [user_names[u] for u in t["user_ids"] if u in dev_ids],
            "est": est, "spent": spent,
            "pct": spent / est * 100.0 if est else None,
            "age": tasks_report.age_days(t["write_date"]),
            "stale": overdue > 0,
            "limit": tasks_report.STALE_DAYS.get(stage),
        })
    return [{"stage": s, "rows": rows} for s, rows in groups.items()]


def charge(data, cfg, scope, kind, devs_filter):
    ts, dev_hours, per_dev = data["ts"], data["dev_hours"], data["per_dev"]
    out = []
    for dev_id, name in data["devs"]:
        if devs_filter and name not in devs_filter:
            continue
        rows, est, own, left, unest, parked, n = [], 0.0, 0.0, 0.0, 0, 0, 0
        for t in per_dev[dev_id]:
            project = _project(scope, t["project_id"])
            if not _keep(project, kind):
                continue
            counted, is_parked = workload.is_counted(t), workload.is_parked(t)
            own_h = ts.get((dev_id, t["id"]), 0.0)
            devs_h = dev_hours.get(t["id"], 0.0)
            remaining = workload.remaining(t, dev_hours)
            if counted:
                n += 1
                est += t["dev_time"]
                own += own_h
                left += max(remaining, 0.0)
                unest += 0 if t["dev_time"] else 1
            elif is_parked:
                parked += 1
            rows.append({
                "id": t["id"], "url": _task_url(cfg, t["id"]), "name": t["name"],
                "project": project, "stage": workload.stage_name(t),
                "parked": is_parked, "counted": counted,
                "own": own_h, "devs_h": devs_h, "task_h": t["effective_hours"],
                "est": t["dev_time"], "left": remaining if t["dev_time"] else None,
                "over": bool(t["dev_time"]) and remaining < 0,
                # Share of the estimate already burnt by the dev team.
                "burn": (min(devs_h / t["dev_time"] * 100.0, 100.0)
                         if t["dev_time"] else None),
            })
        out.append({"name": name, "tasks": n, "est": est, "own": own,
                    "left": left, "unest": unest, "parked": parked, "rows": rows,
                    "burn": min(own / est * 100.0, 100.0) if est else None})
    out.sort(key=lambda d: -d["left"])
    top = max((d["left"] for d in out), default=0.0) or 1.0
    for d in out:
        d["left_pct"] = d["left"] / top * 100.0
    return out


def build(qctx, todo, cfg, devs=None, kind="all"):
    scope, data = qctx["scope"], qctx["data"]
    kind = kind if kind in ("all", "planned", "quickstart") else "all"
    groups = a_prendre(todo, cfg, scope, kind)
    load = charge(data, cfg, scope, kind, devs)
    unclaimed = [r for g in groups for r in g["rows"]]
    return {
        "kind": kind, "all_names": [n for _i, n in data["devs"]],
        "groups": groups, "load": load,
        "kpi": {
            "unclaimed": len(unclaimed),
            "unclaimed_spec": sum(len(g["rows"]) for g in groups
                                  if g["stage"] == tasks_report.STAGE_SPEC),
            "stale": sum(1 for r in unclaimed if r["stale"]),
            "live": sum(d["tasks"] for d in load),
            "left": sum(d["left"] for d in load),
            "est": sum(d["est"] for d in load),
            "unest": sum(d["unest"] for d in load),
            "parked": sum(d["parked"] for d in load),
        },
        "planned_projects": sorted({r["project"]["name"] for g in groups
                                    for r in g["rows"] if r["project"]["planned"]}
                                   | {r["project"]["name"] for d in load
                                      for r in d["rows"] if r["project"]["planned"]}),
        "parked_stages": workload.PARKED_STAGES,
        "dev_stages": workload.DEV_STAGES, "spec_stage": workload.SPEC_STAGE,
        "stale_days": tasks_report.STALE_DAYS,
    }
