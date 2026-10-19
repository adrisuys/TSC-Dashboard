#!/usr/bin/env python3
"""Délais de traitement des étapes Spec/Estim et To Do (sortie Markdown).

Usage:
    python3 dev_stats_report.py                 # 90 derniers jours
    python3 dev_stats_report.py --days 180
    python3 dev_stats_report.py --today 2026-08-21   # date de référence figée

Two lead times, both read off `project.task.duration_tracking` — a computed JSON
field, `{stage_id: seconds}`, which Odoo builds server-side from its own tracking
values. That matters: `mail.tracking.value` itself is refused to an ordinary
account (it is reserved to Role/Administrator), so this field is the only way to
get stage history without an admin key, and it works retroactively over the whole
history of every ticket.

What it cannot give:

*The moment `dev_time` was written.* No readable field carries it. The metric is
therefore the time the ticket spent **in** the Spec/Estim stage, which is the
estimation lead time — and, per the brief, what we agreed to measure for now. A
ticket that is estimated and then left sitting in Spec still accumulates: that is
a real cost, and the live table below flags it (a ticket in Spec that already
carries a `dev_time` is an estimation waiting to be moved on).

*A per-passage split.* The field is a total per stage, so a ticket sent back from
To Do to Spec adds to the same bucket. That is the right answer to "how much time
did this ticket consume in Spec" and the wrong one to "how long did the first
pass take"; the reports says which it is rather than pretending.

*Business hours.* Only the total is exposed, never the entry and exit stamps, so
the delay cannot be netted of nights and week-ends. Everything here is wall
clock, and the median rather than the mean is the headline for exactly that
reason: one ticket that crossed a holiday must not move the number everyone
reads.

An "Approved" end signal was tried and dropped: only 72 of the 150 tickets
carrying a `dev_time` over the last 120 days have the state event at all, and the
mapping from the message subtype to the state does not hold up (47 tickets sit in
`01_in_progress` with "Task Ready" as their last state message). Half a signal
that cannot be verified is worse than a clean one that measures slightly less.

Le rapport est en français, comme dev_capacity_report : il est lu par l'équipe.
Read-only : search, search_read, read.
"""
import argparse
import datetime
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_capacity_report as cap  # noqa: E402  (legend/cell/monday/wlabel)
import dev_plan_scope  # noqa: E402  (marquage 📌 planifié / ⚡ quickstart)
import dev_tasks_report as tasks  # noqa: E402  (fmt_state)
import dev_workload_report as workload  # noqa: E402  (tag + stage names)
import digest_config  # noqa: E402
import md_to_html  # noqa: E402  (folded — les sections repliées de l'onglet 4)

SPEC_STAGE = workload.SPEC_STAGE
TODO_STAGE = "To Do"

TASK_FIELDS = ["id", "name", "project_id", "stage_id", "state", "dev_time",
               "dev_analyst_id", "user_ids", "create_date",
               "date_last_stage_update", "duration_tracking"]

# Champs posés par tsc_project (>= 19.0.0.7). Quand ils sont là, ils priment :
# ils mesurent le délai que l'on veut vraiment — entrée en Spec/Estim jusqu'à la
# pose du dev_time, entrée en To Do jusqu'à l'arrivée en In Progress — là où
# duration_tracking ne sait donner que le temps de séjour dans l'étape. Le
# repli existe parce que le module n'est pas déployé partout, et parce qu'une
# base sans ces colonnes doit continuer à sortir des chiffres.
STORED_FIELDS = {"spec": "time_in_estim", "todo": "time_in_todo"}

# duration_tracking is computed, not stored: it cannot be filtered or grouped on,
# it is read record by record. Chunked so one slow batch cannot time out the run.
READ_CHUNK = 150
SLOW_ROWS = 8          # longest tickets listed per metric
BAR_WIDTH = 14         # largeur des barres de tendance, en blocs
TRIVIAL_SECONDS = 60   # a stage crossed in one gesture, counted but declared


def secs(seconds):
    """« 192.5 h (8.0 j) » — toute durée dans les deux unités, partout.

    L'unité adaptative d'avant (min / h / j selon la valeur) se lisait bien sur
    une ligne isolée et mal dans une colonne : 22.9 h au-dessus de 8.0 j
    demandait une conversion mentale à chaque ligne, et c'est précisément ce
    qu'un tableau de délais sert à éviter. Les jours sont calendaires (24 h),
    comme toutes les durées de ce rapport, jamais des jours ouvrés."""
    if seconds is None:
        return "—"
    return "%.1f h (%.1f j)" % (seconds / 3600.0, seconds / 86400.0)


