#!/usr/bin/env python3
"""Capacité planifiée vs charge quickstart, semaine par semaine (Markdown).

Usage:
    python3 dev_quickstart_report.py
    python3 dev_quickstart_report.py --weeks-fwd 4 --today 2026-08-21

Deux façons de travailler cohabitent dans l'équipe. Certains projets sont dans le
plan de capacité (`project.allocation.slot`, la grille derrière /odoo/action-2407)
avec des développeurs nommés et des jours par semaine ; tout le reste est du
**quickstart** — aucune allocation, n'importe quel développeur, au fil de l'eau.
Ce que ce rapport calcule, pour chacun :

    capacité   heures du resource.calendar, moins les absences
    planifié   jours d'allocation x 8
    libre      capacité − planifié           → la place pour du quickstart
    file       reste à faire (dev_time − heures dev) sur ses tickets quickstart

Trois choix de lecture, parce qu'ils changent les conclusions :

*La capacité est reprise de dev_capacity_report, jamais recalculée ici.* Elle
sort du calendrier de chaque employé, corrigée par `dev_schedules` là où la prod
se trompe. Nathan travaille quatre jours sur un calendrier à 40 h : sur 40 h il a
l'air d'avoir de la place, sur ses 32 h réelles il est le plus chargé de
l'équipe. Redériver ce chiffre ici, c'est en avoir deux versions.

*La semaine courante est donnée entière, pas au prorata.* C'est une vue de
planification, pas un reste-à-faire de la semaine : la colonne « pointé » dit
séparément ce qui a déjà été consommé. Le prorata a sa place dans l'occupation
de `/dev-capacity`, pas ici.

*La file quickstart est un stock, pas un débit.* Elle ne porte pas de semaine :
c'est le reste à brûler sur les tickets déjà assignés. Le verdict la compare aux
heures libres des **deux** semaines affichées, ce qui est la seule division qui
ait un sens entre un stock et une capacité hebdomadaire.

Les jours fériés sont déduits gratuitement : ils sont pré-pointés sur le projet
`Congés`, donc ils sortent de la capacité des semaines à venir comme des passées.

Read-only : search, search_read, read et read_group.
"""
import argparse
import datetime
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_capacity_report as cap  # noqa: E402
import dev_plan_scope  # noqa: E402
import dev_workload_report as workload  # noqa: E402
import digest_config  # noqa: E402

HOURS_PER_DAY = cap.HOURS_PER_DAY
# La semaine courante est détaillée ligne à ligne ; les suivantes ne portent que
# l'engagement du plan, dans le second tableau.
WEEKS_DETAIL = 1
WEEKS_FWD = 5


def hours(value, signed=False):
    # Vrai signe moins (U+2212) plutôt que le trait d'union : la page est
    # typographiée en français, et « −122 h » se lit mieux que « -122 h ».
    if signed:
        return "%s%.0f h" % ("+" if value >= 0 else "−", abs(value))
    return "%.0f h" % value


def verdict(free_two_weeks, queue, unestimated):
    """(badge, texte) — la place restante face à la file quickstart.

    Les tickets sans estimation ne pèsent rien dans `queue` : ils ne doivent
    jamais se lire comme de la capacité libre, d'où le ❓ qui prime sur le vert.
    """
    if free_two_weeks <= NO_ROOM:
        if queue > 0.5:
            return "🔴", "aucune marge, file en attente"
        return "⚫", "entièrement planifié"
    if queue <= 0.5:
        if unestimated:
            return "❓", f"{unestimated} sans estim."
        return "🟢", "rien en file"
    ratio = queue / free_two_weeks
    tail = f" · {unestimated} sans estim." if unestimated else ""
    if ratio < ROOM_BELOW:
        return "🟢", f"de la marge ({ratio * 100:.0f} %){tail}"
    if ratio < TIGHT_BELOW:
        return "🟡", f"juste ({ratio * 100:.0f} %){tail}"
    return "🔴", f"sous l'eau ({ratio * 100:.0f} %){tail}"


# --- calculs -----------------------------------------------------------------

