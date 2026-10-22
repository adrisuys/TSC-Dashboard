"""Unit tests for the timesheet autofill — no network, no real Chrome/Claude data.

    python3 -m unittest discover -s plugins/tsc-timesheet/tests
"""
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import collectors  # noqa: E402
import dayfile  # noqa: E402
import odoo_sync  # noqa: E402
import timeline  # noqa: E402
import ts_config  # noqa: E402
from collectors import Interval, Ref  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 6, 8, 0, tzinfo=UTC)


def at(minutes):
    return T0 + dt.timedelta(minutes=minutes)


def cfg(**over):
    c = ts_config.load(path=None, digest_path=None, environ={})
    c.update(over)
    return c


class TestTaskIds(unittest.TestCase):
    def test_explicit_references(self):
        self.assertEqual(collectors.task_ids_in("see https://x/odoo/project.task/30558 now"), [30558])
        self.assertEqual(collectors.task_ids_in("/web#id=30474&model=project.task&view_type=form"), [30474])
        self.assertEqual(collectors.task_ids_in("push dev/30132 please"), [30132])
        self.assertEqual(collectors.task_ids_in("custom/19.0/capinvest/fix-29715-shop-entry"), [29715])
        self.assertEqual(collectors.task_ids_in("[FIX] thing (#30581)"), [30581])

    def test_hash_ignores_hex_colours(self):
        self.assertEqual(collectors.task_ids_in("--s-billable: #3987e5; --s-internal: #d95926;"), [])
        self.assertEqual(collectors.task_ids_in("see #30581: done"), [30581])

    def test_bare_numbers_only_when_asked(self):
        self.assertEqual(collectors.task_ids_in("yes 29715, ship it"), [])
        self.assertEqual(collectors.task_ids_in("yes 29715, ship it", bare=True), [29715])

    def test_explicit_outrank_bare(self):
        self.assertEqual(collectors.task_ids_in("about 30474 see dev/30581", bare=True), [30581, 30474])

    def test_bare_ignores_ports_and_paths(self):
        self.assertEqual(collectors.task_ids_in("localhost:28069 and v19.30123", bare=True), [])

    def test_repos(self):
        self.assertEqual(collectors.repos_in("/Users/a/Desktop/TSC/custom/19.0/european-schools/dev-30581/x.py"),
                         ["european-schools/dev-30581"])
        self.assertEqual(collectors.repos_in("cd /x/custom/18.0/chantdeole"), ["chantdeole"])

    def test_path_tasks_longest_folder_wins(self):
        rules = collectors.compile_path_tasks({"/w/skills": 27678, "/w/skills/plugins/tsc-dev-digest": 29425})
        self.assertEqual(collectors.path_task_in("/w/skills/plugins/tsc-timesheet/x.py", rules), 27678)
        self.assertEqual(collectors.path_task_in("/w/skills/plugins/tsc-dev-digest/app.py", rules), 29425)
        self.assertIsNone(collectors.path_task_in("/w/skills-old/x.py", rules))
        self.assertIsNone(collectors.path_task_in("/w/custom/19.0/capinvest/x.py", rules))

    def test_readonly_bash(self):
        self.assertTrue(collectors._readonly_bash("cd /x/custom/19.0/a/b && git log | head"))
        self.assertFalse(collectors._readonly_bash("cd /x/custom/19.0/a/b && git commit -m x"))
        self.assertFalse(collectors._readonly_bash("ls; python3 t.py"))