def quantile(values, share):
    """Nearest-rank quantile — no interpolation, so every figure printed is a
    delay some ticket really took."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(int(-(-len(ordered) * share // 1)), len(ordered)) - 1
    return ordered[max(idx, 0)]


def stats(values):
    if not values:
        return {"n": 0}
    return {"n": len(values), "med": quantile(values, 0.5),
            "p80": quantile(values, 0.8), "max": max(values),
            "moy": sum(values) / len(values)}


def stat_cells(st):
    if not st["n"]:
        return ["0", "—", "—", "—", "—"]
    return [str(st["n"]), secs(st["med"]), secs(st["p80"]), secs(st["max"]),
            secs(st["moy"])]


# --- données -----------------------------------------------------------------

def stored_fields_available(kw):
    """True quand project.task porte les deux colonnes de tsc_project."""
    present = kw("project.task", "fields_get",
                 [list(STORED_FIELDS.values()), ["type"]])
    return all(field in present for field in STORED_FIELDS.values())


def stage_ids(kw, names):
    """{nom: {ids}} — un nom d'étape porte plusieurs ids dans cette base
    ("En cours" en a trois), donc tout est traité par ensemble d'ids. Le
    rapprochement se fait sur le nom de base : les libellés de production
    portent des emoji ("Spec validation / Estim ⚖️") qui changent au gré du
    ménage dans le kanban."""
    return digest_config.stage_ids(kw, names)


def fetch_tasks(kw, cfg, tag_id, since, stored=False):
    ids = kw("project.task", "search",
             [[["tag_ids", "in", [tag_id]], ["create_date", ">=", since.isoformat()]]],
             {"order": "id"})
    fields = TASK_FIELDS + (list(STORED_FIELDS.values()) if stored else [])
    tasks = []
    for start in range(0, len(ids), READ_CHUNK):
        tasks += kw("project.task", "read", [ids[start:start + READ_CHUNK]],
                    {"fields": fields})
    return tasks


def spent(task, ids):
    """Secondes passées par le ticket dans cet ensemble d'étapes, ou None s'il
    n'y est jamais passé."""
    tracking = task.get("duration_tracking") or {}
    total, seen = 0.0, False
    for key, value in tracking.items():
        try:
            stage = int(key)
        except (TypeError, ValueError):
            continue
        if stage in ids:
            total += value or 0
            seen = True
    return total if seen else None


def measure(tasks, ids, stored_field=None):
    """(clos, en_cours) — un ticket encore dans l'étape a un compteur qui tourne
    et n'a rien à faire dans une médiane.

    Avec `stored_field`, un délai clos est celui que tsc_project a mesuré et
    posé sur le ticket ; à 0, le délai n'a pas eu lieu (jamais estimé, jamais
    démarré) et le ticket ne compte que s'il attend encore dans l'étape. Sans
    lui, on retombe sur le temps de séjour lu dans duration_tracking."""
    closed, running = [], []
    for task in tasks:
        stage = task["stage_id"][0] if task["stage_id"] else None
        if stored_field:
            hours = task.get(stored_field) or 0.0
            if hours:
                closed.append((task, hours * 3600.0))
            elif stage in ids:
                running.append((task, spent(task, ids) or 0.0))
            continue
        value = spent(task, ids)
        if value is None:
            continue
        (running if stage in ids else closed).append((task, value))
    return closed, running


# --- sections ----------------------------------------------------------------

