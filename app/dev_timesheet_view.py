#!/usr/bin/env python3
"""Tab 3 of the web app — the timesheet report as data for a template.

`dev_timesheet_report` answers the same questions as Markdown for the static
page and the terminal; this module turns the very same `gather()` context into
plain dicts and lists that `webapp/templates/temps.html` lays out natively —
filters, KPI cards, a day grid of cells, stacked bars, an SVG trend. Every
figure goes through the report's own functions (`classify`, `fill_state`,
`year_days`, `ratio`, `movement`, `projection_numbers`), so the two pages can
never print two different answers to one question.

`build(ctx, cfg, devs=None)` — `devs` limits the page to some developers. The
team figures are then recomputed over that selection (still excluding an
untrusted timesheet), so "l'équipe" reads as "the people shown".

Read-only, and no Odoo call of its own: everything comes from `ctx`.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_capacity_report as cap  # noqa: E402
import dev_timesheet_report as r  # noqa: E402

TREND_LIGHT_HOURS = 40.0     # same threshold as the report's "light period"
NB_DEV_ROWS = r.NB_DEV_ROWS


def _badge(value):
    """(css class, label) for a billable percentage against the target."""
    if value is None:
        return "none", "—"
    if value >= r.TARGET:
        return "ok", "à l'objectif"
    if value >= r.AMBER_BELOW:
        return "warn", "proche"
    return "err", "sous l'objectif"


def _fill_status(missing_recent):
    if missing_recent >= 3:
        return "err", "en retard"
    return ("warn", "quelques trous") if missing_recent else ("ok", "à jour")


def _day_kind(ctx, cfg, name, day):
    """The grid cell for one developer-day — the rule of `fill_state`."""
    gap = cfg.get("gap_ratio", 0.5)
    expected = ctx["schedules"][name]["hours"].get(day.weekday(), 0.0)
    if expected <= 0.05:
        return "off"
    if ctx["leave_day"].get((name, day), 0.0) >= gap * expected:
        return "leave"
    if ctx["per_day"].get((name, day), 0.0) >= gap * expected:
        return "full"
    return "empty"


def _weeks(days):
    out = []
    for day in days:
        if not out or day.weekday() <= out[-1][-1].weekday():
            out.append([])
        out[-1].append(day)
    return out


def tenue(ctx, cfg, names, states):
    days = ctx["grid_dates"]
    weeks = _weeks(days)
    rows = []
    for name in names:
        state, sched = states[name], ctx["schedules"][name]
        ystate = r.fill_state(ctx, name, cfg, days=r.year_days(ctx, name))
        cls, label = _fill_status(state["missing_recent"])
        rows.append({
            "name": name,
            "regime": (f"{len(sched['hours'])} j · {cap.gross(sched):.0f} h"
                       if sched["hours"] else "régime inconnu"),
            "weeks": [[{"date": r.date_fr(d), "kind": _day_kind(ctx, cfg, name, d)}
                       for d in week] for week in weeks],
            "holes": len(state["missing"]),
            "coverage": state["coverage"], "worked": state["worked"],
            "expected": state["expected"],
            "y_holes": len(ystate["missing"]), "y_coverage": ystate["coverage"],
            "y_worked": ystate["worked"], "y_expected": ystate["expected"],
            "last": f"{state['last_filled']:%d/%m}" if state["last_filled"] else "—",
            "late": state["late"],
            "lag": state["lag"],
            "status": cls, "status_label": label,
            "missing": [r.date_fr(d) for d in state["missing"]],
            "trusted": state["trusted"],
        })
    trusted = [row for row in rows if row["trusted"]]
    tw = sum(row["y_worked"] for row in trusted)
    te = sum(row["y_expected"] for row in trusted)
    return {"rows": rows,
            "week_labels": [cap.wlabel(cap.week_of(w[0])) for w in weeks],
            "span": (f"{r.date_fr(days[0])} → {r.date_fr(days[-1])}" if days else "—"),
            "team_coverage": r.ratio(tw, te), "team_worked": tw, "team_expected": te}


def facturable(ctx, names, states):
    rows, totals, rtotals, watch = [], defaultdict(float), defaultdict(float), []
    for name in names:
        bil = ctx["kinds"].get((name, "billable"), 0.0)
        inv = ctx["kinds"].get((name, "invest"), 0.0)
        internal = ctx["kinds"].get((name, "internal"), 0.0)
        leave = ctx["kinds"].get((name, "leave"), 0.0)
        worked = bil + inv + internal
        strict, ex_inv = r.ratio(bil, worked), r.ratio(bil, bil + internal)
        rbil = ctx["recent"].get((name, "billable"), 0.0)
        rworked = rbil + ctx["recent"].get((name, "invest"), 0.0) \
            + ctx["recent"].get((name, "internal"), 0.0)
        rstrict = r.ratio(rbil, rworked) if rworked >= r.RECENT_MIN_HOURS else None
        arrow, delta = r.movement(strict, rstrict)
        trusted = states[name]["trusted"]
        if trusted:
            for key, value in (("billable", bil), ("invest", inv),
                               ("internal", internal), ("leave", leave)):
                totals[key] += value
            rtotals["billable"] += rbil
            rtotals["worked"] += rworked
            if rstrict is not None and rstrict < r.TARGET and (delta or 0) <= -r.DELTA_FLOOR:
                watch.append({"name": name, "recent": rstrict, "delta": delta})
        shown = rstrict if ctx["has_recent"] else strict
        cls, label = _badge(shown)
        rows.append({"name": name, "billable": bil, "internal": internal,
                     "invest": inv, "worked": worked, "leave": leave,
                     "strict": strict, "ex_inv": ex_inv, "recent": rstrict,
                     "arrow": arrow, "delta": delta, "trusted": trusted,
                     "shown": shown, "badge": cls, "badge_label": label,
                     "split": _split(bil, internal, inv)})
    tb, ti, tn = totals["billable"], totals["invest"], totals["internal"]
    tworked = tb + ti + tn
    t_strict, t_ex = r.ratio(tb, tworked), r.ratio(tb, tb + tn)
    r_strict = (r.ratio(rtotals["billable"], rtotals["worked"])
                if rtotals["worked"] >= r.RECENT_MIN_HOURS else None)
    t_arrow, t_delta = r.movement(t_strict, r_strict)
    mis = sum(h for (n, _p), h in ctx["misfiled"].items()
              if n in names and states[n]["trusted"])
    fixed = (r.ratio(tb, tworked - mis)
             if tworked > 0.05 and mis / tworked >= r.MISFILED_FLOOR else None)
    shown = r_strict if ctx["has_recent"] else t_strict
    cls, label = _badge(shown)
    team = {"billable": tb, "internal": tn, "invest": ti, "worked": tworked,
            "leave": totals["leave"], "strict": t_strict, "ex_inv": t_ex,
            "recent": r_strict, "arrow": t_arrow, "delta": t_delta,
            "shown": shown, "badge": cls, "badge_label": label,
            "split": _split(tb, tn, ti), "misfiled": mis, "fixed": fixed,
            "invest_cost": (t_ex - t_strict) if (t_ex is not None and t_strict is not None
                                                 and ti > 0.05) else None}
    watch.sort(key=lambda w: w["delta"])
    return {"rows": rows, "team": team, "watch": watch,
            "excluded": [row["name"] for row in rows if not row["trusted"]]}


def _split(bil, internal, inv):
    worked = bil + internal + inv
    if worked <= 0.05:
        return []
    return [{"kind": k, "label": lab, "hours": h, "pct": h / worked * 100.0}
            for k, lab, h in (("billable", "Facturable", bil),
                              ("internal", "Interne", internal),
                              ("invest", "Invest", inv)) if h > 0.05]


def tendance(ctx, names, states):
    counted = {n for n in names if states[n]["trusted"]}
    monthly = ctx["monthly"]
    keys = sorted({b for (b, _k, n) in ctx["buckets"] if n in counted})
    if not monthly:
        keys = keys[-r.TREND_MAX_WEEKS:]
    points, run_bil, run_worked = [], 0.0, 0.0
    for i, key in enumerate(keys):
        bil = sum(ctx["buckets"].get((key, "billable", n), 0.0) for n in counted)
        worked = bil + sum(ctx["buckets"].get((key, k, n), 0.0)
                           for k in ("invest", "internal") for n in counted)
        run_bil += bil
        run_worked += worked
        points.append({"label": r.bucket_label(key, monthly), "billable": bil,
                       "worked": worked, "period": r.ratio(bil, worked),
                       "cumul": r.ratio(run_bil, run_worked),
                       "light": worked < TREND_LIGHT_HOURS,
                       "partial": i == len(keys) - 1})
    proj = (r.projection_numbers(ctx, run_bil, run_worked)
            if run_worked > 0.05 else None)
    return {"points": points, "unit": "mois" if monthly else "semaines",
            "projection": proj, "chart": _chart(points)}


def _chart(points, width=760, height=240):
    """Geometry for the trend SVG, computed here so the template only draws.
    One y-axis, 0–100 %: the bars (period ratio) and the line (cumulative)
    share the unit, so there is no second scale to invent."""
    if len(points) < 2:
        return None
    pad_l, pad_r, pad_t, pad_b = 36, 12, 12, 26
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    step = plot_w / len(points)
    bar_w = max(6.0, min(36.0, step * 0.56))

    def y(pct):
        return pad_t + plot_h * (1 - max(0.0, min(100.0, pct)) / 100.0)

    bars, line = [], []
    for i, p in enumerate(points):
        cx = pad_l + step * (i + 0.5)
        if p["period"] is not None:
            top = y(p["period"])
            bars.append({"x": cx - bar_w / 2, "y": top, "w": bar_w,
                         "h": pad_t + plot_h - top, "cx": cx, "p": p})
        else:
            bars.append({"x": cx - bar_w / 2, "y": pad_t + plot_h, "w": bar_w,
                         "h": 0, "cx": cx, "p": p})
        if p["cumul"] is not None:
            line.append((cx, y(p["cumul"])))
    return {"w": width, "h": height, "pad_l": pad_l, "pad_t": pad_t,
            "plot_w": plot_w, "plot_h": plot_h, "base": pad_t + plot_h,
            "step": step, "bars": bars,
            "line": " ".join(f"{x:.1f},{yy:.1f}" for x, yy in line),
            "dots": [{"x": x, "y": yy} for x, yy in line],
            "target_y": y(r.TARGET),
            "grid": [{"y": y(v), "v": v} for v in (0, 25, 50, 75, 100)]}


def non_facturable(ctx, names, lines_url):
    """Per project for the selection, the client projects that leak, and the
    per-developer project × task detail with its drill-down links."""
    by_project = defaultdict(float)
    for (n, project, _task, kind), h in ctx["by_dev_task"].items():
        if n in names:
            by_project[(project, kind)] += h
    total = sum(by_project.values())
    projects = [{"project": p.strip(), "kind": k, "hours": h,
                 "share": h / total * 100.0 if total else 0.0}
                for (p, k), h in sorted(by_project.items(), key=lambda kv: -kv[1])]
    leaks = []
    for project, partner in ctx["leaking"].items():
        hours = sum(h for (p, _k), h in by_project.items() if p == project)
        if hours >= r.LEAK_FLOOR:
            leaks.append({"project": project.strip(), "partner": partner, "hours": hours})
    leaks.sort(key=lambda x: -x["hours"])

    dev_by_name = {d["name"]: d for d in ctx["devs"]}
    per_dev = []
    for name in names:
        rows = sorted(((k[1], k[2], k[3], h) for k, h in ctx["by_dev_task"].items()
                       if k[0] == name), key=lambda x: -x[3])
        if not rows:
            continue
        dtotal = sum(x[3] for x in rows)
        bil = ctx["kinds"].get((name, "billable"), 0.0)
        items = []
        for project, task, kind, hours in rows:
            pairs = sorted(ctx["nb_ids"].get((name, project, task), ()))
            tids = {t for _p, t in pairs}
            labels = sorted(((lab, h) for (n, p, t, lab), h in ctx["task_labels"].items()
                             if n == name and p == project and t == task),
                            key=lambda x: -x[1])
            items.append({
                "project": project.strip(), "task": task, "kind": kind,
                "hours": hours, "share": hours / dtotal * 100.0 if dtotal else 0.0,
                "recent": ctx["by_dev_task_recent"].get((name, project, task, kind), 0.0),
                "labels": [{"label": lab, "hours": h} for lab, h in labels[:r.NB_LABELS]],
                "rest_n": max(len(labels) - r.NB_LABELS, 0),
                "rest_h": sum(h for _l, h in labels[r.NB_LABELS:]),
                "url": lines_url(ctx, dev_by_name[name], kind, pairs=pairs),
                "task_url": (f"{ctx['odoo_url']}/odoo/project.task/{min(tids)}"
                             if len(tids) == 1 and min(tids) else None),
            })
        rest = rows[NB_DEV_ROWS:]
        per_dev.append({
            "name": name, "total": dtotal, "share": r.ratio(dtotal, dtotal + bil),
            "items": items[:NB_DEV_ROWS],
            "rest_n": len(rest), "rest_h": sum(x[3] for x in rest),
            "rest_share": (sum(x[3] for x in rest) / dtotal * 100.0) if dtotal else 0.0,
            # Exactly the lines of the folded rows, not every line of the dev.
            "rest_url": (lines_url(ctx, dev_by_name[name], pairs=sorted(set().union(*(
                ctx["nb_ids"].get((name, p, t), set()) for p, t, _k, _h in rest))))
                if rest else None),
            "all_url": lines_url(ctx, dev_by_name[name])})
    return {"projects": projects, "total": total, "leaks": leaks, "per_dev": per_dev}


def conges(ctx, names, states):
    by_name = {d["name"]: d for d in ctx["devs"]}
    rows = [{"name": n, "project": p, "hours": h, "trusted": states[n]["trusted"],
             "url": r.lines_url(ctx, by_name[n], kind="internal", leave=True,
                                project=ctx["misfiled_ids"].get((n, p), {0}))}
            for (n, p), h in sorted(ctx["misfiled"].items(), key=lambda kv: -kv[1])
            if n in names]
    who = sorted({row["name"] for row in rows})
    all_url = (r.lines_url(ctx, [by_name[n] for n in who], kind="internal",
                           leave=True,
                           name=who[0] if len(who) == 1 else "Équipe")
               if rows else None)
    return {"rows": rows, "total": sum(x["hours"] for x in rows), "all_url": all_url}


def notes(ctx, names):
    out = []
    for dev in ctx["devs"]:
        if dev["name"] not in names:
            continue
        if len(dev["employees"]) > 1:
            detail = ", ".join(
                f"{e['name']} ({e['company_id'][1] if e['company_id'] else '—'}, "
                f"{ctx['hours_by_emp'].get(e['id'], 0.0):.1f} h)" for e in dev["employees"])
            out.append(f"{dev['name']} a {len(dev['employees'])} fiches employé — "
                       f"{detail}. Leurs heures sont additionnées ; le régime est lu "
                       f"sur {ctx['employees'][dev['name']]['name']}, la plus pointée.")
        join = ctx["joins"].get(dev["name"])
        if join and join > ctx["window_from"]:
            out.append(f"{dev['name']} : fenêtre recalée au {join:%d/%m/%Y}, sa "
                       f"première ligne de pointage — rien n'est compté comme trou avant.")
        if not ctx["schedules"][dev["name"]]["hours"]:
            out.append(f"{dev['name']} n'a pas de calendrier de travail : la grille "
                       f"suppose lundi–vendredi, {cap.HOURS_PER_DAY:.0f} h par jour.")
    for name in ctx["missing"]:
        out.append(f"{name} : aucune fiche employé rattachée — absent de la page.")
    for raw, hours in sorted(ctx["unknown"].items(), key=lambda kv: -kv[1]):
        out.append(f"Type de facturation « {raw} » inconnu ({hours:.1f} h) — "
                   f"compté comme interne.")
    return out


def build(ctx, cfg, devs=None):
    all_names = [d["name"] for d in ctx["devs"]]
    names = [n for n in all_names if not devs or n in devs] or all_names
    states = {n: r.fill_state(ctx, n, cfg) for n in all_names}
    rd = ctx["recent_days"] if ctx["has_recent"] else None
    fact = facturable(ctx, names, states)
    ten = tenue(ctx, cfg, names, states)
    trend = tendance(ctx, names, states)
    return {
        "names": names, "all_names": all_names, "single": len(names) == 1,
        "target": r.TARGET, "amber": r.AMBER_BELOW,
        "window_label": ctx["window_label"], "window_de": ctx["window_de"],
        "window_from": f"{ctx['window_from']:%d/%m/%Y}",
        "today": f"{ctx['today']:%d/%m/%Y}", "is_year": ctx["is_year"],
        "recent_days": rd, "n_lines": ctx["n_lines"],
        "gap_pct": cfg.get("gap_ratio", 0.5) * 100,
        "grid_weeks": r.GRID_WEEKS, "recent_check_days": r.RECENT_DAYS,
        "recent_min_hours": r.RECENT_MIN_HOURS, "delta_floor": r.DELTA_FLOOR,
        "tenue": ten, "facturable": fact, "tendance": trend,
        "non_facturable": non_facturable(ctx, names, r.lines_url),
        "conges": conges(ctx, names, states), "notes": notes(ctx, names),
    }