class TestSessionIntervals(unittest.TestCase):
    idle = dt.timedelta(minutes=15)
    tail = dt.timedelta(minutes=5)

    def ev(self, m, kind="work", text="", ids=(), repos=(), cwd="/Users/a/Desktop/TSC"):
        return (at(m), kind, text, list(ids), list(repos), cwd)

    def test_gap_closes_block_with_tail(self):
        out = collectors.session_intervals(
            [self.ev(0, "prompt", "dev/30581", ids=[30581]), self.ev(10), self.ev(60)],
            self.idle, self.tail)
        spans = [(iv.start, iv.end) for iv in out]
        self.assertEqual(spans, [(at(0), at(10)), (at(10), at(15)), (at(60), at(65))])
        self.assertTrue(all(iv.ref.task_id == 30581 for iv in out))

    def test_backfill_limited_to_first_block(self):
        out = collectors.session_intervals(
            [self.ev(0, "prompt", "tooling"), self.ev(60, "prompt", "hello"),
             self.ev(65, "edit", ids=[30558])],
            self.idle, self.tail)
        self.assertIsNone(out[0].ref.task_id)          # an hour earlier: not the ticket
        self.assertEqual(out[1].ref.task_id, 30558)     # same block: back-filled

    def test_worktree_sets_task_and_other_repo_drops_it(self):
        out = collectors.session_intervals(
            [self.ev(0, "edit", repos=["european-schools/dev-30581"]),
             self.ev(5, "edit", repos=["european-schools/staging"]),
             self.ev(10, "edit", repos=["capinvest/staging"])],
            self.idle, self.tail)
        self.assertEqual(out[0].ref.task_id, 30581)
        self.assertEqual(out[1].ref.task_id, 30581)     # same repo keeps the ticket
        self.assertEqual(out[2].ref.repo, "capinvest/staging")
        self.assertIsNone(out[2].ref.task_id)

    def test_repo_stretch_takes_next_task_in_same_repo(self):
        out = collectors.session_intervals(
            [self.ev(0, "prompt", "create the staging worktree", repos=["multitel"]),
             self.ev(5, "prompt", "now branch dev/29320", ids=[29320])],
            self.idle, self.tail)
        self.assertEqual(out[0].ref.task_id, 29320)

    def test_launch_folder_counts_once(self):
        wt = "/Users/a/Desktop/TSC/custom/19.0/european-schools/dev-30581"
        out = collectors.session_intervals(
            [self.ev(0, cwd=wt), self.ev(5, "edit", repos=["capinvest/x"], cwd=wt), self.ev(10, cwd=wt)],
            self.idle, self.tail)
        self.assertEqual(out[0].ref.task_id, 30581)
        self.assertEqual(out[2].ref.repo, "capinvest/x")

    def test_launch_folder_with_path_task(self):
        rules = collectors.compile_path_tasks({"/w/skills": 27678})
        out = collectors.session_intervals(
            [self.ev(0, "prompt", "tweak it", cwd="/w/skills/plugins/x"), self.ev(5)],
            self.idle, self.tail, rules)
        self.assertEqual([iv.ref.task_id for iv in out], [27678, 27678])


