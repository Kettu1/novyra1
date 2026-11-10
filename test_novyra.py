#!/usr/bin/env python3
"""Automated test suite for Novyra 0.3.0-dev (develop/0.3.0).

Place this file next to novyra.py, then run:

    python3 test_novyra.py

Covers: betting maths, the betting book (stake deduction, cent
rounding, exact stake-limit boundaries, settlement, validation,
atomicity and double-settlement protection), database persistence,
permissions, the emergency stop, blocked-action counting, restart
round-trips, the betting pipeline invariant, autonomous-mode gating,
persistent communication history, Guardian integrity scans and the
watchdog, agent action execution via the LLM planner (including
refusal of unsafe actions), complete auditing of sensitive actions
in the actions table, reliable database connection cleanup, agent
roster uniqueness, LLM fallback behaviour, and CLI input validation.

New for 0.3.0-dev Phase 0: demo isolation (the 'demo' command never
touches the live session and never asks the live Owner), the
aggregate exposure cap (exact boundary, one-cent-over rejection,
headroom freed by settlement, Guardian consistency and tamper
detection), and owner-settled vs model-settled reporting separation
(PnL helpers, 'bets' output tags and the manager report's headline
win rate, which counts only Owner-supplied results).
New for 0.3.0-dev Phase 1 (CEO core): strategic goals (lifecycle,
persistence, task counts, reset), full task records (extended
fields, status validation, history, result submission, overdue
detection, and the backward-compatible migration from a real
0.2.2-schema database), the bounded CEO review cycle (delegation,
verification that never assumes a result, escalation, emergency
stop, decision logging), and the stored-not-enforced config
surface plus the new CLI commands.
New for 0.3.0-dev Phase 2 (Finance & KPI): the append-only financial
ledger (integer cents, EUR-only, opening balances distinct from
revenue), provenance enforced by code path (OwnerSession capability
token plus approval for owner entries, hard-coded estimate provenance
for agent entries, simulated results unbookable), database-level
immutability (triggers + connection authorizer, controlled atomic
reset with guaranteed restoration, fault-injection coverage), the
separate forecasts table, event-based task KPIs (period assignment by
completion event, re-completions counted once, documented denominator
that cannot exceed 100%, verification snapshots that later edits
cannot rewrite), general-numeric KPI snapshots with units, and the
finance CLI and CEO reporting separation.
No network access or LLM endpoint is required.

Every runtime and database opened by a test is closed in teardown,
so the suite leaves no dangling SQLite connections and produces no
resource warnings. Run it with warnings as errors to verify:

    python3 -W error::ResourceWarning test_novyra.py
"""

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import novyra as N


# runtimes opened by the test currently running; the shared base
# class closes them all in teardown
_OPEN_RUNTIMES = []


def make_runtime(db_path=":memory:"):
    """Runtime with all Owner approvals auto-granted for testing.
    Every runtime created here is closed automatically in teardown."""
    rt = N.Runtime(db_path=db_path, demo=False)
    rt.ask_owner = lambda question: True
    _OPEN_RUNTIMES.append(rt)
    return rt


# bare Database connections opened directly by a test; swept in
# teardown so a failing test can never leak a SQLite connection
_OPEN_DBS = []


def make_db(path=":memory:"):
    """Database whose connection is closed automatically in teardown,
    even when the test fails before its own cleanup runs."""
    db = N.Database(path)
    _OPEN_DBS.append(db)
    return db


class NovyraCase(unittest.TestCase):
    """Shared teardown: closes every Runtime (and the bare Database on
    self.db) the test opened, so no SQLite connection outlives its
    test and no resource warnings remain."""

    def tearDown(self):
        while _OPEN_RUNTIMES:
            rt = _OPEN_RUNTIMES.pop()
            try:
                rt.close()
            except Exception:
                pass
        while _OPEN_DBS:
            db = _OPEN_DBS.pop()
            try:
                db.close()
            except Exception:
                pass
        db = getattr(self, "db", None)
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


class TestMath(NovyraCase):
    def test_poisson_probs_normalized(self):
        p = N.match_probs(1.5, 1.1)
        self.assertAlmostEqual(sum(p), 1.0)
        for x in p:
            self.assertGreater(x, 0.0)

    def test_ev(self):
        self.assertAlmostEqual(N.ev_of(0.5, 2.0), 0.0)
        self.assertAlmostEqual(N.ev_of(0.6, 2.0), 0.2)
        self.assertAlmostEqual(N.ev_of(0.4, 2.0), -0.2)

    def test_kelly(self):
        self.assertAlmostEqual(N.kelly_fraction(0.5, 2.0), 0.0)
        self.assertAlmostEqual(N.kelly_fraction(0.6, 2.0), 0.2)
        self.assertEqual(N.kelly_fraction(0.95, 2.0), 0.25)  # capped

    def test_slug(self):
        self.assertEqual(N.slug("Risk Manager"), "risk-manager")
        self.assertEqual(N.slug("  CEO  Agent "), "ceo-agent")


class TestDatabase(NovyraCase):
    def setUp(self):
        self.db = make_db()

    def test_kv_roundtrip(self):
        self.db.kv_set("x", "42")
        self.assertEqual(self.db.kv_get("x"), "42")
        self.db.kv_set("x", "43")
        self.assertEqual(self.db.kv_get("x"), "43")

    def test_memory_search(self):
        self.db.memory_add("agent", "ceo-agent", "Likes concise reports")
        self.db.memory_add("agent", "ceo-agent", "Dislikes em dashes")
        self.db.memory_add("org", "novyra", "Owner likes Saturday scans")
        hits = self.db.memory_search("agent", "ceo-agent", "concise")
        self.assertEqual(len(hits), 1)
        self.assertEqual(self.db.memory_count("agent", "ceo-agent"), 2)

    def test_task_lifecycle(self):
        tid = self.db.task_create("Do things", "CEO Agent", "Exec")
        self.db.task_set_status(tid, "done", "ok")
        self.assertEqual(self.db.task_stats().get("done"), 1)
        with self.assertRaises(N.NovyraError):
            self.db.task_set_status(999, "done")

    def test_log_levels_and_tail(self):
        self.db.log("System", "boot", "hello")
        self.db.log("CLI", "crash", "boom", level="error")
        self.assertEqual(self.db.log_count(level="error"), 1)
        rows = self.db.log_tail(10)
        self.assertTrue(any(r["event"] == "boot" for r in rows))
        errors = self.db.log_tail(10, level="error")
        self.assertEqual(len(errors), 1)

    def test_wipe_table_rejects_unknown(self):
        with self.assertRaises(N.NovyraError):
            self.db.wipe_table("secrets")


class TestBettingBook(NovyraCase):
    def setUp(self):
        self.rt = make_runtime()
        self.book = self.rt.book

    def test_place_deducts_stake(self):
        before = self.book.bankroll
        bet_id = self.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        self.assertEqual(bet_id, 1)
        self.assertAlmostEqual(self.book.bankroll, before - 50.0)
        self.assertEqual(len(self.book.open_bets()), 1)

    def test_settle_win_credits_bankroll(self):
        before = self.book.bankroll
        bet_id = self.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        won, pnl = self.book.settle(bet_id, "home win", "simulated")
        self.assertTrue(won)
        self.assertAlmostEqual(pnl, 75.0)
        self.assertAlmostEqual(self.book.bankroll, before - 50.0 + 125.0)
        self.assertEqual(len(self.book.open_bets()), 0)

    def test_settle_loss_keeps_stake_deducted(self):
        before = self.book.bankroll
        bet_id = self.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        won, pnl = self.book.settle(bet_id, "draw", "simulated")
        self.assertFalse(won)
        self.assertAlmostEqual(pnl, -50.0)
        self.assertAlmostEqual(self.book.bankroll, before - 50.0)

    def test_settle_matches_the_correct_bet(self):
        id1 = self.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        id2 = self.book.place("C vs D", "draw", 3.4, 0.30, 40.0)
        won, pnl = self.book.settle(id1, "home win", "simulated")
        self.assertTrue(won)
        open_ids = [b["id"] for b in self.book.open_bets()]
        self.assertEqual(open_ids, [id2])

    def test_settle_rejects_bad_input(self):
        bet_id = self.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        with self.assertRaises(N.BettingError):
            self.book.settle(bet_id, "halftime", "simulated")
        with self.assertRaises(N.BettingError):
            self.book.settle(9999, "draw", "simulated")
        self.book.settle(bet_id, "draw", "simulated")
        with self.assertRaises(N.BettingError):
            self.book.settle(bet_id, "draw", "simulated")

    def test_place_validation(self):
        with self.assertRaises(N.BettingError):
            self.book.place("A vs B", "home win", 2.5, 0.45, 500.0)  # cap
        with self.assertRaises(N.BettingError):
            self.book.place("A vs B", "home win", 1.0, 0.45, 10.0)  # odds
        with self.assertRaises(N.BettingError):
            self.book.place("A vs B", "home win", 2.5, 1.2, 10.0)  # prob
        with self.assertRaises(N.BettingError):
            self.book.place("A vs B", "halftime", 2.5, 0.45, 10.0)  # outcome
        with self.assertRaises(N.BettingError):
            self.book.place("A vs B", "home win", 2.5, 0.45, 0.5)  # min stake
        # nothing was placed or deducted
        self.assertAlmostEqual(self.book.bankroll, 10000.0)
        self.assertEqual(self.book.open_bets(), [])

    def test_cap_scales_with_small_bankroll(self):
        self.rt.db.kv_set("bankroll", "40.00")
        with self.assertRaises(N.BettingError):
            self.book.place("A vs B", "home win", 2.5, 0.45, 1.0)  # cap 0.80


class TestPermissions(NovyraCase):
    def test_denied_activation_records_denial(self):
        rt = make_runtime()
        rt.ask_owner = lambda question: False
        result = rt.activate_department("Betting Department")
        self.assertIn("Declined", result)
        self.assertFalse(rt.dept_active("Betting Department"))
        self.assertEqual(rt.permissions.denied_actions, 1)
        self.assertEqual(rt.db.approvals_count(), 1)  # denial on file

    def test_emergency_stop_blocks_actions(self):
        rt = make_runtime()
        rt.permissions.halt()
        self.assertTrue(rt.permissions.emergency_stop)
        self.assertFalse(rt.permissions.guard("scan", "Betting Manager"))
        issues = rt.guardian.monitor()
        self.assertTrue(any("emergency" in i.lower() for i in issues))

    def test_emergency_stop_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.db")
            rt = N.Runtime(db_path=path, demo=False)
            self.addCleanup(rt.close)
            rt.permissions.halt()
            rt2 = N.Runtime(db_path=path, demo=False)
            self.addCleanup(rt2.close)
            self.assertTrue(rt2.permissions.emergency_stop)

    def test_approved_activation_persists_approval(self):
        rt = make_runtime()
        rt.activate_department("Betting Department")
        self.assertTrue(rt.dept_active("Betting Department"))
        self.assertEqual(rt.db.approvals_count(), 1)


