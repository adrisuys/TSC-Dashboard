#!/usr/bin/env python3
"""Per-developer DEV TSC workload from the production Odoo (Markdown output).

Usage:
    python3 dev_workload_report.py            # all developers
    python3 dev_workload_report.py "Nathan"   # one developer

Scope — a task counts toward a developer's workload when it carries the digest
tag and either:

  - it sits in To Do / In Progress / En cours / Code Review and the developer is
    among user_ids — development they own; or
  - it sits in "Spec validation / Estim", the developer is its dev_analyst_id and
    its state is not yet Approved — analysis still on their plate.

An *Approved* spec ticket is excluded: the analysis is finished and the ticket is
waiting to be scheduled, so it is nobody's outstanding work and counting it
inflated the "left to burn" figure the leaderboard ranks on.

A developer's table additionally LISTS, marked ⏸️ and left out of every number,
the tickets assigned to them that are parked downstream (see PARKED_STAGES) —
waiting for a third party, a test, a client validation or a release. Those hours
are not theirs today, which is the point of keeping them out of the totals, but
each one can bounce back, so seeing them next to the live work is the difference
between "free" and "free until Thursday".

Per task it reports the developer's OWN timesheet hours, the hours logged by the
dev team, the total hours logged by anyone, the task's dev_time estimate, and the
hours left to burn.

"Left" is dev_time minus the hours logged *by developers* — never minus the task's
effective_hours. dev_time is a dev-side budget, while effective_hours also carries
the functional analyst, the project manager and support, so subtracting it burned
the dev budget with hours no developer ever spent. Odoo has a stored field for
this (remaining_dev_time) with exactly the same definition, but it goes stale — it
disagreed with the timesheets on 6 of 114 live tasks — so the figure is recomputed
here from the timesheet lines the report already fetches. Dev hours rather than
the developer's own hours because two developers on one ticket share one budget:
charging each of them the full remainder would count the same work twice.

Each developer gets a totals line; a leaderboard at the top ranks everyone by
remaining work so it's obvious who can take a new task.

Connection and team scope come from digest_config.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_plan_scope  # noqa: E402
import digest_config  # noqa: E402
import md_to_html  # noqa: E402  (anchor_id only — the folded sections' ids)

# --- scope ------------------------------------------------------------------
# Hours COUNT for these stages, carried by the assignee(s) …
DEV_STAGES = ["To Do", "In Progress", "En cours", "Code Review"]
# … and for a spec ticket carried by its dev analyst while it is not yet Approved.
SPEC_STAGE = "Spec validation / Estim"
# project.task.state values that mean the analyst is done with a spec ticket, so
# it stops counting against them. Approved = analysis agreed, awaiting scheduling.
SPEC_SETTLED_STATES = ["03_approved"]

# SHOWN in a developer's table but NOT counted: work parked downstream, waiting
# on someone else. The developer is free today — which is why these hours stay
# out of the totals — but the ticket can bounce back the moment the client
# replies or a test fails, so it belongs on their list.
# Two names here are aspirational rather than present in this database:
# "Waiting for Feedback" does not exist (its meaning is carried by
# "Attente de tiers"), and stage names are matched by name, so an absent name
# simply matches nothing and costs nothing.
# Every name below is a *base* name: production labels carry emoji
# ("In Progress ⚙️", "Attente de tiers ⏳") which digest_config.base_stage_name
# strips, both when resolving ids and when reading a task's stage back.
PARKED_STAGES = ["À faire", "Unit Testing", "To Review / Integration Testing",
                 "To validate by client", "Attente de tiers",
                 "Waiting for Feedback", "Ready for production"]
DISPLAY_STAGES = DEV_STAGES + PARKED_STAGES
# Stages whose rows drive the numbers. Spec rows are counted too, but they are
# fetched by analyst rather than by assignee, hence the separate query.
COUNTED_STAGES = set(DEV_STAGES) | {SPEC_STAGE}
# Workflow order for a developer's rows: analysis, then owned work, then parked.
STAGE_ORDER = {name: i for i, name
               in enumerate([SPEC_STAGE] + DEV_STAGES + PARKED_STAGES)}

TASK_FIELDS = ["id", "name", "project_id", "stage_id", "user_ids",
               "dev_analyst_id", "state", "dev_time", "effective_hours"]

HOURS_PER_DAY = 8.0
# Remaining-hours thresholds driving the availability badge.
FREE_BELOW = 8.0    # under a day left → can take work now
BUSY_ABOVE = 40.0   # over a week left → don't pile more on
# A full load bar = one working week of remaining work; longer loads are capped.
CAPACITY_HOURS = 40.0
BAR_WIDTH = 20


# --- wording -----------------------------------------------------------------
# The digest is read in English, the dashboard in French, and both render the
# same tables from the same figures — so the strings live here rather than being
# duplicated with the table code. Only what a reader sees is translated:
# docstrings, comments and error text stay in English.
TEXT = {
    "en": {
        "day": "d",
        "title": "DEV TSC — WORKLOAD per developer",
        "tasks_parked": "%d tasks · %d parked",
        "scope": ("_Scope of the hours: %(stages)s. A task shared by two developers "
                  "counts for each of them. \"Left\" = dev\\_time − hours logged "
                  "**by developers** (not the task's total hours, which also carry "
                  "functional, PM and support time that was never part of the dev "
                  "budget), so tasks with no estimate add 0 and are counted "
                  "separately under \"No estim.\"._"),
        "bar_note": ("_A full load bar = %(cap).0fh (one working week) of work "
                     "left; ▸ marks a load capped past that._"),
        "stages": ("%(dev)s as assignee, plus \"%(spec)s\" as dev analyst while the "
                   "ticket is not yet Approved"),
        "parked_note": ("_⏸️ rows are **parked**: assigned to the developer but "
                        "sitting in %(stages)s, waiting on someone else. They are "
                        "greyed and italicised, and their hours are **not** in the "
                        "totals — the developer is free today — but "
                        "each one can come back when the client replies or a test "
                        "fails, so factor them in before piling on new work._"),
        "marker_note": ("_Projects are marked from the capacity plan: 📌 planned "
                        "(allocated days), ⚡ quickstart (no allocation, anyone can "
                        "pick it up). The marker says who pays for the work, not how "
                        "urgent it is: planned and quickstart rows read the same, "
                        "and only parked rows are dimmed._"),
        "lb_header": ["Developer", "Tasks", "Estimated (dev_time)", "Worked (own)",
                      "Left to burn", "No estim.", "⏸️ Parked",
                      "Load (1 wk = full bar)"],
        "total": "TOTAL",
        "row_header": ["ID", "Task", "Project", "Stage", "Worked (own)",
                       "Worked (devs)", "Worked (task)", "Estimated", "Left"],
        "task": "task", "task_plural": "tasks",
        "dev_head": "%(n)d %(plural)s · %(left)s left of %(est)s · %(badge)s%(parked)s",
        "dev_head_plain": "%(n)d %(plural)s · %(left)s left of %(est)s%(parked)s",
        "parked_suffix": " · ⏸️ %d parked",
        "no_tasks": "_(no tasks)_",
        "available": "available", "loaded": "loaded", "full": "full",
        "unestimated": "unestimated", "unest_short": "unest.",
        "and": "or",
    },
    "fr": {
        "day": "j",
        "title": "DEV TSC — CHARGE par développeur",
        "tasks_parked": "%d tickets · %d parqués",
        "scope": ("_Périmètre des heures : %(stages)s. Un ticket partagé par deux "
                  "développeurs compte pour chacun. « Reste » = dev\\_time − heures "
                  "pointées **par les développeurs** (et non les heures totales du "
                  "ticket, qui portent aussi le fonctionnel, la gestion de projet "
                  "et le support, jamais compris dans le budget dev) : un ticket "
                  "sans estimation ajoute 0 et est compté à part sous « Sans "
                  "estim. »._"),
        "bar_note": ("_Une barre pleine = %(cap).0f h (une semaine de travail) "
                     "de reste ; ▸ signale une charge plafonnée au-delà._"),
        "stages": ("%(dev)s comme assigné, plus « %(spec)s » comme analyste dev "
                   "tant que le ticket n'est pas Approved"),
        "parked_note": ("_Les lignes ⏸️ sont **parquées** : assignées au "
                        "développeur mais posées en %(stages)s, en attente de "
                        "quelqu'un d'autre. Elles sont en gris et en italique, et "
                        "leurs heures ne sont **pas** dans les totaux — le "
                        "développeur est libre aujourd'hui — "
                        "mais chacune peut revenir dès que le client répond ou "
                        "qu'un test casse : à prendre en compte avant d'en "
                        "rajouter._"),
        "marker_note": ("_Les projets sont marqués depuis le plan de capacité : "
                        "📌 planifié (jours alloués), ⚡ quickstart (aucune "
                        "allocation, n'importe qui peut le prendre). Le marqueur dit "
                        "qui paie le travail, pas s'il est urgent : les lignes "
                        "planifiées et quickstart se lisent de la même façon, seules "
                        "les lignes parquées sont grisées._"),
        "lb_header": ["Développeur", "Tickets", "Estimé (dev_time)",
                      "Pointé (perso)", "Reste à brûler", "Sans estim.",
                      "⏸️ Parqués", "Charge (1 sem. = barre pleine)"],
        "total": "TOTAL",
        "row_header": ["ID", "Ticket", "Projet", "Étape", "Pointé (perso)",
                       "Pointé (devs)", "Pointé (ticket)", "Estimé", "Reste"],
        "task": "ticket", "task_plural": "tickets",
        "dev_head": ("%(n)d %(plural)s · %(left)s de reste sur %(est)s · "
                     "%(badge)s%(parked)s"),
        "dev_head_plain": ("%(n)d %(plural)s · %(left)s de reste sur "
                           "%(est)s%(parked)s"),
        "parked_suffix": " · ⏸️ %d parqués",
        "no_tasks": "_(aucun ticket)_",
        "available": "disponible", "loaded": "chargé", "full": "plein",
        "unestimated": "sans estim.", "unest_short": "sans estim.",
        "and": "ou",
    },
}


def hd(hours, lang="en"):
    """Format hours with their working-day equivalent, e.g. '24.0h (3.0d)'."""
    return f"{hours:.1f}h ({hours / HOURS_PER_DAY:.1f}{TEXT[lang]['day']})"


def badge(remaining, unestimated=0, lang="en"):
    """(emoji, label) availability badge. Tasks without a dev_time contribute
    nothing to `remaining`, so they must never read as free capacity."""
    T = TEXT[lang]
    if remaining < FREE_BELOW:
        if unestimated:
            return "❓", f"{unestimated} {T['unestimated']}"
        return "🟢", T["available"]
    if remaining <= BUSY_ABOVE:
        emoji, label = "🟡", T["loaded"]
    else:
        emoji, label = "🔴", T["full"]
    return emoji, f"{label} +{unestimated}?" if unestimated else label


def availability(remaining, unestimated=0, lang="en"):
    return "%s %s" % badge(remaining, unestimated, lang)


def unestimated_flag(unestimated, lang="en"):
    """`❓ 3 sans estim.` — the half of the availability badge worth keeping when
    the capacity claim is dropped.

    🟢/🟡/🔴 answer "does this person have room", which needs a capacity model
    this report does not have. "How many of their tickets carry no estimate"
    needs nothing but the tickets, and it is the reason the hours beside it
    cannot be taken at face value: a developer with 0h left and three
    unestimated tickets has not been measured, not freed."""
    return (f"❓ {unestimated} {TEXT[lang]['unestimated']}" if unestimated
            else "")


def load_bar(remaining, unestimated=0, lang="en"):
    """Emoji + a fixed-width block bar against one week of capacity, e.g.
    '🟡 `████░░░░░░░░░░░░░░░░` 20%'. Backticks force a monospace font so the
    bars line up in a rendered Markdown table, not just in the raw text."""
    pct = remaining / CAPACITY_HOURS * 100
    filled = min(int(round(pct / 100 * BAR_WIDTH)), BAR_WIDTH)
    bar = "█" * filled + "░" * (BAR_WIDTH - filled)
    emoji, _ = badge(remaining, unestimated, lang)
    if remaining < FREE_BELOW and unestimated:
        tail = f"{unestimated} {TEXT[lang]['unest_short']}"
    else:
        tail = f"{'▸' if pct > 100 else ''}{pct:.0f}%"
        if unestimated:
            tail += f" +{unestimated}?"
    return f"{emoji} `{bar}` {tail}"


def cell(text):
    return str(text).replace("|", "\\|")


def select_devs(kw, cfg, needle):
    names = cfg["dev_names"]
    if needle:
        matches = [n for n in names if needle.lower() in n.lower()]
        if not matches:
            sys.exit(f"No developer matches {needle!r}. Known: {', '.join(names)}")
        if len(matches) > 1:
            sys.exit(f"{needle!r} is ambiguous: {', '.join(matches)}")
        names = matches
    users = kw("res.users", "search_read", [[["name", "in", names]]],
               {"fields": ["id", "name"]})
    # keep the configured ordering so the report is stable run to run
    by_name = {u["name"]: u["id"] for u in users}
    return [(by_name[n], n) for n in names if n in by_name]


def fetch_tasks(kw, tag_id, dev_ids):
    """Everything assigned to the dev team that is still alive — the counted
    stages and the parked ones — in a single query. `is_counted` sorts out which
    rows drive the numbers."""
    domain = [
        ("tag_ids", "in", [tag_id]),
        ("stage_id", "in", digest_config.stage_id_list(kw, DISPLAY_STAGES)),
        ("user_ids", "in", dev_ids),
    ]
    return kw("project.task", "search_read", [domain], {"fields": TASK_FIELDS})


def fetch_analysis_tasks(kw, tag_id, dev_ids):
    """Spec tickets still waiting on their analyst — theirs by dev_analyst_id
    (not user_ids, which at this stage is usually empty), minus the ones whose
    analysis is settled."""
    domain = [
        ("tag_ids", "in", [tag_id]),
        ("stage_id", "in", digest_config.stage_id_list(kw, [SPEC_STAGE])),
        ("dev_analyst_id", "in", dev_ids),
        ("state", "not in", SPEC_SETTLED_STATES),
    ]
    return kw("project.task", "search_read", [domain], {"fields": TASK_FIELDS})


def fetch_timesheets(kw, task_ids, dev_ids):
    """{(user_id, task_id): hours} for the team's own entries on those tasks."""
    if not task_ids:
        return {}
    groups = kw("account.analytic.line", "read_group", [
        [("task_id", "in", task_ids), ("user_id", "in", dev_ids)],
        ["unit_amount:sum"], ["task_id", "user_id"],
    ], {"lazy": False})
    return {
        (g["user_id"][0], g["task_id"][0]): g["unit_amount"]
        for g in groups if g["task_id"] and g["user_id"]
    }