class TestClaudeTranscript(unittest.TestCase):
    def test_reads_main_thread_prompts_and_tools(self):
        rows = [
            {"type": "user", "timestamp": "2026-10-06T08:00:00Z", "cwd": "/x",
             "message": {"content": "check https://www.tsc-experts.com/odoo/project.task/30558"}},
            {"type": "assistant", "timestamp": "2026-10-06T08:01:00Z", "cwd": "/x",
             "message": {"content": [{"type": "tool_use", "name": "Edit",
                                      "input": {"file_path": "/a/custom/18.0/EMC/dev-30558/m.py"}}]}},
            {"type": "user", "timestamp": "2026-10-06T08:02:00Z", "isSidechain": True,
             "message": {"content": "subagent noise dev/11111"}},
            {"type": "user", "timestamp": "2026-10-06T08:03:00Z", "isMeta": True,
             "message": {"content": "meta"}},
            {"type": "user", "timestamp": "2026-10-06T08:04:00Z",
             "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
        try:
            events = collectors.read_claude_events(fh.name, at(-60), at(60))
        finally:
            os.unlink(fh.name)
        self.assertEqual([e[1] for e in events], ["prompt", "edit", "work"])
        self.assertEqual(events[0][3], [30558])
        self.assertEqual(events[1][4], ["EMC/dev-30558"])

    def test_skill_edits_book_the_folder_task_but_commands_do_not(self):
        def tool(minute, name, args):
            return {"type": "assistant", "timestamp": f"2026-10-06T08:0{minute}:00Z", "cwd": "/x",
                    "message": {"content": [{"type": "tool_use", "name": name, "input": args}]}}
        rows = [
            {"type": "user", "timestamp": "2026-10-06T08:00:00Z", "cwd": "/x",
             "message": {"content": 'why "#3987 Sogenam" on my timesheet?'}},
            tool(1, "Edit", {"file_path": "/w/skills/plugins/tsc-timesheet/collectors.py"}),
            tool(2, "Write", {"file_path": "/w/skills/plugins/tsc-dev-digest/app.py"}),
            tool(3, "Bash", {"command": "python3 /w/skills/plugins/tsc-dev-digest/run.py"}),
        ]
        rules = collectors.compile_path_tasks({"/w/skills": 27678, "/w/skills/plugins/tsc-dev-digest": 29425})
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
        try:
            events = collectors.read_claude_events(fh.name, at(-60), at(60), rules)
        finally:
            os.unlink(fh.name)
        self.assertEqual([e[3] for e in events], [[3987], [27678], [29425], []])


class TestChrome(unittest.TestCase):
    def test_classify(self):
        c = cfg()
        ref, _ = collectors.classify_url("https://www.tsc-experts.com/odoo/project.task/30558", "", c)
        self.assertEqual(ref.task_id, 30558)
        ref, kind = collectors.classify_url("https://github.com/TSC-BE/multitel/pull/25", "Fix (#29320)", c)
        self.assertEqual((ref.repo, ref.task_id, kind), ("multitel", 29320, "review"))
        self.assertEqual(collectors.classify_url("https://meet.google.com/abc", "", c)[0].bucket, "meeting")
        self.assertTrue(collectors.classify_url("https://news.ycombinator.com/", "", c)[0].empty())
        self.assertTrue(collectors.classify_url("https://github.com/odoo/odoo/pull/1", "", c)[0].empty())

    def test_history_file_and_intervals(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "History")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
        con.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)")
        con.execute("INSERT INTO urls VALUES (1, 'https://www.tsc-experts.com/odoo/project.task/30558', 'Task')")
        con.execute("INSERT INTO urls VALUES (2, 'https://example.org/', 'Other')")
        for i, (url, m) in enumerate([(1, 0), (2, 2), (1, 30)]):
            con.execute("INSERT INTO visits VALUES (?, ?, ?)", (i + 1, url, collectors.to_chrome_time(at(m))))
        con.commit()
        con.close()
        visits = collectors.read_chrome_visits(path, at(-10), at(60))
        self.assertEqual(len(visits), 3)
        out = collectors.chrome_intervals(visits, cfg())
        self.assertEqual([(iv.start, iv.end) for iv in out], [(at(0), at(2)), (at(30), at(35))])

    def test_profile_by_email(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "Local State"), "w") as fh:
            json.dump({"profile": {"info_cache": {"Default": {"user_name": "me@home.com"},
                                                  "Profile 2": {"user_name": "Adrien@TSC-experts.com"}}}}, fh)
        self.assertTrue(collectors.find_chrome_profile(tmp, "adrien@tsc-experts.com").endswith("Profile 2"))
        with self.assertRaises(collectors.SourceUnavailable):
            collectors.find_chrome_profile(tmp, "nobody@x.com")


class TestGit(unittest.TestCase):
    def test_parse_log(self):
        out = collectors.parse_git_log(
            "2026-10-06T10:00:00+00:00\x1frefs/heads/dev/30558\x1f[ADD] 30558: module\n"
            "2026-10-06T10:05:00+00:00\x1frefs/heads/x\x1fMerge remote-tracking branch 'origin/dev/22673'\n"
            "2026-10-06T10:09:00+00:00\x1frefs/heads/dev/29320\x1f[FIX] portal\n",
            "EMC", dt.timedelta(minutes=10))
        self.assertEqual([iv.ref.task_id for iv in out], [30558, 29320])
        self.assertEqual(out[0].end - out[0].start, dt.timedelta(minutes=10))


