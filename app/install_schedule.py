#!/usr/bin/env python3
"""Install (or remove) the launchd job that keeps the local DEV TSC web app
(`dev_webapp.py`, http://127.0.0.1:8734) running. The app refreshes itself
from Odoo every 30 min and on its "Resynchroniser" button, and `/dev-app`
pushes a fresh build into it — there is no schedule to install any more.

The web app needs FastAPI, uvicorn and Jinja, so it runs from a dedicated venv
created here (`<output_dir>/venv`); everything else stays standard library on
the system interpreter.

    python3 install_schedule.py              # install / update the web app
    python3 install_schedule.py --status
    python3 install_schedule.py --uninstall

macOS only.

The scripts are copied into a stable runner directory and the job points there,
never into the plugin itself: plugins are installed under a version-numbered
path (…/tsc-dev-digest/1.0.0/scripts), so a job wired straight to the plugin
would silently break on the next version bump. Re-run this after updating the
plugin to refresh the copy.

Until 07/10/2026 a second job (`com.tsc.dev-digest.plugin`) rebuilt the static
`app.html` on a calendar. The web app made it redundant — it refreshed a page
nobody opened and read Odoo five extra times a day — so an install now removes
it wherever it finds it.
"""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import digest  # noqa: E402
import digest_config  # noqa: E402

# The retired calendar job — see the module docstring. Kept only so install,
# uninstall and status can clean it up on machines that still carry it.
RETIRED_LABEL = "com.tsc.dev-digest.plugin"
RETIRED_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{RETIRED_LABEL}.plist")
SOURCE = os.path.dirname(os.path.abspath(__file__))
# A subdirectory, so this never collides with an older hand-rolled digest that
# kept its scripts at the top of the same application-support folder.
RUNNER = os.path.join(digest.output_dir(), "runner")

# The local web app, which also answers the "Resynchroniser" button of a saved
# static page (it kept the old resync server's port and its GET /resync). The
# label predates the web app; kept so a re-install replaces the old resync
# server instead of leaving it running beside the new one.
SERVER_LABEL = "com.tsc.dev-digest.resync-server"
SERVER_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{SERVER_LABEL}.plist")
SERVER_JOB = os.path.join(RUNNER, "dev_webapp.py")
VENV = os.path.join(digest.output_dir(), "venv")
VENV_PYTHON = os.path.join(VENV, "bin", "python")
REQUIREMENTS = "requirements-webapp.txt"
# Newest first. The system 3.9 works, but a Homebrew interpreter is preferred
# when there is one — it is the one a teammate's Mac is likeliest to keep
# current.
VENV_BASES = ["/opt/homebrew/bin/python3.13", "/opt/homebrew/bin/python3.12",
              "/opt/homebrew/bin/python3.11", "/usr/local/bin/python3.12",
              "/usr/bin/python3"]


def domain():
    return f"gui/{os.getuid()}"


def launchctl(*args, check=False):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True,
                          check=check)