def stage_name(task):
    """The stage's base name — decoration stripped, so both the membership tests
    below and the tables read the same label whatever emoji the kanban wears."""
    return (digest_config.base_stage_name(task["stage_id"][1])
            if task["stage_id"] else "-")


def is_counted(task):
    """True when the task's hours belong in the developer's totals; False for a
    parked row, which is displayed for context only."""
    return stage_name(task) in COUNTED_STAGES


def is_parked(task):
    """True when the task sits in a stage that waits on someone else. Asked by
    name on PARKED_STAGES rather than as `not is_counted`, so a task in some
    third stage — a slow ticket already Done, in the Délais tables — is not
    mistaken for parked."""
    return stage_name(task) in PARKED_STAGES


def resolve_tag(kw, cfg):
    """The digest tag's id — the scope every section of every report shares."""
    tag = kw("project.tags", "search", [[["name", "=", cfg["tag"]]]])
    if not tag:
        sys.exit(f"Tag {cfg['tag']!r} not found.")
    return tag[0]


def remaining(task, dev_hours):
    """dev_time left after the dev team's hours. Negative on an overrun; the
    totals clamp it, a blown budget is not free capacity."""
    return task["dev_time"] - dev_hours.get(task["id"], 0.0)


def collect(kw, cfg, devs, tag_id=None):
    """Every figure the workload tables are built from, in one dict.

    Lifted out of main() so a second report can render the same numbers rather
    than recompute them: the dashboard prints this in French, the digest in
    English, and the two therefore cannot drift apart on an hour.
    """
    tag_id = resolve_tag(kw, cfg) if tag_id is None else tag_id
    dev_ids = [d[0] for d in devs]

    dev_tasks = fetch_tasks(kw, tag_id, dev_ids)
    spec_tasks = fetch_analysis_tasks(kw, tag_id, dev_ids)
    tasks = dev_tasks + spec_tasks
    ts = fetch_timesheets(kw, [t["id"] for t in tasks], dev_ids)
    # {task_id: hours logged by the dev team} — the burn that eats dev_time.
    dev_hours = {}
    for (_uid, task_id), hours in ts.items():
        dev_hours[task_id] = dev_hours.get(task_id, 0.0) + hours

    # Bucket each task under every developer assigned to it — by assignee for
    # development work, by analyst for a spec ticket (its own stage, so a task
    # can never land in both sets).
    per_dev = {dev_id: [] for dev_id in dev_ids}
    for t in dev_tasks:
        for uid in t["user_ids"]:
            if uid in per_dev:
                per_dev[uid].append(t)
    for t in spec_tasks:
        analyst = t["dev_analyst_id"][0] if t["dev_analyst_id"] else None
        if analyst in per_dev:
            per_dev[analyst].append(t)

    for rows in per_dev.values():
        rows.sort(key=lambda t: (STAGE_ORDER.get(stage_name(t), 99), t["id"]))

    # --- totals per developer -------------------------------------------------
    # Only counted rows feed the numbers, so the leaderboard keeps meaning
    # "work on their plate now"; parked rows are tallied separately.
    totals = {}
    for dev_id, rows in per_dev.items():
        counted = [t for t in rows if is_counted(t)]
        est = sum(t["dev_time"] for t in counted)
        own = sum(ts.get((dev_id, t["id"]), 0.0) for t in counted)
        left = sum(max(remaining(t, dev_hours), 0.0) for t in counted)
        unest = sum(1 for t in counted if not t["dev_time"])
        totals[dev_id] = {"tasks": len(counted), "est": est, "own": own,
                          "left": left, "unest": unest,
                          "parked": len(rows) - len(counted)}

    counted_ids = {t["id"] for t in tasks if is_counted(t)}
    return {"devs": devs, "tag_id": tag_id, "tasks": tasks, "ts": ts,
            "dev_hours": dev_hours, "per_dev": per_dev, "totals": totals,
            "counted_ids": counted_ids,
            "parked_ids": {t["id"] for t in tasks} - counted_ids}


