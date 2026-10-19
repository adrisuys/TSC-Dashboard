#!/usr/bin/env python3
"""Render the digest's Markdown as a standalone HTML page.

Deliberately not a general Markdown implementation: it covers exactly what the
digest emits — headings, tables (with escaped pipes), bullet lists, links,
bold, italics, code spans, blockquotes, fenced code and the handful of inline
HTML tags the reports use for greying out low-priority rows.

The point is that the scheduled job can hand a manager something readable in a
browser instead of a .md file that opens in TextEdit.
"""
import contextlib
import re
import unicodedata

# Tags the reports emit on purpose. Anything else that looks like a tag is
# escaped, so an angle bracket typed into an Odoo task name stays inert text.
ALLOWED_HTML = re.compile(
    r"</?(?:span|details|summary|br)(?:\s[^<>]*)?/?>", re.IGNORECASE)

RAW_ID = re.compile(r'\bid="([^"]+)"')
CODE_SPAN = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
# An underscore only opens emphasis at a word boundary, so identifiers like
# foo_bar survive untouched. Escaped underscores are allowed *inside* the span:
# the workload note is one long italic sentence that mentions dev\_time.
# The two alternatives are disjoint on purpose — an ambiguous `\\_|[^_\n]`
# backtracks exponentially on a line that never closes its emphasis.
ITALIC = re.compile(r"(?<![\w\\])_((?:[^_\n\\]|\\.)+?)_(?!\w)")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# Split a table row on pipes that are not backslash-escaped.
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
# A list item: "- text", "* text", "+ text". The space is required, so a
# paragraph opening on "**bold**" or a "---" rule is not read as a bullet.
BULLET = re.compile(r"^[-*+]\s+(.*)$")

SENTINEL = "\x00"

TAGS = re.compile(r"<[^>]+>")
# "3.8h", "-8.5h", "42", "12%", "322.0h (40.2d)" — a figure, optionally with a
# unit and a parenthetical conversion. Anything else (a name, a stage, a load
# bar, a timestamp like 2026-08-19 08:46:42) is text.
NUMERIC_CELL = re.compile(r"^-?[\d.,]+\s*(?:h|d|%)?(?:\s*\([^)]*\))?$")
BLANK_CELL = {"", "—", "-"}

# A section heading names its subject before the dash: "Nathan Giacomello — 5
# tasks · 190.0h left of …". Everything after it is a summary that changes on
# every run, so the anchor is built from the name alone and stays stable.
HEADING_SUBJECT = re.compile(r"\s+[—–-]\s+")
# Columns holding a person's name, which is what an anchor can be matched to.
# Deliberately not col-task: a title should never turn itself into a link.
LINKABLE_COLUMNS = {"col-developer", "col-developer-s", "col-dev-analyst",
                    "col-developpeur"}


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link(href, label):
    """An Odoo link opens in its own tab; a `#section` link must stay in the page.
    Without the distinction a table of contents opened four blank tabs."""
    if href.startswith("#"):
        return f'<a href="{href}">{label}</a>'
    return f'<a href="{href}" target="_blank" rel="noopener">{label}</a>'


def inline(text):
    """Render inline Markdown, keeping code spans and allowed HTML verbatim."""
    kept = []

    def stash(html):
        kept.append(html)
        return f"{SENTINEL}{len(kept) - 1}{SENTINEL}"

    text = CODE_SPAN.sub(lambda m: stash(f"<code>{_escape(m.group(1))}</code>"), text)
    text = ALLOWED_HTML.sub(lambda m: stash(m.group(0)), text)
    text = _escape(text)
    text = LINK.sub(lambda m: _link(m.group(2), m.group(1)), text)
    text = BOLD.sub(r"<strong>\1</strong>", text)
    text = ITALIC.sub(r"<em>\1</em>", text)
    text = text.replace("\\|", "|").replace("\\_", "_")
    return re.sub(rf"{SENTINEL}(\d+){SENTINEL}", lambda m: kept[int(m.group(1))], text)


def _cells(row):
    parts = UNESCAPED_PIPE.split(row.strip())
    # A leading and trailing pipe produce empty edge fields; drop them.
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return [p.strip() for p in parts]