class TestTimeline(unittest.TestCase):
    tasks = {30558: {"project_id": 554}, 30581: {"project_id": 617}}

    def test_round_up(self):
        self.assertEqual(timeline.round_up(5, 15), 0.25)
        self.assertEqual(timeline.round_up(15, 15), 0.25)
        self.assertEqual(timeline.round_up(15.5, 15), 0.5)
        self.assertEqual(timeline.round_up(0, 15), 0.0)

    def test_resolve_order(self):
        c = cfg()
        self.assertEqual(timeline.resolve(Ref(task_id=30558), "claude", c, self.tasks), ("task", 30558))
        # Unknown id falls back to the repo's project.
        self.assertEqual(timeline.resolve(Ref(task_id=99999, repo="european-schools/inspector-staging"),
                                          "claude", c, self.tasks), ("project", 813))
        self.assertEqual(timeline.resolve(Ref(bucket="meeting"), "chrome", c, self.tasks), ("bucket", "meeting"))
        self.assertEqual(timeline.resolve(Ref(), "claude", c, self.tasks), ("bucket", "internal"))
        self.assertIsNone(timeline.resolve(Ref(), "chrome", c, self.tasks))

    def test_project_for_repo_longest_key(self):
        rp = cfg()["repo_projects"]
        self.assertEqual(timeline.project_for_repo("european-schools/dev-30581", rp), 617)
        self.assertEqual(timeline.project_for_repo("european-schools/inspector-staging", rp), 813)
        self.assertIsNone(timeline.project_for_repo("unknown-repo", rp))

    def test_overlap_split_and_full(self):
        a, b = ("task", 1), ("task", 2)
        spans = [(at(0), at(60), a), (at(30), at(90), b), (at(0), at(10), a)]
        split = timeline.share_time(spans)
        self.assertAlmostEqual(split[a] / 60, 45)
        self.assertAlmostEqual(split[b] / 60, 45)
        full = timeline.share_time(spans, split=False)
        self.assertAlmostEqual(full[a] / 60, 60)        # its own overlap is not doubled
        self.assertAlmostEqual(full[b] / 60, 60)

    def test_build_lines_no_daily_cap_and_rounding(self):
        ivs = [Interval(at(0), at(11 * 60), "claude", Ref(task_id=30558), "edit", "", ("edit",)),
               Interval(at(11 * 60), at(11 * 60 + 5), "claude", Ref(task_id=30581), "prompt", "fix it", ("prompt",))]
        start, end = timeline.day_bounds(dt.date(2026, 10, 6), UTC)
        lines = {l["id"]: l for l in timeline.build_lines(ivs, cfg(), self.tasks, start, end)}
        self.assertEqual(lines["task-30558"]["hours"], 11.0)   # no 8 h cap
        self.assertEqual(lines["task-30581"]["hours"], 0.25)   # 5 min -> 15 min
        self.assertEqual(lines["task-30558"]["description"], "Dev")
        self.assertEqual(lines["task-30558"]["project_id"], 554)

    def test_short_lines_hidden(self):
        ivs = [Interval(at(0), at(4), "chrome", Ref(task_id=30558), "visit", "", ()),
               Interval(at(10), at(15), "claude", Ref(task_id=30581), "prompt", "fix it", ("prompt",))]
        start, end = timeline.day_bounds(dt.date(2026, 10, 6), UTC)
        ids = [l["id"] for l in timeline.build_lines(ivs, cfg(), self.tasks, start, end)]
        self.assertEqual(ids, ["task-30581"])                 # 4 min dropped, 5 min kept
        ids = [l["id"] for l in timeline.build_lines(ivs, cfg(min_minutes=0), self.tasks, start, end)]
        self.assertEqual(sorted(ids), ["task-30558", "task-30581"])

    def test_clipped_to_day(self):
        ivs = [Interval(at(-600), at(-480), "claude", Ref(task_id=30558), "edit", "", ("edit",))]
        start, end = timeline.day_bounds(dt.date(2026, 10, 6), UTC)
        self.assertEqual(timeline.build_lines(ivs, cfg(), self.tasks, start, end), [])

    def test_describe(self):
        self.assertEqual(timeline.describe({"edit"}, []), "Dev")
        self.assertEqual(timeline.describe({"review"}, []), "Review")
        self.assertEqual(timeline.describe({"skill:tsc-tech-analysis:tech-analysis", "edit"}, []), "Analysis + Dev")
        self.assertEqual(timeline.describe({"prompt"}, ["question"]), "Analysis")