def section_resume(spec_closed, todo_closed, window_days, stored=False, level=2):
    print("\n%s DEV TSC — STATS de délai (étapes Spec/Estim et To Do)\n"
          % ("#" * level))
    if stored:
        source = ("Le délai est celui que `tsc_project` pose sur le ticket : "
                  "**entrée en Spec/Estim → dernier `dev_time` posé tant que le "
                  "ticket y est encore** d'une part, **entrée en To Do → arrivée "
                  "en In Progress** d'autre part. Compté en **heures ouvrées** "
                  "sur le calendrier de la société : ni les nuits, ni les "
                  "week-ends, ni la pause déjeuner — 17 h → 9 h le lendemain "
                  "vaut 1 h. Le délai To Do est mesuré une seule fois ; une "
                  "estimation révisée après le départ de l'étape ne réécrit pas "
                  "l'autre. Un ticket jamais estimé, ou jamais démarré, n'a pas "
                  "de délai : il attend, et il est listé plus bas.")
    else:
        source = ("Le délai est le temps **passé dans l'étape**, lu sur "
                  "`duration_tracking` (Odoo le calcule depuis son propre "
                  "historique de suivi) : c'est le repli tant que `tsc_project` "
                  "≥ 19.0.0.7 n'est pas déployé, et il mesure le séjour dans "
                  "l'étape plutôt que le délai jusqu'à l'estimation ou jusqu'au "
                  "démarrage. Un ticket repassé deux fois par l'étape cumule ses "
                  "deux séjours.")
    unit = ("Médiane en tête plutôt que moyenne : un ticket oublié une semaine "
            "déplace l'une et pas l'autre." if stored else
            "Délai **calendaire** : nuits et week-ends compris — d'où la médiane "
            "en tête plutôt que la moyenne.")
    print(f"_Tickets créés sur les {window_days} derniers jours. {source} {unit} "
          f"Les tickets dont le délai n'est pas clos sont exclus des médianes et "
          f"listés plus bas._\n")
    print("| Étape | Tickets clos | Médiane | p80 | Max | Moyenne | dont traversés < 1 min |")
    print("|---|---|---|---|---|---|---|")
    for label, closed in (("Spec validation / Estim", spec_closed),
                          ("To Do", todo_closed)):
        st = stats([v for _t, v in closed])
        trivial = sum(1 for _t, v in closed if v < TRIVIAL_SECONDS)
        print("| **%s** | %s | %s |" % (label, " | ".join(stat_cells(st)),
                                        trivial or "—"))
    cap.legend("Stats de délai", [
        ("`Médiane`", "la moitié des tickets font mieux. Le chiffre à lire."),
        ("`p80`", "quatre tickets sur cinq font mieux : le mauvais cas courant, "
                  "hors accidents."),
        ("`Moyenne`", "donnée pour comparaison — un seul ticket oublié un mois la "
                      "déplace, pas la médiane."),
        ("`dont traversés < 1 min`", "tickets poussés d'un trait à travers "
                                     "l'étape, sans temps de file. Comptés (le "
                                     "délai est réel), mais déclarés : ils tirent "
                                     "la médiane vers le bas."),
        ("`191.0 h (8.0 j)`", "toute durée est donnée en heures, avec son "
                              "équivalent en jours **calendaires** de 24 h entre "
                              "parenthèses — jamais des jours ouvrés."),
    ])


def section_par_personne(title, note, rows, fold=False):
    """rows: [(nom, [secondes, ...])] — une ligne par personne, triée par médiane."""
    with md_to_html.folded(title, fold):
        print(f"_{note}_\n")
        print("| Personne | Tickets | Médiane | p80 | Max | Moyenne |")
        print("|---|---|---|---|---|---|")
        ranked = sorted(((name, stats(values)) for name, values in rows),
                        key=lambda r: -(r[1].get("med") or 0))
        if not ranked:
            print("| _(aucun ticket sur la période)_ | | | | | |")
            return
        for name, st in ranked:
            print("| **%s** | %s |" % (cap.cell(name), " | ".join(stat_cells(st))))


def bar(value, scale, width=BAR_WIDTH):
    """Barre en blocs, à l'échelle passée. Encadrée de backticks pour forcer une
    police à chasse fixe : c'est ce qui aligne les barres dans un tableau
    Markdown rendu, et pas seulement dans le texte brut. Même idiome que la
    barre de charge du digest — donc lisible aussi en terminal, ce qu'un SVG
    n'aurait pas été."""
    if not value or not scale:
        return ""
    filled = max(1, min(int(round(value / scale * width)), width))
    return "`%s%s`" % ("█" * filled, "░" * (width - filled))


