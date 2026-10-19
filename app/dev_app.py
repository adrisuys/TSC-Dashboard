#!/usr/bin/env python3
"""L'application DEV TSC : trois onglets, une seule page.

    python3 dev_app.py                     # la page, ouverte dans Chrome
    python3 dev_app.py --no-open           # écrite, pas ouverte
    python3 dev_app.py --format md         # les trois onglets à la suite
    python3 dev_app.py --today 2026-08-25 --days 90

Quatre questions, une par onglet, et rien d'autre :

  1  LE TRAVAIL      ce qui attend, et qui porte quoi
  2  CAPACITÉ        combien de place il reste, et si le plan tient
  3  FEUILLES        les pointages sont-ils tenus, et vendus
  4  DÉLAIS          combien de temps les étapes retiennent un ticket

Les onglets ne sont pas un habillage du tableau de bord : c'est ce qui permet
aux onglets 2 et 4 de porter le rapport *entier* — capacité avec occupation,
adhérence, engagement et runway ; délais avec le découpage par analyste et par
assigné — au lieu de leur seule table de tête. Sur une page qui déroule, ces
sections enterrent tout ce qui les précède ; derrière un onglet, elles ne
coûtent rien à qui ne les ouvre pas. Le poids va derrière un onglet plutôt que
sur la page.

C'est aussi ce qui rend l'onglet 4 tenable. Le tableau de bord ne porte que les
médianes d'ensemble, délibérément : sur une page qui déroule, une médiane à côté
d'un nom se lit comme un reproche avant d'être lue comme un chiffre. Derrière un
onglet, on va la chercher — et ce n'est pas la même chose de tomber sur un
classement que d'ouvrir un diagnostic. Le découpage nominatif se lit donc ici
comme dans `/dev-delays` : pour comprendre où les tickets attendent, jamais pour
classer des personnes.

`dev_dashboard.py` n'est pas touché : il reste la page des quatre sections qui
déroulent, et `/dev-delays` reste la page des délais seuls.

Une seule authentification pour les trois onglets, et un seul jeu de chiffres :
la charge, la file quickstart et le marquage 📌/⚡ sortent tous du contexte
`dev_quickstart_report.gather()`, comme dans le tableau de bord. Deux onglets de
cette page ne peuvent donc pas annoncer deux restes à faire différents.

Read-only : search, search_read, read et read_group.
"""
import argparse
import datetime
import io
import os
import sys
import time
import traceback
import urllib.request
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_capacity_report as capacity  # noqa: E402
import dev_quickstart_report as quickstart  # noqa: E402
import dev_stats_report as stats  # noqa: E402
import dev_tasks_report as tasks  # noqa: E402
import dev_timesheet_report as timesheets  # noqa: E402
import dev_workload_report as workload  # noqa: E402
import digest  # noqa: E402
import digest_config  # noqa: E402
import md_to_html  # noqa: E402

TITLE = "DEV TSC"

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]


def date_fr(date, with_time=False):
    txt = "%s %d %s %d" % (JOURS[date.weekday()], date.day,
                           MOIS[date.month - 1], date.year)
    return f"{txt}, {date:%H:%M}" if with_time else txt


class Capture:
    """Collect one tab's Markdown without letting it reach stdout."""

    def __init__(self):
        self.buf = io.StringIO()

    def __enter__(self):
        self._redirect = redirect_stdout(self.buf)
        self._redirect.__enter__()
        return self

    def __exit__(self, *exc):
        return self._redirect.__exit__(*exc)

    @property
    def text(self):
        return self.buf.getvalue()


# Tab 1 counts work; it does not measure the room to do it. Saying so where the
# stock of hours is printed is what keeps someone from reading "8 h de reste" as
# "free this week".
CHARGE_NOTE = ("_Cet onglet dit **ce qui reste à faire**, pas de combien de "
               "temps chacun dispose pour le faire : un reste à faire n'est pas "
               "une disponibilité tant qu'on ne l'a pas confronté au régime du "
               "développeur, à ses congés et aux jours déjà promis à un projet. "
               "**C'est l'onglet 2 qui répond à ça**, avec sa colonne « Temps "
               "allouable »._")


