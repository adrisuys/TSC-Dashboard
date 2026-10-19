#!/usr/bin/env python3
"""Le tableau de bord DEV TSC : une page, quatre sections.

    python3 dev_dashboard.py                  # page HTML + Markdown sur stdout
    python3 dev_dashboard.py --format md      # Markdown seul
    python3 dev_dashboard.py --no-open        # écrit la page sans l'ouvrir
    python3 dev_dashboard.py --today 2026-08-21 --days 180 --weeks-fwd 4

Ce que la page réunit, et pourquoi dans cet ordre :

  1. À PRENDRE     ce que personne ne s'est encore attribué        (dev_tasks_report)
  2. STATS         combien de temps tiennent les étapes Spec et To Do  (dev_stats_report)
  3. CAPACITÉ      planifié vs quickstart, semaine par semaine  (dev_quickstart_report)
  4. CHARGE        qui a quoi sur sa pile, et pour combien d'heures (dev_workload_report)

D'abord ce qui attend une décision, ensuite comment l'équipe tient ses délais,
puis la place disponible, enfin le détail ticket par ticket : on descend du
« quoi faire maintenant » vers le « pourquoi ».

Une seule authentification pour les quatre sections, et un seul jeu de chiffres :
la charge et la file quickstart sortent du même `dev_workload_report.collect()`,
le marquage 📌/⚡ du même plan d'allocation que la section capacité. Deux sections
de cette page ne peuvent donc pas annoncer deux restes à faire différents.

Le rapport est en français de bout en bout, comme /dev-capacity : il est lu par
l'équipe. Les docstrings, commentaires et messages d'erreur restent en anglais.

Read-only : search, search_read, read et read_group.
"""
import argparse
import datetime
import io
import os
import sys
import time
import traceback
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_quickstart_report as quickstart  # noqa: E402
import dev_stats_report as stats  # noqa: E402
import dev_tasks_report as tasks  # noqa: E402
import dev_workload_report as workload  # noqa: E402
import digest  # noqa: E402  (output_dir, open_in_browser, notify)
import digest_config  # noqa: E402
import md_to_html  # noqa: E402

TITLE = "DEV TSC — Tableau de bord"

# Dates en français sans dépendre de la locale du système : un job launchd tourne
# en C et rendrait « Friday 21 August » au milieu d'une page française.
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]


def date_fr(date, with_time=False):
    txt = "%s %d %s %d" % (JOURS[date.weekday()], date.day,
                           MOIS[date.month - 1], date.year)
    return f"{txt}, {date:%H:%M}" if with_time else txt

# Un ancrage par section : `<span id=…>` traverse md_to_html tel quel, ce qui
# donne un sommaire cliquable sans toucher au moteur de rendu (seuls les h3 y
# reçoivent un id, et les h2 des sections portent des compteurs qu'on ne veut
# pas recopier ici).
SECTIONS = [
    ("prendre", "À prendre", "tickets que personne n'a pris"),
    ("stats", "Délais", "temps passé en Spec/Estim et en To Do, tous tickets "
                        "confondus"),
    ("capacite", "Capacité vs quickstart", "qui peut encore prendre un ticket"),
    ("charge", "Charge par développeur", "détail des tickets et des heures"),
]


class ConfigError(Exception):
    """Non-retryable: bad credentials, missing tag, unknown developer."""


def anchor(key):
    print(f'<span id="{key}"></span>')


def sommaire():
    print()
    for key, title, hint in SECTIONS:
        print(f"- [{title}](#{key}) — {hint}")
    print()


def sections(args):
    """Print the four sections. Raises ConfigError for anything a retry cannot
    fix, so a scheduled run fails fast on a revoked key."""
    cfg = digest_config.load()
    kw = digest_config.connect(cfg)
    today = (datetime.date.fromisoformat(args.today) if args.today
             else datetime.date.today())

    # La section capacité rassemble le plan, les calendriers, les pointages et la
    # charge : tout le reste de la page se sert dans ce même contexte.
    ctx = quickstart.gather(kw, cfg, today, args.weeks_fwd)
    scope, data = ctx["scope"], ctx["data"]

    planned = sorted({ctx["projects"].get(pid, (f"#{pid}", False))[0]
                      for (_n, _w, pid), days in ctx["planned"].items()
                      if pid and days > 0})
    print(f"# {TITLE} — {date_fr(today)}\n")
    print(f"_{len(ctx['devs'])} développeur(s) · {ctx['n_slots']} créneaux "
          f"d'allocation · {len(data['counted_ids'])} tickets vivants "
          f"({len(data['parked_ids'])} parqués). Projets dans le plan de "
          f"capacité : {', '.join('📌 ' + p for p in planned) or '—'} ; "
          f"tout le reste est ⚡ quickstart._\n")
    sommaire()

    anchor("prendre")
    tasks.main(["todo"], lang="fr", scope=scope, kw=kw, cfg=cfg)
    anchor("stats")
    # full=False : chiffres d'ensemble et files d'attente, pas de découpage
    # nominatif. Un délai est surtout du temps de file, pas de l'effort : sur une
    # page lue par tout le monde, une médiane à côté d'un nom se lit comme un
    # reproche. Le détail par personne est dans /dev-delays, qu'on ouvre quand on
    # le cherche.
    stats.build((["--days", str(args.days)] if args.days else []) + ["--no-open"],
                kw=kw, cfg=cfg, scope=scope, today=today, full=False)
    anchor("capacite")
    quickstart.render(ctx, cfg)
    anchor("charge")
    # Sans le tableau récapitulatif : la section capacité ci-dessus porte déjà
    # une ligne par développeur, avec le temps qu'il peut encore prendre.
    workload.render(data, cfg, scope, lang="fr", leaderboard=False)