class TestDayfile(unittest.TestCase):
    def line(self, **kw):
        base = {"id": "task-1", "key_type": "task", "task_id": 1, "project_id": 9, "minutes": 30,
                "hours": 0.5, "description": "Dev", "evidence": [], "sources": {}}
        base.update(kw)
        return base

    def test_merge_keeps_decisions_and_pushes(self):
        prev = {"lines": [
            self.line(description="Fix portal", hours=1.0, edited=["description", "hours"]),
            self.line(id="task-2", task_id=2, pushed=True, odoo_ids=[77]),
        ]}
        out = {l["id"]: l for l in dayfile.merge([self.line()], prev)}
        self.assertEqual(out["task-1"]["description"], "Fix portal")
        self.assertEqual(out["task-1"]["hours"], 1.0)
        self.assertTrue(out["task-2"]["pushed"])        # vanished traces, still shown

    def test_default_selection(self):
        c = cfg()
        self.assertTrue(dayfile.default_selected(self.line(), c))
        self.assertFalse(dayfile.default_selected(self.line(hours=0.0, logged_hours=0.5), c))
        self.assertFalse(dayfile.default_selected(self.line(key_type="bucket", task_id=None), c))
        self.assertFalse(dayfile.default_selected(self.line(key_type="project", task_id=None), c))
        self.assertFalse(dayfile.default_selected(self.line(task_id=None), c))  # task cleared by hand

    def test_deduct_logged_proposes_the_remainder(self):
        c = cfg()
        partial = self.line(hours=1.0, logged_hours=0.5)
        dayfile.deduct_logged(partial)
        self.assertEqual((partial["hours"], partial["tracked_hours"]), (0.5, 1.0))
        self.assertTrue(dayfile.default_selected(partial, c))
        odd = self.line(hours=1.0, logged_hours=0.33)
        dayfile.deduct_logged(odd)
        self.assertEqual(odd["hours"], 0.75)                # rounded up to 15 min
        covered = self.line(hours=0.5, logged_hours=0.75)
        dayfile.deduct_logged(covered)
        self.assertEqual(covered["hours"], 0.0)
        self.assertFalse(dayfile.default_selected(covered, c))
        edited = self.line(hours=1.0, logged_hours=0.5, edited=["hours"])
        dayfile.deduct_logged(edited)
        self.assertEqual(edited["hours"], 1.0)              # a hand-set amount is kept

    def test_pushed_line_reopens_for_the_top_up(self):
        grown = self.line(hours=1.0, logged_hours=0.75, pushed=True, odoo_ids=[87063])
        dayfile.reopen_if_grown(grown)
        dayfile.deduct_logged(grown)
        self.assertFalse(grown["pushed"])
        self.assertEqual((grown["hours"], grown["odoo_ids"]), (0.25, [87063]))
        self.assertTrue(dayfile.default_selected(grown, cfg()))
        done = self.line(hours=0.75, logged_hours=0.75, pushed=True)
        dayfile.reopen_if_grown(done)
        self.assertTrue(done["pushed"])
        gone = self.line(hours=1.0, logged_hours=0.0, pushed=True, vanished=True)
        dayfile.reopen_if_grown(gone)                       # deleted in Odoo by hand: left alone
        self.assertTrue(gone["pushed"])

    def test_apply_edits_rounds_and_marks(self):
        data = {"lines": [self.line(), self.line(id="task-2", pushed=True)]}
        dayfile.apply_edits(data, [{"id": "task-1", "hours": 0.8, "description": " Review "},
                                   {"id": "task-2", "hours": 3}])
        self.assertEqual(data["lines"][0]["hours"], 0.75)
        self.assertEqual(data["lines"][0]["description"], "Review")
        self.assertEqual(sorted(data["lines"][0]["edited"]), ["description", "hours"])
        self.assertEqual(data["lines"][1]["hours"], 0.5)  # pushed lines are frozen

    def test_change_project_drops_task_and_survives_rebuild(self):
        data = {"lines": [self.line(id="task-3987", task_id=3987, project_id=88,
                                    task_name="Sogenam", project_name="Sogenam - Web")], "logged": []}
        dayfile.apply_edits(data, [{"id": "task-3987", "project_id": "106", "task_id": None}])
        line = data["lines"][0]
        self.assertEqual((line["project_id"], line["task_id"]), (106, None))
        self.assertEqual(sorted(line["edited"]), ["project_id", "task_id"])

        def kw(model, method, args, kwargs=None):
            return [{"id": 106, "name": "TSC - Internal", "company_id": [1, "TSC"]}] \
                if model == "project.project" else []
        dayfile.refresh_tasks(data, kw)
        self.assertEqual(line["project_name"], "TSC - Internal")
        self.assertNotIn("task_name", line)

        rebuilt = dayfile.merge([self.line(id="task-3987", task_id=3987, project_id=88)], data)
        self.assertEqual((rebuilt[0]["project_id"], rebuilt[0]["task_id"]), (106, None))

    def test_pushable(self):
        self.assertTrue(dayfile.pushable(self.line(selected=True)))
        self.assertFalse(dayfile.pushable(self.line(selected=True, pushed=True)))
        self.assertFalse(dayfile.pushable(self.line(selected=True, key_type="bucket")))
        self.assertFalse(dayfile.pushable(self.line(selected=True, task_id=None)))
        self.assertFalse(dayfile.pushable(self.line(selected=False)))

    def test_record_push(self):
        data = {"lines": [self.line(selected=True), self.line(id="task-2", selected=True)]}
        dayfile.record_push(data, [{"id": "task-1", "odoo_id": 5, "error": None},
                                   {"id": "task-2", "odoo_id": None, "error": "locked"}])
        self.assertTrue(data["lines"][0]["pushed"])
        self.assertFalse(data["lines"][0]["selected"])
        self.assertEqual(data["lines"][1]["push_error"], "locked")