class TestPersistence(NovyraCase):
    def test_full_restart_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "novyra.db")
            rt = make_runtime(path)
            rt.activate_department("Betting Department")
            bet_id = rt.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
            rt.db.memory_add("org", "novyra", "Owner likes Saturday scans")
            rt.db.task_create("task one", "CEO Agent", "Exec")
            bank = rt.book.bankroll

            rt2 = N.Runtime(db_path=path, demo=False)
            self.addCleanup(rt2.close)
            self.assertTrue(rt2.dept_active("Betting Department"))
            self.assertEqual(len(rt2.book.open_bets()), 1)
            self.assertAlmostEqual(rt2.book.bankroll, bank)
            hits = rt2.db.memory_search("org", "novyra", "Saturday")
            self.assertEqual(len(hits), 1)
            # the bet from the previous session can be settled normally
            won, pnl = rt2.book.settle(bet_id, "home win", "owner")
            self.assertTrue(won)
            self.assertAlmostEqual(rt2.book.bankroll, bank + 125.0)

    def test_reset_wipes_operations_but_keeps_roster(self):
        rt = make_runtime()
        rt.activate_department("Betting Department")
        rt.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        rt.reset_data()
        self.assertTrue(rt.dept_active("Betting Department"))
        self.assertAlmostEqual(rt.book.bankroll, 10000.0)
        self.assertEqual(rt.db.bets_all(), [])
        self.assertEqual(rt.db.tasks_all(), [])


class TestPipeline(NovyraCase):
    def test_scan_runs_and_ledger_stays_consistent(self):
        rt = make_runtime()
        rt.set_autonomy(True)  # full autonomous pipeline for this test
        rt.activate_department("Betting Department")
        for _ in range(5):
            rt.run_betting_scan()
        rows = rt.db.bets_all()
        settled = [r for r in rows if r["status"] != "open"]
        open_bets = [r for r in rows if r["status"] == "open"]
        expected = 10000.0 \
            + sum((r["pnl"] or 0.0) for r in settled) \
            - sum(r["stake"] for r in open_bets)
        self.assertAlmostEqual(rt.book.bankroll, expected, places=2)
        self.assertEqual(int(rt.db.kv_get("scans", "0")), 5)

    def test_scan_blocked_during_emergency_stop(self):
        rt = make_runtime()
        rt.activate_department("Betting Department")
        rt.permissions.halt()
        rt.run_betting_scan()
        self.assertEqual(rt.db.bets_all(), [])

    def test_propose_manager_actually_manages(self):
        rt = make_runtime()
        result = rt.propose("Ecommerce Manager")
        self.assertIn("Ecommerce Manager", result)
        mgr = rt.manager_of("Ecommerce Department")
        self.assertIsNotNone(mgr)
        self.assertTrue(mgr.is_manager)


class TestRosterAndBus(NovyraCase):
    def test_unique_ids_for_duplicate_names(self):
        rt = make_runtime()
        a = rt.instantiate("Advertising", "Ecommerce Department",
                           "runs ads", "worker")
        b = rt.instantiate("Advertising", "Marketing Department",
                           "plans ads", "worker")
        self.assertNotEqual(a.id, b.id)

    def test_bus_messages_are_audited(self):
        rt = make_runtime()
        rt.bus.send("CEO Agent", "Owner", "test subject", "hello")
        rows = rt.db.log_tail(20)
        self.assertTrue(any(r["event"] == "message"
                            and "test subject" in r["detail"]
                            for r in rows))


class TestLLMIntegration(NovyraCase):
    def test_unconfigured_client_returns_none(self):
        client = N.LLMClient()
        if client.configured:
            self.skipTest("NOVYRA_LLM_URL is set in the environment")
        self.assertIsNone(client.chat("system", "hello"))

    def test_agent_uses_llm_when_available(self):
        rt = make_runtime()
        rt.llm.configured = True
        rt.llm.chat = lambda system, user: "LLM ANALYSIS: all good"
        worker = rt.find("developer-agent")
        self.assertIsNotNone(worker)
        reply = worker.think("status report please")
        self.assertIn("LLM ANALYSIS", reply)

    def test_agent_falls_back_without_llm(self):
        rt = make_runtime()
        self.assertFalse(rt.llm.configured)
        worker = rt.find("developer-agent")
        reply = worker.think("status report please")
        self.assertIn("Developer Agent", reply)
        self.assertIn("T00", reply)  # task logged on the board

    def test_llm_failure_never_crashes_agent(self):
        rt = make_runtime()
        rt.llm.configured = True

        def broken(system, user):
            rt.llm.last_error = "HTTPError: 401"
            return None

        rt.llm.chat = broken
        worker = rt.find("developer-agent")
        reply = worker.think("status report please")
        self.assertIn("Developer Agent", reply)
        self.assertEqual(rt.db.log_count(level="warn") >= 1, True)


class TestCLIValidation(NovyraCase):
    def test_bad_commands_do_not_crash(self):
        rt = make_runtime()
        cli = N.CLI(rt)
        bad_inputs = [
            "log abc", "log -5", "log 0",
            "settle", "settle abc home", "settle 1 bogus",
            "recall", "remember", "activate", "say",
            "propose", "meeting", "tasks", "bets",
            "autonomy", "autonomy maybe",
            "messages abc", "messages -1", "messages",
            "do", "guardian", "integrity",
            "bogus command that means nothing",
            "activate Not A Department",
        ]
        for line in bad_inputs:
            cli.dispatch(line)  # must not raise

    def test_settle_requires_valid_args_and_open_bet(self):
        rt = make_runtime()
        rt.activate_department("Betting Department")
        cli = N.CLI(rt)
        cli.dispatch("settle 1 draw")  # no bet with id 1 yet
        bet_id = rt.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        cli.dispatch("settle %d draw" % bet_id)
        rows = rt.db.bets_all()
        self.assertEqual(rows[0]["status"], "settled")
        self.assertEqual(rows[0]["result"], "draw")

    def test_unknown_text_reaches_ceo(self):
        rt = make_runtime()
        cli = N.CLI(rt)
        cli.dispatch("what is our situation")
        # CEO routes it as an owner request onto the task board
        self.assertEqual(len(rt.db.tasks_all()), 1)


class TestAtomicity(NovyraCase):
    """Bet placement and settlement must be all-or-nothing."""

    def test_failed_bet_insert_rolls_back_bankroll(self):
        rt = make_runtime()
        book = rt.book
        before = book.bankroll

        def explode(*args, **kwargs):
            raise N.NovyraError("simulated disk error")

        rt.db.bet_add = explode
        with self.assertRaises(N.NovyraError):
            book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        self.assertAlmostEqual(book.bankroll, before)
        self.assertEqual(rt.db.bets_all(), [])

    def test_failed_settle_payout_rolls_back_bet(self):
        rt = make_runtime()
        book = rt.book
        before = book.bankroll
        bet_id = book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        original_kv_set = rt.db.kv_set

        def angry_kv_set(key, value, commit=True):
            if key == "bankroll":
                raise N.NovyraError("simulated write error")
            return original_kv_set(key, value, commit)

        rt.db.kv_set = angry_kv_set
        with self.assertRaises(N.NovyraError):
            book.settle(bet_id, "home win", "simulated")
        rt.db.kv_set = original_kv_set
        # the bet is untouched and no money moved
        bet = rt.db.bet_get(bet_id)
        self.assertEqual(bet["status"], "open")
        self.assertAlmostEqual(book.bankroll, before - 50.0)

    def test_double_settle_credits_bankroll_only_once(self):
        rt = make_runtime()
        book = rt.book
        bet_id = book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        won, pnl = book.settle(bet_id, "home win", "simulated")
        self.assertTrue(won)
        after_first = book.bankroll
        with self.assertRaises(N.BettingError):
            book.settle(bet_id, "home win", "simulated")
        self.assertAlmostEqual(book.bankroll, after_first)
        self.assertAlmostEqual(book.realized_pnl(), pnl)

    def test_stake_rounded_to_cents_and_invariant_exact(self):
        rt = make_runtime()
        book = rt.book
        bet_id = book.place("A vs B", "home win", 2.5, 0.45, 33.333)
        bet = rt.db.bet_get(bet_id)
        self.assertAlmostEqual(bet["stake"], 33.33)
        self.assertAlmostEqual(book.bankroll, 10000.0 - 33.33)
        book.settle(bet_id, "home win", "owner")
        expected = (book.initial_bankroll + book.realized_pnl()
                    - book.open_exposure())
        self.assertAlmostEqual(book.bankroll, expected, places=2)


class TestAutonomy(NovyraCase):
    """Autonomous simulated money movements stay disabled unless the
    Owner explicitly authorises them."""

    def _ctx(self):
        return {"fixture": {"home": "Helsinki Wolves", "away": "Turku United"},
                "pick": ("home win", 0.45, 2.5), "stake": 50.0, "run": 1}

    def test_pipeline_asks_approval_when_autonomy_off(self):
        rt = make_runtime()
        rt.activate_department("Betting Department")
        pm = rt.find("portfolio-manager")
        ctx = self._ctx()
        before = rt.db.approvals_count()
        pm.step(ctx)
        self.assertIn("bet_id", ctx)
        self.assertEqual(rt.db.approvals_count(), before + 1)
        review = rt.find("post-match-review")
        ctx["probs"] = (0.45, 0.30, 0.25)
        reply = review.step(ctx)
        bet = rt.db.bet_get(ctx["bet_id"])
        self.assertEqual(bet["status"], "open")  # no autonomous settlement
        self.assertIn("stays open", reply)

    def test_pipeline_respects_declined_bet(self):
        rt = make_runtime()
        rt.activate_department("Betting Department")
        rt.ask_owner = lambda question: False
        pm = rt.find("portfolio-manager")
        ctx = self._ctx()
        pm.step(ctx)
        self.assertNotIn("bet_id", ctx)
        self.assertEqual(rt.db.bets_all(), [])
        self.assertAlmostEqual(rt.book.bankroll, 10000.0)
        self.assertEqual(rt.permissions.denied_actions, 1)

    def test_autonomous_mode_settles_without_per_bet_approval(self):
        rt = make_runtime()
        rt.set_autonomy(True)
        rt.activate_department("Betting Department")
        pm = rt.find("portfolio-manager")
        ctx = self._ctx()
        before = rt.db.approvals_count()
        pm.step(ctx)
        self.assertIn("bet_id", ctx)
        self.assertEqual(rt.db.approvals_count(), before)  # no prompt
        review = rt.find("post-match-review")
        ctx["probs"] = (0.45, 0.30, 0.25)
        review.step(ctx)
        bet = rt.db.bet_get(ctx["bet_id"])
        self.assertEqual(bet["status"], "settled")

    def test_cmd_autonomy_toggle_requires_approval_to_enable(self):
        rt = make_runtime()
        cli = N.CLI(rt)
        cli.dispatch("autonomy maybe")  # usage, no crash, no change
        self.assertFalse(rt.sim_autonomy)
        cli.dispatch("autonomy on")     # ask_owner auto-grants
        self.assertTrue(rt.sim_autonomy)
        cli.dispatch("autonomy off")    # disabling is always allowed
        self.assertFalse(rt.sim_autonomy)
        rt.ask_owner = lambda question: False
        cli.dispatch("autonomy on")     # denied stays off
        self.assertFalse(rt.sim_autonomy)


