#!/usr/bin/env python3
"""Report DEV TSC developer tasks (TODO or in-progress) from the production Odoo.

Usage:
    python3 dev_tasks_report.py todo
    python3 dev_tasks_report.py in_progress

Definitions (developer workflow):
  - dev_analyst_id : the developer doing the technical analysis
  - user_ids       : the developer(s) doing the development

TODO:
  - stage "Spec validation / Estim" WITHOUT a dev_analyst_id
  - stage "To Do" with NONE of the developers in user_ids

IN PROGRESS:
  - stage "Spec validation / Estim" WITH a dev_analyst_id
  - stage "To Do" with at least one developer in user_ids
  - stage "In Progress" with at least one developer in user_ids

Connection and team scope come from digest_config (env, then
~/.config/tsc-dev-digest/config.json, then built-in defaults).
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_plan_scope  # noqa: E402
import digest_config  # noqa: E402

# Base names: production labels carry emoji ("Spec validation / Estim ⚖️",
# "In Progress ⚙️") which digest_config strips, both when resolving ids and when
# reading a task's stage back — so retitling a kanban column cannot break this.
STAGE_SPEC = "Spec validation / Estim"
STAGE_TODO = "To Do"
STAGE_PROGRESS = "In Progress"
STAGE_ENCOURS = "En cours"

# Workflow order for grouping rows by stage; unknown stages sort last.
STAGE_ORDER = {STAGE_SPEC: 0, STAGE_TODO: 1, STAGE_PROGRESS: 2, STAGE_ENCOURS: 3}


def resolve_ids(kw, cfg):
    tag = kw("project.tags", "search", [[["name", "=", cfg["tag"]]]])
    if not tag:
        sys.exit(f"Tag {cfg['tag']!r} not found.")
    devs = kw("res.users", "search_read", [[["name", "in", cfg["dev_names"]]]],
              {"fields": ["id"]})
    return tag[0], [d["id"] for d in devs]


def build_domain(kw, mode, tag_id, dev_ids, cfg):
    base = ["&", ("tag_ids", "in", [tag_id])]
    ids = digest_config.stage_ids(kw, [STAGE_SPEC, STAGE_TODO, STAGE_PROGRESS])
    spec = sorted(ids[STAGE_SPEC])
    todo = sorted(ids[STAGE_TODO])
    progress = sorted(ids[STAGE_PROGRESS])
    if not spec or not todo:
        sys.exit(f"Étapes introuvables : {STAGE_SPEC!r} / {STAGE_TODO!r}.")
    if mode == "todo":
        return base + [
            "|",
            "&", ("stage_id", "in", spec), ("dev_analyst_id", "=", False),
            "&", ("stage_id", "in", todo), ("user_ids", "not in", dev_ids),
        ]
    if mode == "in_progress":
        return base + [
            "|", "|", "|",
            "&", ("stage_id", "in", spec), ("dev_analyst_id", "!=", False),
            "&", ("stage_id", "in", todo), ("user_ids", "in", dev_ids),
            "&", ("stage_id", "in", progress), ("user_ids", "in", dev_ids),
            "&", ("stage_id", "in", cfg["stage_encours_ids"]), ("user_ids", "in", dev_ids),
        ]
    sys.exit("mode must be 'todo' or 'in_progress'")


def hd(hours):
    """Format hours, e.g. '24.0h'."""
    return f"{hours:.1f}h"


def fmt_time(estim, spent, lang="en"):
    """spent/estim (percentage) when both exist; otherwise whichever is set.
    Flags over-budget tasks (spent > estimate) with a warning emoji."""
    e, s = round(estim, 1), round(spent, 1)
    if e and s:
        pct = s / e * 100
        warn = " ⚠️" if pct > 100 else ""
        return f"{hd(s)} / {hd(e)} ({pct:.0f}%){warn}"
    if s:
        return f"{hd(s)} {TEXT[lang]['spent']}"
    if e:
        return f"{hd(e)} {TEXT[lang]['est']}"
    return "—"


def cell(text):
    return str(text).replace("|", "\\|")


# project.task.state — the task status badge, independent of stage_id.
# '01_in_progress' is the default, so it's rendered quietly.
# The digest reads in English, the dashboard in French, off the same table.
TEXT = {
    "en": {
        "todo": "TODO", "in_progress": "IN PROGRESS",
        "count": ("%d task", "%d tasks"), "none": "_(none)_",
        "header": ["ID", "Task", "Project", "Status", "Dev analyst",
                   "Developer(s)", "Time (spent/est)", "Last updated"],
        "spent": "spent", "est": "est.", "day": "d",
        "states": {
            "01_in_progress": "In Progress",
            "02_changes_requested": "🟠 Changes req.",
            "03_approved": "🟢 Approved",
            "04_waiting_normal": "⏸️ Waiting",
            "1_done": "✅ Done",
            "1_canceled": "🚫 Cancelled",
        },
    },
    "fr": {
        "todo": "À PRENDRE", "in_progress": "EN COURS",
        "count": ("%d ticket", "%d tickets"), "none": "_(aucun)_",
        "header": ["ID", "Ticket", "Projet", "Statut", "Analyste dev",
                   "Développeur(s)", "Temps (pointé/estimé)", "Dernière MàJ"],
        "spent": "pointées", "est": "estimées", "day": "j",
        "states": {
            "01_in_progress": "En cours",
            "02_changes_requested": "🟠 Modifs demandées",
            "03_approved": "🟢 Approuvé",
            "04_waiting_normal": "⏸️ En attente",
            "1_done": "✅ Terminé",
            "1_canceled": "🚫 Annulé",
        },
    },
}


def fmt_state(state, lang="en"):
    return TEXT[lang]["states"].get(state, state or "—")


# Max days a task may sit untouched in a stage before it's flagged stale.
STALE_DAYS = {STAGE_SPEC: 1, STAGE_TODO: 3, STAGE_PROGRESS: 7, STAGE_ENCOURS: 7}


def age_days(write_date):
    # Odoo write_date is UTC ("YYYY-MM-DD HH:MM:SS").
    updated = datetime.strptime(write_date, "%Y-%m-%d %H:%M:%S")
    return (datetime.utcnow() - updated).total_seconds() / 86400


def overdue_days(write_date, stage):
    """Days past the stage's staleness limit; negative if still fresh."""
    limit = STALE_DAYS.get(stage)
    if limit is None:
        return float("-inf")
    return age_days(write_date) - limit


