#!/usr/bin/env python3
"""Tab 0 of the web app — "Vue d'ensemble": the six questions the dashboard
answers, one card each, in reading order:

  1. Tickets à prendre        what is ready to be picked up
  2. En cours                 what people are working on
  3. Dispo maintenant         who can take a ticket this week
  4. Dispo à venir            who has room in the coming weeks
  5. Facturable               where we stand against the target
  6. Feuilles de temps        is everybody up to date

Each card is an overview, not an analysis: a status (ok / warn / err), one
headline figure, and at most a few named pain points, with a link to the tab
that holds the detail. A card turns orange or red only on something worth
looking at — the rules sit next to each card below.

Built from the other tabs' own view models (`dev_travail_view`,
`dev_capacite_view`, `dev_timesheet_view`), so this page cannot disagree with
them. No Odoo call of its own.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_timesheet_report as ts_report  # noqa: E402

# How many pain points a card names before summing up the rest.
TOP = 3
# Weeks shown ahead on "Dispo à venir", after the current one.
AHEAD_WEEKS = 4


def _worst(*states):
    order = {"ok": 0, "warn": 1, "err": 2}
    return max(states, key=order.get) if states else "ok"


def _items(rows, fmt):
    """At most TOP formatted pain points, then "+n autres"."""
    out = [fmt(r) for r in rows[:TOP]]
    if len(rows) > TOP:
        out.append({"text": f"+{len(rows) - TOP} autre(s)", "muted": True})
    return out


def tickets(tv):
    """Tickets à prendre — red when 3+ tickets sit in their stage past the
    limit, orange from one."""
    rows = [r | {"stage": g["stage"]} for g in tv["groups"] for r in g["rows"]]
    stale = sorted((r for r in rows if r["stale"]), key=lambda r: -r["age"])
    k = tv["kpi"]
    state = "err" if len(stale) >= 3 else "warn" if stale else "ok"
    return {
        "key": "tickets", "title": "Tickets à prendre", "href": "/travail",
        "state": state, "value": str(k["unclaimed"]),
        "sub": (f"{k['unclaimed_spec']} en spec/estim · "
                f"{k['unclaimed'] - k['unclaimed_spec']} en To Do"),
        "lead": (f"{len(stale)} traîne(nt) dans leur étape" if stale
                 else "Rien ne traîne dans son étape."),
        "items": _items(stale, lambda r: {
            "text": f"#{r['id']} {r['name']}", "url": r["url"],
            "meta": f"{r['stage']} · {r['age']:.0f} j", "err": True}),
    }


def en_cours(tv):
    """En cours — orange on a ticket past its estimate or a developer with
    nothing in progress; red from three tickets past their estimate."""
    over = sorted(({"dev": d["name"], **r} for d in tv["load"] for r in d["rows"]
                   if r["counted"] and r["over"]), key=lambda r: r["left"])
    idle = [d["name"] for d in tv["load"] if d["tasks"] == 0]
    k = tv["kpi"]
    state = "err" if len(over) >= 3 else "warn" if (over or idle) else "ok"
    items = [{"text": f"{n} n'a aucun ticket en cours", "warn": True} for n in idle]
    items += _items(over, lambda r: {
        "text": f"#{r['id']} {r['name']}", "url": r["url"],
        "meta": f"{r['dev']} · {-r['left']:+.0f} h sur l'estimé"})
    return {
        "key": "encours", "title": "En cours", "href": "/travail",
        "state": state, "value": f"{k['live']}",
        "sub": f"tickets · {k['left']:.0f} h de reste à brûler"
               + (f" · {k['unest']} sans estimation" if k["unest"] else ""),
        "lead": (f"{len(over)} ticket(s) au-delà de l'estimé" if over
                 else "Aucun ticket au-delà de son estimé."),
        "people": [{"name": d["name"], "value": f"{d['tasks']} · {d['left']:.0f} h",
                    "pct": d["left_pct"]} for d in tv["load"]],
        "items": items,
    }


def dispo_maintenant(cv):
    """Dispo maintenant — green while someone has room this week; orange when
    the team queue exceeds its free time but someone still has room; red when
    nobody has."""
    sem = cv["sem"]
    rows = sorted(sem["rows"], key=lambda r: -r["allocable"])
    room = [r for r in rows if r["state"] == "ok" and r["allocable"] > 0.5]
    total = sem["totals"]["allocable"]
    state = "err" if not room else "warn" if total < 0 else "ok"
    return {
        "key": "maintenant", "title": "Dispo maintenant", "href": "/capacite",
        "state": state, "value": f"{total:+.0f} h",
        "sub": (f"allouables {sem['label']} · {sem['totals']['free']:.0f} h libres "
                f"pour {sem['totals']['queue']:.0f} h de quickstart en file"),
        "lead": (f"Peut prendre un ticket : {', '.join(r['name'] for r in room)}" if room
                 else "Personne n'a de place cette semaine."),
        "people": [{"name": r["name"],
                    "value": ("plein" if r["state"] == "full"
                              else f"{r['allocable']:+.0f} h"),
                    "state": r["state"]} for r in rows],
        "items": [],
    }


def dispo_a_venir(cv):
    """Dispo à venir — free days per developer over the next weeks (the plan
    alone). Orange on a project with dev work left and no dev planned, red
    from three."""
    rows = []
    labels = cv["future_labels"][1:1 + AHEAD_WEEKS]
    for r in cv["forward"]:
        cells = r["cells"][1:1 + AHEAD_WEEKS]
        rows.append({"name": r["name"], "pool": r["pool_idle"],
                     "cells": [{"free": max(c["available"] - c["days"], 0.0),
                                "available": c["available"]} for c in cells]})
    no_dev = sorted((p for p in cv["runway"] if p["state"] == "none"),
                    key=lambda p: -p["left"])
    state = "err" if len(no_dev) >= 3 else "warn" if no_dev else "ok"
    total = sum(c["free"] for r in rows if not r["pool"] for c in r["cells"])
    return {
        "key": "avenir", "title": "Dispo à venir", "href": "/capacite#engagement",
        "state": state, "value": f"{total:.0f} j",
        "sub": f"libres au plan sur {len(labels)} semaines ({labels[0]} → {labels[-1]})"
               if labels else "aucune semaine à venir",
        "lead": (f"{len(no_dev)} projet(s) avec du reste à faire et aucun dev planifié"
                 if no_dev else "Tout reste à faire a du dev planifié."),
        "grid": {"labels": labels, "rows": rows},
        "items": _items(no_dev, lambda p: {
            "text": p["project"], "meta": f"{p['left']:.0f} h de reste"}),
    }


def _band(pct):
    """Colour of a billable % — the timesheet report's target and amber line."""
    if pct is None:
        return "none"
    return ("ok" if pct >= ts_report.TARGET
            else "warn" if pct >= ts_report.AMBER_BELOW else "err")