def tab_travail(ctx, kw, cfg):
    """1 — ce qui attend une décision, puis qui porte déjà quoi."""
    scope, data = ctx["scope"], ctx["data"]
    with Capture() as out:
        print("_Ce que personne n'a pris, puis ce que chacun porte. Les tickets "
              "📌 sont déjà placés dans le plan de capacité ; les ⚡ quickstart, "
              "en caractères normaux, sont ceux qui restent à caser._\n")
        tasks.main(["todo"], lang="fr", scope=scope, kw=kw, cfg=cfg)
        # show_load=False: la barre de charge mesure le reste à faire contre une
        # semaine de 40 h forfaitaire — un modèle de capacité, et un mauvais. La
        # vraie réponse est dans l'onglet 2, et deux réponses valent moins qu'une.
        # fold=True: la fiche de chaque développeur est repliée. Le classement
        # au-dessus porte déjà le nom, le nombre de tickets et le reste à
        # brûler ; le détail ligne à ligne ne se lit qu'une fois qu'on a choisi
        # de qui. Replié, l'onglet tient sur un écran et le sommaire des six
        # développeurs redevient lisible d'un coup d'œil ; un nom du classement
        # ouvre la fiche visée.
        workload.render(data, cfg, scope, lang="fr", leaderboard=True,
                        show_load=False, extra_note=CHARGE_NOTE, fold=True)
    return out.text


def tab_capacite(ctx, cfg, args):
    """2 — la place de la semaine, puis le plan confronté au réel.

    Les deux, et dans cet ordre : la première table répond « qui peut prendre ce
    ticket » en dix secondes, le rapport qui suit dit si l'on peut croire la
    réponse — un développeur libre sur le papier mais à 105 % d'occupation ne
    l'est pas."""
    with Capture() as deep:
        # fold=True, comme l'onglet 1 : la fiche d'adhérence de chaque
        # développeur est repliée sur sa ligne de synthèse. Six blocs de deux
        # tables enterraient les sections C à E — engagement, autonomie, qualité
        # des données — au milieu de l'onglet.
        capacity.build(["--weeks-back", str(args.weeks_back),
                        "--weeks-fwd", str(args.weeks_fwd),
                        "--today", ctx["today"].isoformat()],
                       kw=ctx["kw"], cfg=cfg, fold=True)
    with Capture() as out:
        quickstart.render(ctx, cfg)
        print("\n---\n")
        print(demote_title(deep.text))
    return out.text


def demote_title(markdown):
    """Drop a standalone report's own `# ` title, keeping its scope line.

    `dev_capacity_report` is a page in its own right and opens with an H1. Left
    alone it plants a second page title halfway down tab 2, at the same weight
    as the application's own — the reader reads it as the start of a new
    document. Only the first line is touched, and only if it is an H1: the
    sections below keep their levels."""
    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        return "\n".join(lines[i + 1:]) if line.startswith("# ") else markdown
    return markdown


def tab_delais(ctx, cfg, args):
    """4 — combien de temps un ticket reste bloqué dans Spec/Estim et To Do.

    `full=True`, contrairement au tableau de bord : le découpage par analyste et
    par assigné est tout l'intérêt d'un onglet qu'on ouvre exprès. Le tableau de
    bord s'en tient aux médianes d'ensemble parce qu'il déroule sous les yeux de
    tout le monde ; ici il faut cliquer, et ce qu'on vient chercher est
    précisément « où ça bloque, et chez qui »."""
    with Capture() as body:
        # fold=True : le résumé d'ensemble — les médianes par étape — reste
        # ouvert ; tendance, découpages nominatifs, tickets les plus lents et
        # listes d'attente se déplient. C'est aussi ce qui convient au ton de
        # l'onglet : un découpage par personne qu'on ouvre exprès se lit comme
        # un diagnostic, pas comme un classement affiché.
        stats.build((["--days", str(args.days_delays)] if args.days_delays else [])
                    + ["--no-open"],
                    kw=ctx["kw"], cfg=cfg, scope=ctx["scope"],
                    today=ctx["today"], full=True, fold=True)
    with Capture() as out:
        print("_Un délai d'étape est surtout du **temps de file**, pas de "
              "l'effort : un ticket qui dort trois semaines en Spec n'a pas "
              "coûté trois semaines de travail à son analyste. Les découpages "
              "par personne ci-dessous se lisent comme un diagnostic — où les "
              "tickets attendent — et jamais comme un classement._\n")
        print(demote_title(body.text))
    return out.text