def main(argv=None, lang="en", data=None, scope=None):
    argv = sys.argv[1:] if argv is None else argv
    needle = argv[0] if argv else None
    cfg = digest_config.load()
    kw = digest_config.connect(cfg)

    if data is None:
        devs = select_devs(kw, cfg, needle)
        if not devs:
            sys.exit("No developer found in Odoo.")
        data = collect(kw, cfg, devs)
    if scope is None:
        # Marks every project 📌 planned / ⚡ quickstart from the capacity plan, so
        # "is this ticket already paid for by an allocated day" is readable in the
        # table instead of remembered.
        scope, _slots = dev_plan_scope.build(kw, cfg)

    render(data, cfg, scope, lang=lang)


def render(data, cfg, scope, lang="en", leaderboard=True, show_load=True,
           extra_note=None, fold=False):
    """Print the leaderboard and one table per developer, in `lang`.

    `leaderboard=False` drops the recap table: on the dashboard the same
    per-developer totals already sit in the capacity section, one row per
    developer, next to the hours they can still take on — repeating them above
    the detail tables only invited comparing two rankings of the same people.

    `show_load=False` drops the load-bar column. The bar measures work left
    against a flat 40 h week, which is a capacity model, and a poor one: it
    knows nothing of a four-day week, of leave, or of the days already promised
    to a project in the allocation plan. Where a real capacity section is on the
    same page — the application's tab 2, with its `Temps allouable` — the bar is
    a second, worse answer to a question already answered properly, and two
    answers is worse than one. The digest, which has no capacity section at all,
    keeps it: there it is the only load signal there is.

    `fold=True` folds each developer's section: the name, the ticket count, the
    hours left and the flag stay on one clickable line, the table of tickets
    opens on demand. Six developers with a dozen rows each is most of a page,
    and on a page that already opens with the unclaimed tickets and a
    leaderboard, that detail is what one reads *after* deciding whose section to
    read. The summary line carries everything the ranking above it ranks on, so
    a folded section still answers "who is loaded" without being opened. Left
    off by default: the digest is read as one scroll, and a mail-shaped document
    that hides its content behind clicks is worse, not shorter.

    The digest calls this in English, the dashboard in French, off the same
    `collect()` payload — so a figure can never differ between the two pages,
    only its label."""
    T = TEXT[lang]
    devs, totals, per_dev = data["devs"], data["totals"], data["per_dev"]
    ts, dev_hours = data["ts"], data["dev_hours"]
    counted_ids, parked_ids = data["counted_ids"], data["parked_ids"]

    stages_txt = T["stages"] % {"dev": " · ".join(DEV_STAGES), "spec": SPEC_STAGE}
    parked_txt = "%s %s %s" % (" · ".join(PARKED_STAGES[:-1]), T["and"],
                               PARKED_STAGES[-1])
    # h2: the digest writes the "# DEV TSC — Digest" h1, sections sit under it.
    print("\n## %s (%s)\n" % (T["title"],
                              T["tasks_parked"] % (len(counted_ids), len(parked_ids))))
    print(T["scope"] % {"stages": stages_txt, "cap": CAPACITY_HOURS} + "\n")
    if show_load:
        print(T["bar_note"] % {"cap": CAPACITY_HOURS} + "\n")
    print(T["parked_note"] % {"stages": parked_txt} + "\n")
    print(T["marker_note"] + "\n")
    if extra_note:
        print(extra_note + "\n")

    # --- leaderboard ---------------------------------------------------------
    if leaderboard:
        print_leaderboard(data, T, lang, show_load)

    # --- one table per developer ---------------------------------------------
    print_per_dev(data, cfg, scope, T, lang, show_load, fold)