def week_figures(name, week, sched, leave, planned):
    """(capacité, planifié_jours, planifié_heures, libre, jours_congé) d'une
    semaine pour un développeur."""
    leave_hours = leave[(name, week)]
    capacity = max(cap.gross(sched) - leave_hours, 0.0)
    days = sum(d for (n, w, _p), d in planned.items() if n == name and w == week)
    planned_hours = days * HOURS_PER_DAY
    return (capacity, days, planned_hours, max(capacity - planned_hours, 0.0),
            leave_hours / HOURS_PER_DAY)


def week_projects(name, week, planned, projects, scope):
    """Projets portant au moins un jour d'allocation cette semaine-là."""
    out = set()
    for (n, w, pid), days in planned.items():
        if n == name and w == week and days:
            out.add(projects.get(pid, (f"#{pid}", False))[0])
    return ", ".join(sorted(cap.cell(p) for p in out)) or "—"


def split_queue(data, dev_id, scope):
    """(dev_time, pointé perso, reste, sans_estim) sur les tickets **quickstart**
    du développeur.

    Les chiffres viennent de dev_workload_report : même définition du reste
    (dev_time − heures pointées **par les développeurs**), donc le tableau de
    charge et celui-ci ne peuvent pas se contredire. Les heures pointées perso
    sortent du même relevé, d'où deux colonnes qui ne se contredisent pas non
    plus : `reste` retire les heures de **tous** les développeurs, `pointé perso`
    ne montre que les siennes — deux développeurs sur un ticket partagent un
    budget, mais pas leurs heures."""
    estimated = own = queue = 0.0
    unestimated = 0
    for task in data["per_dev"][dev_id]:
        if not workload.is_counted(task):
            continue          # ⏸️ parqué : listé ailleurs, jamais compté
        if scope.is_planned(task["project_id"]):
            continue          # 📌 planifié : payé par des jours d'allocation
        estimated += task["dev_time"]
        own += data["ts"].get((dev_id, task["id"]), 0.0)
        queue += max(workload.remaining(task, data["dev_hours"]), 0.0)
        if not task["dev_time"]:
            unestimated += 1
    return estimated, own, queue, unestimated


def pointed(name, week, actual, scope, projects):
    """Heures pointées cette semaine-là, coupées planifié / quickstart. Les
    absences sont déjà hors de `actual`."""
    quick = plan = 0.0
    for (n, w, pid), value in actual.items():
        if n != name or w != week:
            continue
        project = [pid, projects.get(pid, (f"#{pid}", False))[0]] if pid else None
        if scope.is_planned(project):
            plan += value
        else:
            quick += value
    return quick, plan


# --- sections ----------------------------------------------------------------

def semaine_data(devs, detail, schedules, leave, planned, projects, scope, data):
    """B1 as data — one row per developer, most constrained first, plus the
    three team totals the summary sentence carries. Printed by
    `section_semaine`, rendered natively by the web app."""
    week = detail[0]
    rows, totals = [], defaultdict(float)
    for uid, _eid, name in devs:
        sched = schedules[name]
        capacity, days, planned_hours, free, leave_days = week_figures(
            name, week, sched, leave, planned)
        estimated, own, queue, unestimated = split_queue(data, uid, scope)
        allocable = free - queue
        badge = "⚫" if free <= 0.05 else ("🟢" if allocable > 0.5 else "🔴")
        # Seuls les trois chiffres de la phrase de synthèse sont cumulés : une
        # ligne TOTAL n'apprenait rien de plus qu'elle.
        for key, value in (("free", free), ("queue", queue),
                           ("allocable", allocable)):
            totals[key] += value
        rows.append({
            "name": name, "days": len(sched["hours"]), "hours": cap.gross(sched),
            "overridden": sched["overridden"], "capacity": capacity,
            "planned_days": days, "planned_hours": planned_hours, "free": free,
            "leave_days": leave_days,
            "projects": sorted({projects.get(pid, (f"#{pid}", False))[0]
                                for (n, w, pid), d in planned.items()
                                if n == name and w == week and d}),
            "estimated": estimated, "own": own, "queue": queue,
            "unestimated": unestimated, "parked": data["totals"][uid]["parked"],
            "allocable": allocable, "badge": badge,
            "state": {"⚫": "full", "🟢": "ok", "🔴": "err"}[badge]})
    # Le plus contraint en haut : c'est la ligne qui décide qui ne prend rien.
    rows.sort(key=lambda r: r["allocable"])
    return {"week": week, "label": cap.wlabel(week), "rows": rows,
            "totals": dict(totals)}