def _plain(cell):
    """Cell text with markup and the overrun marker stripped, for classifying."""
    return TAGS.sub("", cell).replace("⚠️", "").strip()


def _is_number(cell):
    text = _plain(cell)
    return text in BLANK_CELL or bool(NUMERIC_CELL.match(text))


def _slug(text):
    """Lower-case ascii slug. Diacritics are folded rather than treated as
    separators, so the French capacity report yields `developpeur` instead of
    the `d-veloppeur` a plain strip would produce — and a heading and a cell
    naming the same person still slug to the same anchor."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", text)
                     if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")


def _anchor(text):
    """Anchor id for a heading, or for a cell that might point at one."""
    return _slug(HEADING_SUBJECT.split(_plain(text), 1)[0])


def anchor_id(text):
    """The id this page gives a section named `text` — public, so a report that
    folds a section into a `<details>` can put the *same* id on it as the `### `
    it replaces, and the links pointing at it keep working."""
    return _anchor(text)


def section_tags(title, fold, level=3):
    """(opening, closing) Markdown for a section titled `title`.

    Folded, the `<details>` carries the id the heading would have carried, so a
    link written against the flat page keeps working — which is the whole reason
    this lives here rather than in each report: the ids, the fold and the script
    that opens a folded target are one contract, and `to_body` owns it."""
    if not fold:
        return "#" * level + f" {title}", ""
    # `dev` keeps the summary in the h3 register; a folded h2 is styled as one.
    cls = "section dev" if level >= 3 else "section"
    # The summary is rendered here, not by `to_body`: a raw HTML line passes
    # through that function untouched, so a title carrying `_renfort …_` would
    # keep its underscores — the one thing the heading it replaces got for free.
    return (f'<details class="{cls}" id="{anchor_id(title)}">'
            f"<summary>{inline(title)}</summary>", "</details>")


@contextlib.contextmanager
def folded(title, fold, level=3):
    """Print a section's opening, run its body, print its closing.

    A context manager because most of these section bodies return early on "no
    rows to show", and a closing tag that a `return` can skip is a broken page.
    It prints Markdown, which is the reports' business rather than this module's
    — but the ids, the fold and the script that opens a folded target are one
    contract, and it is honoured here."""
    opening, closing = section_tags(title, fold, level)
    print(f"\n{opening}\n")
    try:
        yield
    finally:
        if closing:
            print(f"\n{closing}\n")


def _col_class(label, column):
    """Per-column classes, so the stylesheet targets a column by meaning rather
    than by position — the leaderboard and the per-developer tables have
    different column counts, and nth-child cannot tell them apart.

    A column of figures also gets `num`: those never wrap and are right-aligned,
    which is what leaves the width for the columns of prose next to them."""
    slug = _slug(_plain(label))
    cls = f"col-{slug}" if slug else "col"
    # "at least one real figure" keeps a column of dashes from being called
    # numeric on the strength of its blanks alone.
    if (column and all(_is_number(c) for c in column)
            and any(ch.isdigit() for c in column for ch in _plain(c))):
        cls += " num"
    return cls


def _table(lines, start, anchors=()):
    """Consume a table starting at `start`; return (html, next_index).

    `anchors` are the section ids on the page: a name cell matching one becomes
    a link to that section, so the leaderboard jumps to a developer's tickets."""
    header = _cells(lines[start])
    i = start + 2  # skip the |---|---| separator
    rows = []
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        # Pad or trim so a malformed row cannot break the column alignment.
        rows.append((_cells(lines[i]) + [""] * len(header))[:len(header)])
        i += 1

    classes = [_col_class(label, [r[n] for r in rows])
               for n, label in enumerate(header)]

    # The wrapper, not the table, is the scroll container: it lets the table be
    # width:100% and share out its slack, and only scrolls when even the
    # wrapped text cannot fit.
    out = ['<div class="table-wrap">', "<table>", "<thead><tr>"]
    out += [f'<th class="{c}">{inline(h)}</th>' for h, c in zip(header, classes)]
    out += ["</tr></thead>", "<tbody>"]
    for row in rows:
        tds = []
        for value, cls in zip(row, classes):
            html = inline(value)
            slug = _anchor(value)
            if cls.split()[0] in LINKABLE_COLUMNS and slug in anchors:
                html = f'<a class="jump" href="#{slug}">{html}</a>'
            tds.append(f'<td class="{cls}">{html}</td>')
        out.append("<tr>" + "".join(tds) + "</tr>")
    out += ["</tbody>", "</table>", "</div>"]
    return "\n".join(out), i


