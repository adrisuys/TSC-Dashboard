#!/usr/bin/env python3
"""Tab 2 of the web app — "Capacité" as data for a template.

Same two layers as the static tab, in the same order:

  1. this week against the quickstart queue — who can take a ticket
     (`dev_quickstart_report.semaine_data`), then what the plan already
     commits in the weeks after (`engagement_data`);
  2. the capacity report proper (`dev_capacity_report`): occupation,
     adherence, commitment ahead, runway per project, data quality.

No figure is computed here: every number comes from those modules' `*_data`
functions — the very ones their Markdown sections print — so the two pages
are one computation laid out twice. This module only filters by developer and
adds what a native layout needs (bands, bar widths, a scale).

Read-only, and no Odoo call of its own.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_capacity_report as cap  # noqa: E402
import dev_quickstart_report as qs  # noqa: E402
import md_to_html  # noqa: E402


def _band(share):
    """CSS band for an occupation share — the report's own thresholds."""
    if share is None:
        return "none"
    if share < cap.ROOM_BELOW:
        return "ok"
    if share < cap.FULL_ABOVE:
        return "warn"
    return "err"


def _keep(rows, names, key="name"):
    return [r for r in rows if r[key] in names]


def _who(sem_rows, engagement, occ_rows, forward):
    """"Qui est disponible ?" — the whole team, whatever the developer filter,
    most available first: this week's allocable time (free − quickstart
    queue), then the free hours the plan leaves in the weeks after. The
    recent occupation rides along so a "free" row that is really an
    incomplete plan reads as such."""
    ahead = {r["name"]: r["free_avg"] for r in engagement}
    occ = {r["name"]: r for r in occ_rows}
    pool = {r["name"] for r in forward if r["pool_idle"]}
    rows = []
    for r in sem_rows:
        o = occ.get(r["name"], {})
        rows.append({
            "name": r["name"], "state": r["state"], "allocable": r["allocable"],
            "free": r["free"], "queue": r["queue"],
            "unestimated": r["unestimated"], "leave_days": r["leave_days"],
            "projects": r["projects"], "ahead": ahead.get(r["name"], 0.0),
            "occ": o.get("avg"), "occ_band": _band(o.get("avg")),
            "gap": o.get("gap", False), "pool": r["name"] in pool})
    rows.sort(key=lambda r: (-r["allocable"], -r["ahead"]))
    return rows