def engagement_data(devs, future, schedules, leave, planned, projects):
    """B2 as data — per developer, days committed and hours left free per week
    ahead, their average, and the projects committed to."""
    rows = []
    for _uid, _eid, name in devs:
        sched = schedules[name]
        cells, frees, targets = [], [], set()
        for week in future:
            _capacity, days, _ph, free, _lv = week_figures(
                name, week, sched, leave, planned)
            cells.append({"week": week, "label": cap.wlabel(week),
                          "days": days, "free": free})
            frees.append(free)
            for (n, w, pid), value in planned.items():
                if n == name and w == week and value:
                    targets.add(projects.get(pid, (f"#{pid}", False))[0])
        rows.append({"name": name, "days": len(sched["hours"]),
                     "hours": cap.gross(sched), "cells": cells,
                     "free_avg": sum(frees) / len(frees) if frees else 0.0,
                     "targets": sorted(targets)})
    return rows


def section_semaine(devs, detail, schedules, leave, planned, actual, projects,
                    scope, data, cfg):
    """B1 — la semaine courante : ce qui est planifié, et ce qu'il reste de place.

    Une seule colonne compte vraiment, la dernière : `temps allouable` = heures
    libres de la semaine − reste à faire quickstart. Positive, le développeur
    peut encore prendre un ticket ; négative, il en a déjà plus que sa semaine
    n'en contient."""
    week = detail[0]
    print(f"\n## Capacité vs quickstart — {cap.wlabel(week)} (semaine courante)\n")
    print("_`Planifié` = jours d'allocation × 8 h sur un projet du plan de "
          "capacité. `Libre` = capacité de la semaine (calendrier propre du "
          "développeur, absences et fériés déduits) moins le planifié : la place "
          "pour du quickstart. Les trois colonnes QS ne portent que les tickets "
          "**hors plan** : leur `dev_time`, les heures que le développeur y a "
          "lui-même pointées, et ce qu'il reste à brûler. Semaine entière, non "
          "proratisée — on planifie, on ne fait pas un reste-à-faire du jour._\n")
    cap.legend("Capacité vs quickstart", [
        ("`Heures/sem.`", "jours ouvrés et heures par semaine du développeur "
                          "(`*` = calendrier de prod corrigé par `dev_schedules`)."),
        ("`2.0 j`", "jours d'allocation engagés sur la semaine."),
        ("`—` (planifié)", "aucune allocation : la semaine entière est "
                           "disponible pour du quickstart."),
        ("`(−1 j congé)`", "absence ou férié déjà déduit de la capacité."),
        ("`Dev\\_time QS`", "somme des estimations des tickets quickstart "
                            "assignés ; `+n ?` = tickets sans estimation, qui "
                            "pèsent 0 h et ne sont donc pas de la place libre."),
        ("`Pointé perso QS`", "heures que le développeur a lui-même pointées sur "
                              "ces tickets, depuis toujours."),
        ("`Reste QS`", "`dev_time` − heures pointées **par les développeurs** sur "
                       "ces tickets. Un stock, pas un débit hebdomadaire."),
        ("⏸️", "tickets parqués en aval (attente de tiers, validation client, "
                "prod) : hors de toutes les colonnes, mais ils reviennent."),
        ("`Temps allouable`", "`Libre` − `Reste QS`. 🟢 positif : il peut prendre "
                              "un ticket de plus. 🔴 négatif : la file dépasse "
                              "déjà sa semaine. ⚫ : semaine entièrement "
                              "planifiée, aucune place quickstart par "
                              "construction."),
    ])
    header = ["Développeur", "Heures/sem.", f"{cap.wlabel(week)} planifié",
              f"{cap.wlabel(week)} libre", "Projet assigné", "Dev_time QS",
              "Pointé perso QS", "Reste QS", "⏸️ Parqués", "Temps allouable"]
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")

    sem = semaine_data(devs, detail, schedules, leave, planned, projects,
                       scope, data)
    totals = sem["totals"]
    for r in sem["rows"]:
        print("| " + " | ".join([
            f"**{cap.cell(r['name'])}**",
            "%d j / %.0f h%s" % (r["days"], r["hours"],
                                 " *" if r["overridden"] else ""),
            f"{r['planned_days']:.1f} j" if r["planned_days"] else "—",
            hours(r["free"]) + (f" (−{r['leave_days']:.1f} j congé)"
                                if r["leave_days"] > 0.1 else ""),
            ", ".join(cap.cell(p) for p in r["projects"]) or "—",
            hours(r["estimated"]) + (f" +{r['unestimated']} ?" if r["unestimated"] else ""),
            hours(r["own"]),
            hours(r["queue"]),
            str(r["parked"]) if r["parked"] else "—",
            f"{r['badge']} {hours(r['allocable'], signed=True)}",
        ]) + " |")
    print(f"\n**Équipe : {hours(totals['free'])} libres cette semaine pour "
          f"{hours(totals['queue'])} de quickstart en file → "
          f"{hours(totals['allocable'], signed=True)} allouables.** "
          + ("La file dépasse la place de la semaine ; ce qui déborde attendra "
             "la suivante." if totals["allocable"] < 0 else
             "Il reste de la place pour de nouveaux tickets.") + "\n")
    print("_Le tableau ne juge que le quickstart : les heures de projets "
          "planifiés se lisent contre les jours d'allocation, pas contre les "
          "heures libres, et l'autonomie de chaque projet est dans "
          "`/dev-capacity`._\n")