def to_body(markdown):
    lines = markdown.splitlines()
    # The leaderboard is emitted before the sections it links to, so collect the
    # section ids up front. Only h3 gets one: h1 and h2 all start "DEV TSC — …"
    # and would collide on a single useless anchor.
    # A folded section carries its id on the raw `<details>` line instead, and
    # is just as linkable — the script below opens the one a jump lands on.
    anchors = []
    for line in lines:
        found = HEADING.match(line.strip())
        slug = None
        if found and len(found.group(1)) == 3:
            slug = _anchor(found.group(2))
        elif line.strip().startswith("<details"):
            tag = RAW_ID.search(line)
            slug = tag.group(1) if tag else None
        if slug and slug not in anchors:
            anchors.append(slug)
    used = set()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1  # closing fence
            out.append(f"<pre><code>{_escape(chr(10).join(block))}</code></pre>")
            continue

        if not stripped:
            i += 1
            continue

        # A line that is only block-level HTML (<details>, </details>) passes through.
        if stripped.startswith("<") and ALLOWED_HTML.match(stripped):
            out.append(stripped)
            i += 1
            continue

        heading = HEADING.match(stripped)
        if heading:
            level = len(heading.group(1))
            attr = ""
            if level == 3:
                slug = _anchor(heading.group(2))
                if slug and slug not in used:
                    used.add(slug)
                    attr = f' id="{slug}"'
            out.append(f"<h{level}{attr}>{inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        if stripped.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<blockquote>" + "<br>".join(inline(q) for q in quote)
                       + "</blockquote>")
            continue

        if (stripped.startswith("|") and i + 1 < len(lines)
                and TABLE_SEP.match(lines[i + 1]) and "|" in lines[i + 1]):
            html, i = _table(lines, i, anchors)
            out.append(html)
            continue

        # Bullets. Without this the Data quality block — six one-line notes —
        # was swallowed by the paragraph collector below and rendered as one
        # run-on paragraph with literal "- " separators.
        if BULLET.match(stripped):
            items = []
            while i < len(lines) and (BULLET.match(lines[i].strip())
                                      or (items and lines[i].strip()
                                          and lines[i][:1].isspace())):
                found = BULLET.match(lines[i].strip())
                if found:
                    items.append(found.group(1))
                else:
                    # An indented continuation line belongs to the item above.
                    items[-1] += " " + lines[i].strip()
                i += 1
            out.append("<ul>"
                       + "".join(f"<li>{inline(it)}</li>" for it in items)
                       + "</ul>")
            continue

        # Always consume the current line first: a "|" line that failed the
        # table check above would otherwise fail this loop's guard too, leaving
        # the index parked on it forever.
        para = [stripped]
        i += 1
        while (i < len(lines) and lines[i].strip()
               and not lines[i].lstrip().startswith(("|", "#", ">", "```"))
               and not BULLET.match(lines[i].strip())):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline(' '.join(para))}</p>")

    return "\n".join(out)


CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1c1e21; --muted: #6b7280;
  --line: #e5e7eb; --head: #f6f7f9; --zebra: #fbfbfc;
  --link: #1a56db; --quote: #fff7ed; --quote-line: #f59e0b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181d; --fg: #e6e8eb; --muted: #9aa1ab;
    --line: #2c3038; --head: #1e2127; --zebra: #1a1d22;
    --link: #7aa2f7; --quote: #2a2113; --quote-line: #b45309;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.5rem 4rem;
  background: var(--bg); color: var(--fg);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
/* The tables are the page, so let them have the screen: a 27" monitor should
   not show a 1400px column between two oceans of white. Prose is capped
   separately below, because a 2000px-wide sentence is unreadable. */