def wspan(iso_monday):
    """s35 (24/08→28/08) : l'étiquette de cap.wlabel, plus les jours ouvrés
    qu'elle couvre.

    Réservée à la tendance, qui se lit sur douze semaines et souvent des mois
    plus tard : « s35 » seul oblige à convertir de tête. Les en-têtes de colonne
    des tableaux de capacité gardent la forme courte, faute de largeur."""
    monday = datetime.date.fromisoformat(iso_monday)
    friday = monday + datetime.timedelta(days=4)
    return (f"{cap.wlabel(iso_monday)} ({monday.strftime('%d/%m')}"
            f"→{friday.strftime('%d/%m')})")


def tendance_data(spec_closed, todo_closed):
    """(weeks, per_week, medians, scale) — the last twelve creation-week
    cohorts, each stage's median, and the single scale both series share."""
    per_week = defaultdict(lambda: {"spec": [], "todo": []})
    for key, closed in (("spec", spec_closed), ("todo", todo_closed)):
        for task, value in closed:
            week = cap.week_of(datetime.date.fromisoformat(task["create_date"][:10]))
            per_week[week][key].append(value)
    weeks = sorted(per_week)[-12:]
    medians = {(w, k): quantile(per_week[w][k], 0.5)
               for w in weeks for k in ("spec", "todo") if per_week[w][k]}
    # Une seule échelle pour les deux séries : deux échelles indépendantes
    # feraient passer une file de 6 h pour l'égale d'une file de 6 jours.
    scale = max(medians.values()) if medians else 0
    return weeks, per_week, medians, scale


def section_tendance(spec_closed, todo_closed, fold=False):
    with md_to_html.folded("Tendance par semaine de création", fold):
        weeks, per_week, medians, scale = tendance_data(spec_closed, todo_closed)
        print(f"_Cohortes : les tickets **créés** cette semaine-là ont mis ce temps à "
              f"franchir l'étape. Les barres partagent une échelle unique, "
              f"{secs(scale)} pleine, pour que les deux étapes se "
              f"comparent aussi. Une "
              f"semaine récente n'a que ses tickets déjà sortis de l'étape, donc les "
              f"plus rapides — à lire avec le nombre de tickets à côté._\n")
        print("| Semaine | Spec — n | Spec — médiane | Spec | To Do — n | "
              "To Do — médiane | To Do |")
        print("|---|---|---|---|---|---|---|")
        for week in weeks:
            row = per_week[week]
            cells = [wspan(week)]
            for key in ("spec", "todo"):
                median = medians.get((week, key))
                cells += [str(len(row[key])) if row[key] else "—",
                          secs(median),
                          bar(median, scale)]
            print("| " + " | ".join(cells) + " |")


def section_lents(cfg, label, closed, scope, fold=False):
    with md_to_html.folded(f"Les plus lents — {label}", fold):
        if not closed:
            print("_(aucun ticket clos sur la période)_")
            return
        print("| ID | Ticket | Projet | Délai | Étape actuelle |")
        print("|---|---|---|---|---|")
        for task, value in sorted(closed, key=lambda r: -r[1])[:SLOW_ROWS]:
            row = [digest_config.task_link(cfg, task["id"]),
                   cap.cell(task["name"]),
                   cap.cell(scope.label(task["project_id"])), secs(value),
                   cap.cell(workload.stage_name(task))]
            # Only a ticket now parked is dimmed — its delay is behind it and the
            # next move is not ours. Same convention as the CHARGE table.
            if workload.is_parked(task):
                row = dev_plan_scope.dim_row(row)
            print("| " + " | ".join(row) + " |")


def split(running, held):
    """(portés, sans personne) — deux listes de (task, value)."""
    return ([r for r in running if held(r[0])],
            [r for r in running if not held(r[0])])


def assignee_names(kw, running):
    """{id: nom} pour tout utilisateur assigné à l'un de ces tickets.

    Toute l'équipe, pas seulement les développeurs : un ticket DEV TSC posé sur
    un fonctionnel attend quelqu'un lui aussi, et une case vide voudrait dire
    « personne », ce qui serait faux."""
    ids = sorted({uid for task, _ in running for uid in task["user_ids"]})
    if not ids:
        return {}
    return {u["id"]: u["name"]
            for u in kw("res.users", "read", [ids], {"fields": ["name"]})}