def section_engagement(devs, future, schedules, leave, planned, projects, scope):
    """B2 — ce qui est déjà engagé au-delà des deux semaines détaillées.

    Section repliée par défaut : c'est de la prospective, pas la décision du
    jour. La question « qui peut prendre ce ticket » se règle dans le tableau
    de la semaine courante juste au-dessus ; ces cinq semaines-là ne se lisent
    que quand on planifie. Le `<summary>` remplace le `## `, d'où la classe
    `section` que `md_to_html` habille comme un h2 (aucun sommaire ne pointe
    vers ce titre : les ancres de la page sont posées par `dev_dashboard`)."""
    if not future:
        return
    print(f"\n<details class=\"section\"><summary>Engagement des semaines "
          f"suivantes ({cap.wlabel(future[0])} → {cap.wlabel(future[-1])})"
          f"</summary>\n")
    print("_Jours d'allocation déjà posés, et ce qu'ils laissent de libre sur la "
          "semaine propre de chaque développeur. Une semaine à `—` n'est pas une "
          "semaine libre démontrée : c'est une semaine que le plan ne couvre pas "
          "encore._\n")
    cap.legend("Engagement à venir", [
        ("`2.0 j · 24 h`", "jours engagés · heures que la semaine laisse libres."),
        ("`— · 40 h`", "rien d'engagé : trou dans le plan, pas disponibilité "
                       "démontrée."),
        ("`Libre moy./sem.`", "moyenne des heures libres par semaine sur la "
                              "période."),
        ("`Engagé sur`", "tout projet portant au moins un jour sur la période."),
    ])
    header = (["Développeur", "Régime"] + [cap.wlabel(w) for w in future]
              + ["Libre moy./sem.", "Engagé sur"])
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(["---"] * len(header)) + "|")
    for r in engagement_data(devs, future, schedules, leave, planned, projects):
        cells = [("%.1f j · %s" % (c["days"], hours(c["free"]))) if c["days"]
                 else "— · %s" % hours(c["free"]) for c in r["cells"]]
        print("| " + " | ".join(
            [f"**{cap.cell(r['name'])}**", "%d j / %.0f h" % (r["days"], r["hours"])]
            + cells
            + [hours(r["free_avg"]),
               ", ".join(cap.cell(t) for t in r["targets"]) or "—"]) + " |")
    print("\n</details>\n")