main { max-width: 2400px; margin: 0 auto; }
main > p, main > blockquote { max-width: 100ch; }
h1 { font-size: 1.6rem; margin: 0 0 1.5rem; padding-bottom: .6rem;
     border-bottom: 2px solid var(--line); }
h2 { font-size: 1.25rem; margin: 2.5rem 0 .5rem; }
h3 { font-size: 1.02rem; margin: 1.6rem 0 .5rem; color: var(--muted);
     font-weight: 600; letter-spacing: .01em;
     /* Land below the top edge when jumped to from the leaderboard. */
     scroll-margin-top: 1rem; }
/* No scroll-behavior: smooth here. It looks nicer but the animated scroll never
   ran in the Chrome app window this page opens in — the jump was a silent no-op
   with the hash set and the page still at the top. An instant jump always works,
   and :target below is what tells you where you landed. */
/* A name in the leaderboard jumps to that person's section. Underlined rather
   than coloured: a whole column in link blue reads as a table of links. */
a.jump { color: inherit; text-decoration: none;
         border-bottom: 1px dotted var(--muted); }
a.jump:hover { color: var(--link); border-bottom-color: var(--link); }
:target { animation: flash 1.4s ease-out; }
@keyframes flash { from { background: var(--quote); } to { background: none; } }
p { margin: .5rem 0; }
h2 + p em, p em:only-child { color: var(--muted); font-size: .87rem; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
       font-size: .85em; letter-spacing: -.02em; white-space: pre; }
pre { background: var(--head); border: 1px solid var(--line); border-radius: 6px;
      padding: .8rem 1rem; overflow-x: auto; }
pre code { white-space: pre; font-size: .8rem; }
blockquote { margin: 1rem 0; padding: .8rem 1rem; background: var(--quote);
             border-left: 4px solid var(--quote-line); border-radius: 4px; }
/* One note per line, hanging indent, so a Data quality block reads as a list
   of findings rather than a wall of prose. */
ul { margin: .5rem 0 1.1rem; padding-left: 1.15rem; }
li { margin: .3rem 0; line-height: 1.5; }
details { margin: .8rem 0; }
summary { cursor: pointer; color: var(--muted); }
/* A folded section: the summary stands in for the h2 it replaces, so it has to
   read like one. Muted small type is for legends — a section title in that
   register disappears. */
details.section { margin: 2.5rem 0 1.2rem; }
details.section > summary { color: var(--fg); font-size: 1.25rem;
                            font-weight: 600; }
details.section > summary::marker { color: var(--muted); font-size: .8rem; }
/* A folded developer section stands in for an h3, not an h2: same size, same
   muted register, so folding a section does not promote it. */
details.section.dev { margin: 1.6rem 0 .6rem; scroll-margin-top: 1rem; }
details.section.dev > summary { font-size: 1.02rem; color: var(--muted);
                               letter-spacing: .01em; }
details.section.dev[open] > summary { color: var(--fg); }
details.section.dev > summary:hover { color: var(--fg); }
/* Only this box scrolls sideways, and only once wrapping has run out. */
.table-wrap { overflow-x: auto; margin: .4rem 0 1.2rem; }
table { width: 100%; border-collapse: collapse; }
/* Text wraps by default — that is what keeps the whole row on screen instead
   of pushing the hours off the right edge. */
th, td { border: 1px solid var(--line); padding: .45rem .65rem;
         text-align: left; vertical-align: top;
         white-space: normal; line-height: 1.4; }
th { background: var(--head); font-weight: 600; font-size: .82rem;
     text-transform: uppercase; letter-spacing: .03em; color: var(--muted); }
tbody tr:nth-child(even) { background: var(--zebra); }
/* Figures stay on one line and line up digit under digit. The *header* above
   them is allowed to wrap: "WORKED (DEVS)" set on one line is far wider than
   any figure under it, and that width is stolen from the task title. */
td.num { white-space: nowrap; text-align: right;
         font-variant-numeric: tabular-nums; }
th.num { text-align: right; }
/* The task title is the one column that earns the right to wrap, so give it
   the slack the others give up. */
/* Only the title breaks mid-word: an Odoo task name can hold a URL or an
   identifier with no space in it. Applied to headers too it turned
   "ESTIMATED" into "ESTIMATE / D". */