class TestMessagePersistence(NovyraCase):
    def test_messages_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "novyra.db")
            rt = make_runtime(path)
            rt.bus.send("CEO Agent", "Owner", "weekly summary",
                        "all departments on track")
            rt.bus.send("Guardian Agent", "CEO Agent", "patrol report",
                        "nothing to flag")
            rt2 = N.Runtime(db_path=path, demo=False)
            self.addCleanup(rt2.close)
            rows = rt2.db.messages_tail(10)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["subject"], "weekly summary")
            self.assertEqual(rows[1]["recipient"], "CEO Agent")
            self.assertEqual(rt2.db.messages_count(), 2)

    def test_cli_messages_command_and_validation(self):
        rt = make_runtime()
        cli = N.CLI(rt)
        cli.dispatch("messages abc")  # invalid count, must not crash
        rt.bus.send("CEO Agent", "Owner", "hello", "body text")
        cli.dispatch("messages 5")    # must not crash
        self.assertEqual(rt.db.messages_count(), 1)

    def test_reset_wipes_messages_and_actions(self):
        rt = make_runtime()
        rt.bus.send("CEO Agent", "Owner", "hello")
        rt.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        self.assertEqual(rt.db.messages_count(), 1)
        self.assertEqual(rt.db.actions_count(), 1)
        rt.reset_data()
        self.assertEqual(rt.db.messages_count(), 0)
        # the wipe clears the watch; exactly one watched action
        # remains, the reset itself
        self.assertEqual(rt.db.actions_count(), 1)
        row = rt.db.conn.execute(
            "SELECT action FROM actions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["action"], "reset data")


class TestGuardian(NovyraCase):
    def test_full_scan_clean_system(self):
        rt = make_runtime()
        self.assertEqual(rt.guardian.full_scan(), [])

    def test_full_scan_detects_ledger_tampering(self):
        rt = make_runtime()
        rt.db.kv_set("bankroll", "9500.00")  # money vanishes off-book
        findings = rt.guardian.full_scan()
        self.assertTrue(any("ledger" in f.lower() for f in findings))
        self.assertTrue(any("critical" in f.lower() for f in findings))

    def test_full_scan_detects_corrupt_settlement(self):
        rt = make_runtime()
        bet_id = rt.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        rt.book.settle(bet_id, "home win", "owner")
        rt.db.conn.execute("UPDATE bets SET pnl = NULL WHERE id = ?",
                           (bet_id,))
        rt.db.conn.commit()
        findings = rt.guardian.full_scan()
        self.assertTrue(any("settled bet" in f.lower() for f in findings))

    def test_full_scan_flags_enabled_autonomy(self):
        rt = make_runtime()
        rt.set_autonomy(True)
        findings = rt.guardian.full_scan()
        self.assertTrue(any("autonomous" in f.lower() for f in findings))

    def test_blocked_actions_counted_separately_from_denials(self):
        rt = make_runtime()
        rt.permissions.halt()
        rt.permissions.guard("run scan", "Betting Manager")
        self.assertEqual(rt.permissions.blocked_actions, 1)
        self.assertEqual(rt.permissions.denied_actions, 0)
        issues = rt.guardian.monitor()
        self.assertTrue(any("blocked" in i.lower() for i in issues))

    def test_watchdog_advances_on_new_warnings(self):
        rt = make_runtime()
        cli = N.CLI(rt)
        cli.watchdog()  # baseline patrol
        cursor0 = int(rt.db.kv_get("watchdog_cursor", "0"))
        rt.db.log("Test", "warning", "something odd", level="warn")
        cli.watchdog()
        cursor1 = int(rt.db.kv_get("watchdog_cursor", "0"))
        self.assertGreater(cursor1, cursor0)

    def test_money_actions_are_recorded_for_the_guardian(self):
        rt = make_runtime()
        bet_id = rt.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        self.assertEqual(rt.db.actions_count(allowed=True), 1)
        rt.book.settle(bet_id, "home win", "owner", actor="Owner")
        self.assertEqual(rt.db.actions_count(allowed=True), 2)
        self.assertEqual(rt.db.actions_count(allowed=False), 0)

    def test_cli_guardian_command(self):
        rt = make_runtime()
        cli = N.CLI(rt)
        cli.dispatch("guardian")  # must not crash on a clean system
        self.assertEqual(rt.guardian.full_scan(), [])


class TestAgentActions(NovyraCase):
    """Agents execute whitelisted actions; unsafe plans are refused."""

    def _llm_runtime(self, reply):
        rt = make_runtime()
        rt.llm.configured = True
        rt.llm.chat = lambda system, user: reply
        return rt

    def test_llm_act_executes_whitelisted_actions(self):
        plan = ('[{"action":"create_task","title":"Draft weekly report"},'
                '{"action":"remember","fact":"Owner wants concise '
                'output"},'
                '{"action":"message","recipient":"CEO Agent",'
                '"subject":"update","body":"done"},'
                '{"action":"search_memory","keyword":"report"}]')
        rt = self._llm_runtime(plan)
        worker = rt.find("developer-agent")
        tasks_before = len(rt.db.tasks_all())
        reply = worker.act("prepare the weekly report")
        self.assertEqual(len(rt.db.tasks_all()), tasks_before + 1)
        self.assertTrue(any(t["title"] == "Draft weekly report"
                            for t in rt.db.tasks_all()))
        hits = rt.db.memory_search("agent", worker.id, "concise")
        self.assertEqual(len(hits), 1)
        self.assertEqual(rt.db.messages_count(), 1)
        self.assertIn("executed", reply.lower())
        self.assertEqual(rt.db.actions_count(allowed=True), 4)

    def test_llm_act_refuses_unsafe_or_unknown_actions(self):
        rt = self._llm_runtime(
            '[{"action":"wipe_database"},'
            '{"action":"message","recipient":"Ghost Agent",'
            '"subject":"x","body":"y"}]')
        worker = rt.find("developer-agent")
        reply = worker.act("delete everything and message a ghost")
        # whole plan unsafe (first item) -> deterministic fallback
        self.assertIn("Developer Agent", reply)
        rt2 = self._llm_runtime(
            '[{"action":"message","recipient":"Ghost Agent",'
            '"subject":"x","body":"y"}]')
        worker2 = rt2.find("developer-agent")
        reply2 = worker2.act("message a ghost")
        # plan valid, but the recipient is refused and recorded
        self.assertEqual(rt2.db.actions_count(allowed=False), 1)
        self.assertIn("Developer Agent", reply2)  # fallback used

    def test_llm_act_invalid_json_falls_back(self):
        rt = self._llm_runtime("I cannot answer in JSON, sorry.")
        worker = rt.find("developer-agent")
        reply = worker.act("do something")
        self.assertIn("Developer Agent", reply)
        self.assertTrue(rt.db.tasks_all())  # fallback logged real work

    def test_cli_do_command_executes_and_validates(self):
        plan = ('[{"action":"create_task","title":"Review pipeline"}]')
        rt = self._llm_runtime(plan)
        cli = N.CLI(rt)
        cli.dispatch("do")  # usage, no crash
        cli.dispatch("do Developer Agent review the pipeline")
        self.assertTrue(any(t["title"] == "Review pipeline"
                            for t in rt.db.tasks_all()))

    def test_do_blocked_for_workers_during_emergency_stop(self):
        calls = []

        def spy(system, user):
            calls.append(user)
            return '[{"action":"none"}]'

        rt = make_runtime()
        rt.llm.configured = True
        rt.llm.chat = spy
        rt.permissions.halt()
        cli = N.CLI(rt)
        cli.dispatch("do Developer Agent anything")
        self.assertEqual(calls, [])  # the LLM was never even consulted
        self.assertEqual(rt.db.tasks_all(), [])


class TestStakeLimits(NovyraCase):
    """Stake limits are enforced in integer cents: a stake that
    displays as exactly the cap is accepted, one cent below too,
    one cent above is rejected, and the cap never rounds up."""

    def setUp(self):
        self.rt = make_runtime()
        self.book = self.rt.book

    def test_stake_exactly_at_cap_is_accepted(self):
        # bankroll 100.00 -> cap exactly 2.00
        self.rt.db.kv_set("bankroll", "100.00")
        self.book.place("A vs B", "home win", 2.5, 0.45, 2.0)
        self.assertAlmostEqual(self.book.bankroll, 98.00)
        self.assertEqual(len(self.book.open_bets()), 1)

    def test_stake_one_cent_below_cap_is_accepted(self):
        self.rt.db.kv_set("bankroll", "100.00")
        self.book.place("A vs B", "home win", 2.5, 0.45, 1.99)
        self.assertAlmostEqual(self.book.bankroll, 98.01)

    def test_stake_one_cent_above_cap_is_rejected(self):
        self.rt.db.kv_set("bankroll", "100.00")
        with self.assertRaises(N.BettingError) as cm:
            self.book.place("A vs B", "home win", 2.5, 0.45, 2.01)
        self.assertIn("cap", str(cm.exception))
        # nothing was placed or deducted
        self.assertAlmostEqual(self.book.bankroll, 100.00)
        self.assertEqual(self.book.open_bets(), [])

    def test_cap_boundary_at_default_bankroll(self):
        # default bankroll 10000.00 -> cap exactly 200.00
        self.book.place("A vs B", "home win", 2.5, 0.45, 200.0)
        self.assertAlmostEqual(self.book.bankroll, 9800.00)
        with self.assertRaises(N.BettingError):
            self.book.place("C vs D", "draw", 3.4, 0.30, 200.01)

    def test_cap_with_fractional_cents_truncates_never_rounds_up(self):
        # bankroll 100.35 -> raw cap 2.007; the fraction must truncate
        # to 2.00 (old float code could round the cap up to a
        # rejectable 2.01 and then refuse a stake that displayed as
        # exactly the cap)
        self.rt.db.kv_set("bankroll", "100.35")
        self.book.place("A vs B", "home win", 2.5, 0.45, 2.0)
        self.assertAlmostEqual(self.book.bankroll, 98.35)
        with self.assertRaises(N.BettingError):
            self.book.place("C vs D", "draw", 3.4, 0.30, 2.01)

    def test_risk_manager_never_proposes_above_the_cap(self):
        rt = self.rt
        rt.activate_department("Betting Department")
        rt.db.kv_set("bankroll", "100.35")
        risk = rt.find("risk-manager")
        ctx = {"fixture": {"home": "Helsinki", "away": "Turku"},
               "pick": ("home win", 0.90, 8.0), "run": 1}
        reply = risk.step(ctx)
        self.assertIn("stake", ctx)
        self.assertLessEqual(ctx["stake"], 2.0)  # floored, not rounded up
        self.assertIn("Approved stake", reply)
        # and the proposed stake is actually placeable at this bankroll
        self.book.place("Helsinki vs Turku", "home win", 8.0, 0.90,
                        ctx["stake"])


class TestResourceCleanup(NovyraCase):
    """A Runtime owns exactly one SQLite connection; close() must
    release it, be idempotent, work as a context manager, and the
    shutdown must be audited and durable."""

    def test_close_releases_the_connection(self):
        rt = make_runtime()
        rt.db.kv_set("x", "1")
        rt.close()
        self.assertTrue(rt.db.closed)
        self.assertTrue(rt._closed)
        with self.assertRaises(sqlite3.Error):
            rt.db.kv_get("x")  # the connection is really gone

    def test_close_is_idempotent(self):
        rt = make_runtime()
        rt.close()
        rt.close()  # second close must be a harmless no-op

    def test_runtime_context_manager_closes_database(self):
        with make_runtime() as rt:
            rt.db.kv_set("x", "1")
        self.assertTrue(rt.db.closed)

    def test_database_context_manager_closes(self):
        with N.Database(":memory:") as db:
            db.kv_set("x", "1")
        self.assertTrue(db.closed)

    def test_bad_database_path_fails_without_leaking(self):
        with self.assertRaises(N.NovyraError):
            N.Database("/no/such/dir/novyra.db")

    def test_shutdown_is_audited_and_data_survives_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.db")
            rt = make_runtime(path)
            rt.book.place("A vs B", "home win", 2.5, 0.45, 10.0)
            bank = rt.book.bankroll
            rt.close()
            rt2 = N.Runtime(db_path=path, demo=False)
            self.addCleanup(rt2.close)
            rows = rt2.db.log_tail(5)
            self.assertTrue(any(r["event"] == "shutdown"
                                for r in rows))
            # closing must never lose data: the ledger is intact
            self.assertAlmostEqual(rt2.book.bankroll, bank)
            self.assertEqual(len(rt2.book.open_bets()), 1)


class TestAuditLogging(NovyraCase):
    """Approvals, denials, blocked actions, refused money operations
    and other sensitive operations must all reach the actions table
    with their outcome, so the Guardian watches the whole permission
    and approval system."""

    def _last_action(self, rt):
        return rt.db.conn.execute(
            "SELECT actor, action, detail, allowed FROM actions "
            "ORDER BY id DESC LIMIT 1").fetchone()

    def test_approval_and_denial_are_recorded(self):
        rt = make_runtime()
        rt.activate_department("Betting Department")  # approved
        self.assertEqual(rt.db.actions_count(allowed=True), 1)
        rt.ask_owner = lambda question: False
        rt.activate_department("Ecommerce Department")  # denied
        self.assertEqual(rt.db.actions_count(allowed=False), 1)
        row = self._last_action(rt)
        self.assertEqual(row["actor"], "Owner")
        self.assertEqual(row["action"], "activate department")

    def test_blocked_action_during_stop_is_recorded(self):
        rt = make_runtime()
        rt.permissions.halt()
        self.assertFalse(rt.permissions.guard("run betting scan",
                                             "Betting Manager"))
        self.assertEqual(rt.db.actions_count(allowed=False), 1)
        row = self._last_action(rt)
        self.assertEqual(row["actor"], "Betting Manager")
        self.assertIn("scan", row["action"])
        self.assertIn("blocked", row["detail"])

    def test_over_cap_placement_is_recorded_as_refused(self):
        rt = make_runtime()
        with self.assertRaises(N.BettingError):
            rt.book.place("A vs B", "home win", 2.5, 0.45, 500.0,
                          actor="Portfolio Manager")
        self.assertEqual(rt.db.actions_count(allowed=False), 1)
        row = self._last_action(rt)
        self.assertEqual(row["actor"], "Portfolio Manager")
        self.assertEqual(row["action"], "place bet")
        self.assertIn("refused", row["detail"])

    def test_double_settlement_is_recorded_as_refused(self):
        rt = make_runtime()
        bet_id = rt.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        self.assertEqual(rt.db.actions_count(allowed=False), 0)
        rt.book.settle(bet_id, "home win", "owner", actor="Owner")
        with self.assertRaises(N.BettingError):
            rt.book.settle(bet_id, "draw", "owner", actor="Owner")
        self.assertEqual(rt.db.actions_count(allowed=False), 1)
        self.assertEqual(rt.db.actions_count(allowed=True), 2)
        row = self._last_action(rt)
        self.assertIn("refused", row["detail"])

    def test_stop_resume_and_autonomy_changes_are_recorded(self):
        rt = make_runtime()
        rt.permissions.halt()
        rt.permissions.do_resume()
        rt.set_autonomy(True)
        rt.set_autonomy(False)
        rows = [(r["actor"], r["action"], r["allowed"])
                for r in rt.db.conn.execute(
                    "SELECT actor, action, allowed FROM actions "
                    "ORDER BY id").fetchall()]
        self.assertIn(("Owner", "emergency stop", 1), rows)
        self.assertIn(("Owner", "resume", 1), rows)
        self.assertIn(("Owner", "autonomy on", 1), rows)
        self.assertIn(("Owner", "autonomy off", 1), rows)

    def test_reset_is_recorded_after_the_wipe(self):
        rt = make_runtime()
        rt.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        rt.reset_data()
        self.assertEqual(rt.db.actions_count(), 1)  # only the reset
        row = self._last_action(rt)
        self.assertEqual(row["actor"], "Owner")
        self.assertEqual(row["action"], "reset data")


class TestDemoIsolation(NovyraCase):
    """Phase 0 / audit E1: the 'demo' command runs on a throwaway
    in-memory runtime. Nothing the demo does reaches the live
    session, and the demo never asks the live Owner for anything."""

    def test_demo_command_leaves_the_live_session_untouched(self):
        rt = make_runtime()
        rt.activate_department("Betting Department")
        rt.book.place("A vs B", "home win", 2.5, 0.45, 100.0)
        before = {
            "bankroll": rt.book.bankroll,
            "bets": len(rt.db.bets_all()),
            "actions": rt.db.actions_count(),
            "approvals": rt.db.approvals_count(),
            "autonomy": rt.sim_autonomy,
            "agents": len(rt.agents),
            "tasks": len(rt.db.tasks_all()),
            "messages": rt.db.messages_count(),
        }
        asked = []
        rt.ask_owner = lambda question: asked.append(question) or True
        cli = N.CLI(rt)
        cli.dispatch("demo")
        # the demo runs on its own runtime: the live Owner is never
        # prompted for an approval, and no live state moves
        self.assertEqual(asked, [])
        self.assertAlmostEqual(rt.book.bankroll, before["bankroll"])
        self.assertFalse(rt.sim_autonomy)
        self.assertEqual(rt.sim_autonomy, before["autonomy"])
        self.assertEqual(len(rt.db.bets_all()), before["bets"])
        self.assertEqual(len(rt.db.tasks_all()), before["tasks"])
        self.assertEqual(len(rt.agents), before["agents"])
        self.assertEqual(rt.db.messages_count(), before["messages"])
        self.assertEqual(rt.db.actions_count(), before["actions"])
        self.assertEqual(rt.db.approvals_count(), before["approvals"])
        # the live session still works normally afterwards
        cli.dispatch("bets")
        self.assertAlmostEqual(rt.book.bankroll, before["bankroll"])


class TestAggregateExposure(NovyraCase):
    """Phase 0 / audit E2: total open exposure is capped at 10% of
    the bankroll, enforced transactionally at placement and measured
    against the post-placement state, so the Guardian's 10% scan can
    never flag a bet the book accepted.

    The per-bet cap is 2% of the current bankroll, which shrinks
    with every placement, so four successive maximum bets are
    200.00, 196.00, 192.08 and 188.23 (all in integer cents, never
    rounded up). After them the bankroll is 9223.69 with 776.31 at
    risk (8.4%), and the aggregate cap allows exactly one more
    132.78 stake (909.09 = int(0.10 * 909091)); one cent more is
    refused."""

    def setUp(self):
        self.rt = make_runtime()
        self.book = self.rt.book

    def _place(self, stake):
        return self.book.place("A vs B", "home win", 2.5, 0.45, stake)

    def _fill_with_four_max_bets(self):
        self._place(200.0)
        self._place(196.0)
        self._place(192.08)
        self._place(188.23)

    def test_fifth_max_bet_is_rejected_by_the_aggregate_cap(self):
        # four successive per-bet caps fill exposure to 776.31 on a
        # 9223.69 bankroll; the fifth cap-sized bet (184.47) would
        # push exposure to 960.78 over the 903.92 aggregate limit
        self._fill_with_four_max_bets()
        self.assertAlmostEqual(self.book.bankroll, 9223.69)
        self.assertAlmostEqual(self.book.open_exposure(), 776.31)
        with self.assertRaises(N.BettingError) as cm:
            self._place(184.47)
        self.assertIn("exposure", str(cm.exception))
        self.assertEqual(len(self.book.open_bets()), 4)
        self.assertAlmostEqual(self.book.bankroll, 9223.69)

    def test_aggregate_boundary_exact_and_one_cent_over(self):
        # after the four max bets the largest legal stake is exactly
        # 132.78: exposure 909.09 equals int(0.10 * 909091)
        self._fill_with_four_max_bets()
        self._place(132.78)
        self.assertEqual(len(self.book.open_bets()), 5)
        self.assertAlmostEqual(self.book.open_exposure(), 909.09)
        self.assertAlmostEqual(self.book.bankroll, 9090.91)
        # one cent more is refused and audited
        with self.assertRaises(N.BettingError) as cm:
            self._place(132.79)
        self.assertIn("exposure", str(cm.exception))
        self.assertEqual(len(self.book.open_bets()), 5)
        self.assertAlmostEqual(self.book.bankroll, 9090.91)
        self.assertEqual(self.rt.db.actions_count(allowed=False), 1)

    def test_settlement_frees_exposure_headroom(self):
        ids = [self._place(200.0), self._place(196.0),
               self._place(192.08), self._place(188.23)]
        with self.assertRaises(N.BettingError):
            self._place(184.47)
        # settling a bet as a loss frees its stake from the exposure
        # without changing the bankroll (stakes are deducted up front)
        self.book.settle(ids[0], "draw")
        self.assertAlmostEqual(self.book.bankroll, 9223.69)
        self._place(184.47)
        self.assertEqual(len(self.book.open_bets()), 4)
        self.assertAlmostEqual(self.book.bankroll, 9039.22)

    def test_legal_placements_never_trip_the_guardian(self):
        # fill exposure to exactly the legal maximum; the Guardian's
        # exposure scan must stay silent because the enforcement uses
        # the same post-placement state the scan will see
        self._fill_with_four_max_bets()
        self._place(132.78)
        findings = self.rt.guardian.full_scan()
        self.assertFalse(any("exposure" in f.lower()
                             for f in findings))

    def test_tampered_bankroll_still_flags_in_guardian(self):
        # enforcement makes an organic breach impossible, so a breach
        # means tampering: the Guardian must still detect it
        self._fill_with_four_max_bets()
        self.rt.db.kv_set("bankroll", "7500.00")
        findings = self.rt.guardian.full_scan()
        self.assertTrue(any("exposure" in f.lower()
                            for f in findings))


class TestResultSetSeparation(NovyraCase):
    """Phase 0 / audit E3: results the Owner supplied ('settle'
    command) are reported separately from the model's own simulated
    results, and headline win-rate evidence counts only
    owner-settled bets."""

    def _mixed_ledger(self):
        rt = make_runtime()
        rt.activate_department("Betting Department")
        book = rt.book
        a = book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        b = book.place("C vs D", "draw", 3.4, 0.30, 40.0)
        c = book.place("E vs F", "away win", 3.0, 0.25, 30.0)
        d = book.place("G vs H", "home win", 2.2, 0.50, 20.0)
        book.settle(a, "home win", "owner", actor="Owner")   # +75.00
        book.settle(c, "draw", "owner", actor="Owner")       # -30.00
        book.settle(b, "draw", "simulated")                 # +96.00
        book.settle(d, "home win", "simulated")             # +24.00
        # owner-settled: 2 bets, 1 won, PnL +45.00
        # model-settled: 2 bets, PnL +120.00
        return rt

    def test_pnl_helpers_split_by_result_source(self):
        rt = self._mixed_ledger()
        book = rt.book
        self.assertAlmostEqual(book.realized_pnl(), 165.0)
        self.assertAlmostEqual(book.realized_pnl("owner"), 45.0)
        self.assertAlmostEqual(book.realized_pnl("simulated"), 120.0)
        self.assertEqual(len(book.settled_rows("owner")), 2)
        self.assertEqual(len(book.settled_rows("simulated")), 2)
        self.assertEqual(len(book.settled_rows()), 4)

    def test_bets_command_separates_the_sources(self):
        rt = self._mixed_ledger()
        cli = N.CLI(rt)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.dispatch("bets")
        out = buf.getvalue()
        # every settled row is tagged with its result source
        self.assertIn("[owner]", out)
        self.assertIn("[model]", out)
        self.assertIn("Owner-settled: 2 bet(s), 1 won, win rate "
                      "50.0%, PnL +45.00.", out)
        # the model line is present, labelled, and wraps safely
        self.assertIn("Model-settled (synthetic results, not "
                      "evidence of edge): 2 bet(s),", out)
        self.assertIn("+120.00.", out)

    def test_bets_command_has_no_model_line_when_all_owner_settled(self):
        rt = make_runtime()
        bet = rt.book.place("A vs B", "home win", 2.5, 0.45, 50.0)
        rt.book.settle(bet, "home win", "owner", actor="Owner")
        cli = N.CLI(rt)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.dispatch("bets")
        out = buf.getvalue()
        self.assertIn("Owner-settled: 1 bet(s), 1 won, win rate "
                      "100.0%, PnL +75.00.", out)
        self.assertNotIn("Model-settled", out)

    def test_manager_report_headline_uses_owner_results_only(self):
        rt = self._mixed_ledger()
        mgr = rt.manager_of("Betting Department")
        text = mgr.report()
        # the naive win rate over all settled bets would be 3/4 =
        # 75.0%; the headline must stay at the owner-settled 50.0%
        self.assertIn("Owner-settled: 2 bet(s), win rate 50.0%, "
                      "PnL +45.00 (the only performance evidence)",
                      text)
        self.assertIn("Model-settled (synthetic results, not "
                      "evidence of edge): 2 bet(s), PnL +120.00",
                      text)
        self.assertIn("(owner-settled +45.00, model-settled "
                      "+120.00)", text)
        self.assertNotIn("75.0%", text)


class TestGoals(NovyraCase):
    """Phase 1: strategic goals are persistent, validated and wiped
    by reset like all operational data."""

    def setUp(self):
        self.db = make_db()

    def test_goal_lifecycle(self):
        gid = self.db.goal_create("Grow revenue", "more sales",
                                  "10 new customers", "2026-12-31")
        g = self.db.goal_get(gid)
        self.assertEqual(g["title"], "Grow revenue")
        self.assertEqual(g["objective"], "more sales")
        self.assertEqual(g["success_criteria"], "10 new customers")
        self.assertEqual(g["target_date"], "2026-12-31")
        self.assertEqual(g["status"], "active")
        self.db.goal_set_status(gid, "achieved")
        self.assertEqual(self.db.goal_get(gid)["status"], "achieved")

    def test_goal_unknown_id_and_status_rejected(self):
        with self.assertRaises(N.NovyraError):
            self.db.goal_get(999)
        with self.assertRaises(N.NovyraError):
            self.db.goal_set_status(999, "active")
        gid = self.db.goal_create("x")
        with self.assertRaises(N.NovyraError):
            self.db.goal_set_status(gid, "bogus")

    def test_goal_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.db")
            db = N.Database(path)
            gid = db.goal_create("Persist", success_criteria="still there")
            db.close()
            db2 = N.Database(path)
            self.addCleanup(db2.close)
            self.assertEqual(db2.goal_get(gid)["success_criteria"],
                             "still there")

    def test_goal_task_counts(self):
        gid = self.db.goal_create("G")
        self.db.task_create("a", "CEO Agent", "Exec", goal_id=gid)
        self.db.task_create("b", "CEO Agent", "Exec", goal_id=gid)
        self.db.task_create("c", "CEO Agent", "Exec")
        self.assertEqual(self.db.goal_task_counts(), {gid: 2})

    def test_reset_wipes_goals(self):
        rt = make_runtime()
        rt.db.goal_create("Temporary goal")
        rt.reset_data()
        self.assertEqual(rt.db.goals_all(), [])


class TestTaskFields(NovyraCase):
    """Phase 1: full task records, backward-compatible migration from
    a real 0.2.2-schema database, and result submission."""

    def setUp(self):
        self.db = make_db()

    def test_extended_create_roundtrip(self):
        tid = self.db.task_create(
            "Ship it", "CEO Agent", "Exec", priority="high", goal_id=1,
            deadline="2026-12-31", cost_estimate=5.0,
            expected_impact="big", risk_level="low",
            success_criteria="shipped and verified")
        t = self.db.task_get(tid)
        self.assertEqual(t["goal_id"], 1)
        self.assertEqual(t["deadline"], "2026-12-31")
        self.assertAlmostEqual(t["cost_estimate"], 5.0)
        self.assertEqual(t["expected_impact"], "big")
        self.assertEqual(t["risk_level"], "low")
        self.assertEqual(t["success_criteria"], "shipped and verified")
        self.assertEqual(t["status"], "pending")
        self.assertEqual(t["history"], "")
        self.assertIsNone(t["verified_result"])

    def test_backward_compatible_create_signature(self):
        tid = self.db.task_create("Old style", "CEO Agent", "Exec")
        t = self.db.task_get(tid)
        self.assertIsNone(t["goal_id"])
        self.assertIsNone(t["deadline"])
        self.assertEqual(t["status"], "pending")

    def test_status_flow_is_validated(self):
        tid = self.db.task_create("x", "CEO Agent", "Exec")
        for status in ("delegated", "in_progress", "verifying",
                       "done", "failed"):
            self.db.task_set_status(tid, status)
        with self.assertRaises(N.NovyraError):
            self.db.task_set_status(tid, "bogus")

    def test_task_get_unknown_rejected(self):
        with self.assertRaises(N.NovyraError):
            self.db.task_get(999)

    def test_empty_result_cannot_be_submitted(self):
        tid = self.db.task_create("x", "CEO Agent", "Exec")
        with self.assertRaises(N.NovyraError):
            self.db.task_report(tid, "   ")
        self.assertEqual(self.db.task_get(tid)["status"], "pending")

    def test_task_report_records_history_and_moves_to_verifying(self):
        tid = self.db.task_create("x", "CEO Agent", "Exec")
        self.db.task_history_append(tid, "started work")
        self.db.task_report(tid, "Finished the draft")
        t = self.db.task_get(tid)
        self.assertEqual(t["status"], "verifying")
        self.assertIn("started work", t["history"])
        self.assertIn("result submitted: Finished the draft",
                      t["history"])

    def test_overdue_detection_ignores_done_and_future(self):
        late = self.db.task_create("late", "CEO Agent", "Exec",
                                   deadline="2000-01-01")
        finished = self.db.task_create("old but done", "CEO Agent", "Exec",
                                       deadline="2000-01-01")
        self.db.task_set_status(finished, "done")
        future = self.db.task_create("future", "CEO Agent", "Exec",
                                     deadline="2999-01-01")
        ids = [t["tid"] for t in self.db.tasks_overdue(today="2026-01-01")]
        self.assertIn(late, ids)
        self.assertNotIn(finished, ids)
        self.assertNotIn(future, ids)

    def test_migration_from_a_real_0_2_2_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            old = sqlite3.connect(path)
            old.execute(
                "CREATE TABLE tasks (tid INTEGER PRIMARY KEY AUTOINCREMENT, "
                "title TEXT NOT NULL, owner TEXT NOT NULL, dept TEXT NOT NULL, "
                "priority TEXT NOT NULL DEFAULT 'normal', "
                "status TEXT NOT NULL DEFAULT 'pending', "
                "note TEXT NOT NULL DEFAULT '', created TEXT NOT NULL)")
            old.execute(
                "INSERT INTO tasks (title, owner, dept, status, created) "
                "VALUES ('legacy', 'CEO Agent', 'Exec', 'done', "
                "'2026-01-01T00:00:00')")
            old.commit()
            old.close()
            db = N.Database(path)
            self.addCleanup(db.close)
            # the legacy row survives with every value intact
            t = db.task_get(1)
            self.assertEqual(t["title"], "legacy")
            self.assertEqual(t["status"], "done")
            self.assertIsNone(t["goal_id"])
            self.assertEqual(t["risk_level"], "normal")
            self.assertEqual(t["history"], "")
            # and the migrated database accepts new-style tasks
            tid = db.task_create("new", "CEO Agent", "Exec", goal_id=1)
            self.assertEqual(db.task_get(tid)["goal_id"], 1)


class TestCEOReview(NovyraCase):
    """Phase 1: the bounded CEO review cycle. The CEO delegates,
    verifies submitted results and escalates; it never assumes a
    result and never acts during an emergency stop."""

    def _goal_and_task(self, rt, dept="Marketing Department"):
        gid = rt.db.goal_create("Launch campaign")
        rt.activate_department(dept)
        tid = rt.db.task_create("Draft copy", "CEO Agent", dept,
                                goal_id=gid)
        return gid, tid

    def test_review_delegates_pending_goal_tasks(self):
        rt = make_runtime()
        gid, tid = self._goal_and_task(rt)
        out = rt.ceo.review_cycle()
        self.assertIn("Delegated T%03d" % tid, out)
        self.assertEqual(rt.db.task_get(tid)["status"], "delegated")
        self.assertTrue(any("Delegate T%03d" % tid in d["question"]
                            for d in rt.db.decisions_all()))

    def test_review_skips_tasks_without_goal_or_manager(self):
        rt = make_runtime()
        plain = rt.db.task_create("No goal", "CEO Agent",
                                  "Marketing Department")
        gid = rt.db.goal_create("G")
        orphan = rt.db.task_create("No manager", "CEO Agent",
                                   "Trading Department", goal_id=gid)
        rt.ceo.review_cycle()
        self.assertEqual(rt.db.task_get(plain)["status"], "pending")
        self.assertEqual(rt.db.task_get(orphan)["status"], "pending")

    def test_review_verifies_submitted_result_and_records_evidence(self):
        rt = make_runtime()
        gid, tid = self._goal_and_task(rt)
        rt.db.task_report(tid, "Copy drafted and approved")
        out = rt.ceo.review_cycle()
        t = rt.db.task_get(tid)
        self.assertEqual(t["status"], "done")
        self.assertIn("Copy drafted and approved", t["verified_result"])
        self.assertEqual(t["verification"], "verified by CEO review")
        self.assertIn("Verified T%03d" % tid, out)

    def test_review_never_assumes_done_without_a_result(self):
        rt = make_runtime()
        gid, tid = self._goal_and_task(rt)
        rt.db.task_set_status(tid, "verifying")  # no result submitted
        out = rt.ceo.review_cycle()
        self.assertNotEqual(rt.db.task_get(tid)["status"], "done")
        self.assertIn("Escalated T%03d" % tid, out)
        self.assertTrue(any("not assumed done" in d["reason"]
                            for d in rt.db.decisions_all()))

    def test_review_blocked_during_emergency_stop(self):
        rt = make_runtime()
        gid, tid = self._goal_and_task(rt)
        rt.permissions.halt()
        out = rt.ceo.review_cycle()
        self.assertIn("Emergency stop", out)
        self.assertEqual(rt.db.task_get(tid)["status"], "pending")

    def test_review_escalates_overdue_tasks(self):
        rt = make_runtime()
        rt.db.task_create("Late thing", "CEO Agent", "Exec",
                          deadline="2000-01-01")
        out = rt.ceo.review_cycle()
        self.assertIn("deadline 2000-01-01 passed", out)
        self.assertTrue(any("Escalate overdue" in d["question"]
                            for d in rt.db.decisions_all()))

    def test_review_is_bounded(self):
        rt = make_runtime()
        rt.activate_department("Marketing Department")
        gid = rt.db.goal_create("Many tasks")
        for i in range(15):
            rt.db.task_create("task %d" % i, "CEO Agent",
                              "Marketing Department", goal_id=gid)
        out = rt.ceo.review_cycle()
        delegated = [t for t in rt.db.tasks_all()
                     if t["status"] == "delegated"]
        self.assertEqual(len(delegated), N.MAX_REVIEW_ACTIONS)
        self.assertIn("Review cap reached", out)

    def test_review_on_a_quiet_board(self):
        rt = make_runtime()
        out = rt.ceo.review_cycle()
        self.assertIn("Nothing to delegate", out)

    def test_daily_report_includes_goals_and_verification_counts(self):
        rt = make_runtime()
        gid, tid = self._goal_and_task(rt)
        rt.db.task_report(tid, "result here")
        rt.ceo.review_cycle()
        text = rt.ceo.daily_report()
        self.assertIn("Goal G%03d" % gid, text)
        self.assertIn("verified done: 1", text)


class TestPhase1ConfigAndCLI(NovyraCase):
    """Phase 1: the stored-not-enforced config surface and the new
    CLI commands."""

    def test_config_defaults_and_unknown_keys(self):
        db = make_db()
        self.assertEqual(db.config_get("autonomy_threshold"), "0.87")
        self.assertEqual(db.config_get("max_autonomous_spend"), "100.00")
        self.assertIsNone(db.config_get("daily_autonomous_budget"))
        with self.assertRaises(N.NovyraError):
            db.config_get("not_a_key")
        with self.assertRaises(N.NovyraError):
            db.config_set("not_a_key", "1")

    def test_config_set_validates_values(self):
        db = make_db()
        db.config_set("autonomy_threshold", "0.9")
        self.assertEqual(db.config_get("autonomy_threshold"), "0.9")
        db.config_set("max_autonomous_spend", 250)
        self.assertEqual(db.config_get("max_autonomous_spend"), "250.00")
        db.config_set("daily_autonomous_budget", "50")
        self.assertEqual(db.config_get("daily_autonomous_budget"), "50.00")
        db.config_set("daily_autonomous_budget", None)  # unset again
        self.assertIsNone(db.config_get("daily_autonomous_budget"))
        with self.assertRaises(N.NovyraError):
            db.config_set("autonomy_threshold", "abc")
        with self.assertRaises(N.NovyraError):
            db.config_set("max_autonomous_spend", "-1")

    def test_cli_goal_and_goals_commands(self):
        rt = make_runtime()
        cli = N.CLI(rt)
        cli.dispatch("goal Grow revenue | sell more | 10 deals | "
                     "2026-12-31")
        goals = rt.db.goals_all()
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["title"], "Grow revenue")
        self.assertEqual(goals[0]["target_date"], "2026-12-31")
        cli.dispatch("goal Bad date | x | y | 31-12-2026")  # rejected
        self.assertEqual(len(rt.db.goals_all()), 1)

    def test_cli_review_and_bad_phase1_inputs_do_not_crash(self):
        rt = make_runtime()
        cli = N.CLI(rt)
        for line in ("goals", "goal", "review", "review extra args",
                     "goal | | | 2026-13-99"):
            cli.dispatch(line)  # must not raise
        self.assertEqual(rt.db.goals_all(), [])

    def test_tasks_command_shows_goal_deadline_and_verification(self):
        rt = make_runtime()
        gid = rt.db.goal_create("G")
        tid = rt.db.task_create("Visible", "CEO Agent", "Exec",
                                goal_id=gid, deadline="2026-12-31")
        rt.db.task_report(tid, "done deal")
        rt.ceo.review_cycle()
        cli = N.CLI(rt)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.dispatch("tasks")
        out = buf.getvalue()
        self.assertIn("goal G%03d" % gid, out)
        self.assertIn("due 2026-12-31", out)
        self.assertIn("[verified]", out)


