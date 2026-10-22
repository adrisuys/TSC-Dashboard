# timesheet/ — the engine behind the "Ma feuille de temps" tab

Copied **unchanged** from the `tsc-timesheet` plugin (`plugins/tsc-timesheet/scripts/`):
`collectors.py`, `dayfile.py`, `odoo_sync.py`, `timeline.py`, `ts_config.py`, and its
tests (`tests/test_timesheet.py`, import path adjusted). Keep them verbatim so a new
plugin version is a plain copy over; the web app's glue lives in `../dev_feuille.py`.

Not copied: `review_server.py` and `review.html` (replaced by the tab and
`dev_feuille.py`), `timesheet.py` and `setup.py` (the CLI stays in the plugin).

The engine reads traces **on the Mac that runs the web app**: Claude Code
transcripts (`~/.claude/projects`), Chrome history (needs Full Disk Access for the
venv's python, since launchd runs it), and git commits under `workspace_root`
(`~/Desktop/TSC` by default — also TCC-protected). Config and day files are the
plugin's own (`~/.config/tsc-timesheet/`, `~/Library/Application Support/tsc-timesheet/`),
so the CLI `/timesheet` and the tab share the same days.

    python3 -m unittest discover -s app/timesheet/tests