class TestOdooSync(unittest.TestCase):
    def test_pick_employee_by_company(self):
        emps = [{"id": 68, "company_id": 2}, {"id": 69, "company_id": 1}]
        self.assertEqual(odoo_sync.pick_employee(emps, 1), 69)
        self.assertEqual(odoo_sync.pick_employee(emps, 5), 68)
        self.assertIsNone(odoo_sync.pick_employee([], 1))

    def test_search_tasks_by_id_ignores_project(self):
        domains = []

        def kw(model, method, args, kwargs=None):
            domains.append(args[0])
            return []
        odoo_sync.search_tasks(kw, "27678", project_id=88)
        odoo_sync.search_tasks(kw, "skills", project_id=88)
        self.assertEqual(domains[0], [("id", "=", 27678)])
        self.assertEqual(domains[1], [("project_id", "=", 88), ("name", "ilike", "skills")])

    def test_search_projects(self):
        domains = []

        def kw(model, method, args, kwargs=None):
            domains.append(args[0])
            return [{"id": 106, "name": "Internal", "partner_id": False}]
        self.assertEqual(odoo_sync.search_projects(kw, "inter"), [{"id": 106, "name": "Internal", "partner": ""}])
        odoo_sync.search_projects(kw, "106")
        self.assertEqual(domains, [[("name", "ilike", "inter")], [("id", "=", 106)]])

    def test_push_one_call_per_line_and_errors_isolated(self):
        import xmlrpc.client
        calls = []

        def kw(model, method, args, kwargs=None):
            if method == "search_read" and model == "hr.employee":
                return [{"id": 89, "company_id": [1, "TSC"]}]
            if method == "search_read":
                return []
            calls.append(args[0])
            if args[0].get("task_id") == 2:
                raise xmlrpc.client.Fault(1, "Traceback...\nUserError: period locked")
            return 500 + len(calls)

        lines = [{"id": "task-1", "task_id": 1, "project_id": 9, "hours": 0.75, "description": "Dev"},
                 {"id": "task-2", "task_id": 2, "project_id": 9, "hours": 0.25, "description": ""}]
        res = odoo_sync.push(kw, 1253, dt.date(2026, 10, 6), lines)
        self.assertEqual(res[0]["odoo_id"], 501)
        self.assertEqual(res[1]["error"], "UserError: period locked")
        self.assertEqual(calls[0], {"date": "2026-10-06", "project_id": 9, "task_id": 1,
                                    "name": "Dev", "unit_amount": 0.75, "employee_id": 89})
        self.assertEqual(calls[1]["name"], "/")


class TestConfig(unittest.TestCase):
    def test_digest_credentials_reused_and_overridden(self):
        tmp = tempfile.mkdtemp()
        digest = os.path.join(tmp, "d.json")
        own = os.path.join(tmp, "o.json")
        with open(digest, "w") as fh:
            json.dump({"login": "a@b.c", "api_key": "k1", "dev_names": ["x"]}, fh)
        with open(own, "w") as fh:
            json.dump({"api_key": "k2", "repo_projects": {"newrepo": 1}}, fh)
        c = ts_config.load(path=own, digest_path=digest, environ={})
        self.assertEqual((c["login"], c["api_key"]), ("a@b.c", "k2"))
        self.assertNotIn("dev_names", c)
        self.assertEqual(c["repo_projects"]["newrepo"], 1)
        self.assertEqual(c["repo_projects"]["european-schools"], 617)   # merged, not replaced
        self.assertEqual(c["chrome_profile_email"], "a@b.c")
        c = ts_config.load(path=own, digest_path=digest, environ={"TSC_TIMESHEET_API_KEY": "k3"})
        self.assertEqual(c["api_key"], "k3")

    def test_missing_credentials(self):
        with self.assertRaises(ts_config.ConfigError):
            ts_config.require_credentials(cfg(login=""))


if __name__ == "__main__":
    unittest.main()