def who(task, names, analyst_first):
    """Qui tient le ticket aujourd'hui : les assignés, ou l'analyste dev à
    défaut. L'ordre suit l'étape — en Spec/Estim c'est l'analyste qui doit
    l'estimation, en To Do l'assigné qui doit le démarrage — et le suffixe dit
    lequel des deux est affiché, pour qu'un « — » veuille bien dire personne."""
    analyst = task["dev_analyst_id"][1] if task["dev_analyst_id"] else ""
    assignees = ", ".join(names[uid] for uid in task["user_ids"] if uid in names)
    # Le suffixe reste en police normale : l'italique gris est réservé aux
    # lignes parquées (dev_plan_scope.dim_row), et rien ici n'est parqué.
    order = [(analyst, " (analyste)"), (assignees, "")] if analyst_first \
        else [(assignees, ""), (analyst, " (analyste)")]
    for name, suffix in order:
        if name:
            return cap.cell(name) + suffix
    return "—"


def section_en_cours(cfg, label, running, scope, threshold_days, extra=None,
                     names=None, analyst_first=False, unowned=(), fold=False):
    """Tickets encore dans l'étape : compteur qui tourne, donc hors statistiques
    mais c'est là que se trouve ce sur quoi on peut agir aujourd'hui."""
    with md_to_html.folded(f"En attente maintenant — {label}", fold):
        if not running:
            print("_(aucun ticket dans cette étape)_")
            return
        qui = ("« Qui » : l'analyste dev qui porte l'estimation, ou les assignés à "
               "défaut" if analyst_first else
               "« Qui » : les assignés, ou l'analyste dev à défaut")
        print(f"_Compteur en cours, donc exclu des médianes ci-dessus. ⚠️ au-delà de "
              f"{threshold_days:g} j calendaire(s) dans l'étape. {qui}._\n")
        # Ce tableau est le complément exact d'« À prendre » : ce qui n'a encore
        # personne est listé là-bas, pas ici, sinon les deux sections racontent deux
        # histoires du même ticket. On les nomme quand même — /dev-delays se lit
        # seul, sans section « À prendre » au-dessus.
        if unowned:
            manque = "analyste dev" if analyst_first else "assigné"
            ids = ", ".join(str(t["id"]) for t, _ in sorted(unowned, key=lambda r: -r[1]))
            print(f"_Plus {len(unowned)} ticket(s) sans {manque}, à prendre plutôt "
                  f"qu'à relancer : {ids}._\n")
        head = (["ID", "Ticket", "Projet", "Statut", "Qui", "Dans l'étape depuis"]
                + ([extra[0]] if extra else []))
        print("| " + " | ".join(head) + " |")
        print("|" + "|".join(["---"] * len(head)) + "|")
        for task, value in sorted(running, key=lambda r: -r[1]):
            flag = " ⚠️" if value > threshold_days * 86400 else ""
            row = [digest_config.task_link(cfg, task["id"]), cap.cell(task["name"]),
                   cap.cell(scope.label(task["project_id"])),
                   tasks.fmt_state(task["state"], "fr"),
                   who(task, names or {}, analyst_first), secs(value) + flag]
            if extra:
                row.append(extra[1](task))
            # No dimming here: every row is a ticket still sitting in Spec/Estim or
            # To Do, so all of them are live and waiting on us.
            print("| " + " | ".join(row) + " |")


# --- main --------------------------------------------------------------------