def build(qctx, cctx, cfg, devs=None):
    all_names = [n for _u, _e, n in cctx["devs"]]
    names = set(n for n in all_names if not devs or n in devs) or set(all_names)

    # --- 1. this week vs quickstart ------------------------------------------
    sem = qs.semaine_data(qctx["devs"], qctx["detail"], qctx["schedules"],
                          qctx["leave"], qctx["planned"], qctx["projects"],
                          qctx["scope"], qctx["data"])
    sem_rows = _keep(sem["rows"], names)
    scale = max([abs(r["allocable"]) for r in sem_rows] + [1.0])
    for r in sem_rows:
        r["bar"] = min(abs(r["allocable"]) / scale * 50.0, 50.0)
    totals = {k: sum(r[k] for r in sem_rows) for k in ("free", "queue", "allocable")}
    engagement_all = qs.engagement_data(qctx["devs"], qctx["future"],
                                        qctx["schedules"], qctx["leave"],
                                        qctx["planned"], qctx["projects"])
    engagement = _keep(engagement_all, names)

    # --- 2. capacity report ----------------------------------------------------
    occ = cap.occupation_data(cctx)
    occ_rows = _keep(occ["rows"], names)
    for r in occ_rows:
        r["band"] = _band(r["avg"])
        for c in r["cells"]:
            c["band"] = "leave" if c["share"] is None and c["leave"] else _band(c["share"])
        worked = r["client"] + r["internal"]
        r["client_pct"] = r["client"] / worked * 100.0 if worked > 0.05 else 0.0
    qs_share = cap.quickstart_share_data(cctx)
    qs_rows = _keep(qs_share["rows"], names)
    for r in qs_rows:
        w = r["worked"] or 1.0
        r["plan_pct"], r["quick_pct"] = r["plan"] / w * 100.0, r["quick"] / w * 100.0
        r["internal_pct"] = r["internal"] / w * 100.0
    if len(qs_rows) == len(qs_share["rows"]):
        qs_team = qs_share["team"]
    else:  # a developer filter: the KPI follows it
        worked = sum(r["worked"] for r in qs_rows)
        quick = sum(r["quick"] for r in qs_rows)
        qs_team = {"worked": worked, "quick": quick,
                   "share": quick / worked * 100.0 if worked > 0.05 else None}
    adherence = _keep(cap.adherence_data(cctx), names)
    for a in adherence:
        top = max([max(p["planned"], p["logged"]) for p in a["planned_rows"]] + [1.0])
        for p in a["planned_rows"]:
            p["planned_w"] = p["planned"] / top * 100.0
            p["logged_w"] = p["logged"] / top * 100.0
    forward_all = cap.forward_data(cctx, occ["occupancy"])
    forward = _keep(forward_all, names)
    for r in forward:
        r["band"] = _band(r["occupancy"])
        for c in r["cells"]:
            c["pct"] = (min(c["days"] / c["available"] * 100.0, 100.0)
                        if c["available"] > 0.05 else (100.0 if c["days"] else 0.0))
    runway = cap.runway_data(cctx)
    top_weeks = max([r["weeks"] for r in runway if r["weeks"]] + [1.0])
    for r in runway:
        r["bar"] = (r["weeks"] / top_weeks * 100.0) if r["weeks"] else 0.0
    notes = [{"warn": line.startswith("⚠️"), "html": md_to_html.inline(line)}
             for line in cap.notes_data(cctx)]

    avgs = [r["avg"] for r in occ_rows if r["avg"] is not None and not r["gap"]]
    past, future = cctx["past"], cctx["future"]
    return {
        "all_names": all_names, "single": len(names) == 1,
        "room_below": cap.ROOM_BELOW, "full_above": cap.FULL_ABOVE,
        "gap_pct": cfg["gap_ratio"] * 100, "unplanned_rows": cap.UNPLANNED_ROWS,
        "dev_roles": cfg["dev_role_names"],
        "sem": {"label": sem["label"], "rows": sem_rows, "totals": totals},
        "engagement": engagement,
        "engagement_span": (f"{engagement[0]['cells'][0]['label']} → "
                            f"{engagement[0]['cells'][-1]['label']}"
                            if engagement and engagement[0]["cells"] else ""),
        "occ": occ_rows,
        "past_labels": [{"label": cap.wlabel(w), "current": w == cctx["weeks"]["current"]}
                        for w in past],
        "past_span": f"{cap.wlabel(past[0])} → {cap.wlabel(past[-1])}",
        "future_labels": [cap.wlabel(w) for w in future],
        "future_span": (f"{cap.wlabel(future[0])} → {cap.wlabel(future[-1])}"
                        if future else ""),
        "adherence": adherence, "forward": forward, "runway": runway,
        "qs_share": qs_rows,
        "notes": notes,
        "who": _who(sem["rows"], engagement_all, occ["rows"], forward_all),
        "kpi": {
            "free": totals["free"], "queue": totals["queue"],
            "allocable": totals["allocable"],
            "occupation": sum(avgs) / len(avgs) if avgs else None,
            "qs_share": qs_team["share"], "qs_hours": qs_team["quick"],
            "qs_worked": qs_team["worked"],
            "unmeasured": sum(1 for r in occ_rows if r["gap"]),
            "no_dev": sum(1 for r in runway if r["state"] == "none"),
            "no_dev_hours": sum(r["left"] for r in runway if r["state"] == "none"),
        },
        "n_lines": cctx["n_lines"], "n_slots": cctx["n_slots"],
        "n_tasks": cctx["n_tasks"],
    }