class TestOwnerSessionAndProvenance(NovyraCase):
    """Phase 2: provenance is enforced by code path. Only the
    OwnerSession token holder can book confirmed entries; agents can
    only produce estimates; simulated results are unbookable."""

    def setUp(self):
        self.rt = make_runtime()
        self.fin = self.rt.finance
        self.session = N.OwnerSession()

    def test_owner_entry_requires_capability_token(self):
        with self.assertRaises(N.NovyraError):
            self.fin.record_owner_entry(None, "revenue", 100)
        with self.assertRaises(N.NovyraError):
            self.fin.record_owner_entry("Owner", "revenue", 100)
        with self.assertRaises(N.NovyraError):
            self.fin.record_owner_entry(object(), "revenue", 100)
        self.assertEqual(self.fin.transactions_count(), 0)

    def test_owner_entry_records_owner_provenance(self):
        txid = self.fin.record_owner_entry(self.session, "revenue", 100,
                                           business="shop",
                                           category="sales")
        row = self.rt.db.conn.execute(
            "SELECT * FROM transactions WHERE txid = ?", (txid,)).fetchone()
        self.assertEqual(row["source"], "owner")
        self.assertEqual(row["created_by"], "Owner")
        self.assertIsNone(row["agent"])
        self.assertEqual(row["amount_cents"], 10000)
        self.assertEqual(row["currency"], "EUR")
        # the booking was approval-gated and audited
        self.assertTrue(self.rt.db.approvals_count() >= 1)

    def test_declined_owner_entry_records_nothing(self):
        self.rt.ask_owner = lambda question: False
        self.assertIsNone(self.fin.record_owner_entry(self.session,
                                                      "revenue", 100))
        self.assertEqual(self.fin.transactions_count(), 0)

    def test_agent_entry_forced_to_estimate(self):
        txid = self.fin.record_agent_entry("Revenue Tracking", "revenue",
                                           250, business="shop")
        row = self.rt.db.conn.execute(
            "SELECT * FROM transactions WHERE txid = ?", (txid,)).fetchone()
        self.assertEqual(row["source"], "estimate")
        self.assertEqual(row["created_by"], "Revenue Tracking")
        self.assertEqual(row["agent"], "Revenue Tracking")
        # it is a watched action in the audit trail
        self.assertTrue(any("record revenue estimate" == a["action"]
                            for a in self.rt.db.conn.execute(
                                "SELECT * FROM actions").fetchall()))

    def test_agent_entry_blocked_during_emergency_stop(self):
        self.rt.permissions.halt()
        self.assertIsNone(self.fin.record_agent_entry(
            "Revenue Tracking", "revenue", 250))
        self.assertEqual(self.fin.transactions_count(), 0)

    def test_simulated_source_is_unbookable(self):
        # there is no simulated source in the ledger at all; even raw
        # SQL cannot book one (CHECK constraint)
        with self.assertRaises(sqlite3.IntegrityError):
            self.rt.db.conn.execute(
                "INSERT INTO transactions (ts, kind, amount_cents, "
                "source, created_by, created) VALUES (?, 'revenue', 100, "
                "'simulated', 'x', ?)", (N.now_str(), N.now_str()))

    def test_unsupported_currency_rejected(self):
        with self.assertRaises(N.NovyraError):
            self.fin.record_owner_entry(self.session, "revenue", 100,
                                        currency="USD")
        with self.assertRaises(N.NovyraError):
            self.fin.record_agent_entry("Revenue Tracking", "revenue",
                                        100, currency="SEK")
        self.assertEqual(self.fin.transactions_count(), 0)

    def test_non_positive_amount_rejected(self):
        for bad in (0, -5, 0.001):  # 0.001 rounds to zero cents
            with self.assertRaises(N.NovyraError):
                self.fin.record_owner_entry(self.session, "revenue", bad)
            with self.assertRaises(N.NovyraError):
                self.fin.record_agent_entry("Revenue Tracking",
                                            "expense", bad)

    def test_corrections_are_offsetting_entries(self):
        first = self.fin.record_owner_entry(self.session, "revenue", 100)
        correction = self.fin.record_offsetting_correction(
            self.session, first, note="wrong amount")
        rows = self.fin.transactions_tail(10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["kind"], "expense")
        self.assertIn("correction of #%d" % first, rows[1]["note"])
        self.assertEqual(self.fin.operating_profit_cents(), 0)
        # opening balances are not offset, they are re-recorded
        opening = self.fin.record_owner_entry(self.session,
                                              "opening_balance", 500)
        with self.assertRaises(N.NovyraError):
            self.fin.record_offsetting_correction(self.session, opening)


