#!/usr/bin/env python3
"""Tab 4 of the web app — "Délais" as data for a template.

How long the Spec/Estim and To Do stages hold a ticket: the overall figures,
the weekly cohorts, the per-person breakdown, the slowest tickets, and what is
waiting right now. Everything comes from `dev_stats_report` — its `gather()`
context and its `stats`, `tendance_data`, `by_person` and `waiting` — the same
functions its Markdown prints from, so the two pages cannot disagree.

The per-person breakdown is shown as on the static tab: a tab one opens on
purpose reads as a diagnosis (where tickets wait), never as a ranking.

Read-only, and no Odoo call of its own.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_stats_report as st  # noqa: E402
import dev_tasks_report as tasks_report  # noqa: E402
import dev_travail_view as travail  # noqa: E402  (STATES, _project)
import dev_workload_report as workload  # noqa: E402

STAGES = (("spec", st.SPEC_STAGE), ("todo", st.TODO_STAGE))


def _task_url(cfg, task_id):
    return f"{cfg['url']}/odoo/project.task/{task_id}"


def _ticket(ctx, task, value):
    cfg, scope = ctx["cfg"], ctx["scope"]
    state_label, state_cls = travail.STATES.get(task["state"], (task["state"] or "—", ""))
    return {"id": task["id"], "url": _task_url(cfg, task["id"]), "name": task["name"],
            "project": travail._project(scope, task["project_id"]),
            "stage": workload.stage_name(task), "parked": workload.is_parked(task),
            "state": state_label, "state_cls": state_cls, "value": value,
            "dev_time": task["dev_time"]}


def _people(rows):
    """Per person, the report's own stats — sorted by median, slowest first."""
    out = [{"name": name, **st.stats(values)} for name, values in rows]
    out.sort(key=lambda r: -(r.get("med") or 0))
    top = max([r.get("p80") or 0 for r in out] + [1.0])
    for r in out:
        r["med_w"] = (r.get("med") or 0) / top * 100.0
        r["p80_w"] = (r.get("p80") or 0) / top * 100.0
    return out


def _chart(weeks, per_week, medians, scale, width=760, height=230):
    """Grouped bars, one pair per creation week, on the single scale the report
    uses for both stages (two scales would make 6 h look like 6 days)."""
    if not weeks or not scale:
        return None
    pad_l, pad_r, pad_t, pad_b = 46, 10, 10, 26
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    step = plot_w / len(weeks)
    bar_w = max(5.0, min(18.0, step * 0.32))
    groups = []
    for i, week in enumerate(weeks):
        cx = pad_l + step * (i + 0.5)
        bars = []
        for j, (key, label) in enumerate(STAGES):
            med = medians.get((week, key))
            h = (med / scale * plot_h) if med else 0.0
            bars.append({"key": key, "x": cx - bar_w - 1 + j * (bar_w + 2), "w": bar_w,
                         "y": pad_t + plot_h - h, "h": h})
        groups.append({"cx": cx, "label": st.cap.wlabel(week), "span": st.wspan(week),
                       "bars": bars,
                       "spec_n": len(per_week[week]["spec"]),
                       "todo_n": len(per_week[week]["todo"]),
                       "spec_med": medians.get((week, "spec")),
                       "todo_med": medians.get((week, "todo"))})
    ticks = [{"y": pad_t + plot_h * (1 - f), "v": scale * f} for f in (0, .25, .5, .75, 1)]
    return {"w": width, "h": height, "pad_l": pad_l, "pad_t": pad_t, "plot_w": plot_w,
            "plot_h": plot_h, "base": pad_t + plot_h, "step": step, "groups": groups,
            "ticks": ticks}


def build(ctx, cfg):
    spec_held, spec_free, todo_held, todo_free = st.waiting(ctx)
    by_analyst, by_dev, outside = st.by_person(ctx)
    weeks, per_week, medians, scale = st.tendance_data(ctx["spec_closed"],
                                                        ctx["todo_closed"])
    resume = []
    for key, label in STAGES:
        closed = ctx[f"{key}_closed"]
        values = [v for _t, v in closed]
        resume.append({"key": key, "label": label, **st.stats(values),
                       "trivial": sum(1 for v in values if v < st.TRIVIAL_SECONDS)})

    def lents(closed):
        return [_ticket(ctx, t, v)
                for t, v in sorted(closed, key=lambda r: -r[1])[:st.SLOW_ROWS]]

    def attente(held, free, threshold, analyst_first):
        names = ctx["names"]
        rows = []
        for task, value in sorted(held, key=lambda r: -r[1]):
            row = _ticket(ctx, task, value)
            analyst = task["dev_analyst_id"][1] if task["dev_analyst_id"] else ""
            assignees = ", ".join(names[u] for u in task["user_ids"] if u in names)
            order = ([(analyst, "analyste"), (assignees, "")] if analyst_first
                     else [(assignees, ""), (analyst, "analyste")])
            row["who"], row["who_role"] = next(((n, r) for n, r in order if n), ("—", ""))
            row["stale"] = value > threshold * 86400
            rows.append(row)
        top = max([r["value"] for r in rows] + [1.0])
        for r in rows:
            r["bar"] = r["value"] / top * 100.0
        return {"rows": rows, "threshold": threshold,
                "stale": sum(1 for r in rows if r["stale"]),
                "unowned": [t["id"] for t, _v in sorted(free, key=lambda r: -r[1])]}

    return {
        "window_days": ctx["window_days"], "since": f"{ctx['since']:%d/%m/%Y}",
        "today": f"{ctx['today']:%d/%m/%Y}", "stored": ctx["stored"],
        "n_tasks": len(ctx["tasks"]), "resume": resume,
        "trend": {"chart": _chart(weeks, per_week, medians, scale), "scale": scale},
        "analysts": _people(by_analyst), "devs": _people(by_dev), "outside": outside,
        "slow_spec": lents(ctx["spec_closed"]), "slow_todo": lents(ctx["todo_closed"]),
        "wait_spec": attente(spec_held, spec_free, ctx["spec_threshold"], True),
        "wait_todo": attente(todo_held, todo_free, ctx["todo_threshold"], False),
        "counts": {k: (len(ctx[f"{k}_closed"]), len(ctx[f"{k}_running"]))
                   for k, _l in STAGES},
        "stale_days": tasks_report.STALE_DAYS,
    }