def build(argv=None, kw=None, cfg=None, scope=None, today=None, full=True,
          fold=False):
    """`full=False` prints the overall figures and the waiting lists only.

    On the shared dashboard the per-analyst and per-assignee breakdowns are
    deliberately left out: that page is read by everyone, and a median with a
    name next to it turns a delay — which is mostly queueing, not effort — into
    a finger pointed at someone. The full picture stays available on demand
    through /dev-delays, where it is asked for rather than stumbled upon.

    `fold=True` folds every section below the overall figures — the trend,
    the per-person breakdowns, the slowest tickets and the two waiting
    lists. On its own page this report is read top to bottom; in the
    application it is one tab of four, and a tab has to open on its answer
    rather than on three hundred rows."""
    ap = argparse.ArgumentParser(description="Délais Spec/Estim et To Do.")
    ap.add_argument("--days", type=int, default=None,
                    help="fenêtre en jours sur la date de création "
                         "(défaut : stats_window_days, 90)")
    ap.add_argument("--today", default=None, help="date de référence (YYYY-MM-DD)")
    ap.add_argument("--format", choices=["md", "html"], default="html",
                    help="sortie enregistrée (défaut : html)")
    ap.add_argument("--no-open", dest="open_it", action="store_false",
                    help="ne pas ouvrir la page")
    ap.add_argument("--tab", dest="app", action="store_false",
                    help="onglet ordinaire plutôt que fenêtre d'application")
    args = ap.parse_args(argv)

    cfg = cfg or digest_config.load()
    kw = kw or digest_config.connect(cfg)
    today = today or (datetime.date.fromisoformat(args.today) if args.today
                      else datetime.date.today())
    ctx = gather(kw, cfg, today, args.days, scope)
    scope, tasks, window_days, since = (ctx["scope"], ctx["tasks"],
                                        ctx["window_days"], ctx["since"])
    stored = ctx["stored"]
    spec_closed, spec_running = ctx["spec_closed"], ctx["spec_running"]
    todo_closed, todo_running = ctx["todo_closed"], ctx["todo_running"]
    spec_threshold, todo_threshold = ctx["spec_threshold"], ctx["todo_threshold"]

    # Une page à part porte son propre h1 ; sur le tableau de bord la section
    # s'insère sous le h1 de la page.
    section_resume(spec_closed, todo_closed, window_days, stored,
                   level=1 if full else 2)

    section_tendance(spec_closed, todo_closed, fold=fold)

    if full:
        section_nominative(ctx, fold=fold)

    names = ctx["names"]
    spec_held, spec_free, todo_held, todo_free = waiting(ctx)
    section_en_cours(cfg, "Spec validation / Estim", spec_held, scope,
                     spec_threshold,
                     extra=("Estimation posée ?",
                            lambda t: ("✅ %.1f h — à pousser en To Do" % t["dev_time"])
                            if t["dev_time"] else "—"),
                     names=names, analyst_first=True, unowned=spec_free,
                     fold=fold)
    section_en_cours(cfg, "To Do", todo_held, scope, todo_threshold,
                     extra=("Estimation",
                            lambda t: "%.1f h" % t["dev_time"] if t["dev_time"] else "—"),
                     names=names, analyst_first=False, unowned=todo_free,
                     fold=fold)

    print(f"\n_{len(tasks)} tickets lus · fenêtre : {since.isoformat()} → "
          f"{today.isoformat()} (création) · "
          f"Spec {len(spec_closed)} clos / {len(spec_running)} en cours · "
          f"To Do {len(todo_closed)} clos / {len(todo_running)} en cours._\n")
    return args


def gather(kw, cfg, today, days=None, scope=None, since=None):
    """Every input of the report, in one dict — lifted out of build() so the
    web app lays the same delays out natively. The person breakdown, the
    weekly trend and the waiting lists are `by_person`, `tendance_data` and
    `waiting`, shared by both pages. `since` — a calendar period's first day,
    from the web app's picker — replaces `days` when given."""
    if since is not None:
        window_days = (today - since).days
    else:
        window_days = days or cfg.get("stats_window_days", 90)
        since = today - datetime.timedelta(days=window_days)
    tag_id = workload.resolve_tag(kw, cfg)
    stages = stage_ids(kw, [SPEC_STAGE, TODO_STAGE])
    spec_ids, todo_ids = stages[SPEC_STAGE], stages[TODO_STAGE]
    if not spec_ids or not todo_ids:
        sys.exit(f"Étapes introuvables : {SPEC_STAGE!r} / {TODO_STAGE!r}.")
    stored = stored_fields_available(kw)
    tasks = fetch_tasks(kw, cfg, tag_id, since, stored)
    if scope is None:
        scope, _slots = dev_plan_scope.build(kw, cfg, today=today)
    spec_closed, spec_running = measure(tasks, spec_ids,
                                        STORED_FIELDS["spec"] if stored else None)
    todo_closed, todo_running = measure(tasks, todo_ids,
                                        STORED_FIELDS["todo"] if stored else None)
    thresholds = cfg.get("stats_thresholds") or {}
    return {
        "cfg": cfg, "today": today, "window_days": window_days, "since": since,
        "stored": stored, "tasks": tasks, "scope": scope,
        "spec_closed": spec_closed, "spec_running": spec_running,
        "todo_closed": todo_closed, "todo_running": todo_running,
        "spec_threshold": thresholds.get("spec_days", 1),
        "todo_threshold": thresholds.get("todo_days", 3),
        # Une seule lecture des noms pour les deux tableaux d'attente.
        "names": assignee_names(kw, spec_running + todo_running),
        "devs": dict(workload.select_devs(kw, cfg, None)),
    }