td.col-task { min-width: 16rem; overflow-wrap: anywhere; }
td.col-status, td.col-dev-analyst, td.col-developer-s { max-width: 11rem; }
/* Project and stage come from short, closed vocabularies ("Chant d'Eole - Odoo
   - BE", "To Review / Integration Testing"). Letting them wrap put almost every
   row on two lines — 104 of 122 — to save ~90px the title did not need; held on
   one line only 18 rows still wrap, and the page is a fifth shorter. A stage
   name long enough to overflow would scroll the wrapper, not break the layout. */
td.col-id, td.col-project, td.col-projet, td.col-stage, td.col-developer,
td.col-developpeur, td.col-regime,
td.col-last-updated, td.col-time-spent-est { white-space: nowrap; }
/* The load bar is one glyph run; a wrap in the middle of it is nonsense. */
td.col-load-1-wk-full-bar { white-space: nowrap; text-align: left; }
"""

TABS_CSS = """
/* The tab bar sticks: the whole point of tabs is that switching costs nothing,
   and a bar you have to scroll back up to reach costs a scroll each time. */
.tabs { position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap;
  gap: .2rem; background: var(--bg); border-bottom: 2px solid var(--line);
  margin: 0 0 1.6rem; padding-top: .2rem; }
.tabs button { appearance: none; border: 0; background: transparent;
  color: var(--muted); font: inherit; font-weight: 600; padding: .7rem 1.1rem;
  cursor: pointer; border-bottom: 3px solid transparent; margin-bottom: -2px;
  border-radius: 6px 6px 0 0; }
.tabs button:hover { color: var(--fg); background: var(--head); }
.tabs button[aria-selected="true"] { color: var(--link);
  border-bottom-color: var(--link); }
.tabs .num { display: inline-block; min-width: 1.4em; margin-right: .35em;
  color: var(--muted); font-variant-numeric: tabular-nums; }
.tabs button[aria-selected="true"] .num { color: var(--link); }
.tabs .resync { margin-left: auto; align-self: center; color: var(--link);
  background: var(--bg); border: 1px solid var(--line); border-radius: 6px;
  padding: .5rem .9rem; font: inherit; font-weight: 600; cursor: pointer; }
.tabs .resync:hover:not(:disabled) { background: var(--head); }
.tabs .resync:disabled { opacity: .6; cursor: default; }
.pane > h1:first-child { display: none; }  /* the page already carries one */
/* Progressive enhancement: with no script the panes simply stack, which is a
   worse page but a complete one. Hiding is only ever done by the script. */
.js .pane { display: none; }
.js .pane.active { display: block; }
"""

TABS_JS = """
document.documentElement.classList.add('js');
(function () {
  var buttons = [].slice.call(document.querySelectorAll('.tabs button'));
  var panes = [].slice.call(document.querySelectorAll('.pane'));
  function show(key, push) {
    if (!panes.some(function (p) { return p.id === 'pane-' + key; })) return;
    buttons.forEach(function (b) {
      b.setAttribute('aria-selected', String(b.dataset.tab === key));
    });
    panes.forEach(function (p) { p.classList.toggle('active', p.id === 'pane-' + key); });
    // The hash, not a variable: a reload, a bookmark and a link into the page
    // all land on the tab you were reading. The page is reopened four times a
    // day, and coming back to tab 1 every time is what makes tabs annoying.
    if (push && location.hash !== '#' + key) history.replaceState(null, '', '#' + key);
    window.scrollTo(0, 0);
  }
  // A section id is namespaced by its tab ('capacite--nathan-giacomello'), so
  // the hash names both what to show and where to scroll.
  function target() { return decodeURIComponent(location.hash.slice(1)); }
  // A jump link can land on a folded section: the browser scrolls to it and
  // shows a closed box, which reads as a broken link. Open it, then let :target
  // flash it like any other section.
  function reveal() {
    var el = location.hash && document.getElementById(target());
    if (!el) return;
    if (el.tagName === 'DETAILS' && !el.open) el.open = true;
    el.scrollIntoView();
  }
  buttons.forEach(function (b) {
    b.addEventListener('click', function () { show(b.dataset.tab, true); });
  });
  window.addEventListener('hashchange', function () {
    show(target().split('--')[0], false);
    reveal();
  });
  show(target().split('--')[0] || buttons[0].dataset.tab, false);
  reveal();
})();
"""

# A no-op when the button is absent (render_tabs() called with no
# resync_port), so this can always be concatenated onto TABS_JS.
RESYNC_JS = """
(function () {
  var btn = document.getElementById('resync-btn');
  if (!btn) return;
  var idle = btn.textContent;
  var port = btn.dataset.port;
  btn.addEventListener('click', function () {
    btn.disabled = true;
    btn.textContent = '⟳ Resynchronisation…';
    fetch('http://127.0.0.1:' + port + '/resync')
      .then(function (r) {
        if (!r.ok) throw new Error('build failed');
        location.reload();
      })
      .catch(function () {
        btn.disabled = false;
        btn.textContent = '⚠ Échec — le serveur de resync tourne ?';
        setTimeout(function () { btn.textContent = idle; }, 4000);
      });
  });
})();
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""