def tab_temps(kw, cfg, today, days, recent):
    """3 — les pointages : tenus d'abord, facturables ensuite.

    Deux fenêtres côte à côte, parce qu'il y a deux questions. L'année civile
    (`days=None`, le défaut) juge l'objectif annuel de 85 % : il doit repartir
    de zéro au 1er janvier plutôt que traîner l'année précédente derrière lui.
    Les `recent` derniers jours décrivent l'équipe d'aujourd'hui, et c'est de
    là que sort la liste « à suivre de près ». `--days N` remplace les deux par
    une seule fenêtre glissante."""
    with Capture() as out:
        tctx = timesheets.gather(kw, cfg, today, days, recent)
        print(f"_{tctx['n_lines']} lignes de pointage lues. La tenue des "
              "feuilles vient avant le ratio facturable, et pas l'inverse : un "
              "ratio calculé sur des journées manquantes mesure qui pense à "
              "cliquer, pas comment l'équipe vend son temps._\n")
        # fold=True : les deux sections de tête — tenue, puis facturable —
        # restent ouvertes, ce sont les deux questions de l'onglet. La tendance,
        # le détail du non facturable, les congés mal pointés et les notes se
        # déplient.
        timesheets.render(tctx, cfg, fold=True)
    return out.text


def sections(args):
    """(header, [(key, label, markdown)]). Raises SystemExit for anything a
    retry cannot fix, so a scheduled run fails fast on a revoked key."""
    cfg = digest_config.load()
    kw = digest_config.connect(cfg)
    today = (datetime.date.fromisoformat(args.today) if args.today
             else datetime.date.today())

    ctx = quickstart.gather(kw, cfg, today, args.weeks_fwd)
    ctx["kw"], ctx["today"] = kw, today

    planned = sorted({ctx["projects"].get(pid, (f"#{pid}", False))[0]
                      for (_n, _w, pid), days in ctx["planned"].items()
                      if pid and days > 0})
    header = (
        f"# {TITLE} — {date_fr(today)}\n\n"
        f"_{len(ctx['devs'])} développeur(s) · {ctx['n_slots']} créneaux "
        f"d'allocation · {len(ctx['data']['counted_ids'])} tickets vivants "
        f"({len(ctx['data']['parked_ids'])} parqués). Projets dans le plan de "
        f"capacité : {', '.join('📌 ' + p for p in planned) or '—'} ; tout le "
        f"reste est ⚡ quickstart._\n")

    return header, [
        ("travail", "Le travail", tab_travail(ctx, kw, cfg)),
        ("capacite", "Capacité", tab_capacite(ctx, cfg, args)),
        ("temps", "Feuilles de temps",
         tab_temps(kw, cfg, today, args.days, args.recent)),
        ("delais", "Délais", tab_delais(ctx, cfg, args)),
    ]


def build(args):
    """(status, header, tabs) — la page, ou l'erreur qui explique son absence."""
    last = ""
    for attempt in range(1, args.retries + 1):
        try:
            header, tabs = sections(args)
            return "ok", header, tabs
        except SystemExit as exc:
            return "config", str(exc.code), []
        except Exception:  # noqa: BLE001 — network/XML-RPC noise, worth a retry
            last = traceback.format_exc()
            if attempt < args.retries:
                time.sleep(args.retry_delay)
    return "network", last, []