def waiting(ctx):
    """(spec_held, spec_free, todo_held, todo_free).

    Le partage est celui d'« À prendre » (dev_tasks_report.build_domain) : en
    Spec/Estim c'est dev_analyst_id qui dit si quelqu'un porte le ticket, en
    To Do un assigné de l'équipe dev. Même règle des deux côtés, donc aucun
    ticket ne peut figurer dans les deux sections."""
    dev_ids = set(ctx["devs"])
    spec_held, spec_free = split(ctx["spec_running"],
                                 lambda t: bool(t["dev_analyst_id"]))
    todo_held, todo_free = split(
        ctx["todo_running"], lambda t: any(uid in dev_ids for uid in t["user_ids"]))
    return spec_held, spec_free, todo_held, todo_free


def by_person(ctx):
    """(by_analyst, by_dev, outside) — Spec/Estim delays per current dev
    analyst, To Do delays per current dev-team assignee (a ticket with two
    counts for each), and how many closed To Do tickets sit outside the team."""
    by_analyst = defaultdict(list)
    for task, value in ctx["spec_closed"]:
        if task["dev_analyst_id"]:
            by_analyst[task["dev_analyst_id"][1]].append(value)
    # Restreint à l'équipe dev : le flux DEV TSC porte aussi des assignés
    # fonctionnels, dont le délai d'attente n'est pas celui qu'on pilote ici.
    dev_ids = ctx["devs"]
    by_dev = defaultdict(list)
    outside = 0
    for task, value in ctx["todo_closed"]:
        if not any(uid in dev_ids for uid in task["user_ids"]):
            outside += 1
            continue
        for uid in task["user_ids"]:
            if uid in dev_ids:
                by_dev[uid].append(value)
    return (list(by_analyst.items()),
            [(dev_ids[uid], values) for uid, values in by_dev.items()], outside)


def section_nominative(ctx, fold=False):
    """Le détail nominatif et les tickets les plus lents — /dev-delays seulement."""
    cfg, scope = ctx["cfg"], ctx["scope"]
    by_analyst, by_dev, outside = by_person(ctx)
    section_par_personne(
        "Spec/Estim — par analyste dev",
        "Analyste porté par le ticket aujourd'hui (`dev_analyst_id`) : c'est le "
        "dernier connu, pas forcément celui qui a fait l'estimation. Les tickets "
        "sans analyste sont hors tableau.",
        by_analyst, fold=fold)

    section_par_personne(
        "To Do — par assigné (équipe dev)",
        "Temps d'attente avant démarrage, par assigné actuel. Un ticket à deux "
        "assignés compte pour chacun ; l'assigné a pu être posé après coup. "
        f"{outside} ticket(s) clos assignés hors équipe dev sont hors tableau, "
        "mais restent dans le résumé ci-dessus.",
        by_dev, fold=fold)

    section_lents(cfg, "Spec validation / Estim", ctx["spec_closed"], scope,
                  fold=fold)
    section_lents(cfg, "To Do", ctx["todo_closed"], scope, fold=fold)


TITLE = "DEV TSC — Délais des étapes"


def main(argv=None):
    """HTML par défaut : le rapport complet fait sept tableaux et se lit comme
    une page. `--format md` n'écrit que le Markdown. Dans les deux cas le
    Markdown part sur stdout, pour qu'un appelant en terminal garde les
    chiffres."""
    argv = sys.argv[1:] if argv is None else argv
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        args = build(argv, full=True)
    markdown = buf.getvalue()
    print(markdown)

    import digest as digest_mod
    import md_to_html
    out_dir = digest_mod.output_dir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"delays.{args.format}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md_to_html.render(markdown, title=TITLE)
                 if args.format == "html" else markdown)
    print(path, file=sys.stderr)
    if args.format == "html" and args.open_it:
        digest_mod.open_in_browser(path, app=args.app)
    return 0


if __name__ == "__main__":
    sys.exit(main())