def fmt_updated(write_date, stage, lang="en"):
    """Append ⚠️ when the task hasn't been touched within the stage's limit."""
    if overdue_days(write_date, stage) > 0:
        return (f"⚠️ {write_date} "
                f"({age_days(write_date):.0f}{TEXT[lang]['day']})")
    return write_date


def stage_name(t):
    return (digest_config.base_stage_name(t["stage_id"][1])
            if t["stage_id"] else "-")


def fetch(kw, cfg, mode="todo"):
    """(tasks, user_names, dev_ids) for `mode` — grouped by stage in workflow
    order, oldest-updated first within a stage. Lifted out of main() so the
    web app lays out the same tickets natively."""
    tag_id, dev_ids = resolve_ids(kw, cfg)
    dev_id_set = set(dev_ids)
    domain = build_domain(kw, mode, tag_id, dev_ids, cfg)
    tasks = kw("project.task", "search_read", [domain], {
        "fields": ["id", "name", "project_id", "stage_id", "state", "dev_analyst_id",
                   "user_ids", "dev_time", "write_date", "effective_hours"],
    })
    tasks.sort(key=lambda t: (STAGE_ORDER.get(stage_name(t), 99),
                              -age_days(t["write_date"])))
    user_names = {}
    all_uids = {u for t in tasks for u in t["user_ids"] if u in dev_id_set}
    if all_uids:
        for u in kw("res.users", "read", [list(all_uids)], {"fields": ["name"]}):
            user_names[u["id"]] = u["name"]
    return tasks, user_names, dev_id_set


def main(argv=None, lang="en", scope=None, kw=None, cfg=None):
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else "todo"
    T = TEXT[lang]
    cfg = cfg or digest_config.load()
    # One page assembling several reports authenticates once and passes `kw` in.
    kw = kw or digest_config.connect(cfg)
    if scope is None:
        # 📌 planned / ⚡ quickstart, straight from the capacity plan.
        scope, _slots = dev_plan_scope.build(kw, cfg)
    tasks, user_names, dev_id_set = fetch(kw, cfg, mode)

    title = T["todo"] if mode == "todo" else T["in_progress"]
    # h2: the digest writes the "# DEV TSC — Digest" h1, sections sit under it.
    count = T["count"][0] if len(tasks) == 1 else T["count"][1]
    print(f"\n## DEV TSC — {title} ({count % len(tasks)})\n")
    if not tasks:
        print(T["none"])
        return

    header = T["header"]

    def print_row(t):
        stage = stage_name(t)
        analyst = t["dev_analyst_id"][1] if t["dev_analyst_id"] else "-"
        devs = ", ".join(user_names[u] for u in t["user_ids"] if u in dev_id_set) or "-"
        row = [
            digest_config.task_link(cfg, t["id"]),
            cell(t["name"]),
            cell(scope.label(t["project_id"])),
            fmt_state(t["state"], lang),
            cell(analyst),
            cell(devs),
            fmt_time(t["dev_time"], t["effective_hours"], lang),
            fmt_updated(t["write_date"], stage, lang),
        ]
        # Nothing is dimmed: this table only ever lists live stages (Spec/Estim,
        # To Do, In Progress, En cours).
        print("| " + " | ".join(str(c) for c in row) + " |")

    # One table per stage, in workflow order (tasks are already sorted by stage).
    for stage in dict.fromkeys(stage_name(t) for t in tasks):
        group = [t for t in tasks if stage_name(t) == stage]
        print(f"\n### {stage} ({len(group)})\n")
        print("| " + " | ".join(header) + " |")
        print("|" + "|".join(["---"] * len(header)) + "|")
        for t in group:
            print_row(t)


if __name__ == "__main__":
    main()