def failure_markdown(status, detail, stamp, retries):
    if status == "config":
        lead = [f"> ⚠️ **L'application n'est pas configurée** (vérifié {stamp}).",
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


WEBAPP_URL = f"http://127.0.0.1:{digest_config.RESYNC_PORT}/"


def refresh_webapp():
    """Ask the local web app (`dev_webapp.py`) to refresh its data now, in the
    background. False when it is not running — the caller then falls back to
    the static page."""
    req = urllib.request.Request(WEBAPP_URL + "refresh", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except OSError:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="L'application DEV TSC, en onglets.")
    ap.add_argument("--format", choices=["md", "html"], default="html",
                    help="sortie enregistrée (défaut : html)")
    ap.add_argument("--no-open", dest="open_it", action="store_false",
                    help="ne pas ouvrir la page dans Chrome")
    ap.add_argument("--tab", dest="app", action="store_false",
                    help="ouvrir dans un onglet ordinaire plutôt qu'en fenêtre "
                         "d'application")
    ap.add_argument("--refresh", dest="focus", action="store_false",
                    help="recharger la fenêtre déjà ouverte sans la mettre au "
                         "premier plan, et n'en ouvrir aucune s'il n'y en a "
                         "pas (mode du travail planifié)")
    ap.add_argument("--no-save", dest="save_files", action="store_false")
    ap.add_argument("--out-dir", default=digest.output_dir())
    ap.add_argument("--quiet", action="store_true",
                    help="ne pas écrire le Markdown sur stdout")
    ap.add_argument("--notify", action="store_true",
                    help="notification macOS à chaque exécution")
    ap.add_argument("--notify-errors", dest="notify_errors",
                    action="store_true",
                    help="notification macOS uniquement lorsque l'exécution "
                         "échoue, et seulement au passage en échec (mode du "
                         "travail planifié)")
    ap.add_argument("--today", help="date de référence (YYYY-MM-DD)")
    ap.add_argument("--days", type=int, default=None,
                    help="remplace les deux fenêtres du ratio facturable par "
                         "une seule, glissante, de N jours (défaut : année "
                         f"civile + {timesheets.RECENT_WINDOW} jours côte à côte)")
    ap.add_argument("--recent", type=int, default=timesheets.RECENT_WINDOW,
                    help="fenêtre « situation actuelle » de l'onglet 3, en "
                         f"jours (défaut : {timesheets.RECENT_WINDOW})")
    ap.add_argument("--days-delais", dest="days_delays", type=int, default=None,
                    help="fenêtre des statistiques de délai, en jours "
                         "(défaut : celle de la config, 90)")
    ap.add_argument("--weeks-back", type=int, default=4,
                    help="semaines d'historique de capacité (défaut : 4)")
    ap.add_argument("--weeks-fwd", type=int, default=quickstart.WEEKS_FWD,
                    help="semaines d'engagement à venir "
                         f"(défaut : {quickstart.WEEKS_FWD})")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--retry-delay", type=int, default=30)
    args = ap.parse_args(argv)

    stamp = date_fr(datetime.datetime.now(), with_time=True)
    status, header, tabs = build(args)
    if status != "ok":
        header, tabs = "", [("erreur", "Erreur",
                             failure_markdown(status, header, stamp, args.retries))]

    if not args.quiet:
        # En terminal, les onglets n'existent pas : on déroule les trois, chacun
        # sous son titre, plutôt que d'imprimer le seul premier.
        print(header)
        for _key, label, markdown in tabs:
            print(f"\n{'=' * 72}\n{label}\n{'=' * 72}\n")
            print(markdown)

    if args.save_files:
        os.makedirs(args.out_dir, exist_ok=True)
        path = os.path.join(args.out_dir, f"app.{args.format}")
        if args.format == "html":
            content = md_to_html.render_tabs(
                tabs, title=TITLE, header=header,
                resync_port=digest_config.RESYNC_PORT)
        else:
            content = header + "\n".join(f"\n# {label}\n\n{md}"
                                         for _k, label, md in tabs)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"Enregistré : {path}", file=sys.stderr)
        # The web app is the page now: a foreground run asks it to refresh
        # (in the background — it reads Odoo with its own, native views) and
        # shows it. The static file is still written, and opened only when the
        # web app is not running.
        if (args.open_it and args.focus and args.format == "html"
                and status == "ok" and refresh_webapp()):
            digest.open_url_app(WEBAPP_URL, focus=True)
            print(f"Application : {WEBAPP_URL}", file=sys.stderr)
        elif args.open_it:
            digest.open_in_browser(path, app=args.app, focus=args.focus)

    if args.notify:
        digest.notify(status, "app")
    elif args.notify_errors:
        digest.notify_failure_once(status, "app")
    return {"ok": 0, "network": 1, "config": 2}[status]


if __name__ == "__main__":
    sys.exit(main())