def print_leaderboard(data, T, lang, show_load=True):
    devs, totals = data["devs"], data["totals"]
    counted_ids, parked_ids = data["counted_ids"], data["parked_ids"]
    # The load bar is the last column, so dropping it is a slice — but slicing
    # the shared TEXT list in place would strip it for every later caller.
    header = T["lb_header"] if show_load else T["lb_header"][:-1]
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")
    for dev_id, dev_name in sorted(devs, key=lambda d: -totals[d[0]]["left"]):
        s = totals[dev_id]
        cells = [f"**{cell(dev_name)}**", str(s["tasks"]), hd(s["est"], lang),
                 hd(s["own"], lang), hd(s["left"], lang), s["unest"] or "—",
                 s["parked"] or "—"]
        if show_load:
            cells.append(load_bar(s["left"], s["unest"], lang))
        print("| " + " | ".join(str(c) for c in cells) + " |")
    if len(devs) > 1:
        g = {k: sum(s[k] for s in totals.values())
             for k in ("est", "own", "left", "unest")}
        cells = [f"_{T['total']}_", str(len(counted_ids)), hd(g["est"], lang),
                 hd(g["own"], lang), hd(g["left"], lang), g["unest"] or "—",
                 len(parked_ids) or "—"]
        if show_load:
            cells.append("")
        print("| " + " | ".join(str(c) for c in cells) + " |")