class TestFinancialMaths(NovyraCase):
    """Phase 2: confirmed figures only in the headline numbers,
    integer-cent precision, opening balances distinct from revenue."""

    def setUp(self):
        self.rt = make_runtime()
        self.fin = self.rt.finance
        self.session = N.OwnerSession()

    def test_revenue_expense_and_profit(self):
        self.fin.record_owner_entry(self.session, "revenue", 300)
        self.fin.record_owner_entry(self.session, "revenue", 250.50)
        self.fin.record_owner_entry(self.session, "expense", 120)
        self.assertEqual(self.fin.revenue_cents(), 55050)
        self.assertEqual(self.fin.expense_cents(), 12000)
        self.assertEqual(self.fin.operating_profit_cents(), 43050)

    def test_opening_balance_is_not_revenue(self):
        self.fin.record_owner_entry(self.session, "opening_balance", 5000)
        self.fin.record_owner_entry(self.session, "revenue", 300)
        self.assertEqual(self.fin.revenue_cents(), 30000)
        self.assertEqual(self.fin.opening_balance_cents(), 500000)
        self.assertEqual(self.fin.cash_position_cents(), 530000)
        self.assertEqual(self.fin.cash_flow_cents(), 30000)

    def test_cash_flow_excludes_opening_balances(self):
        self.fin.record_owner_entry(self.session, "opening_balance", 1000)
        self.fin.record_owner_entry(self.session, "revenue", 400)
        self.fin.record_owner_entry(self.session, "expense", 150)
        self.assertEqual(self.fin.cash_flow_cents(), 25000)
        self.assertEqual(self.fin.cash_position_cents(), 125000)

    def test_per_business_split(self):
        self.fin.record_owner_entry(self.session, "revenue", 100,
                                    business="shop")
        self.fin.record_owner_entry(self.session, "revenue", 50,
                                    business="shop")
        self.fin.record_owner_entry(self.session, "revenue", 30,
                                    business="agency")
        self.fin.record_owner_entry(self.session, "expense", 20,
                                    business="shop")
        self.assertEqual(self.fin.revenue_cents(business="shop"), 15000)
        self.assertEqual(self.fin.revenue_cents(business="agency"), 3000)
        self.assertEqual(self.fin.expense_cents(business="shop"), 2000)
        self.assertEqual(self.fin.operating_profit_cents(business="shop"),
                         13000)

    def test_per_category_split(self):
        self.fin.record_owner_entry(self.session, "expense", 100,
                                    category="salaries")
        self.fin.record_owner_entry(self.session, "expense", 40,
                                    category="salaries")
        self.fin.record_owner_entry(self.session, "expense", 25,
                                    category="tools")
        self.assertEqual(self.fin.by_category("expense"),
                         {"salaries": 14000, "tools": 2500})

    def test_estimates_never_enter_confirmed_figures(self):
        self.fin.record_owner_entry(self.session, "revenue", 100)
        self.fin.record_agent_entry("Revenue Tracking", "revenue", 999)
        self.fin.record_agent_entry("Expense Management", "expense", 500)
        self.assertEqual(self.fin.revenue_cents(), 10000)
        self.assertEqual(self.fin.expense_cents(), 0)
        self.assertEqual(self.fin.estimate_cents("revenue"), 99900)
        self.assertEqual(self.fin.estimate_cents("expense"), 50000)

    def test_cent_precision(self):
        self.fin.record_owner_entry(self.session, "revenue", 0.10)
        self.fin.record_owner_entry(self.session, "revenue", 0.20)
        self.fin.record_owner_entry(self.session, "revenue", 0.30)
        # 0.1 + 0.2 in floats is 0.30000000000000004; in cents it is 60
        self.assertEqual(self.fin.revenue_cents(), 60)

    def test_runway_and_insufficient_data(self):
        self.assertIsNone(self.fin.runway())
        self.fin.record_owner_entry(self.session, "opening_balance", 700)
        self.fin.record_owner_entry(self.session, "expense", 100)
        self.fin.record_owner_entry(self.session, "expense", 200)
        # cash position = 700 opening - 300 expenses = 400;
        # one month of expenses so far: avg 300
        self.assertEqual(self.fin.cash_position_cents(), 40000)
        self.assertAlmostEqual(self.fin.runway(), 400 / 300)

    def test_margin_and_runway_helpers(self):
        self.assertAlmostEqual(N.profit_margin(25000, 100000), 0.25)
        self.assertIsNone(N.profit_margin(100, 0))
        self.assertIsNone(N.profit_margin(100, -5))
        self.assertAlmostEqual(N.runway_months(1200000, 100000), 12.0)
        self.assertIsNone(N.runway_months(500000, 0))

    def test_empty_ledger_reports_zeros(self):
        self.assertEqual(self.fin.revenue_cents(), 0)
        self.assertEqual(self.fin.expense_cents(), 0)
        self.assertEqual(self.fin.operating_profit_cents(), 0)
        self.assertEqual(self.fin.cash_position_cents(), 0)
        self.assertEqual(self.fin.cash_flow_cents(), 0)


