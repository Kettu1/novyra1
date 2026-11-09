#!/usr/bin/env python3
"""Automated test suite for Novyra 0.2.1.

Place this file next to novyra.py, then run:

    python3 test_novyra.py

Covers: betting maths, the betting book (stake deduction, cent
rounding, settlement, validation, atomicity and double-settlement
protection), database persistence, permissions, the emergency stop,
blocked-action counting, restart round-trips, the betting pipeline
invariant, autonomous-mode gating, persistent communication history,
Guardian integrity scans and the watchdog, agent action execution
via the LLM planner (including refusal of unsafe actions), agent
roster uniqueness, LLM fallback behaviour, and CLI input validation.
No network access or LLM endpoint is required.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import novyra as N


def make_runtime(db_path=":memory:"):
    """Runtime with all Owner approvals auto-granted for testing."""
    rt = N.Runtime(db_path=db_path, demo=False)
    rt.ask_owner = lambda question: True
    return rt


class TestMath(unittest.TestCase):
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


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.db = N.Database(":memory:")

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


class TestBettingBook(unittest.TestCase):
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


class TestPermissions(unittest.TestCase):
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
            rt.permissions.halt()
            rt2 = N.Runtime(db_path=path, demo=False)
            self.assertTrue(rt2.permissions.emergency_stop)

    def test_approved_activation_persists_approval(self):
        rt = make_runtime()
        rt.activate_department("Betting Department")
        self.assertTrue(rt.dept_active("Betting Department"))
        self.assertEqual(rt.db.approvals_count(), 1)


class TestPersistence(unittest.TestCase):
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


class TestPipeline(unittest.TestCase):
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


class TestRosterAndBus(unittest.TestCase):
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


class TestLLMIntegration(unittest.TestCase):
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


class TestCLIValidation(unittest.TestCase):
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


class TestAtomicity(unittest.TestCase):
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


class TestAutonomy(unittest.TestCase):
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


class TestMessagePersistence(unittest.TestCase):
    def test_messages_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "novyra.db")
            rt = make_runtime(path)
            rt.bus.send("CEO Agent", "Owner", "weekly summary",
                        "all departments on track")
            rt.bus.send("Guardian Agent", "CEO Agent", "patrol report",
                        "nothing to flag")
            rt2 = N.Runtime(db_path=path, demo=False)
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
        self.assertEqual(rt.db.actions_count(), 0)


class TestGuardian(unittest.TestCase):
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


class TestAgentActions(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()