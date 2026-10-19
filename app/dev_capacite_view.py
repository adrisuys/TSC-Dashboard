#!/usr/bin/env python3
"""Tab 2 of the web app — "Disponibilité" as data for a template.

Two questions:

  1. who can take a ticket this week — this week against the quickstart
     queue (`dev_quickstart_report.semaine_data`);
  2. who has room in the weeks ahead — what the plan already commits
     (`dev_capacity_report.forward_data`, `dev_quickstart_report.engagement_data`).

The overview also reads the runway per project (projects with dev work left
and no dev planned) and the data-quality notes from here.

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

    # --- 2. the weeks ahead ---------------------------------------------------
    # Occupation rides along on the forward grid: free plan days read
    # differently for someone already above 100 %.
    occ = cap.occupation_data(cctx)
    forward_all = cap.forward_data(cctx, occ["occupancy"])
    forward = _keep(forward_all, names)
    for r in forward:
        r["band"] = _band(r["occupancy"])
        for c in r["cells"]:
            c["pct"] = (min(c["days"] / c["available"] * 100.0, 100.0)
                        if c["available"] > 0.05 else (100.0 if c["days"] else 0.0))
    runway = cap.runway_data(cctx)
    notes = [{"warn": line.startswith("⚠️"), "html": md_to_html.inline(line)}
             for line in cap.notes_data(cctx)]

    future = cctx["future"]
    return {
        "all_names": all_names, "single": len(names) == 1,
        "room_below": cap.ROOM_BELOW, "full_above": cap.FULL_ABOVE,
        "gap_pct": cfg["gap_ratio"] * 100,
        "sem": {"label": sem["label"], "rows": sem_rows, "totals": totals},
        "engagement": engagement,
        "engagement_span": (f"{engagement[0]['cells'][0]['label']} → "
                            f"{engagement[0]['cells'][-1]['label']}"
                            if engagement and engagement[0]["cells"] else ""),
        "future_labels": [cap.wlabel(w) for w in future],
        "future_span": (f"{cap.wlabel(future[0])} → {cap.wlabel(future[-1])}"
                        if future else ""),
        "forward": forward, "runway": runway, "notes": notes,
        "n_lines": cctx["n_lines"], "n_slots": cctx["n_slots"],
        "n_tasks": cctx["n_tasks"],
    }