def open_section(dev_name, head, fold):
    """Open a developer's section, folded or not, and return its closing text.

    Folded, the `<summary>` stands in for the `### ` — same text, same id, so
    the leaderboard's jump links keep landing on the right person."""
    opening, closing = md_to_html.section_tags(f"{dev_name} — {head}", fold)
    print(f"\n{opening}\n")
    return f"\n{closing}\n" if closing else ""


def print_per_dev(data, cfg, scope, T, lang, show_load=True, fold=False):
    # "Worked (devs)" sits next to "Left" because it is the figure Left subtracts;
    # "Worked (task)" is the wider total and is context only.
    devs, totals, per_dev = data["devs"], data["totals"], data["per_dev"]
    ts, dev_hours = data["ts"], data["dev_hours"]
    header = T["row_header"]
    for dev_id, dev_name in sorted(devs, key=lambda d: -totals[d[0]]["left"]):
        rows = per_dev[dev_id]
        s = totals[dev_id]
        # Same rule as the load bar: 🟢 disponible / 🔴 plein is a verdict on
        # capacity drawn from a stock of ticket hours alone. The unestimated
        # count survives — it is a fact about the tickets, not a claim about the
        # week, and it is what stops "0h de reste" being read as "free".
        badge_txt = (availability(s["left"], s["unest"], lang) if show_load
                     else unestimated_flag(s["unest"], lang))
        head = (T["dev_head"] if badge_txt else T["dev_head_plain"]) % {
            "n": s["tasks"],
            "plural": T["task"] if s["tasks"] == 1 else T["task_plural"],
            "left": hd(s["left"], lang), "est": hd(s["est"], lang),
            "badge": badge_txt,
            "parked": T["parked_suffix"] % s["parked"] if s["parked"] else "",
        }
        close = open_section(dev_name, head, fold)
        if not rows:
            print(T["no_tasks"])
            print(close, end="")
            continue
        print("| " + " | ".join(header) + " |")
        print("|" + "|".join(["---"] * len(header)) + "|")
        for t in rows:
            own = ts.get((dev_id, t["id"]), 0.0)
            devs_h = dev_hours.get(t["id"], 0.0)
            # Shown unclamped: a negative Left is the overrun, and rounding it up
            # to 0.0h would hide by how much the dev budget was blown.
            left = remaining(t, dev_hours)
            over = " ⚠️" if t["dev_time"] and left < 0 else ""
            row = [
                digest_config.task_link(cfg, t["id"]),
                cell(t["name"]),
                cell(scope.label(t["project_id"])),
                cell(stage_name(t)),
                f"{own:.1f}h",
                f"{devs_h:.1f}h",
                f"{t['effective_hours']:.1f}h",
                f"{t['dev_time']:.1f}h" if t["dev_time"] else "—",
                f"{left:.1f}h{over}" if t["dev_time"] else "—",
            ]
            # Parked rows are on the list but out of the numbers — greyed and
            # flagged so the table's hours never look like they should add up to
            # the totals. Live rows stay in normal type whatever their project.
            if is_parked(t):
                row = dev_plan_scope.dim_row(row)
                row[0] = f"⏸️ {row[0]}"
            print("| " + " | ".join(str(c) for c in row) + " |")
        print(close, end="")


if __name__ == "__main__":
    main()