# --- assemblage --------------------------------------------------------------

def gather(kw, cfg, today, weeks_fwd=WEEKS_FWD, needle=None, data=None):
    """Tout ce dont les deux sections ont besoin, en un aller-retour Odoo."""
    current = cap.monday(today)
    detail = [(current + datetime.timedelta(weeks=n)).isoformat()
              for n in range(WEEKS_DETAIL)]
    future = [(current + datetime.timedelta(weeks=n)).isoformat()
              for n in range(WEEKS_DETAIL, WEEKS_DETAIL + weeks_fwd)]

    devs, employees, duplicated = cap.select_devs(kw, cfg, needle)
    if not devs:
        sys.exit("Aucun développeur trouvé dans Odoo.")
    emp_ids = [d[1] for d in devs]
    schedules = cap.fetch_schedules(kw, cfg, employees)

    date_from = current
    date_to = datetime.date.fromisoformat((future or detail)[-1]) + datetime.timedelta(days=6)
    # Le détecteur de régime n'a pas sa place ici : il vit dans /dev-capacity,
    # d'où une fenêtre de détection réduite à rien.
    leave, _todate, actual, _weekday, _n = cap.fetch_actuals(
        kw, cfg, emp_ids, date_from, date_to, (today, today))
    planned, _dev_days, _roles, n_slots = cap.fetch_plan(kw, emp_ids, date_from, date_to)
    # Le plan sert deux fois : à chiffrer les semaines, et à classer les projets.
    # Un seul créneau de plus de 0 jour suffit à faire d'un projet un projet
    # planifié — c'est ce qui fait que European Schools n'est plus un cas
    # particulier codé en dur.
    scope = dev_plan_scope.Scope(
        {pid for (_n, _w, pid), days in planned.items() if pid and days > 0}, cfg)
    if data is None:
        data = workload.collect(kw, cfg, [(uid, name) for uid, _eid, name in devs])
    projects = cap.fetch_project_names(
        kw, {pid for (_n, _w, pid) in actual} | {pid for (_n, _w, pid) in planned}
        | {t["project_id"][0] for t in data["tasks"] if t["project_id"]}
        | set(cfg["leave_project_ids"]))
    return {"devs": devs, "detail": detail, "future": future,
            "schedules": schedules, "leave": leave, "actual": actual,
            "planned": planned, "projects": projects, "scope": scope,
            "data": data, "n_slots": n_slots, "duplicated": duplicated}


def render(ctx, cfg):
    section_semaine(ctx["devs"], ctx["detail"], ctx["schedules"], ctx["leave"],
                    ctx["planned"], ctx["actual"], ctx["projects"], ctx["scope"],
                    ctx["data"], cfg)
    section_engagement(ctx["devs"], ctx["future"], ctx["schedules"], ctx["leave"],
                       ctx["planned"], ctx["projects"], ctx["scope"])
    unplanned = [name for _u, _e, name in ctx["devs"]
                 if not any(n == name and days for (n, _w, _p), days
                            in ctx["planned"].items())]
    if unplanned:
        print(f"\n_Aucune allocation sur la période pour {', '.join(unplanned)} — "
              f"100 % quickstart" +
              (f" (dont {', '.join(cfg['pool_devs'])}, renfort par conception)"
               if any(n in cfg["pool_devs"] for n in unplanned) else "") + "._\n")


def build(argv=None):
    ap = argparse.ArgumentParser(
        description="Capacité planifiée vs charge quickstart.")
    ap.add_argument("dev", nargs="?", help="un seul développeur (nom ou fragment)")
    ap.add_argument("--weeks-fwd", type=int, default=WEEKS_FWD,
                    help="semaines d'engagement après la semaine courante "
                         "(défaut : 5)")
    ap.add_argument("--today", help="date de référence (YYYY-MM-DD)")
    args = ap.parse_args(argv)
    today = (datetime.date.fromisoformat(args.today) if args.today
             else datetime.date.today())
    cfg = digest_config.load()
    kw = digest_config.connect(cfg)
    render(gather(kw, cfg, today, args.weeks_fwd, args.dev), cfg)


def main(argv=None):
    build(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