def facturable(tv_temps):
    """Facturable — the badge of the population the target applies to (the
    Belgian employees, `cfg["target_devs"]`); the people below the target and
    dropping are named, then billable work booked non-billable. Everyone else
    is listed in neutral, for information."""
    f, nf = tv_temps["facturable"], tv_temps["non_facturable"]
    t = f["goal"] or f["team"]
    state = ({"none": "warn"}.get(_band(t["strict"]), _band(t["strict"]))
             if f["goal"] else "ok")
    leaks = sum(x["hours"] for x in nf["leaks"])
    items = [{"text": f"{w['name']} : {w['recent']:.0f} % sur {tv_temps['recent_days']} j",
              "meta": f"{w['delta']:+.1f} pts", "warn": True} for w in f["watch"]]
    if leaks >= 1:
        items.append({"text": f"{leaks:.0f} h travaillées pour un client mais "
                              "typées non facturables", "meta": "à corriger",
                      "warn": True})
    proj = tv_temps["tendance"].get("projection")
    sub = (f"🎯 {f['goal_label']} · objectif {tv_temps['target']:.0f} % · "
           f"{tv_temps['window_label']}" if f["goal"]
           else f"hors objectif · {tv_temps['window_label']}")
    if t["recent"] is not None and tv_temps["recent_days"]:
        sub += f" · {t['recent']:.0f} % sur {tv_temps['recent_days']} j"
    if proj and proj.get("needed") is not None:
        sub += (" · fin d'année hors d'atteinte" if proj["needed"] > 100
                else f" · {proj['needed']:.0f} % requis d'ici fin d'année")
    return {
        "key": "facturable", "title": "Facturable", "href": "/temps#facturable",
        "state": state,
        "value": "—" if t["strict"] is None else f"{t['strict']:.0f} %",
        "sub": sub,
        "lead": (f"Sous l'objectif et en recul : {', '.join(w['name'] for w in f['watch'])}"
                 if f["watch"] else "Personne n'est à la fois sous l'objectif et en recul.")
                + (f" Équipe entière : {f['team']['strict']:.0f} % (info)."
                   if f["goal"] and f["others"] and f["team"]["strict"] is not None
                   else ""),
        # The ones under the target first; the others in neutral, no mark.
        "people": [{"name": ("🎯 " if r["goal"] else "") + r["name"],
                    "value": "—" if r["strict"] is None else f"{r['strict']:.0f} %",
                    "state": _band(r["strict"]) if r["goal"] else "none",
                    "pct": min(r["strict"] or 0, 100),
                    "mark": tv_temps["target"] if r["goal"] else None}
                   for r in sorted(f["rows"], key=lambda r: not r["goal"])],
        "items": items,
    }


