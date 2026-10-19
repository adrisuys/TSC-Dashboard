#!/usr/bin/env python3
"""The periods the web app offers, in one place: a key, a button label, and
the dates it covers. Every period picker on every tab draws from this list,
so "Ce mois" means the same dates everywhere.

Two kinds:

- **calendar** — this week, last month, this quarter, this year… A running
  one stops at today, today included; a past one is complete.
- **rolling** — the last N days, ending today.

Each section offers only the periods that make sense for it (a creation
cohort read over "cette semaine" would only hold the fastest tickets, a
billable rate cannot cover a period that ended); `choices(keys)` builds the
picker's list for it.
"""
import datetime

# "Depuis toujours" still needs a floor for the queries; nobody here logged
# time in Odoo before it.
EPOCH = datetime.date(2015, 1, 1)

# key -> button label, in the order a picker shows them.
LABELS = {
    "week": "Cette semaine", "last_week": "Semaine dernière",
    "month": "Ce mois", "last_month": "Mois dernier",
    "quarter": "Ce trimestre", "last_quarter": "Trimestre dernier",
    "year": "Cette année", "last_year": "Année dernière",
    "all": "Depuis toujours",
    "30d": "30 derniers jours", "90d": "90 derniers jours",
    "180d": "180 derniers jours", "365d": "365 derniers jours",
}

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]


def choices(keys):
    """[(key, label)] for a picker, in the order given."""
    return [(k, LABELS[k]) for k in keys]


def rolling_days(key):
    """30 for "30d", None for a calendar period."""
    return int(key[:-1]) if key.endswith("d") and key[:-1].isdigit() else None


def from_legacy(value, allowed, default):
    """A key from the query string, accepting the old bare day counts ("90")
    so bookmarks made before the pickers were unified still land."""
    if value and value.isdigit():
        value = f"{value}d"
    return value if value in allowed else default


def _quarter_start(d):
    return d.replace(month=3 * ((d.month - 1) // 3) + 1, day=1)


def period_range(key, today):
    """(first day, last day) — both included. A running period ends today."""
    days = rolling_days(key)
    if days:
        return today - datetime.timedelta(days=days), today
    monday = today - datetime.timedelta(days=today.weekday())
    first_of_month = today.replace(day=1)
    if key == "week":
        return monday, today
    if key == "last_week":
        return monday - datetime.timedelta(days=7), monday - datetime.timedelta(days=1)
    if key == "month":
        return first_of_month, today
    if key == "last_month":
        end = first_of_month - datetime.timedelta(days=1)
        return end.replace(day=1), end
    if key == "quarter":
        return _quarter_start(today), today
    if key == "last_quarter":
        end = _quarter_start(today) - datetime.timedelta(days=1)
        return _quarter_start(end), end
    if key == "year":
        return today.replace(month=1, day=1), today
    if key == "last_year":
        return datetime.date(today.year - 1, 1, 1), datetime.date(today.year - 1, 12, 31)
    if key == "all":
        return EPOCH, today
    raise ValueError(f"période inconnue : {key}")


def window_text(key, today):
    """(label, label after "de") for a period that runs to today — French
    needs "de l'année 2026" but "des 90 derniers jours" and "d'octobre 2026",
    and no rule derives one from the other."""
    days = rolling_days(key)
    if days:
        return f"{days} derniers jours", f"des {days} derniers jours"
    if key == "month":
        name = f"{MOIS[today.month - 1]} {today.year}"
        return name, ("d'" if name[0] in "aeiouy" else "de ") + name
    if key == "quarter":
        name = f"T{(today.month - 1) // 3 + 1} {today.year}"
        return name, f"du {name}"
    if key == "year":
        return f"année {today.year}", f"de l'année {today.year}"
    raise ValueError(f"période non glissante jusqu'à aujourd'hui : {key}")