def build_server_plist(log):
    """No schedule: `KeepAlive` restarts it if it ever exits, `RunAtLoad`
    starts it immediately on install rather than waiting for the next login."""
    return {
        "Label": SERVER_LABEL,
        "ProgramArguments": [VENV_PYTHON, SERVER_JOB],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def sync_runner():
    """Copy the scripts, the web app's templates and assets, and its
    requirements to the stable runner dir the launchd jobs point at."""
    os.makedirs(RUNNER, exist_ok=True)
    for name in sorted(os.listdir(SOURCE)):
        if name.endswith(".py") or name == REQUIREMENTS:
            shutil.copy2(os.path.join(SOURCE, name), os.path.join(RUNNER, name))
    web = os.path.join(RUNNER, "webapp")
    if os.path.isdir(web):
        shutil.rmtree(web)          # a template deleted upstream must go too
    shutil.copytree(os.path.join(SOURCE, "webapp"), web)
    # The timesheet engine behind "Ma feuille de temps" (dev_feuille.py).
    engine = os.path.join(RUNNER, "timesheet")
    if os.path.isdir(engine):
        shutil.rmtree(engine)
    shutil.copytree(os.path.join(SOURCE, "timesheet"), engine,
                    ignore=shutil.ignore_patterns("__pycache__", "tests"))
    return RUNNER


def ensure_venv():
    """Create the web app's venv if needed and install its requirements. Run on
    every install, so a requirement added upstream lands on the next sync."""
    if not os.path.exists(VENV_PYTHON):
        base = next((p for p in VENV_BASES if os.path.exists(p)), sys.executable)
        print(f"Creating the web app venv with {base} …")
        subprocess.run([base, "-m", "venv", VENV], check=True)
    result = subprocess.run([VENV_PYTHON, "-m", "pip", "install", "-q",
                             "--disable-pip-version-check", "-r",
                             os.path.join(RUNNER, REQUIREMENTS)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"pip install failed in {VENV}:\n{result.stderr.strip()}")


def _load(label, path, data):
    """Write `path` and (re)bootstrap `label` from it. bootout first so a
    re-run replaces the job instead of erroring on a duplicate."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(data, fh)
    launchctl("bootout", f"{domain()}/{label}")
    # bootout returns before launchd has finished tearing the job down; a
    # bootstrap sent in that window fails ("5: Input/output error") and leaves
    # the job not loaded at all. Wait for it to be gone, and retry once.
    for _ in range(50):
        if launchctl("print", f"{domain()}/{label}").returncode != 0:
            break
        time.sleep(0.1)
    result = launchctl("bootstrap", domain(), path)
    if result.returncode != 0:
        time.sleep(1)
        result = launchctl("bootstrap", domain(), path)
    if result.returncode != 0:
        sys.exit(f"launchctl bootstrap failed for {label}: "
                 f"{result.stderr.strip() or result.stdout.strip()}")
    launchctl("enable", f"{domain()}/{label}")


def retire_calendar_job():
    """Unload and delete the old calendar job, if this machine still has it."""
    launchctl("bootout", f"{domain()}/{RETIRED_LABEL}")
    if os.path.exists(RETIRED_PLIST):
        os.remove(RETIRED_PLIST)
        print(f"✓ Removed the old scheduled job {RETIRED_LABEL}")


def install():
    cfg = digest_config.require_credentials(digest_config.load())
    if not digest_config.authenticate(cfg):
        sys.exit("Credentials are rejected by Odoo — run setup_credentials.py first.")

    out = digest.output_dir()
    os.makedirs(out, exist_ok=True)
    log = os.path.join(out, "launchd.log")
    sync_runner()
    ensure_venv()

    retire_calendar_job()
    _load(SERVER_LABEL, SERVER_PLIST, build_server_plist(log))
    # RunAtLoad is not enough: right after a bootout, launchd can accept the
    # bootstrap yet leave the job "pended nondemand spawn = speculative" with
    # runs = 0 — loaded, never started, and the button fails with nothing in
    # the log. Kickstarting forces the spawn whether or not that happened.
    launchctl("kickstart", "-k", f"{domain()}/{SERVER_LABEL}")

    print(f"✓ Web app running: http://127.0.0.1:{digest_config.RESYNC_PORT}")
    print(f"  job     {SERVER_PLIST}")
    print(f"  runs    {VENV_PYTHON} {SERVER_JOB}")
    print(f"  copied  from {SOURCE}   (log: {log})")
    print("\nRe-run this after updating the plugin to refresh the copied scripts —"
         " it restarts the web app too, so it always runs the current code.")


def uninstall():
    retire_calendar_job()
    for label, plist in ((SERVER_LABEL, SERVER_PLIST),):
        launchctl("bootout", f"{domain()}/{label}")
        if os.path.exists(plist):
            os.remove(plist)
            print(f"✓ Removed {plist}")
        else:
            print(f"Nothing to remove — {plist} does not exist.")
    if os.path.isdir(RUNNER):
        shutil.rmtree(RUNNER)
        print(f"✓ Removed {RUNNER}")
    print("The saved pages and the log were left in place.")


def status():
    if os.path.exists(RETIRED_PLIST):
        print(f"⚠ The retired job {RETIRED_LABEL} is still installed — "
              f"re-run this script without arguments to remove it.")
    for label, plist in ((SERVER_LABEL, SERVER_PLIST),):
        print(f"\n{label}")
        print(f"plist   {plist}  {'(present)' if os.path.exists(plist) else '(absent)'}")
        result = launchctl("print", f"{domain()}/{label}")
        if result.returncode != 0:
            print("launchd  not loaded")
            continue
        for line in result.stdout.splitlines():
            if any(k in line for k in ("state =", "last exit code", "runs =")):
                print(f"launchd {line.strip()}")


def main():
    ap = argparse.ArgumentParser(
        description="Install the DEV TSC local web app (macOS).")
    ap.add_argument("--uninstall", action="store_true", help="remove the job")
    ap.add_argument("--status", action="store_true", help="show whether it is loaded")
    args = ap.parse_args()

    if sys.platform != "darwin":
        sys.exit("The installer is macOS-only (launchd). Elsewhere, run "
                 "dev_webapp.py yourself from a venv with requirements-webapp.txt.")

    if args.status:
        status()
    elif args.uninstall:
        uninstall()
    else:
        install()


if __name__ == "__main__":
    main()