def build(args):
    """(status, markdown) — la page, ou l'erreur qui explique son absence."""
    buf = io.StringIO()
    last = ""
    for attempt in range(1, args.retries + 1):
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                sections(args)
            return "ok", buf.getvalue()
        except SystemExit as exc:
            # The reports call sys.exit(<message>) for anything a retry cannot fix.
            return "config", str(exc.code)
        except Exception:  # noqa: BLE001 — network/XML-RPC noise, worth a retry
            last = traceback.format_exc()
            if attempt < args.retries:
                time.sleep(args.retry_delay)
    return "network", last


def failure_page(status, detail, stamp, retries):
    if status == "config":
        lead = [f"> ⚠️ **Le tableau de bord n'est pas configuré** (vérifié {stamp}).",
                "> Odoo a répondu, mais le rapport n'a pas pu tourner. Cela ne se "
                "réglera pas tout seul — voir le détail ci-dessous, puis "
                "`/dev-digest-setup`."]
    else:
        lead = [f"> ⚠️ **Odoo est injoignable** après {retries} tentative(s) "
                f"(vérifié {stamp}).",
                "> Probablement pas de connexion. La prochaine exécution "
                "réessaiera."]
    return "\n".join([f"# {TITLE} — {stamp}", ""] + lead + [
        "", "<details><summary>Détail de l'erreur</summary>", "", "```",
        detail.rstrip(), "```", "", "</details>"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tableau de bord DEV TSC.")
    ap.add_argument("--format", choices=["md", "html"], default="html",
                    help="sortie enregistrée (défaut : html)")
    ap.add_argument("--no-open", dest="open_it", action="store_false",
                    help="ne pas ouvrir la page dans Chrome")
    ap.add_argument("--tab", dest="app", action="store_false",
                    help="ouvrir dans un onglet ordinaire plutôt qu'en fenêtre "
                         "d'application")
    ap.add_argument("--no-save", dest="save_files", action="store_false",
                    help="ne rien écrire sur le disque")
    ap.add_argument("--out-dir", default=digest.output_dir(),
                    help="répertoire des rapports enregistrés")
    ap.add_argument("--quiet", action="store_true",
                    help="ne pas écrire le Markdown sur stdout")
    ap.add_argument("--notify", action="store_true",
                    help="notification macOS en fin d'exécution")
    ap.add_argument("--today", help="date de référence (YYYY-MM-DD)")
    ap.add_argument("--days", type=int, default=None,
                    help="fenêtre des stats de délai, en jours (défaut : 90)")
    ap.add_argument("--weeks-fwd", type=int, default=quickstart.WEEKS_FWD,
                    help="semaines d'engagement après les deux détaillées "
                         "(défaut : 4)")
    ap.add_argument("--retries", type=int, default=3,
                    help="tentatives avant abandon (défaut : 3)")
    ap.add_argument("--retry-delay", type=int, default=30,
                    help="secondes entre deux tentatives (défaut : 30)")
    args = ap.parse_args(argv)

    stamp = date_fr(datetime.datetime.now(), with_time=True)
    status, body = build(args)
    markdown = body if status == "ok" else failure_page(status, body, stamp,
                                                        args.retries)

    if not args.quiet:
        print(markdown)

    if args.save_files:
        os.makedirs(args.out_dir, exist_ok=True)
        # La page HTML est le livrable, et le format par défaut : un seul fichier
        # par exécution, réécrit à chaque fois. Pas de copie datée — le tableau
        # de bord est rebâti plusieurs fois par jour, une copie datée ne
        # garderait que la dernière de la journée. `--format md` écrit le
        # Markdown à la place, pour un lecteur en terminal.
        path = os.path.join(args.out_dir, f"dashboard.{args.format}")
        content = (md_to_html.render(markdown, title=TITLE)
                   if args.format == "html" else markdown)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        # stderr, pour que le Markdown de stdout reste collable tel quel.
        print(f"Enregistré : {path}", file=sys.stderr)
        if args.open_it:
            # Fenêtre d'application : sans onglets ni barre d'adresse, le
            # tableau de bord est une page qu'on garde ouverte, pas un onglet de
            # navigation. Les liens cliqués dedans partent quand même en onglets
            # Chrome ordinaires. `--tab` rend l'onglet classique.
            digest.open_in_browser(path, app=args.app)

    if args.notify:
        digest.notify(status, "dashboard")
    return {"ok": 0, "network": 1, "config": 2}[status]


if __name__ == "__main__":
    sys.exit(main())