class TestTaskEventsAndKPIs(NovyraCase):
    """Phase 2: append-only task events, period assignment by event
    timestamp, documented denominator, verification snapshots."""

    def setUp(self):
        self.rt = make_runtime()
        self.db = self.rt.db

    def _insert_event(self, tid, ts, event, from_status, to_status,
                      actor="test", detail=""):
        self.db.conn.execute(
            "INSERT INTO task_events (tid, ts, event, from_status, "
            "to_status, actor, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tid, ts, event, from_status, to_status, actor, detail))
        self.db.conn.commit()

    def test_created_and_status_events_recorded(self):
        tid = self.db.task_create("Write plan", "CEO Agent", "Exec")
        kinds = [(e["event"], e["to_status"])
                 for e in self.db.task_event_rows(tid)]
        self.assertEqual(kinds, [("created", "pending")])
        self.db.task_set_status(tid, "done", actor="CEO Agent")
        kinds = [(e["event"], e["from_status"], e["to_status"])
                 for e in self.db.task_event_rows(tid)]
        self.assertEqual(kinds, [("created", None, "pending"),
                                 ("status_change", "pending", "done")])

    def _insert_task(self, title, created):
        """Insert a task row with an explicit creation timestamp, for
        period-assignment scenarios (task_create always uses now)."""
        cur = self.db.conn.execute(
            "INSERT INTO tasks (title, owner, dept, status, created) "
            "VALUES (?, 'CEO Agent', 'Exec', 'pending', ?)",
            (title, created))
        self.db.conn.commit()
        return cur.lastrowid

    def test_reopen_recomplete_produces_two_done_events(self):
        tid = self._insert_task("x", "2026-10-01T09:00:00")
        self._insert_event(tid, "2026-10-05T10:00:00", "status_change",
                           "pending", "done")
        self._insert_event(tid, "2026-10-20T10:00:00", "status_change",
                           "done", "pending")
        self._insert_event(tid, "2026-11-05T10:00:00", "status_change",
                           "pending", "done")
        oct_k = self.db.task_completion_kpis("2026-10")
        nov_k = self.db.task_completion_kpis("2026-11")
        self.assertEqual(len(oct_k["completed"]), 1)
        self.assertEqual(len(oct_k["eligible"]), 1)
        self.assertEqual(len(nov_k["completed"]), 1)
        self.assertEqual(len(nov_k["eligible"]), 1)
        self.assertEqual(oct_k["events"], 1)
        self.assertEqual(nov_k["events"], 1)

    def test_completion_rate_cannot_exceed_100(self):
        tid = self._insert_task("x", "2026-10-01T09:00:00")
        self._insert_event(tid, "2026-10-05T10:00:00", "status_change",
                           "pending", "done")
        self._insert_event(tid, "2026-10-20T10:00:00", "status_change",
                           "done", "pending")
        self._insert_event(tid, "2026-10-25T10:00:00", "status_change",
                           "pending", "done")
        k = self.db.task_completion_kpis("2026-10")
        # one unique task completed, one eligible, two raw events
        self.assertEqual(len(k["completed"]), 1)
        self.assertEqual(len(k["eligible"]), 1)
        self.assertEqual(k["events"], 2)
        self.assertLessEqual(len(k["completed"]), len(k["eligible"]))

    def test_eligible_excludes_tasks_completed_before_period(self):
        tid = self.db.task_create("old", "CEO Agent", "Exec")
        self._insert_event(tid, "2026-09-05T10:00:00", "status_change",
                           "pending", "done")
        k = self.db.task_completion_kpis("2026-10")
        self.assertEqual(len(k["completed"]), 0)
        self.assertEqual(len(k["eligible"]), 0)

    def test_eligible_includes_task_reopened_within_period(self):
        # done in September, reopened and re-completed in November:
        # November must count the task as eligible even though its
        # status just before the period was done, and December (after
        # the re-completion) must not count it at all
        tid = self._insert_task("reopened", "2026-09-01T09:00:00")
        self._insert_event(tid, "2026-09-05T10:00:00", "status_change",
                           "pending", "done")
        self._insert_event(tid, "2026-11-03T10:00:00", "status_change",
                           "done", "in_progress")
        self._insert_event(tid, "2026-11-06T10:00:00", "status_change",
                           "in_progress", "done")
        k = self.db.task_completion_kpis("2026-11")
        self.assertEqual(len(k["eligible"]), 1)
        self.assertEqual(len(k["completed"]), 1)
        self.assertEqual(k["events"], 1)
        k_dec = self.db.task_completion_kpis("2026-12")
        self.assertEqual(len(k_dec["eligible"]), 0)
        self.assertEqual(len(k_dec["completed"]), 0)

    def test_eligible_excludes_tasks_created_after_period(self):
        # created now (November); October must not count it
        self.db.task_create("future", "CEO Agent", "Exec")
        k = self.db.task_completion_kpis("2026-10")
        self.assertEqual(len(k["eligible"]), 0)

    def test_pre_phase2_completions_are_unattributed(self):
        # a done task with no event history at all (legacy row)
        self.db.conn.execute(
            "INSERT INTO tasks (title, owner, dept, status, created) "
            "VALUES ('legacy', 'CEO Agent', 'Exec', 'done', ?)",
            (N.now_str(),))
        self.db.conn.commit()
        k = self.db.task_completion_kpis(N.now_str()[:7])
        self.assertEqual(len(k["completed"]), 0)
        self.assertEqual(len(k["unattributed"]), 1)

    def test_verification_snapshot_at_completion(self):
        rt = make_runtime()
        gid = rt.db.goal_create("G")
        rt.activate_department("Marketing Department")
        tid = rt.db.task_create("Draft", "CEO Agent",
                                "Marketing Department", goal_id=gid)
        rt.db.task_report(tid, "The copy is finished")
        rt.ceo.review_cycle()
        done_events = [e for e in rt.db.task_event_rows(tid)
                       if e["to_status"] == "done"]
        self.assertEqual(len(done_events), 1)
        self.assertTrue(done_events[0]["detail"].startswith("verified="))
        self.assertIn("The copy is finished",
                      done_events[0]["detail"])

    def test_historical_kpi_unchanged_after_later_edit(self):
        rt = make_runtime()
        gid = rt.db.goal_create("G")
        rt.activate_department("Marketing Department")
        tid = rt.db.task_create("Draft", "CEO Agent",
                                "Marketing Department", goal_id=gid)
        rt.db.task_report(tid, "Original verified evidence")
        rt.ceo.review_cycle()
        period = N.now_str()[:7]
        before = rt.db.verified_completion_count(period)
        self.assertEqual(before, 1)
        # a later edit of the current verification fields must not
        # rewrite the historical snapshot
        rt.db.task_update_verification(tid, "tampered result",
                                       "edited", "CEO Agent")
        self.assertEqual(rt.db.verified_completion_count(period), 1)
        events = rt.db.task_event_rows(tid)
        self.assertTrue(any(e["event"] == "verification_update"
                            for e in events))
        done_event = [e for e in events if e["to_status"] == "done"][0]
        self.assertIn("Original verified evidence", done_event["detail"])

    def test_verification_update_is_in_the_schema_constraint(self):
        tid = self.db.task_create("x", "CEO Agent", "Exec")
        self.db.task_update_verification(tid, "r", "v", "CEO Agent")
        kinds = {e["event"] for e in self.db.task_event_rows(tid)}
        self.assertIn("verification_update", kinds)
        # and an unknown event kind is rejected by the CHECK itself
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute(
                "INSERT INTO task_events (tid, ts, event, actor) "
                "VALUES (?, ?, 'bogus_event', 'x')",
                (tid, N.now_str()))
            self.db.conn.commit()

    def test_report_event_recorded(self):
        tid = self.db.task_create("x", "CEO Agent", "Exec")
        self.db.task_report(tid, "Finished", actor="Data Collector")
        events = self.db.task_event_rows(tid)
        self.assertTrue(any(e["event"] == "report" for e in events))
        self.assertEqual(events[-1]["to_status"], "verifying")

    def test_goal_progress_kpi(self):
        gid = self.db.goal_create("G")
        a = self.db.task_create("a", "CEO Agent", "Exec", goal_id=gid)
        self.db.task_create("b", "CEO Agent", "Exec", goal_id=gid)
        self.db.task_set_status(a, "done")
        counts = self.db.goal_task_counts()
        done = len([t for t in self.db.tasks_all()
                    if t["goal_id"] == gid and t["status"] == "done"])
        self.assertEqual(counts[gid], 2)
        self.assertEqual(done, 1)  # progress 1/2 = 50%


class TestImmutabilityAndReset(NovyraCase):
    """Phase 2: database-level append-only protection, the controlled
    reset, and its guaranteed restoration."""

    def setUp(self):
        self.rt = make_runtime()
        self.db = self.rt.db
        self.fin = self.rt.finance
        self.session = N.OwnerSession()
        self.fin.record_owner_entry(self.session, "revenue", 100)

    def _assert_blocked(self, sql, params=()):
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.conn.execute(sql, params)
            self.db.conn.commit()

    def test_update_and_delete_blocked_on_all_protected_tables(self):
        self._assert_blocked(
            "UPDATE transactions SET amount_cents = 1")
        self._assert_blocked("DELETE FROM transactions")
        self._assert_blocked("UPDATE forecasts SET amount_cents = 1")
        self._assert_blocked("DELETE FROM forecasts")
        self._assert_blocked("UPDATE kpis SET value = 1")
        self._assert_blocked("DELETE FROM kpis")
        self._assert_blocked("UPDATE task_events SET detail = 'x'")
        self._assert_blocked("DELETE FROM task_events")
        # nothing was destroyed
        self.assertEqual(self.fin.transactions_count(), 1)

    def test_inserts_still_work_while_protected(self):
        self.fin.record_owner_entry(self.session, "expense", 30)
        self.assertEqual(self.fin.transactions_count(), 2)

    def test_drop_trigger_blocked_while_locked(self):
        self._assert_blocked("DROP TRIGGER transactions_no_update")
        # and the trigger still protects
        self._assert_blocked("DELETE FROM transactions")

    def test_reset_wipes_protected_tables_and_restores_protection(self):
        self.rt.db.reset_data()
        self.assertEqual(self.fin.transactions_count(), 0)
        present = {r["name"] for r in self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()}
        for name in N.PROTECTED_TRIGGER_NAMES:
            self.assertIn(name, present)
        # protection is active again
        self._assert_blocked("DELETE FROM transactions")
        self._assert_blocked("UPDATE transactions SET amount_cents = 1")
        # and new entries work
        self.fin.record_owner_entry(self.session, "revenue", 5)
        self.assertEqual(self.fin.transactions_count(), 1)

    def test_failed_reset_leaves_database_intact(self):
        # fault injection: a wipe step fails mid-procedure
        original = N.PROTECTED_TABLES
        N.PROTECTED_TABLES = ("transactions", "bogus_table")
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.db.reset_protected_data()
        finally:
            N.PROTECTED_TABLES = original
        # the transaction rolled back: data intact, lock re-engaged,
        # all triggers present, protection active
        self.assertEqual(self.fin.transactions_count(), 1)
        self.assertTrue(self.db._financial_lock)
        present = {r["name"] for r in self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()}
        for name in N.PROTECTED_TRIGGER_NAMES:
            self.assertIn(name, present)
        self._assert_blocked("DELETE FROM transactions")
        # the ledger still works after the failed reset
        self.fin.record_owner_entry(self.session, "expense", 10)
        self.assertEqual(self.fin.transactions_count(), 2)

    def test_guardian_flags_missing_trigger(self):
        # simulate external tampering: drop a trigger with the lock
        # released (the only way), then scan
        self.db._financial_lock = False
        try:
            self.db.conn.execute("DROP TRIGGER transactions_no_update")
            self.db.conn.commit()
        finally:
            self.db._financial_lock = True
        findings = self.rt.guardian.full_scan()
        self.assertTrue(any("trigger" in f.lower() and "missing"
                            in f.lower() for f in findings))

    def test_reset_is_owner_approved_and_audited(self):
        cli = N.CLI(self.rt)
        cli.dispatch("reset")  # make_runtime auto-approves
        self.assertTrue(self.rt.db.approvals_count() >= 1)
        self.assertTrue(any("reset" in a["action"]
                            for a in self.rt.db.conn.execute(
                                "SELECT * FROM actions").fetchall()))

    def test_correction_path_works_despite_immutability(self):
        first = self.fin.transactions_tail(1)[0]["txid"]
        self.fin.record_offsetting_correction(self.session, first)
        rows = self.fin.transactions_tail(10)
        self.assertEqual(len(rows), 2)
        # the original row is untouched
        self.assertEqual(rows[0]["amount_cents"], 10000)
        self.assertEqual(rows[0]["kind"], "revenue")


class TestFinanceCLIAndCEO(NovyraCase):
    """Phase 2: the finance CLI, CEO reporting separation and the
    relabelled Finance Department metrics."""

    def setUp(self):
        self.rt = make_runtime()
        self.cli = N.CLI(self.rt)
        self.fin = self.rt.finance
        self.session = self.cli.owner_session

    def _out(self, line):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.cli.dispatch(line)
        return buf.getvalue()

    def test_finance_summary_sections(self):
        self.fin.record_owner_entry(self.session, "revenue", 500)
        self.fin.record_agent_entry("Revenue Tracking", "revenue", 900)
        out = self._out("finance")
        self.assertIn("Confirmed (Owner)", out)
        self.assertIn("500.00", out)
        self.assertIn("Agent estimates (NOT confirmed)", out)
        self.assertIn("Forecasts (NOT results)", out)

    def test_cli_revenue_and_expense_roundtrip(self):
        self._out("finance revenue 1250.50 shop sales")
        self._out("finance expense 300 shop tools")
        self.assertEqual(self.fin.revenue_cents(business="shop"), 125050)
        self.assertEqual(self.fin.expense_cents(business="shop"), 30000)
        self.assertEqual(self.fin.operating_profit_cents(business="shop"),
                         95050)
        out = self._out("finance transactions")
        self.assertIn("revenue", out)
        self.assertIn("[owner]", out)

    def test_cli_opening_balance(self):
        self._out("finance opening 10000")
        self.assertEqual(self.fin.revenue_cents(), 0)
        self.assertEqual(self.fin.cash_position_cents(), 1000000)

    def test_cli_forecast_kept_separate_from_actuals(self):
        self._out("finance forecast revenue 5000 2026-12 shop")
        self._out("finance revenue 2000")
        self.assertEqual(len(self.fin.forecasts_all()), 1)
        f = self.fin.forecasts_all()[0]
        self.assertEqual(f["direction"], "revenue")
        self.assertEqual(f["period"], "2026-12")
        self.assertEqual(f["business"], "shop")
        self.assertEqual(f["amount_cents"], 500000)
        # the forecast is not in the confirmed figures
        self.assertEqual(self.fin.revenue_cents(), 200000)
        out = self._out("finance forecasts")
        self.assertIn("2026-12", out)
        self.assertIn("revenue", out)

    def test_forecast_vs_actual_comparison(self):
        self._out("finance forecast revenue 5000 2026-11")
        self._out("finance revenue 4200")
        cmp = self.fin.forecast_vs_actual("2026-11")
        self.assertEqual(len(cmp["forecasts"]), 1)
        self.assertEqual(cmp["actual_revenue_cents"], 420000)
        self.assertEqual(cmp["actual_expense_cents"], 0)

    def test_cli_kpi_snapshot_with_unit(self):
        self._out("finance kpi visitors 12345 count")
        rows = self.fin.kpis_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "visitors")
        self.assertAlmostEqual(rows[0]["value"], 12345.0)
        self.assertEqual(rows[0]["unit"], "count")
        self.assertEqual(rows[0]["source"], "owner")

    def test_cli_bad_inputs_do_not_crash(self):
        for line in ("finance", "finance revenue", "finance revenue abc",
                     "finance forecast revenue 100 2026-13",
                     "finance forecast revenue abc 2026-12",
                     "finance kpi visitors abc",
                     "finance bogus subcommand", "finance transactions x"):
            out = self._out(line)  # must not raise
        self.assertEqual(self.fin.transactions_count(), 0)

    def test_ceo_report_separates_finance_from_simulation(self):
        self.fin.record_owner_entry(self.session, "revenue", 400)
        self.fin.record_agent_entry("Revenue Tracking", "revenue", 77)
        self.rt.activate_department("Betting Department")
        report = self.rt.ceo.daily_report()
        self.assertIn("Finance (Owner-confirmed)", report)
        self.assertIn("400.00", report)
        self.assertIn("NOT confirmed", report)
        self.assertIn("not business performance", report.lower())

    def test_finance_department_metrics_are_labelled(self):
        metrics = self.rt._generate_metrics("Finance Department")
        self.assertIn("Data provenance", metrics)
        self.assertIn("not recorded results",
                      metrics["Data provenance"])
        # other departments keep their example metrics untouched
        self.assertNotIn("Data provenance",
                         self.rt._generate_metrics("Marketing Department"))


if __name__ == "__main__":
    unittest.main()