def feuilles(tv_temps):
    """Feuilles de temps — the fill status of each developer over the last
    working days (dev_timesheet_report rules): red as soon as one is behind."""
    rows = tv_temps["tenue"]["rows"]
    late = [r for r in rows if r["status"] != "ok"]
    up = len(rows) - len(late)
    state = _worst(*[r["status"] for r in rows]) if rows else "ok"
    return {
        "key": "feuilles", "title": "Feuilles de temps", "href": "/temps#tenue",
        "state": state, "value": f"{up}/{len(rows)}",
        "sub": f"à jour · couverture équipe {tv_temps['tenue']['team_coverage'] or 0:.0f} %",
        "lead": ("Tout le monde est à jour." if not late
                 else f"À relancer : {', '.join(r['name'] for r in late)}"),
        "items": [{"text": f"{r['name']} : {r['holes']} jour(s) à rattraper",
                   "meta": f"dernier jour pointé {r['last']}",
                   "warn": r["status"] == "warn", "err": r["status"] == "err"}
                  for r in late],
    }


def a_corriger(cv, tv_temps):
    """"Données à corriger" — only what makes a figure wrong, never context."""
    out = []
    c = tv_temps["conges"]
    if c["rows"]:
        who = sorted({r["name"] for r in c["rows"]})
        out.append({"text": f"{c['total']:.0f} h de congé pointées hors du projet "
                            f"Congés ({', '.join(who)})", "url": c["all_url"]})
    excluded = tv_temps["facturable"]["excluded"]
    if excluded:
        out.append({"text": "Exclus des totaux facturables, feuille incomplète : "
                            + ", ".join(excluded)})
    out += [{"html": n["html"]} for n in cv["notes"] if n["warn"]]
    return out


def build(travail_vm, capacite_vm, temps_vm):
    cards = [tickets(travail_vm), en_cours(travail_vm),
             dispo_maintenant(capacite_vm), dispo_a_venir(capacite_vm),
             facturable(temps_vm), feuilles(temps_vm)]
    return {"cards": cards, "fixes": a_corriger(capacite_vm, temps_vm),
            "target": ts_report.TARGET}