TABBED_PAGE = PAGE.replace("<style>{css}</style>",
                           "<style>{css}</style>").replace(
    "</body>", "<script>{script}</script>\n</body>")


def _namespace_ids(body, key):
    """Prefix a pane's section ids, and the links pointing at them, with its tab.

    Two tabs both hold a section per developer, so `id="nathan-giacomello"`
    existed twice on the page: every link resolved to the copy in tab 1, and a
    jump from tab 2 scrolled to a hidden element — silently, since the browser
    will not scroll to something in a `display:none` pane. Namespacing makes the
    ids unique, and `#capacite--nathan-giacomello` also names the tab, which is
    what lets the script select it before jumping."""
    body = re.sub(r'(\bid=")([^"]+)(")', rf'\1{key}--\2\3', body)
    return re.sub(r'(\bhref="#)([^"]+)(")', rf'\1{key}--\2\3', body)


def render(markdown, title="DEV TSC — Digest"):
    return PAGE.format(title=_escape(title), css=CSS, body=to_body(markdown))


def render_tabs(tabs, title="DEV TSC", header="", resync_port=None):
    """A single page carrying several Markdown documents behind a tab bar.

    `tabs` is [(key, label, markdown)]. Each document is rendered by the same
    `to_body` as `render`, so a tab is not a second kind of page — anything that
    works in the dashboard works in a tab, and the two cannot drift apart.

    `header` is Markdown shown above the bar, on every tab: the title, the date,
    and the one-line scope. It stays put because it is what the reader needs to
    trust any of the three tabs, and repeating it inside each one would push the
    first table below the fold.

    `resync_port` adds a "Resynchroniser" button to the tab bar, calling
    `dev_resync_server.py` on that port to rebuild the page on demand — omit it
    (the default) for a page with no such server behind it."""
    parts = [to_body(header)] if header else []
    bar = ['<nav class="tabs" role="tablist">']
    for i, (key, label, _md) in enumerate(tabs, 1):
        bar.append(f'<button role="tab" data-tab="{_escape(key)}" '
                   f'aria-controls="pane-{_escape(key)}" '
                   f'aria-selected="{"true" if i == 1 else "false"}">'
                   f'<span class="num">{i}</span>{_escape(label)}</button>')
    if resync_port:
        bar.append(f'<button type="button" id="resync-btn" class="resync" '
                   f'data-port="{resync_port}">⟳ Resynchroniser</button>')
    bar.append("</nav>")
    parts.append("\n".join(bar))
    for i, (key, label, markdown) in enumerate(tabs, 1):
        active = " active" if i == 1 else ""
        parts.append(f'<section class="pane{active}" id="pane-{_escape(key)}" '
                     f'role="tabpanel" aria-label="{_escape(label)}">')
        parts.append(_namespace_ids(to_body(markdown), key))
        parts.append("</section>")
    return TABBED_PAGE.format(title=_escape(title), css=CSS + TABS_CSS,
                              body="\n".join(parts), script=TABS_JS + RESYNC_JS)


if __name__ == "__main__":
    import sys
    print(render(sys.stdin.read()))
