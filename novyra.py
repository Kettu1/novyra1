#!/usr/bin/env python3
"""Novyra 0.2.1: a multi-agent company operating system for your terminal.

The Owner (you) sits at the top. The CEO Agent coordinates a 57-entry
blueprint (48 agents + 9 shared systems) across six departments: core
management, Betting, Ecommerce, Marketing, Trading and Finance.

Changelog 0.2.1 (from 0.2.0)
----------------------------
* Atomic money operations: bet placement and settlement now run in a
  single SQLite transaction. A failure part-way through can no longer
  deduct a stake without recording the bet, or credit a win without
  settling it. Double settlement is rejected inside the same
  transaction that pays out, so a race cannot pay twice.
* Agent action execution: agents can execute real, useful tasks, not
  only produce text. The new 'do <agent> <instruction>' command asks
  the agent to plan whitelisted actions (create tasks, store memories,
  message other agents, search memory) as JSON; the runtime validates
  the plan and executes it deterministically. Money, approvals, roster
  and filesystem operations are excluded from the whitelist by design,
  so an LLM can never spend funds or bypass the Owner.
* Guardian upgrade: the Guardian Agent actively monitors agent
  actions. Every sensitive action attempt is recorded in a new actions
  table (with allowed/refused outcome), a CLI watchdog reports new
  warnings after every command, and the new 'guardian' command runs a
  deep integrity scan: database integrity, the bankroll ledger
  invariant, corrupted settlements, exposure concentration, approval
  patterns and suspicious behaviour.
* Persistent communication history: all messages between agents are
  stored in SQLite (new messages table) and survive restarts. The new
  'messages [n]' command shows recent traffic.
* Autonomous financial actions are disabled by default. Scans ask for
  Owner approval before placing each simulated bet and no longer
  auto-settle with synthetic results. The new 'autonomy on|off'
  command (approval-gated to enable) restores fully autonomous
  simulated placement and settlement.
* Betting correctness: stakes are rounded to cents on placement and
  settlement PnL is derived from the same cent-rounded credit that
  touches the bankroll, so the ledger invariant (bankroll = initial +
  realised PnL - open exposure) holds exactly.
* Security: emergency-stop blocks are counted separately from Owner
  denials; the LLM endpoint must use http(s); proposing a department
  manager now brings the whole department online through the normal
  approval flow instead of leaving a half-staffed department.
* Betting schema integrity: CHECK constraints on new databases.
* The Developer Agent now logs requests on the task board (its old
  fallback reply claimed work without recording any).

Changelog 0.2.0 (from 0.1.0)
----------------------------
* Persistence: everything lives in SQLite (memories, tasks, decisions,
  approvals, audit log, bets, bankroll, emergency state, active agents).
  State survives restarts. Demo mode is forced onto a throwaway
  in-memory database so demo approvals can never touch real data.
* LLM integration: any OpenAI-compatible endpoint (Mistral API, OpenAI,
  Ollama, LM Studio) via NOVYRA_LLM_URL, NOVYRA_LLM_KEY,
  NOVYRA_LLM_MODEL. Agents use it for analysis and conversation, with a
  clean fallback to deterministic simulation replies when unconfigured.
* Betting book: a real ledger. Stakes are deducted at placement,
  settlement matches the exact bet by id, realised PnL and exposure are
  tracked, and every pipeline result is labelled SIMULATED.
* Permissions: all sensitive actions go through an approval table,
  denials are counted, the emergency stop persists across restarts,
  and resume requires approval too.
* Architecture: deterministic control plane (money, approvals, math)
  is separated from the analysis plane (LLM or simulation).
* Tests: automated suite in test_novyra.py.

Usage
-----
    python3 novyra.py              interactive, persists to novyra.db
    NOVYRA_DB=other.db python3 novyra.py
    python3 novyra.py --demo       throwaway demo, nothing is saved
    python3 test_novyra.py         automated test suite

LLM configuration (optional)
----------------------------
    export NOVYRA_LLM_URL=https://api.mistral.ai/v1/chat/completions
    export NOVYRA_LLM_KEY=your-api-key
    export NOVYRA_LLM_MODEL=mistral-small-latest
"""

from __future__ import annotations

import difflib
import json
import math
import os
import random
import sqlite3
import sys
import textwrap
import urllib.request
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

VERSION = "0.2.1"


# ---------------------------------------------------------------- utilities

class C:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"

    @classmethod
    def off(cls):
        if not sys.stdout.isatty():
            for name in list(vars(cls)):
                if name.isupper() and isinstance(getattr(cls, name), str):
                    setattr(cls, name, "")


def slug(text):
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "agent"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def wrap(text, indent="  "):
    return textwrap.fill(str(text), width=76,
                         initial_indent=indent, subsequent_indent=indent)


def hdr(title, char="=", width=76):
    pad = max(1, width - len(title) - 4)
    print("\n%s%s %s %s%s" % (C.BOLD + C.CYAN, char * 2, title, char * pad,
                              C.RESET))


def money(x):
    return "%.2f EUR" % x


def pct(p):
    return "%.1f%%" % (p * 100.0)


# ---------------------------------------------------------------- exceptions

class NovyraError(Exception):
    """Base class for recoverable Novyra errors."""


class BettingError(NovyraError):
    """Raised for invalid or rejected betting operations."""


# Actions an agent may propose through the LLM action planner (the
# 'do' command). Deliberately excludes anything touching money,
# approvals, the agent roster or the filesystem: agents plan, the
# deterministic runtime decides, and the Owner stays in control.
AGENT_ACTIONS = ("create_task", "remember", "message",
                 "search_memory", "none")


# ------------------------------------------------------------ LLM integration

class LLMClient:
    """Minimal OpenAI-compatible chat client built on urllib.

    Configure with NOVYRA_LLM_URL, NOVYRA_LLM_KEY, NOVYRA_LLM_MODEL and
    NOVYRA_LLM_TIMEOUT. When the endpoint is not configured, chat()
    returns None and callers fall back to deterministic replies. The
    client never raises: failures are recorded in last_error.
    """

    def __init__(self):
        self.url = os.environ.get("NOVYRA_LLM_URL", "").strip()
        self.key = os.environ.get("NOVYRA_LLM_KEY", "").strip()
        self.model = os.environ.get("NOVYRA_LLM_MODEL",
                                    "mistral-small-latest").strip()
        try:
            self.timeout = float(os.environ.get("NOVYRA_LLM_TIMEOUT", "60"))
        except ValueError:
            self.timeout = 60.0
        self.configured = bool(self.url) and \
            self.url.startswith(("http://", "https://"))
        if self.url and not self.configured:
            self.last_error = ("NOVYRA_LLM_URL must start with "
                               "http:// or https://")
        else:
            self.last_error = None

    def chat(self, system, user):
        if not self.configured:
            return None
        self.last_error = None
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": 300,
            "temperature": 0.7,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer %s" % self.key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            reply = str(data["choices"][0]["message"]["content"]).strip()
            return reply or None
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            return None


# ------------------------------------------------------------- persistence

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info', actor TEXT NOT NULL,
    event TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    scope TEXT NOT NULL, owner TEXT NOT NULL, fact TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
    tid INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
    owner TEXT NOT NULL, dept TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    agent TEXT NOT NULL, question TEXT NOT NULL,
    choice TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    action TEXT NOT NULL, detail TEXT NOT NULL, granted INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    match_name TEXT NOT NULL, outcome TEXT NOT NULL, stake REAL NOT NULL,
    odds REAL NOT NULL, prob REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'simulated',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'settled')),
    result TEXT, result_source TEXT, pnl REAL,
    CHECK (odds > 1.0), CHECK (prob > 0.0 AND prob < 1.0),
    CHECK (stake >= 1.0)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    sender TEXT NOT NULL, recipient TEXT NOT NULL,
    subject TEXT NOT NULL, body TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    actor TEXT NOT NULL, action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    allowed INTEGER NOT NULL DEFAULT 1 CHECK (allowed IN (0, 1)));
"""

TABLES = ("kv", "log", "memory", "tasks", "decisions", "approvals",
          "bets", "messages", "actions")


class Database:
    """SQLite persistence layer. One instance per session, every write
    commits immediately so a crash loses at most one action."""

    def __init__(self, path):
        self.path = path
        try:
            self.conn = sqlite3.connect(path)
            self.conn.row_factory = sqlite3.Row
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        except sqlite3.Error as e:
            raise NovyraError("Cannot open database %s: %s" % (path, e))

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    @contextmanager
    def transact(self):
        """Atomic multi-statement transaction: commit on success,
        roll back everything on any failure, so money writes and the
        matching ledger rows can never diverge."""
        try:
            yield
            self.conn.commit()
        except BaseException:
            try:
                self.conn.rollback()
            except sqlite3.Error:
                pass
            raise

    def _ts(self):
        return datetime.now().isoformat(timespec="seconds")

    # ---- key/value store (bankroll, emergency stop, counters, roster)

    def kv_get(self, key, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?",
                                (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key, value, commit=True):
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))
        if commit:
            self.conn.commit()

    # ---- audit log

    def log(self, actor, event, detail="", level="info"):
        self.conn.execute(
            "INSERT INTO log (ts, level, actor, event, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (self._ts(), level, actor, event, detail))
        self.conn.commit()

    def log_tail(self, n=10, level=None):
        if level:
            rows = self.conn.execute(
                "SELECT * FROM log WHERE level = ? ORDER BY id DESC LIMIT ?",
                (level, n)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM log ORDER BY id DESC LIMIT ?",
                (n,)).fetchall()
        return list(reversed(rows))

    def log_count(self, level=None):
        if level:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM log "
                                    "WHERE level = ?", (level,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM log").fetchone()
        return row["n"]

    # ---- memory (organisation scope and per-agent scope)

    def memory_add(self, scope, owner, fact):
        self.conn.execute(
            "INSERT INTO memory (ts, scope, owner, fact) VALUES (?, ?, ?, ?)",
            (self._ts(), scope, owner, fact))
        self.conn.commit()

    def memory_search(self, scope, owner=None, keyword=None, limit=50):
        q = "SELECT ts, fact FROM memory WHERE scope = ?"
        params = [scope]
        if owner:
            q += " AND owner = ?"
            params.append(owner)
        if keyword:
            q += " AND lower(fact) LIKE ?"
            params.append("%" + keyword.lower() + "%")
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [(r["ts"], r["fact"])
                for r in self.conn.execute(q, params).fetchall()]

    def memory_count(self, scope, owner):
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM memory WHERE scope = ? AND owner = ?",
            (scope, owner)).fetchone()
        return row["n"]

    def memory_count_all(self):
        row = self.conn.execute("SELECT COUNT(*) AS n FROM memory").fetchone()
        return row["n"]

    # ---- task board

    def task_create(self, title, owner, dept, priority="normal"):
        cur = self.conn.execute(
            "INSERT INTO tasks (title, owner, dept, priority, status, "
            "created) VALUES (?, ?, ?, ?, 'pending', ?)",
            (title, owner, dept, priority, self._ts()))
        self.conn.commit()
        return cur.lastrowid

    def task_set_status(self, tid, status, note=""):
        cur = self.conn.execute(
            "UPDATE tasks SET status = ?, note = ? WHERE tid = ?",
            (status, note, tid))
        self.conn.commit()
        if cur.rowcount == 0:
            raise NovyraError("Unknown task id T%03d" % tid)

    def tasks_all(self):
        return self.conn.execute("SELECT * FROM tasks ORDER BY tid") \
            .fetchall()

    def task_stats(self, dept=None):
        if dept:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE dept = ? "
                "GROUP BY status", (dept,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status") \
                .fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ---- decisions and approvals

    def decision_add(self, agent, question, choice, reason):
        self.conn.execute(
            "INSERT INTO decisions (ts, agent, question, choice, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (self._ts(), agent, question, choice, reason))
        self.conn.commit()

    def decisions_all(self):
        return self.conn.execute("SELECT * FROM decisions ORDER BY id") \
            .fetchall()

    def approval_add(self, action, detail, granted):
        self.conn.execute(
            "INSERT INTO approvals (ts, action, detail, granted) "
            "VALUES (?, ?, ?, ?)", (self._ts(), action, detail,
                                     1 if granted else 0))
        self.conn.commit()

    def approvals_count(self):
        row = self.conn.execute("SELECT COUNT(*) AS n FROM approvals") \
            .fetchone()
        return row["n"]

    # ---- communication history (persistent message bus)

    def message_add(self, sender, recipient, subject, body, commit=True):
        cur = self.conn.execute(
            "INSERT INTO messages (ts, sender, recipient, subject, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (self._ts(), sender, recipient, subject, body))
        if commit:
            self.conn.commit()
        return cur.lastrowid

    def messages_tail(self, n=20):
        rows = self.conn.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?",
            (n,)).fetchall()
        return list(reversed(rows))

    def messages_count(self):
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages").fetchone()
        return row["n"]

    # ---- guardian action watch (every sensitive action attempt)

    def action_add(self, actor, action, detail, allowed, commit=True):
        self.conn.execute(
            "INSERT INTO actions (ts, actor, action, detail, allowed) "
            "VALUES (?, ?, ?, ?, ?)",
            (self._ts(), actor, action, detail,
             1 if allowed else 0))
        if commit:
            self.conn.commit()

    def actions_count(self, allowed=None):
        if allowed is None:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM actions").fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM actions WHERE allowed = ?",
                (1 if allowed else 0,)).fetchone()
        return row["n"]

    # ---- bet ledger

    def bet_add(self, match_name, outcome, stake, odds, prob,
                source="simulated", commit=True):
        cur = self.conn.execute(
            "INSERT INTO bets (ts, match_name, outcome, stake, odds, prob, "
            "source, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
            (self._ts(), match_name, outcome, stake, odds, prob, source))
        if commit:
            self.conn.commit()
        return cur.lastrowid

    def bet_get(self, bet_id):
        return self.conn.execute("SELECT * FROM bets WHERE id = ?",
                                (bet_id,)).fetchone()

    def bet_settle(self, bet_id, result, result_source, pnl, commit=True):
        cur = self.conn.execute(
            "UPDATE bets SET status = 'settled', result = ?, "
            "result_source = ?, pnl = ? WHERE id = ? AND status = 'open'",
            (result, result_source, pnl, bet_id))
        if commit:
            self.conn.commit()
        if cur.rowcount == 0:
            raise BettingError("Bet #%d is not open" % bet_id)

    def bets_open(self):
        return self.conn.execute(
            "SELECT * FROM bets WHERE status = 'open' ORDER BY id").fetchall()

    def bets_all(self):
        return self.conn.execute("SELECT * FROM bets ORDER BY id").fetchall()

    # ---- maintenance

    def integrity_check(self):
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return row[0] if row else "unknown"

    def wipe_table(self, table):
        if table not in TABLES:
            raise NovyraError("Refusing to wipe unknown table %s" % table)
        self.conn.execute("DELETE FROM %s" % table)
        self.conn.commit()


# ----------------------------------------------------------- betting maths
# Pure functions, covered by the automated test suite.

OUTCOMES = ("home win", "draw", "away win")
MIN_STAKE = 1.0
MAX_STAKE_FRACTION = 0.02
INITIAL_BANKROLL = 10000.0


def poisson_pmf(lam, k):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def match_probs(lh, la, max_goals=8):
    """1X2 probabilities from expected goals via a Poisson grid."""
    ph = pd = pa = 0.0
    for h in range(max_goals):
        for a in range(max_goals):
            p = poisson_pmf(lh, h) * poisson_pmf(la, a)
            if h > a:
                ph += p
            elif h == a:
                pd += p
            else:
                pa += p
    s = ph + pd + pa
    return ph / s, pd / s, pa / s


def strength_probs(home_edge):
    """Second model: logistic split of non-draw mass by team strength."""
    pd = 0.26
    rest = 1.0 - pd
    ph = rest / (1.0 + math.exp(-home_edge))
    return ph, pd, rest - ph


def ev_of(p, odds):
    """Expected value of a 1 unit stake at decimal odds."""
    return p * (odds - 1.0) - (1.0 - p)


def kelly_fraction(p, odds):
    """Full Kelly fraction, floored at 0 and capped at 0.25."""
    b = odds - 1.0
    return max(0.0, min((p * b - (1.0 - p)) / b, 0.25))


class BettingBook:
    """The betting department's bankroll and bet ledger.

    Stakes are deducted from the bankroll at placement time, and the
    money write and the bet write happen in one atomic SQLite
    transaction: a failure can never deduct money without recording
    the bet, or record a bet without the deduction. Settlement
    matches the exact bet by id, re-checks that it is still open
    inside the same transaction that pays out (so double settlement
    cannot pay twice), and credits stake times odds back on a win.
    All money values are rounded to cents, so the ledger invariant
    (bankroll = initial + realised PnL - open exposure) holds exactly.
    """

    def __init__(self, db):
        self.db = db
        if self.db.kv_get("bankroll") is None:
            self.db.kv_set("bankroll", "%.2f" % INITIAL_BANKROLL)
            self.db.kv_set("bankroll_initial", "%.2f" % INITIAL_BANKROLL)

    @property
    def bankroll(self):
        return float(self.db.kv_get("bankroll"))

    @property
    def initial_bankroll(self):
        return float(self.db.kv_get("bankroll_initial",
                                    "%.2f" % INITIAL_BANKROLL))

    def open_bets(self):
        return self.db.bets_open()

    def place(self, match_name, outcome, odds, prob, stake,
              source="simulated", actor=None):
        if outcome not in OUTCOMES:
            raise BettingError("outcome must be one of %s"
                               % ", ".join(OUTCOMES))
        if odds <= 1.0:
            raise BettingError("odds must be above 1.0, got %.2f" % odds)
        if not 0.0 < prob < 1.0:
            raise BettingError("prob must be strictly between 0 and 1")
        stake = round(float(stake), 2)
        if stake < MIN_STAKE:
            raise BettingError("stake %.2f is below the %.2f minimum"
                               % (stake, MIN_STAKE))
        with self.db.transact():
            bankroll = float(self.db.kv_get("bankroll"))
            cap = MAX_STAKE_FRACTION * bankroll
            if stake > cap + 1e-9:
                raise BettingError("stake %.2f exceeds the %.2f cap "
                                   "(%.0f%% of bankroll)"
                                   % (stake, cap,
                                      MAX_STAKE_FRACTION * 100))
            if stake > bankroll + 1e-9:
                raise BettingError("insufficient bankroll: %.2f available"
                                   % bankroll)
            self.db.kv_set("bankroll", "%.2f" % (bankroll - stake),
                           commit=False)
            bet_id = self.db.bet_add(match_name, outcome, stake, odds,
                                     prob, source, commit=False)
        self.db.action_add(actor or "Betting Portfolio Manager",
                           "place bet",
                           "#%d %s on %s, stake %.2f (SIMULATED)"
                           % (bet_id, match_name, outcome, stake), 1)
        return bet_id

    def settle(self, bet_id, result, result_source="simulated",
               actor=None):
        if result not in OUTCOMES:
            raise BettingError("result must be one of %s"
                               % ", ".join(OUTCOMES))
        with self.db.transact():
            bet = self.db.bet_get(bet_id)
            if bet is None:
                raise BettingError("no bet with id #%d" % bet_id)
            if bet["status"] != "open":
                raise BettingError("bet #%d is already settled" % bet_id)
            won = (bet["outcome"] == result)
            if won:
                credit = round(bet["stake"] * bet["odds"], 2)
                pnl = round(credit - bet["stake"], 2)
            else:
                credit = 0.0
                pnl = -round(bet["stake"], 2)
            # the guarded UPDATE makes double settlement impossible
            self.db.bet_settle(bet_id, result, result_source, pnl,
                               commit=False)
            if credit:
                bankroll = float(self.db.kv_get("bankroll"))
                self.db.kv_set("bankroll", "%.2f" % (bankroll + credit),
                               commit=False)
        self.db.action_add(actor or "Post-Match Review", "settle bet",
                           "#%d result %s (%s): %s, PnL %+.2f"
                           % (bet_id, result, result_source,
                              "won" if won else "lost", pnl), 1)
        return won, pnl

    def settled_rows(self):
        return [r for r in self.db.bets_all() if r["status"] != "open"]

    def realized_pnl(self):
        return sum((r["pnl"] or 0.0) for r in self.settled_rows())

    def open_exposure(self):
        return sum(r["stake"] for r in self.open_bets())


# ---------------------------------------------------------------- blueprint

BLUEPRINT = [
    ("Executive & Core Management", [
        ("CEO Agent", "Leads and coordinates the entire Novyra ecosystem", "manager"),
        ("Developer Agent", "Builds agents and infrastructure, gated by Owner approval", "worker"),
        ("Guardian Agent", "Monitors security, reliability and abnormal behaviour", "worker"),
    ]),
    ("Betting Department", [
        ("Betting Manager", "Heads the Betting Department and its KPIs", "manager"),
        ("Data Collector", "Collects fixtures, stats, odds and lineup data", "worker"),
        ("Data Quality", "Validates and normalises all incoming sports data", "worker"),
        ("Market Intelligence", "Tracks odds movements across bookmakers", "worker"),
        ("Opportunity Scanner", "Scans markets for opportunities worth analysing", "worker"),
        ("Prediction Engine", "Models outcome probabilities with multiple methods", "worker"),
        ("Value Finder", "Computes implied probabilities and expected value", "worker"),
        ("Confidence", "Assesses prediction reliability separately from probability", "worker"),
        ("Risk Manager", "Enforces bankroll and concentration risk limits", "worker"),
        ("Portfolio Manager", "Allocates stakes and manages open positions", "worker"),
        ("Simulation", "Tests strategies on synthetic bet samples", "worker"),
        ("Post-Match Review", "Compares predictions with actual results", "worker"),
        ("Learning", "Analyses model performance and recommends improvements", "worker"),
        ("Betting Memory", "Stores betting history, strategies and patterns", "worker"),
    ]),
    ("Ecommerce Department", [
        ("Ecommerce Manager", "Heads the Ecommerce Department and its KPIs", "manager"),
        ("Product Research", "Researches products with commercial potential", "worker"),
        ("Market Research", "Analyses demand, competitors and customer needs", "worker"),
        ("Supplier", "Finds and compares manufacturers and suppliers", "worker"),
        ("Pricing", "Calculates margins and recommends selling prices", "worker"),
        ("Store Management", "Runs store listings, categories and inventory", "worker"),
        ("Advertising", "Runs and optimises paid advertising campaigns", "worker"),
        ("Customer Support", "Handles customer questions, complaints and returns", "worker"),
        ("Sales Analytics", "Tracks revenue, conversion and sales trends", "worker"),
    ]),
    ("Marketing Department", [
        ("Marketing Manager", "Heads the Marketing Department and its KPIs", "manager"),
        ("SEO", "Improves organic visibility and keyword rankings", "worker"),
        ("Content Creation", "Writes articles, copy and social content", "worker"),
        ("Social Media", "Plans and analyses social publishing", "worker"),
        ("Advertising", "Plans paid campaigns and monitors CPC, CPA, ROAS", "worker"),
        ("Email Marketing", "Builds newsletters and automated sequences", "worker"),
        ("Marketing Analytics", "Compares channels and reports performance", "worker"),
        ("Conversion Optimization", "Improves journeys and runs A/B tests", "worker"),
    ]),
    ("Trading Department", [
        ("Trading Manager", "Heads the Trading Department and its KPIs", "manager"),
        ("Market Data", "Collects prices, indicators and market datasets", "worker"),
        ("Market Analysis", "Analyses trends, technicals and fundamentals", "worker"),
        ("Strategy", "Researches and backtests quantitative models", "worker"),
        ("Signal Detection", "Detects and filters market trading signals", "worker"),
        ("Risk Manager", "Enforces exposure, drawdown and sizing limits", "worker"),
        ("Portfolio Manager", "Manages open positions and allocation", "worker"),
        ("Trade Review", "Reviews completed trades and mistakes", "worker"),
    ]),
    ("Finance Department", [
        ("Finance Manager", "Heads the Finance Department and its KPIs", "manager"),
        ("Revenue Tracking", "Tracks revenue from all departments", "worker"),
        ("Expense Management", "Categorises costs and flags unusual spending", "worker"),
        ("Financial Forecasting", "Builds scenarios and forecasts cash needs", "worker"),
        ("Cash Flow", "Monitors liquidity and upcoming obligations", "worker"),
        ("Financial Reporting", "Produces monthly and annual summaries", "worker"),
    ]),
]

SHARED_SYSTEMS = [
    ("Organisation Memory", "Company-wide knowledge shared between agents"),
    ("Agent Memory", "Private memory for each individual agent"),
    ("Learning System", "Collects feedback and drives improvement"),
    ("Decision Engine", "Records decisions and their reasoning"),
    ("Communication System", "Message bus between agents and departments"),
    ("Task Management", "Tasks, priorities, assignment and tracking"),
    ("Audit & Logging", "Traceable history of agent actions"),
    ("Safety & Permissions", "Owner approval gates and emergency stop"),
    ("Betting Book", "Bankroll, ledger, risk limits and settlement"),
]

DEPT_ORDER = [d for d, _ in BLUEPRINT]

DEPT_RECOMMENDATIONS = {
    "Ecommerce Department": [
        "test two new product listings this week",
        "renegotiate supplier shipping costs",
        "launch a bundle offer to raise average order value",
    ],
    "Marketing Department": [
        "shift budget toward the best ROAS channel",
        "refresh the top three landing pages",
        "start an email re-engagement sequence",
    ],
    "Trading Department": [
        "tighten signal filters before adding size",
        "backtest the momentum model on more history",
        "review the two worst performing positions",
    ],
    "Finance Department": [
        "trim one recurring cost category",
        "build a 90 day cash flow scenario",
        "reconcile department revenue against targets",
    ],
}

TEAMS = [
    ("Helsinki Wolves", 1.85), ("Espoo Rangers", 1.55),
    ("Turku United", 1.40), ("Tampere Blitz", 1.60),
    ("Oulu Force", 1.20), ("Kuopio Kings", 1.35),
    ("Lahti Lions", 1.30), ("Pori Storm", 1.10),
]
BOOKIES = ["NordicBet", "Veikkaus", "Betfair", "Unibet"]


def BLUEPRINT_MEMBERS(dept):
    for d, members in BLUEPRINT:
        if d == dept:
            return members
    return []


# ------------------------------------------------- safety & permissions

class Permissions:
    """Safety & Permission System, backed by the database.

    Every sensitive action goes through request(), which records an
    approval or denial row. The emergency stop is persisted, so a
    restart does not silently re-enable halted agents, and resume also
    requires Owner approval.
    """

    def __init__(self, runtime):
        self.runtime = runtime
        if self.runtime.db.kv_get("emergency_stop") is None:
            self.runtime.db.kv_set("emergency_stop", "0")
        if self.runtime.db.kv_get("denied_actions") is None:
            self.runtime.db.kv_set("denied_actions", "0")
        if self.runtime.db.kv_get("blocked_actions") is None:
            self.runtime.db.kv_set("blocked_actions", "0")

    @property
    def emergency_stop(self):
        return self.runtime.db.kv_get("emergency_stop") == "1"

    @property
    def denied_actions(self):
        return int(self.runtime.db.kv_get("denied_actions", "0"))

    @property
    def blocked_actions(self):
        return int(self.runtime.db.kv_get("blocked_actions", "0"))

    def _bump_denied(self):
        self.runtime.db.kv_set("denied_actions", str(self.denied_actions + 1))

    def _bump_blocked(self):
        self.runtime.db.kv_set("blocked_actions",
                               str(self.blocked_actions + 1))

    def request(self, action, detail=""):
        question = "Owner approval required: %s%s" % (
            action, (": %s" % detail) if detail else "")
        granted = self.runtime.ask_owner(question)
        self.runtime.db.approval_add(action, detail, granted)
        self.runtime.db.log("Owner", "approved" if granted else "denied",
                            "%s %s" % (action, detail))
        if not granted:
            self._bump_denied()
        return granted

    def guard(self, action, agent_name):
        """Runtime gate: returns False when the emergency stop is on."""
        if self.emergency_stop:
            self._bump_blocked()
            self.runtime.db.log(
                "Guardian Agent", "blocked",
                "%s attempted '%s' during emergency stop"
                % (agent_name, action), level="warn")
            return False
        return True

    def halt(self):
        self.runtime.db.kv_set("emergency_stop", "1")
        self.runtime.db.log("Owner", "emergency stop",
                            "all agents halted", level="warn")

    def do_resume(self):
        self.runtime.db.kv_set("emergency_stop", "0")
        self.runtime.db.log("Owner", "resume", "agents back online")


# ------------------------------------------------------------ communication

@dataclass
class Msg:
    sender: str
    recipient: str
    subject: str
    body: str = ""
    ts: object = None


class Bus:
    """Communication System. Every message is persisted to the
    messages table and recorded in the audit log, so communication
    history survives restarts."""

    def __init__(self, db):
        self.db = db
        self.history = []

    def send(self, sender, recipient, subject, body=""):
        ts = datetime.now().isoformat(timespec="seconds")
        self.db.message_add(sender, recipient, subject, body)
        m = Msg(sender, recipient, subject, body, ts)
        self.history.append(m)
        self.db.log("Communication System", "message",
                    "%s -> %s: %s" % (sender, recipient, subject))
        return m

    def tail(self, n=20):
        return self.db.messages_tail(n)


# ---------------------------------------------------------------- the agents

class Agent:
    """Base agent. The control plane (money, approvals, deployment) is
    deterministic; the analysis plane is the LLM when configured, or a
    heuristic fallback otherwise."""

    is_manager = False

    def __init__(self, name, dept, role, runtime):
        self.name = name
        self.id = runtime.unique_id(name)
        self.dept = dept
        self.role = role
        self.runtime = runtime

    def remember(self, fact):
        self.runtime.db.memory_add("agent", self.id, fact)

    def recall(self, keyword=None):
        return self.runtime.db.memory_search("agent", self.id, keyword)

    def send(self, recipient, subject, body=""):
        return self.runtime.bus.send(self.name, recipient, subject, body)

    def llm_reply(self, text):
        llm = self.runtime.llm
        if not llm.configured:
            return None
        notes = [f for _, f in self.recall()][:5]
        context = ("Recent memory notes:\n%s"
                   % "\n".join("- " + n for n in notes)
                   if notes else "No relevant memory notes.")
        system = ("You are %s, %s in the %s of Novyra, a company operated "
                  "by AI agents under a human Owner. Responsibilities: %s "
                  "Answer in character, at most 120 words. You analyse and "
                  "advise; money movements, approvals and deployments are "
                  "handled by deterministic Novyra systems outside your "
                  "control, so never claim to have executed an action or "
                  "spent funds."
                  % (self.name, self.role.lower(), self.dept, self.role))
        reply = llm.chat(system, "Message: %s\n\n%s" % (text, context))
        if reply:
            self.remember("Replied (LLM) to: %s" % text[:80])
        elif llm.last_error:
            self.runtime.db.log("LLM", "warning",
                                "LLM call failed: %s" % llm.last_error,
                                level="warn")
        return reply

    def think(self, text):
        reply = self.llm_reply(text)
        if reply:
            return reply
        return self.heuristic(text)

    # -- action execution: the LLM plans, deterministic code acts

    def act(self, instruction):
        """Plan and execute safe, useful actions for an instruction.

        With an LLM configured the agent proposes a JSON plan of
        whitelisted actions; the runtime validates and executes it
        deterministically. Without an LLM, or on any invalid or unsafe
        plan, the agent falls back to its heuristic, which also
        executes real work. The whitelist never includes money,
        approval, roster or filesystem operations, so execution
        cannot bypass the Owner.
        """
        rt = self.runtime
        plan = None
        if rt.llm.configured:
            plan = self._llm_plan(instruction)
            if plan is None and rt.llm.last_error is None:
                rt.db.log(self.name, "act",
                          "LLM action plan invalid or unsafe; fell back "
                          "to deterministic behaviour", level="warn")
        if plan is None:
            return self.heuristic(instruction)
        lines = self._execute_plan(plan)
        if lines is None:
            return self.heuristic(instruction)
        rt.db.log(self.name, "act",
                  "executed %d planned action(s)" % len(lines))
        out = ["%s executed %d action(s) for '%s':"
               % (self.name, len(lines), instruction[:60])]
        out.extend("  - " + l for l in lines)
        return "\n".join(out)

    def _llm_plan(self, instruction):
        """Ask the LLM for a JSON action plan. Returns the validated
        plan, or None for any missing, malformed or unsafe reply."""
        llm = self.runtime.llm
        system = (
            "You are %s, %s in the %s of Novyra. Decide on concrete "
            "actions to carry out the Owner's instruction. Reply ONLY "
            "with a JSON array of at most 4 action objects, no prose. "
            "Allowed actions and their fields: "
            '{"action":"create_task","title":"short task title"}, '
            '{"action":"remember","fact":"durable note"}, '
            '{"action":"message","recipient":"agent name",'
            '"subject":"short","body":"message text"}, '
            '{"action":"search_memory","keyword":"word"}, '
            '{"action":"none"}. You cannot move money, approve '
            "actions, create agents or touch files; do not invent "
            "other actions."
            % (self.name, self.role.lower(), self.dept))
        raw = llm.chat(system, "Instruction: %s" % instruction)
        if not raw:
            return None
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start:end + 1])
        except ValueError:
            return None
        if not isinstance(data, list) or not data or len(data) > 4:
            return None
        for item in data:
            if not isinstance(item, dict) \
                    or item.get("action") not in AGENT_ACTIONS:
                return None
        return data

    def _execute_plan(self, plan):
        """Validate and execute each planned action. Refused items are
        recorded in the actions table with allowed=0."""
        rt = self.runtime
        lines = []
        for item in plan:
            action = item.get("action")
            try:
                if action == "create_task":
                    title = str(item.get("title", "")).strip()[:100]
                    if not title:
                        raise NovyraError("empty task title")
                    tid = rt.db.task_create(title, self.name, self.dept)
                    lines.append("created task T%03d: %s" % (tid, title))
                elif action == "remember":
                    fact = str(item.get("fact", "")).strip()[:200]
                    if not fact:
                        raise NovyraError("empty memory fact")
                    self.remember(fact)
                    lines.append("stored memory: %s" % fact[:60])
                elif action == "message":
                    recipient = str(item.get("recipient", "")).strip()
                    name = recipient
                    if recipient.lower() != "owner":
                        hit = rt.find(recipient)
                        if not isinstance(hit, Agent):
                            raise NovyraError(
                                "no active agent matches '%s'" % recipient)
                        name = hit.name
                    subject = str(item.get("subject", "")).strip()[:80] \
                        or "message from %s" % self.name
                    body = str(item.get("body", ""))[:500]
                    self.send(name, subject, body)
                    lines.append("sent a message to %s" % name)
                elif action == "search_memory":
                    keyword = str(item.get("keyword", "")).strip()[:60]
                    hits = self.recall(keyword)
                    lines.append("memory search '%s': %d hit(s)"
                                 % (keyword, len(hits)))
                elif action == "none":
                    lines.append("no actions needed")
            except NovyraError as e:
                rt.db.action_add(self.name, str(action),
                                 "refused: %s" % e, 0)
                rt.db.log(self.name, "action refused", str(e),
                          level="warn")
                continue
            rt.db.action_add(self.name, str(action), "", 1)
        return lines or None

    def heuristic(self, text):
        return ("%s received your message. As %s I focus on: %s."
                % (self.name, self.role, self.role.lower()))

    def standup(self):
        return "Standing by (%s)." % self.role


class Worker(Agent):
    """Generic specialist worker. Used for every agent without a
    dedicated behaviour class."""

    def heuristic(self, text):
        tid = self.runtime.db.task_create("Follow up: %s" % text[:48],
                                         self.name, self.dept)
        mgr = self.runtime.manager_of(self.dept)
        if mgr:
            self.send(mgr.name, "logged owner request",
                      "%s queued as T%03d" % (text[:60], tid))
        return ("Understood. As %s my focus is: %s. I logged this as T%03d "
                "and will factor it into my next work cycle."
                % (self.name, self.role.lower(), tid))


class ManagerAgent(Agent):
    is_manager = True

    def __init__(self, name, dept, role, runtime):
        super().__init__(name, dept, role, runtime)
        self.metrics = {}

    def think(self, text):
        return self.report()

    def heuristic(self, text):
        return self.report()

    def report(self):
        rt = self.runtime
        lines = ["%s operational report:" % self.name]
        for k, v in self.metrics.items():
            lines.append("  - %s: %s" % (k, v))
        team = rt.dept_agents(self.dept)
        pending = rt.db.task_stats(self.dept).get("pending", 0)
        lines.append("  - Team: %d specialists, %d pending tasks"
                     % (len(team), pending))
        rec = DEPT_RECOMMENDATIONS.get(self.dept)
        if rec:
            lines.append("  - Recommendation: %s" % random.choice(rec))
        self.send("CEO Agent", "department report",
                  "delivered %s" % now_str())
        self.remember("Delivered department report")
        return "\n".join(lines)

    def standup(self):
        pending = self.runtime.db.task_stats(self.dept).get("pending", 0)
        return ("Coordinating %d specialists, %d pending tasks."
                % (len(self.runtime.dept_agents(self.dept)), pending))


class CEOAgent(Agent):
    is_manager = True
    metrics = {}

    def think(self, text):
        rt = self.runtime
        t = " %s " % text.lower()
        self.remember("Owner said: %s" % text[:100])

        if rt.permissions.emergency_stop:
            return ("Emergency stop is engaged. I can still talk, but every "
                    "department pipeline is frozen. Use 'resume' to "
                    "restart, which needs your approval.")

        if t.strip() in ("hello", "hi", "hey", "moi", "hei"):
            return ("Hello, Owner. I am coordinating %d blueprint entries. "
                    "Ask for a 'report', run a 'scan', or say 'activate "
                    "<department>' to grow the company."
                    % rt.blueprint_size())

        if any(w in t for w in (" report", " status", " how are")):
            return self.daily_report()

        if any(w in t for w in (" betting", " odds", " scan", " value")):
            if rt.dept_active("Betting Department"):
                rt.run_betting_scan()
                return ("Scan complete. The full chain from Data Collector "
                        "to Betting Memory has logged this run.")
            return ("The Betting Department is still planned. Say "
                    "'activate Betting Department' and I will set it up "
                    "after your approval.")

        for keyword, dept in (("ecommerce", "Ecommerce Department"),
                              ("shop", "Ecommerce Department"),
                              ("marketing", "Marketing Department"),
                              ("seo", "Marketing Department"),
                              ("trading", "Trading Department"),
                              ("stocks", "Trading Department"),
                              ("finance", "Finance Department"),
                              ("cash", "Finance Department"),
                              ("revenue", "Finance Department")):
            if keyword in t:
                if rt.dept_active(dept):
                    return rt.manager_of(dept).report()
                return ("%s is still planned. Say 'activate %s' when you "
                        "want it online." % (dept, dept))

        if any(w in t for w in (" hire", " new agent", " propose", " build")):
            return ("Good instinct. Use 'propose <agent name>' and I will "
                    "draft the business case for your approval. The "
                    "Developer Agent handles construction after you sign "
                    "off. Use 'agents' to see the full blueprint.")

        if " stop" in t:
            return ("If you want a full halt, use the 'stop' command. It "
                    "engages the emergency stop in the Safety & Permission "
                    "System and freezes every agent. Resuming also needs "
                    "your approval.")

        if "who" in t and ("you" in t or "novyra" in t):
            return ("I am the CEO Agent of Novyra, version %s. I coordinate "
                    "%d blueprint entries, keep you informed, and propose "
                    "new agents when there is a genuine business need. You "
                    "always have final approval."
                    % (VERSION, rt.blueprint_size()))

        reply = self.llm_reply(text)
        if reply:
            return reply

        tid = rt.db.task_create("Owner request: %s" % text[:48],
                                "CEO Agent", "Executive & Core Management")
        return ("Noted, logged as T%03d. I have no department that owns "
                "this yet. Activate one with 'activate <department>', or "
                "propose a new agent with 'propose <name>'." % tid)

    def daily_report(self):
        rt = self.runtime
        L = ["NOVYRA DAILY REPORT  (%s)" % now_str()]
        L.append("Organisation: %d active agents, %d blueprint entries, "
                 "%d decisions logged"
                 % (len(rt.agents), rt.blueprint_size(),
                    len(rt.db.decisions_all())))
        for dept in DEPT_ORDER:
            mgr = rt.manager_of(dept)
            if mgr:
                first = next(iter(mgr.metrics.items()), None)
                tail = (", %s: %s" % first) if first else ""
                L.append("  - %s: online%s" % (dept, tail))
            else:
                L.append("  - %s: planned" % dept)
        if rt.dept_active("Betting Department"):
            book = rt.book
            L.append("Betting (simulation): bankroll %s, realised PnL "
                     "%+.2f, %d open positions, exposure %s"
                     % (money(book.bankroll), book.realized_pnl(),
                        len(book.open_bets()), money(book.open_exposure())))
        issues = rt.guardian.monitor()
        if issues:
            L.append("Risk (Guardian):")
            for i in issues:
                L.append("  ! " + i)
        else:
            L.append("Risk (Guardian): all systems nominal.")
        for dept in DEPT_ORDER:
            if not rt.dept_active(dept):
                L.append("Next step: activate the %s." % dept)
                break
        self.send("Owner", "daily report", "delivered %s" % now_str())
        rt.db.decision_add(self.name, "Issue daily report?",
                           "published", "routine reporting duty")
        return "\n".join(L)


class DeveloperAgent(Agent):
    def heuristic(self, text):
        builds = self.runtime.db.memory_count("agent", self.id)
        tid = self.runtime.db.task_create("Review request: %s" % text[:48],
                                          self.name, self.dept)
        return ("Developer Agent online. Codebase v%s. I have built %d "
                "agents so far. I logged your request as T%03d. "
                "Restrictions in force: I cannot create or activate "
                "agents without Owner approval, enforced by the Safety "
                "& Permission System."
                % (VERSION, builds, tid))


class GuardianAgent(Agent):
    """Security and integrity monitor. monitor() is the cheap patrol
    used in reports and after every command; full_scan() is the deep
    check behind the 'guardian' command."""

    def monitor(self):
        rt = self.runtime
        issues = []
        if rt.permissions.emergency_stop:
            issues.append("Emergency stop is engaged, all agents halted.")
        denied = rt.permissions.denied_actions
        blocked = rt.permissions.blocked_actions
        if denied or blocked:
            issues.append("%d Owner denial(s) and %d blocked action(s) "
                          "on record." % (denied, blocked))
        errors = rt.db.log_count(level="error")
        if errors:
            issues.append("%d errors in the audit log." % errors)
        failed = rt.db.task_stats().get("failed", 0)
        if failed:
            issues.append("%d failed tasks." % failed)
        open_bets = rt.book.open_bets()
        if open_bets:
            issues.append("%d open simulated positions, %s at risk."
                          % (len(open_bets), money(rt.book.open_exposure())))
        if rt.sim_autonomy:
            issues.append("Autonomous simulated betting is ENABLED; "
                          "scans place and settle without per-bet "
                          "approval.")
        return issues

    def full_scan(self):
        """Deep integrity scan: database health, the bankroll ledger
        invariant, settlement records, exposure concentration,
        approval patterns and suspicious behaviour. Every finding is
        returned as a human-readable line; the scan itself is audited."""
        rt = self.runtime
        db = rt.db
        findings = []
        try:
            result = db.integrity_check()
            if result != "ok":
                findings.append("CRITICAL: database integrity check "
                                "failed: %s" % result)
        except NovyraError as e:
            findings.append("CRITICAL: integrity check error: %s" % e)
        book = rt.book
        expected = (book.initial_bankroll + book.realized_pnl()
                    - book.open_exposure())
        gap = book.bankroll - expected
        if abs(gap) > 0.011:
            findings.append(
                "CRITICAL: bankroll %s does not match the ledger "
                "(expected %s, gap %+.2f). Money may be missing or "
                "tampered with."
                % (money(book.bankroll), money(expected), gap))
        bad = [r["id"] for r in db.bets_all()
               if r["status"] != "open"
               and (r["pnl"] is None or r["result"] is None)]
        if bad:
            findings.append("DATA: %d settled bet(s) missing a result "
                            "or PnL: %s"
                            % (len(bad),
                               ", ".join("#%d" % b for b in bad)))
        open_bets = book.open_bets()
        if open_bets and book.bankroll > 0:
            ratio = book.open_exposure() / book.bankroll
            if ratio > 0.10:
                findings.append("RISK: open exposure is %.1f%% of the "
                                "bankroll (limit 10%%)."
                                % (ratio * 100))
        approvals = db.approvals_count()
        denied = rt.permissions.denied_actions
        if approvals >= 4 and denied * 2 > approvals:
            findings.append("SUSPICIOUS: %d of %d approval requests "
                            "were denied. Review recent activity."
                            % (denied, approvals))
        blocked = rt.permissions.blocked_actions
        if blocked:
            findings.append("%d action(s) were blocked during an "
                            "emergency stop." % blocked)
        errors = db.log_count(level="error")
        if errors:
            findings.append("%d error(s) in the audit log." % errors)
        try:
            roster = json.loads(db.kv_get("agents_json", "[]"))
            if not isinstance(roster, list):
                raise ValueError("roster is not a list")
        except (ValueError, TypeError):
            findings.append("DATA: the persisted agent roster is "
                            "unreadable. Fix it before restarting.")
        if rt.sim_autonomy:
            findings.append("AUTH: autonomous simulated betting is "
                            "ENABLED (per-bet approval bypassed by "
                            "Owner authorisation).")
        if rt.permissions.emergency_stop:
            findings.append("Emergency stop is engaged, all agents "
                            "halted.")
        db.log("Guardian Agent", "integrity scan",
               "%d finding(s)" % len(findings),
               level="warn" if findings else "info")
        db.kv_set("last_guardian_scan", now_str())
        return findings

    def think(self, text):
        reply = self.llm_reply(text)
        if reply:
            return reply
        return self.heuristic(text)

    def heuristic(self, text):
        issues = self.monitor()
        if not issues:
            return ("Guardian Agent: all systems nominal. Permissions, "
                    "access controls and workflows look clean. I will "
                    "alert the CEO and you the moment anything looks off.")
        return ("Guardian Agent ALERT:\n"
                + "\n".join("  ! " + i for i in issues))


# ------------------------------------------------------------ betting agents

class BettingWorker(Worker):
    def step(self, ctx):
        return "%s has no pipeline duties in this run." % self.name


class DataCollector(BettingWorker):
    def step(self, ctx):
        (home, sh), (away, sa) = random.sample(TEAMS, 2)
        xg_h = sh * random.uniform(0.75, 1.30)
        xg_a = sa * random.uniform(0.50, 0.95)
        ctx["fixture"] = {
            "home": home, "away": away, "league": "Suomi League",
            "xg_h": xg_h, "xg_a": xg_a,
            "injuries": "%d-%d" % (random.randint(0, 2),
                                   random.randint(0, 3)),
            "kickoff": "%s 19:00" % now_str()[:10],
        }
        return ("Collected fixture: %s vs %s (%s). Pulled team stats, xG "
                "history, injury lists (%s) and odds feeds from %d "
                "providers."
                % (home, away, ctx["fixture"]["league"],
                   ctx["fixture"]["injuries"], len(BOOKIES)))


class DataQuality(BettingWorker):
    def step(self, ctx):
        issues = []
        if random.random() < 0.30:
            issues.append("one provider returned stale odds, dropped")
        if random.random() < 0.15:
            issues.append("duplicate fixture entry merged")
        ctx["quality"] = "warnings: " + "; ".join(issues) if issues \
            else "clean"
        return ("Validated the dataset. Timestamps consistent, no missing "
                "keys. %s"
                % (("Findings: " + "; ".join(issues) + ".")
                   if issues else "No anomalies detected."))


class MarketIntelligence(BettingWorker):
    def step(self, ctx):
        f = ctx["fixture"]
        fair = match_probs(f["xg_h"], f["xg_a"])
        ctx["fair"] = fair
        odds = {o: {} for o in OUTCOMES}
        for bookie in BOOKIES:
            for i, o in enumerate(OUTCOMES):
                odds[o][bookie] = (1.0 / fair[i]) * random.uniform(0.93, 0.97)
        if random.random() < 0.65:
            o = random.choice(OUTCOMES)
            i = OUTCOMES.index(o)
            odds[o][random.choice(BOOKIES)] = \
                (1.0 / fair[i]) * random.uniform(1.02, 1.12)
        ctx["odds"] = odds
        ctx["best"] = {o: max(odds[o].values()) for o in OUTCOMES}
        drifted = random.choice(OUTCOMES)
        return ("Market check: %d bookmakers compared. Mild drift on the "
                "%s. Best prices: %s."
                % (len(BOOKIES), drifted,
                   ", ".join("%s @ %.2f" % (o, ctx["best"][o])
                             for o in OUTCOMES)))


class OpportunityScanner(BettingWorker):
    def step(self, ctx):
        f = ctx["fixture"]
        ctx["priority"] = random.choice(["high", "medium", "high"])
        return ("Scan verdict: fixture %s vs %s prioritised as %s. Market "
                "liquidity and data coverage pass the bar. Sent to the "
                "Prediction Engine."
                % (f["home"], f["away"], ctx["priority"]))


class PredictionEngine(BettingWorker):
    def step(self, ctx):
        f = ctx["fixture"]
        m1 = match_probs(f["xg_h"], f["xg_a"])
        m2 = strength_probs((f["xg_h"] - f["xg_a"]) * 1.1)
        ctx["m1"], ctx["m2"] = m1, m2
        ctx["probs"] = tuple(0.6 * a + 0.4 * b for a, b in zip(m1, m2))
        return ("Two models blended. Poisson xG model: %s. Team strength "
                "model: %s. Blend (60/40): %s."
                % (" / ".join(pct(x) for x in m1),
                   " / ".join(pct(x) for x in m2),
                   " / ".join(pct(x) for x in ctx["probs"])))


class ValueFinder(BettingWorker):
    def step(self, ctx):
        best = ctx["best"]
        probs = ctx["probs"]
        table = []
        for i, o in enumerate(OUTCOMES):
            table.append((o, probs[i], best[o], ev_of(probs[i], best[o])))
        pick = max(table, key=lambda r: r[3])
        if pick[3] <= 0:
            ctx["abort"] = ("No positive expected value anywhere. Best EV "
                            "was %s at %+.1f%%. Passing on this fixture."
                            % (pick[0], pick[3] * 100))
            return ("Compared blended probabilities against best odds. "
                    "Implied edges: %s. No qualifying value."
                    % ", ".join("%s EV %+.1f%%" % (r[0], r[3] * 100)
                                for r in table))
        ctx["pick"] = pick
        return ("Expected value scan: %s. Best edge: %s at %.2f with "
                "blended probability %s, EV %+.1f%%."
                % (", ".join("%s EV %+.1f%%" % (r[0], r[3] * 100)
                             for r in table),
                   pick[0], pick[2], pct(pick[1]), pick[3] * 100))


class Confidence(BettingWorker):
    def step(self, ctx):
        pick = ctx["pick"]
        i = OUTCOMES.index(pick[0])
        gap = abs(ctx["m1"][i] - ctx["m2"][i])
        conf = "high" if gap < 0.05 else ("medium" if gap < 0.12 else "low")
        ctx["confidence"] = conf
        return ("Reliability check: model gap on the pick is %.1f points, "
                "sample quality good. Confidence rated %s. Note: this is "
                "reliability, not the probability of winning."
                % (gap * 100, conf))


class BettingRiskManager(BettingWorker):
    def step(self, ctx):
        pick = ctx["pick"]
        book = self.runtime.book
        bankroll = book.bankroll
        frac = kelly_fraction(pick[1], pick[2])
        stake = round(min(frac * bankroll,
                          MAX_STAKE_FRACTION * bankroll), 2)
        match = "%s vs %s" % (ctx["fixture"]["home"], ctx["fixture"]["away"])
        same = sum(1 for b in book.open_bets()
                   if b["match_name"] == match)
        if same >= 2:
            ctx["abort"] = ("Concentration limit: %d positions already "
                            "open on this match." % same)
            return "Risk view: concentration limit reached. Declined."
        if stake < MIN_STAKE:
            ctx["abort"] = ("Edge too thin: Kelly stake %.2f is below "
                            "the %.2f minimum." % (stake, MIN_STAKE))
            return ("Risk view: Kelly fraction %.2f%% of bankroll gives "
                    "%.2f, below the minimum stake. No bet."
                    % (frac * 100, stake))
        ctx["stake"] = stake
        exposure = book.open_exposure() + stake
        return ("Risk view: Kelly %.2f%%, capped at 2%%. Approved stake %s "
                "(%.1f%% of bankroll). Exposure after placement: %.1f%%."
                % (frac * 100, money(stake), stake / bankroll * 100,
                   exposure / bankroll * 100))


class BettingPortfolioManager(BettingWorker):
    def step(self, ctx):
        if "stake" not in ctx:
            return "Portfolio: nothing to place this run."
        rt = self.runtime
        book = rt.book
        match = "%s vs %s" % (ctx["fixture"]["home"], ctx["fixture"]["away"])
        pick = ctx["pick"]
        if not rt.sim_autonomy:
            detail = ("SIMULATED bet on %s in %s at odds %.2f, blended "
                      "probability %s, stake %s"
                      % (pick[0], match, pick[2], pct(pick[1]),
                         money(ctx["stake"])))
            if not rt.permissions.request("place simulated bet", detail):
                ctx["abort"] = ("Owner declined the bet on %s. Nothing "
                                "was placed." % match)
                return "Portfolio: declined by the Owner."
        try:
            bet_id = book.place(match, pick[0], pick[2], pick[1],
                               ctx["stake"], source="simulated",
                               actor=self.name)
        except BettingError as e:
            ctx["abort"] = "Placement declined: %s" % e
            return "Portfolio: %s" % e
        ctx["bet_id"] = bet_id
        self.runtime.db.task_create("Track bet #%d: %s" % (bet_id, match),
                                    self.name, self.dept)
        return ("Placed SIMULATED bet #%d: %s on %s at odds %.2f, stake %s "
                "(deducted now). Bankroll: %s.%s"
                % (bet_id, match, pick[0], pick[2], money(ctx["stake"]),
                   money(book.bankroll),
                   "" if rt.sim_autonomy else
                   " The bet stays open: settlement needs the Owner "
                   "('settle') or autonomous mode."))


class Simulation(BettingWorker):
    def step(self, ctx):
        if "bet_id" not in ctx:
            return ("Simulation: nothing to test this run. Reference "
                    "sample: 400 synthetic bets at comparable edges "
                    "average ROI between -0.8% and +2.5%.")
        pick = ctx["pick"]
        n = 500
        wins = sum(1 for _ in range(n) if random.random() < pick[1])
        roi = (wins * (pick[2] - 1) - (n - wins)) / float(n)
        ctx["sim_roi"] = roi
        return ("Monte Carlo: 500 bets at this edge -> %d wins, estimated "
                "ROI %+.1f%%. The 2%% stake cap keeps drawdowns inside "
                "limits."
                % (wins, roi * 100))


class PostMatchReview(BettingWorker):
    def step(self, ctx):
        f = ctx["fixture"]
        probs = ctx["probs"]
        r = random.random()
        result = OUTCOMES[0] if r < probs[0] else (
            OUTCOMES[1] if r < probs[0] + probs[1] else OUTCOMES[2])
        ctx["result"] = result
        if "bet_id" not in ctx:
            predicted = ctx["pick"][0] if "pick" in ctx else "n/a"
            self.remember("Shadow review: predicted %s, simulated result "
                          "%s (no bet placed)." % (predicted, result))
            return ("SIMULATED RESULT, learning only, no money involved: "
                    "%s. Model predicted %s."
                    % (result, predicted))
        if not self.runtime.sim_autonomy:
            # Synthetic results must never move money without the
            # Owner: outside autonomous mode the bet stays open and
            # the Owner settles it (or not) with real information.
            self.remember(
                "Simulated result %s for bet #%d recorded for review "
                "only: settlement is disabled outside autonomous mode."
                % (result, ctx["bet_id"]))
            return ("SIMULATED RESULT (review only): %s. Model predicted "
                    "%s. Autonomous settlement is disabled, so bet #%d "
                    "stays open for the Owner to settle with "
                    "'settle <id> <home|draw|away>'."
                    % (result, ctx["pick"][0], ctx["bet_id"]))
        try:
            won, pnl = self.runtime.book.settle(ctx["bet_id"], result,
                                                "simulated",
                                                actor=self.name)
        except BettingError as e:
            ctx["abort"] = "Settlement failed: %s" % e
            return "Review: %s" % e
        ctx["won"] = won
        correct = ctx["pick"][0] == result
        return ("SIMULATED RESULT: %s. Prediction said %s, so the model "
                "was %s. Bet #%d settled: %s, PnL %+.2f. Bankroll now %s."
                % (result, ctx["pick"][0], "correct" if correct else "wrong",
                   ctx["bet_id"], "WON" if won else "LOST", pnl,
                   money(self.runtime.book.bankroll)))


class Learning(BettingWorker):
    def step(self, ctx):
        note = ("Run #%d: %s vs %s, confidence %s, result %s, bet %s."
                % (ctx["run"], ctx["fixture"]["home"], ctx["fixture"]["away"],
                   ctx.get("confidence", "n/a"), ctx.get("result", "skipped"),
                   ("#%d settled" % ctx["bet_id"]) if "bet_id" in ctx
                   else "not placed"))
        self.remember(note)
        base = ("Calibration log updated. Model agreement on home wins is "
                "holding, draws remain the weak spot; keep the draw "
                "confidence penalty in place.")
        llm = self.runtime.llm
        if llm.configured:
            extra = llm.chat("You are the Learning Agent of a betting "
                             "analytics team. Comment in at most two "
                             "sentences on this run.", note)
            if extra:
                base = base + " LLM analysis: " + extra
        return base


class BettingMemoryAgent(BettingWorker):
    def step(self, ctx):
        summary = ("Run #%d: %s vs %s, pick %s, %s"
                   % (ctx["run"], ctx["fixture"]["home"],
                      ctx["fixture"]["away"],
                      ("%s @ %.2f" % (ctx["pick"][0], ctx["pick"][2]))
                      if "pick" in ctx else "no bet",
                      "won" if ctx.get("won")
                      else ("lost" if "won" in ctx
                            else ("open, awaiting Owner settlement"
                                  if "bet_id" in ctx
                                  else "not placed"))))
        self.remember(summary)
        count = self.runtime.db.memory_count("agent", self.id)
        return ("Archived run summary to betting memory. %d records "
                "available for pattern analysis." % count)


class BettingManager(ManagerAgent):
    def think(self, text):
        t = " %s " % text.lower()
        if any(w in t for w in (" scan", " run", " pipeline", " bet")):
            self.runtime.run_betting_scan()
            return "Scan finished and logged by the full chain."
        return self.report()

    def report(self):
        rt = self.runtime
        book = rt.book
        rows = rt.db.bets_all()
        settled = [r for r in rows if r["status"] != "open"]
        open_bets = [r for r in rows if r["status"] == "open"]
        wins = sum(1 for r in settled if (r["pnl"] or 0) > 0)
        lines = ["Betting Manager operational report:"]
        lines.append("  - Mode: simulation, synthetic data only")
        lines.append("  - Bankroll: %s (initial %s)"
                     % (money(book.bankroll), money(book.initial_bankroll)))
        lines.append("  - Realised PnL: %+.2f" % book.realized_pnl())
        lines.append("  - Open positions: %d" % len(open_bets))
        for p in open_bets:
            lines.append("      * #%d %s, %s @ %.2f, stake %s"
                         % (p["id"], p["match_name"], p["outcome"],
                            p["odds"], money(p["stake"])))
        if settled:
            lines.append("  - Settled bets: %d, win rate %s"
                         % (len(settled), pct(wins / float(len(settled)))))
        lines.append("  - Recommendation: %s"
                     % random.choice(["keep stakes at the 2% cap",
                                      "pause draw bets until calibration "
                                      "improves",
                                      "widen the scanner to two more "
                                      "leagues"]))
        self.send("CEO Agent", "betting report",
                  "delivered %s" % now_str())
        return "\n".join(lines)

    def scan(self):
        rt = self.runtime
        run = int(rt.db.kv_get("scans", "0")) + 1
        rt.db.kv_set("scans", run)
        ctx = {"run": run}
        hdr("BETTING DEPARTMENT, VALUE SCAN #%d (SIMULATION)" % run)
        tid = rt.db.task_create("Value scan #%d" % run, self.name, self.dept)
        chain = rt.dept_agents(self.dept)
        aborted = False
        for agent in chain:
            try:
                out = agent.step(ctx)
            except Exception as e:
                aborted = True
                rt.db.log("Guardian Agent", "pipeline error",
                          "%s: %s" % (agent.name, e), level="error")
                print(wrap("[Guardian] %s failed in this run: %s"
                           % (agent.name, e), indent="  " + C.RED))
                break
            print(wrap("[%s] %s" % (agent.name, out),
                       indent="  " + C.YELLOW))
            if "abort" in ctx:
                aborted = True
                print(wrap("[Pipeline halted] %s" % ctx["abort"],
                           indent="  " + C.RED))
                break
        rt.db.task_set_status(tid, "failed" if aborted else "done",
                              "run #%d" % run)
        book = rt.book
        print(wrap("[Betting Manager] Run #%d archived. Bankroll %s, open "
                   "positions %d."
                   % (run, money(book.bankroll), len(book.open_bets())),
                   indent="  " + C.GREEN))


SPECIAL_CLASSES = {
    ("Executive & Core Management", "CEO Agent"): CEOAgent,
    ("Executive & Core Management", "Developer Agent"): DeveloperAgent,
    ("Executive & Core Management", "Guardian Agent"): GuardianAgent,
    ("Betting Department", "Betting Manager"): BettingManager,
    ("Betting Department", "Data Collector"): DataCollector,
    ("Betting Department", "Data Quality"): DataQuality,
    ("Betting Department", "Market Intelligence"): MarketIntelligence,
    ("Betting Department", "Opportunity Scanner"): OpportunityScanner,
    ("Betting Department", "Prediction Engine"): PredictionEngine,
    ("Betting Department", "Value Finder"): ValueFinder,
    ("Betting Department", "Confidence"): Confidence,
    ("Betting Department", "Risk Manager"): BettingRiskManager,
    ("Betting Department", "Portfolio Manager"): BettingPortfolioManager,
    ("Betting Department", "Simulation"): Simulation,
    ("Betting Department", "Post-Match Review"): PostMatchReview,
    ("Betting Department", "Learning"): Learning,
    ("Betting Department", "Betting Memory"): BettingMemoryAgent,
}


# ---------------------------------------------------------------- the runtime

class Runtime:
    def __init__(self, db_path=None, demo=False):
        self.demo = demo
        if demo:
            path = ":memory:"
        else:
            path = db_path or os.environ.get("NOVYRA_DB") or "novyra.db"
        self.db = Database(path)
        self.llm = LLMClient()
        self.bus = Bus(self.db)
        self.permissions = Permissions(self)
        self.book = BettingBook(self.db)
        if self.db.kv_get("sim_autonomy") is None:
            self.db.kv_set("sim_autonomy", "0")
        self.agents = {}
        self._dept_agents = defaultdict(list)
        self.db.log("System", "boot",
                    "Novyra v%s started (db=%s, llm=%s)"
                    % (VERSION, path,
                       "configured" if self.llm.configured else "off"))

        # core agents always active, never persisted to the roster
        for name, role, kind in BLUEPRINT[0][1]:
            self.instantiate(name, "Executive & Core Management", role,
                             kind, persist=False)
        self.ceo = self.manager_of("Executive & Core Management")
        self.guardian = self.find("guardian-agent")
        if self.db.kv_get("charter_seeded") is None:
            self.db.memory_add("org", "novyra",
                              "Novyra charter: the Owner holds final "
                              "approval over creating and activating "
                              "agents; risk limits are enforced, never "
                              "advisory; every decision is logged.")
            self.db.kv_set("charter_seeded", "1")
        self.replay_persisted()
        # the Guardian watchdog starts from the current end of the log
        row = self.db.conn.execute(
            "SELECT MAX(id) AS m FROM log").fetchone()
        self.db.kv_set("watchdog_cursor", str(row["m"] or 0))

    # ----------------------------------------------------------- lookups

    def unique_id(self, name):
        base = slug(name)
        ident = base
        n = 2
        while ident in self.agents:
            ident = "%s-%d" % (base, n)
            n += 1
        return ident

    def blueprint_size(self):
        return sum(len(members) for _, members in BLUEPRINT) \
            + len(SHARED_SYSTEMS)

    def find(self, query):
        q = query.strip().lower()
        if q in self.agents:
            return self.agents[q]
        exact = [a for a in self.agents.values()
                 if a.name.lower() == q or slug(a.name) == q]
        if exact:
            return exact[0]
        partial = [a for a in self.agents.values() if q in a.name.lower()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            return ("ambiguous", partial)
        return None

    def manager_of(self, dept):
        for a in self._dept_agents[dept]:
            if a.is_manager:
                return a
        return None

    def dept_agents(self, dept):
        return [a for a in self._dept_agents[dept] if not a.is_manager]

    def dept_active(self, dept):
        return self.manager_of(dept) is not None

    def active_departments(self):
        return [d for d in DEPT_ORDER if self.dept_active(d)]

    # -------------------------------------------------- activation & roster

    def instantiate(self, name, dept, role, kind, persist=False):
        cls = SPECIAL_CLASSES.get((dept, name))
        if cls is None:
            cls = ManagerAgent if kind == "manager" else Worker
        agent = cls(name, dept, role, self)
        if (agent.is_manager and isinstance(agent, ManagerAgent)
                and not agent.metrics):
            agent.metrics = self.seed_metrics(dept)
        self.agents[agent.id] = agent
        self._dept_agents[dept].append(agent)
        if persist:
            self._persist_agent(name, dept, role, kind)
        self.db.log("Developer Agent", "instantiated",
                    "%s (%s)" % (name, dept))
        return agent

    def _persist_agent(self, name, dept, role, kind):
        entries = json.loads(self.db.kv_get("agents_json", "[]"))
        entries.append([name, dept, role, kind])
        self.db.kv_set("agents_json", json.dumps(entries))

    def replay_persisted(self):
        """Rebuild the agent roster recorded by previous sessions."""
        for entry in json.loads(self.db.kv_get("agents_json", "[]")):
            try:
                name, dept, role, kind = entry
                self.instantiate(name, dept, role, kind, persist=False)
            except Exception as e:
                self.db.log("System", "replay error", str(e), level="error")

    def seed_metrics(self, dept):
        key = "metrics:%s" % dept
        cached = self.db.kv_get(key)
        if cached:
            try:
                return json.loads(cached)
            except ValueError:
                pass
        metrics = self._generate_metrics(dept)
        self.db.kv_set(key, json.dumps(metrics))
        return metrics

    def _generate_metrics(self, dept):
        def r(a, b):
            return round(random.uniform(a, b), 2)
        if dept == "Ecommerce Department":
            return {"Revenue (30d)": money(r(8000, 15000)),
                    "Orders (30d)": random.randint(120, 260),
                    "Conversion rate": pct(r(0.015, 0.035)),
                    "Average order value": money(r(52, 78))}
        if dept == "Marketing Department":
            return {"Site traffic (30d)": random.randint(9000, 18000),
                    "CAC": money(r(9, 21)),
                    "Blended ROAS": r(1.8, 3.4),
                    "Newsletter subscribers": random.randint(700, 2200)}
        if dept == "Trading Department":
            return {"PnL (30d)": money(r(-900, 2600)),
                    "Open positions": random.randint(2, 7),
                    "Portfolio exposure": money(r(4000, 12000)),
                    "Win rate": pct(r(0.42, 0.58))}
        if dept == "Finance Department":
            return {"Revenue (30d)": money(r(15000, 26000)),
                    "Expenses (30d)": money(r(9000, 14000)),
                    "Cash on hand": money(r(30000, 60000)),
                    "Runway (months)": r(7, 18)}
        return {}

    def developer_build(self, name, dept, role, kind):
        """Instantiate an agent after the caller has obtained approval."""
        dev = self.find("developer-agent")
        agent = self.instantiate(name, dept, role, kind, persist=True)
        if dev:
            dev.remember("Built %s (%s) for %s" % (name, agent.id, dept))
        print(wrap("[Developer] Built %s for the %s." % (name, dept)))
        return agent

    def activate_department(self, dept):
        if dept not in DEPT_ORDER:
            return "Unknown department. Try one of: %s" % ", ".join(DEPT_ORDER)
        if self.dept_active(dept):
            return "The %s is already online." % dept
        if self.permissions.emergency_stop:
            return ("Emergency stop engaged. Activation is blocked until "
                    "you resume.")
        if not self.permissions.request(
                "activate department",
                "bring the %s online (%d agents)"
                % (dept, len(BLUEPRINT_MEMBERS(dept)) - 1)):
            return "Declined. The %s stays planned. No changes made." % dept
        for name, role, kind in BLUEPRINT_MEMBERS(dept):
            if kind == "manager":
                continue
            self.developer_build(name, dept, role, "worker")
        mgr = BLUEPRINT_MEMBERS(dept)[0]
        self.developer_build(mgr[0], dept, mgr[1], "manager")
        self.db.memory_add("org", "novyra",
                           "%s activated by Owner" % dept)
        self.db.decision_add("CEO Agent", "Activate the %s?" % dept,
                             "activated", "Owner approved")
        print(wrap("[CEO] The %s is online. %s reports to me with %d "
                   "specialists."
                   % (dept, mgr[0],
                      len(self.dept_agents(dept)))))
        return None

    def propose(self, query):
        if self.permissions.emergency_stop:
            return "Emergency stop engaged. Use 'resume' first."
        planned = []
        for d, members in BLUEPRINT:
            for n, _, _ in members:
                if not any(a.name == n and a.dept == d
                           for a in self.agents.values()):
                    planned.append((n, d))
        q = query.strip().lower()
        hit = None
        for n, d in planned:
            if n.lower() == q:
                hit = (n, d)
                break
        if hit is None:
            close = difflib.get_close_matches(
                query, [n for n, _ in planned], n=3, cutoff=0.4)
            if close:
                return ("Not planned or already active. Closest planned "
                        "agents: %s. Try 'propose <name>' or 'activate "
                        "<department>'." % ", ".join(close))
            if self.permissions.request("create custom agent",
                                        "'%s' outside the blueprint"
                                        % query.strip()):
                display = query.strip().title()
                self.developer_build(display, "Owner Projects",
                                     "Custom agent defined by the Owner",
                                     "worker")
                return ("[CEO] Custom agent '%s' is now active under "
                        "Owner Projects." % display)
            return "Declined. Nothing was created."
        name, dept = hit
        kind = self._blueprint_kind(dept, name)
        if kind == "manager":
            # A manager heads the department: building it means
            # bringing the whole department online, through the same
            # approval flow as 'activate', never a half-staffed dept.
            print(wrap("[CEO] %s heads the %s, so building it means "
                       "bringing the whole department online."
                       % (name, dept)))
            result = self.activate_department(dept)
            if result:
                return result
            return ("[CEO] The %s is online, led by %s."
                    % (dept, name))
        print(wrap("[CEO] Business case for %s: the %s needs this role "
                   "before it can run at full capability. The Developer "
                   "Agent can build it today." % (name, dept)))
        if not self.permissions.request("build agent",
                                        "%s in the %s" % (name, dept)):
            return "Declined. I will keep it on the planned list."
        role = self._blueprint_role(dept, name)
        self.developer_build(name, dept, role, kind)
        return "[CEO] %s is now active in the %s." % (name, dept)

    def _blueprint_role(self, dept, name):
        for n, role, _ in BLUEPRINT_MEMBERS(dept):
            if n == name:
                return role
        return "Custom agent defined by the Owner"

    def _blueprint_kind(self, dept, name):
        for n, _, k in BLUEPRINT_MEMBERS(dept):
            if n == name:
                return k
        return "worker"

    # ------------------------------------------------------------ pipeline

    @property
    def sim_autonomy(self):
        """True when the Owner has authorised autonomous simulated bet
        placement and settlement. Disabled by default: outside this
        mode the pipeline asks for approval before every bet."""
        return self.db.kv_get("sim_autonomy", "0") == "1"

    def set_autonomy(self, enabled):
        self.db.kv_set("sim_autonomy", "1" if enabled else "0")
        self.db.log("Owner",
                    "autonomy %s" % ("on" if enabled else "off"),
                    "autonomous simulated betting %s"
                    % ("enabled" if enabled else "disabled"),
                    level="warn" if enabled else "info")

    def run_betting_scan(self):
        mgr = self.manager_of("Betting Department")
        if mgr is None:
            print(wrap("The Betting Department is not active yet."))
            return
        if not self.permissions.guard("run betting scan",
                                      "Betting Manager"):
            print(wrap("[Guardian Agent] Emergency stop engaged. The "
                       "pipeline is frozen until the Owner resumes.",
                       indent="  " + C.RED))
            return
        mgr.scan()
        self.db.memory_add("org", "novyra", "Betting scan #%d completed"
                          % int(self.db.kv_get("scans", "0")))

    # -------------------------------------------------------------- owner

    def ask_owner(self, question):
        if self.demo:
            print("  %s %s[y] (demo auto-approval)%s"
                  % (question, C.GREEN, C.RESET))
            return True
        try:
            ans = input("  %s [y/N] " % question).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return ans in ("y", "yes")

    def reset_data(self):
        for table in ("tasks", "bets", "memory", "decisions",
                      "approvals", "log", "messages", "actions"):
            self.db.wipe_table(table)
        self.db.kv_set("bankroll", "%.2f" % INITIAL_BANKROLL)
        self.db.kv_set("bankroll_initial", "%.2f" % INITIAL_BANKROLL)
        self.db.kv_set("scans", "0")
        self.db.kv_set("denied_actions", "0")
        self.db.kv_set("blocked_actions", "0")
        self.db.kv_set("sim_autonomy", "0")
        self.db.log("Owner", "reset",
                    "operational data wiped", level="warn")
        row = self.db.conn.execute(
            "SELECT MAX(id) AS m FROM log").fetchone()
        self.db.kv_set("watchdog_cursor", str(row["m"] or 0))


# --------------------------------------------------------------------- CLI

HELP = """
Commands:
  status                        organisation dashboard
  agents                        full 57-entry blueprint roster
  activate <department>         bring a department online (approval)
  propose <agent name>          CEO business case, then approval, then build
  say <agent> <message>         talk to any active agent directly
  scan                          run the betting value pipeline (simulation)
  report                        CEO daily report
  meeting <department>          department standup
  tasks                         task board
  bets                          bet ledger with PnL
  settle <id> <home|draw|away>  settle an open bet (approval)
  autonomy <on|off>             autonomous simulated betting (approval)
  messages [n]                  recent agent-to-agent messages
  do <agent> <instruction>      agent plans and executes safe actions
  guardian                      deep Guardian integrity scan
  log [n]                       audit log tail (n must be positive)
  remember <fact> / recall <k>  organisation memory
  llm                           LLM status and configuration help
  db                            database location and row counts
  reset                         wipe operational data (approval)
  stop / resume                 emergency stop / resume (both approval)
  demo                          scripted showcase (throwaway database)
  exit                          leave

Anything else you type goes straight to the CEO Agent.
Set NOVYRA_LLM_URL, NOVYRA_LLM_KEY and NOVYRA_LLM_MODEL to give agents
real reasoning; without them Novyra runs in simulation mode.
Autonomous simulated betting is disabled by default: scans ask for
approval before each bet and never settle on their own.
""".strip()


def print_block(agent_name, text, color=C.CYAN):
    print("\n%s%s%s" % (C.BOLD + color, agent_name, C.RESET))
    for line in str(text).splitlines():
        print(wrap(line))


class CLI:
    def __init__(self, runtime):
        self.rt = runtime
        self.table = {
            "help": self.cmd_help, "?": self.cmd_help,
            "status": self.cmd_status,
            "agents": self.cmd_agents, "roster": self.cmd_agents,
            "activate": self.cmd_activate,
            "propose": self.cmd_propose, "hire": self.cmd_propose,
            "say": self.cmd_say, "ask": self.cmd_say,
            "scan": self.cmd_scan,
            "report": self.cmd_report,
            "meeting": self.cmd_meeting, "standup": self.cmd_meeting,
            "tasks": self.cmd_tasks, "task": self.cmd_tasks,
            "bets": self.cmd_bets,
            "settle": self.cmd_settle,
            "autonomy": self.cmd_autonomy,
            "messages": self.cmd_messages, "inbox": self.cmd_messages,
            "do": self.cmd_do,
            "guardian": self.cmd_guardian, "integrity": self.cmd_guardian,
            "log": self.cmd_log, "audit": self.cmd_log,
            "remember": self.cmd_remember,
            "recall": self.cmd_recall,
            "llm": self.cmd_llm,
            "db": self.cmd_db,
            "reset": self.cmd_reset,
            "stop": self.cmd_stop,
            "resume": self.cmd_resume,
            "demo": self.cmd_demo,
            "exit": self.cmd_exit, "quit": self.cmd_exit,
        }

    # ------------------------------------------------------------ commands

    def cmd_help(self, rest):
        print(HELP)

    def cmd_status(self, rest):
        rt = self.rt
        hdr("NOVYRA STATUS  v%s" % VERSION)
        print(wrap("Owner: Jaro  |  Mode: simulation, no real money"))
        print(wrap("Persistence: %s"
                    % ("in-memory (demo, nothing saved)" if rt.demo
                       else rt.db.path)))
        print(wrap("LLM: %s"
                    % ("configured (%s)" % rt.llm.model
                       if rt.llm.configured
                       else "not configured, simulation replies")))
        print(wrap("Autonomous simulated betting: %s"
                    % ("ENABLED (scans place and settle on their own)"
                       if rt.sim_autonomy
                       else "disabled (each simulated bet needs "
                            "approval)")))
        print(wrap("Departments online: %d of %d (%s)"
                   % (len(rt.active_departments()), len(DEPT_ORDER),
                      ", ".join(rt.active_departments()) or "core only")))
        print(wrap("Active agents: %d of %d blueprint entries"
                   % (len(rt.agents), rt.blueprint_size())))
        st = rt.db.task_stats()
        print(wrap("Task board: %s"
                   % (", ".join("%d %s" % (v, k) for k, v in st.items())
                      or "empty")))
        print(wrap("Audit log: %d entries | Decisions: %d | Approvals on "
                   "file: %d"
                   % (rt.db.log_count(), len(rt.db.decisions_all()),
                      rt.db.approvals_count())))
        if rt.dept_active("Betting Department"):
            print(wrap("Bankroll: %s | Open simulated positions: %d "
                       "(exposure %s)"
                       % (money(rt.book.bankroll),
                          len(rt.book.open_bets()),
                          money(rt.book.open_exposure()))))
        if rt.permissions.emergency_stop:
            print(wrap("EMERGENCY STOP ENGAGED. All agents halted. "
                       "'resume' requires approval.", "  " + C.RED))

    def cmd_agents(self, rest):
        rt = self.rt
        hdr("NOVYRA BLUEPRINT ROSTER")
        print("  " + C.BOLD + "OWNER" + C.RESET)
        print("  |")
        for dept, members in BLUEPRINT:
            tag = C.GREEN + "[online]" + C.RESET if rt.dept_active(dept) \
                else C.DIM + "[planned]" + C.RESET
            print("  +- %s %s" % (C.BOLD + dept + C.RESET, tag))
            for name, role, kind in members:
                on = any(a.name == name and a.dept == dept
                         for a in rt.agents.values())
                mark = C.GREEN + "[active]" + C.RESET if on \
                    else C.DIM + "[planned]" + C.RESET
                print("  |    +- %s %s" % (name, mark))
        print("  |")
        print("  +- %s %s" % (C.BOLD + "Shared Systems" + C.RESET,
                             C.GREEN + "[built-in]" + C.RESET))
        for name, role in SHARED_SYSTEMS:
            print("  |    +- %s %s" % (name,
                                       C.GREEN + "[active]" + C.RESET))
        print("  |")
        print("  " + C.DIM + "Core agents (CEO, Developer, Guardian) boot "
              "with the system." + C.RESET)
        print("  " + C.DIM + "Duplicate names across departments get "
              "unique agent ids." + C.RESET)

    def cmd_activate(self, rest):
        rt = self.rt
        if not rest:
            print("Departments: %s" % ", ".join(DEPT_ORDER))
            return
        target = rest.strip()
        for dept in DEPT_ORDER:
            if target.lower() in dept.lower():
                result = rt.activate_department(dept)
                if result:
                    print(wrap(result))
                return
        hit = rt.find(target)
        if isinstance(hit, Agent):
            print(wrap("%s is already active. Use 'say %s <message>' "
                       "to talk to it." % (hit.name, hit.id)))
            return
        if isinstance(hit, tuple):
            print(wrap("Ambiguous, be more specific: %s"
                       % ", ".join(a.name for a in hit[1])))
            return
        print(wrap("'%s' is not active. Use 'propose %s' to build it with "
                   "approval, or see 'agents' for the roster."
                   % (target, target)))

    def cmd_propose(self, rest):
        if not rest:
            print(wrap("Use: propose <agent name>  (see 'agents' for the "
                       "roster)"))
            return
        result = self.rt.propose(rest)
        if result:
            print(wrap(result))

    def cmd_say(self, rest):
        rt = self.rt
        if not rest:
            print(wrap("Use: say <agent> <message>"))
            return
        parts = rest.split(None, 1)
        query, text = parts[0], (parts[1] if len(parts) > 1 else "hello")
        hit = rt.find(query)
        if hit is None:
            print(wrap("No active agent matches '%s'. Use 'agents' to list "
                       "them. If it is still planned, use 'activate "
                       "<department>' or 'propose %s'." % (query, query)))
            return
        if isinstance(hit, tuple):
            print(wrap("Ambiguous, be more specific: %s"
                       % ", ".join(a.name for a in hit[1])))
            return
        if rt.permissions.emergency_stop and not hit.is_manager:
            print_block(hit.name, "Emergency stop engaged. I am halted "
                        "until the Owner resumes operations.", C.RED)
            return
        try:
            reply = hit.think(text)
        except NovyraError as e:
            print_block(hit.name, "I hit a problem: %s" % e, C.RED)
            return
        print_block(hit.name, reply)

    def cmd_scan(self, rest):
        self.rt.run_betting_scan()

    def cmd_report(self, rest):
        print_block("CEO Agent", self.rt.ceo.daily_report())

    def cmd_meeting(self, rest):
        rt = self.rt
        target = rest.strip().lower()
        dept = None
        for d in DEPT_ORDER:
            if target and target in d.lower():
                dept = d
                break
        if dept is None:
            if rt.active_departments():
                dept = rt.active_departments()[-1]
            else:
                print(wrap("No departments are active yet. Use 'activate "
                           "<department>' first."))
                return
        if not rt.dept_active(dept):
            print(wrap("The %s is still planned." % dept))
            return
        if not rt.permissions.guard("hold meeting",
                                    rt.manager_of(dept).name):
            print(wrap("[Guardian Agent] Emergency stop engaged. Meetings "
                       "are frozen until the Owner resumes.",
                       "  " + C.RED))
            return
        hdr("%s STANDUP" % dept.upper())
        for a in rt._dept_agents[dept]:
            print(wrap("%s: %s" % (a.name, a.standup())))
        rt.db.decision_add(rt.manager_of(dept).name,
                           "hold department standup?", "held",
                           "routine coordination")

    def cmd_tasks(self, rest):
        rt = self.rt
        hdr("TASK BOARD")
        rows = rt.db.tasks_all()
        if not rows:
            print(wrap("No tasks yet. Agents create tasks as they work."))
            return
        for t in rows:
            color = C.GREEN if t["status"] == "done" else (
                C.RED if t["status"] == "failed" else C.YELLOW)
            print("  %s%-6s%s %-7s %s[%s]%s %s  (%s)"
                  % (C.DIM, "T%03d" % t["tid"], C.RESET, t["priority"],
                     color, t["status"], C.RESET, t["title"], t["owner"]))

    def cmd_bets(self, rest):
        rt = self.rt
        hdr("BET LEDGER (SIMULATION)")
        rows = rt.db.bets_all()
        if not rows:
            print(wrap("No bets recorded yet. Run 'scan' after activating "
                       "the Betting Department."))
            return
        for r in rows:
            pnl = ("%+.2f" % r["pnl"]) if r["pnl"] is not None else ""
            print("  #%-3d %-26s %-9s stake %8s @ %.2f  %-8s %s"
                  % (r["id"], r["match_name"][:26], r["outcome"],
                     money(r["stake"]), r["odds"], r["status"], pnl))
        settled = [r for r in rows if r["status"] != "open"]
        open_bets = [r for r in rows if r["status"] == "open"]
        wins = sum(1 for r in settled if (r["pnl"] or 0) > 0)
        print(wrap("Bankroll %s, realised PnL %+.2f, %d settled (%d won), "
                   "%d open."
                   % (money(rt.book.bankroll), rt.book.realized_pnl(),
                      len(settled), wins, len(open_bets))))

    def cmd_settle(self, rest):
        rt = self.rt
        parts = rest.split()
        if len(parts) != 2:
            print(wrap("Usage: settle <bet id> <home|draw|away>  "
                       "(see 'bets' for open ids)"))
            return
        if not parts[0].isdigit():
            print(wrap("Bet id must be a number, e.g. settle 3 draw"))
            return
        bet_id = int(parts[0])
        result = {"home": "home win", "draw": "draw",
                  "away": "away win"}.get(parts[1].lower())
        if result is None:
            print(wrap("Result must be home, draw or away."))
            return
        if not rt.permissions.guard("settle bet", "Owner"):
            print(wrap("Emergency stop engaged. Settlement is frozen until "
                       "you resume.", "  " + C.RED))
            return
        if not rt.permissions.request("settle bet",
                                      "mark bet #%d as %s"
                                      % (bet_id, result)):
            print(wrap("Declined. The bet stays open."))
            return
        try:
            won, pnl = rt.book.settle(bet_id, result, "owner", actor="Owner")
        except BettingError as e:
            print(wrap("Settlement failed: %s" % e))
            rt.db.log("CLI", "settle failed",
                      "bet #%d: %s" % (bet_id, e), level="error")
            return
        print(wrap("Bet #%d settled as %s: %s, PnL %+.2f. Bankroll now %s."
                   % (bet_id, result, "WON" if won else "LOST", pnl,
                      money(rt.book.bankroll))))

    def cmd_autonomy(self, rest):
        rt = self.rt
        arg = rest.strip().lower()
        if arg not in ("on", "off"):
            state = "ENABLED" if rt.sim_autonomy else "disabled"
            print(wrap("Autonomous simulated betting is %s. Use "
                       "'autonomy on' (needs approval) or 'autonomy "
                       "off'. When disabled, scans ask for approval "
                       "before each bet and never settle automatically."
                       % state))
            return
        if arg == "off":
            rt.set_autonomy(False)
            print(wrap("[Guardian Agent] Autonomous simulated betting "
                       "is now disabled. Scans will ask for approval "
                       "before each bet.", "  " + C.GREEN))
            return
        if rt.permissions.emergency_stop:
            print(wrap("Emergency stop engaged. Resolve it with "
                       "'resume' first.", "  " + C.RED))
            return
        if rt.permissions.request("enable autonomous simulated betting",
                                  "scans will place and settle simulated "
                                  "bets without further per-bet "
                                  "approval"):
            rt.set_autonomy(True)
            print(wrap("[Guardian Agent] Autonomous simulated betting "
                       "is ENABLED. Scans now place and settle simulated "
                       "bets on their own.", "  " + C.RED))
        else:
            print(wrap("Declined. Autonomous betting stays disabled."))

    def cmd_messages(self, rest):
        rest = rest.strip()
        n = 10
        if rest:
            if not rest.isdigit() or int(rest) <= 0:
                print(wrap("Usage: messages [n] where n is a positive "
                           "number, e.g. messages 25"))
                return
            n = min(int(rest), 200)
        hdr("COMMUNICATION HISTORY (last %d)" % n)
        rows = self.rt.db.messages_tail(n)
        if not rows:
            print(wrap("No messages yet. Agents message each other as "
                       "they work; everything is stored here "
                       "persistently."))
            return
        for m in rows:
            print("  %s  %s -> %s: %s"
                  % (m["ts"][11:19], m["sender"], m["recipient"],
                     m["subject"]))
            if m["body"]:
                print(wrap(m["body"], "      "))

    def cmd_do(self, rest):
        rt = self.rt
        parts = rest.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            print(wrap("Use: do <agent> <instruction>  (the agent plans "
                       "and executes safe actions: tasks, memories, "
                       "messages)"))
            return
        hit = rt.find(parts[0])
        if hit is None:
            print(wrap("No active agent matches '%s'. Use 'agents' to "
                       "list them." % parts[0]))
            return
        if isinstance(hit, tuple):
            print(wrap("Ambiguous, be more specific: %s"
                       % ", ".join(a.name for a in hit[1])))
            return
        if rt.permissions.emergency_stop and not hit.is_manager:
            print_block(hit.name, "Emergency stop engaged. I am halted "
                        "until the Owner resumes operations.", C.RED)
            return
        reply = hit.act(parts[1].strip())
        print_block(hit.name, reply)

    def cmd_guardian(self, rest):
        rt = self.rt
        hdr("GUARDIAN INTEGRITY SCAN")
        findings = rt.guardian.full_scan()
        allowed = rt.db.actions_count(allowed=True)
        refused = rt.db.actions_count(allowed=False)
        print(wrap("Watched actions on file: %d executed, %d refused."
                   % (allowed, refused)))
        if not findings:
            print(wrap("[Guardian Agent] Full scan complete. Database "
                       "integrity ok, the bankroll ledger invariant "
                       "holds, no suspicious patterns. All systems "
                       "nominal.", "  " + C.GREEN))
            return
        for f in findings:
            print(wrap("! " + f, "  " + C.RED))

    def cmd_log(self, rest):
        rest = rest.strip()
        n = 10
        if rest:
            if not rest.isdigit() or int(rest) <= 0:
                print(wrap("Usage: log [n] where n is a positive number, "
                           "e.g. log 25"))
                return
            n = min(int(rest), 500)
        hdr("AUDIT LOG (last %d)" % n)
        for row in self.rt.db.log_tail(n):
            print("  %s  %s%-7s%s %-16s %-10s %s"
                  % (row["ts"][11:19],
                     C.RED if row["level"] == "error" else "",
                     row["level"], C.RESET, row["actor"], row["event"],
                     row["detail"]))

    def cmd_remember(self, rest):
        if not rest:
            print(wrap("Use: remember <fact>"))
            return
        self.rt.db.memory_add("org", "novyra", rest)
        print(wrap("[Organisation Memory] Stored persistently. Every agent "
                   "can recall it when relevant."))

    def cmd_recall(self, rest):
        if not rest:
            print(wrap("Use: recall <keyword>"))
            return
        hits = self.rt.db.memory_search("org", "novyra", rest)
        if not hits:
            print(wrap("[Organisation Memory] Nothing matches '%s'." % rest))
            return
        for ts, fact in hits:
            print(wrap("- [%s] %s" % (ts[:16], fact)))

    def cmd_llm(self, rest):
        rt = self.rt
        hdr("LLM INTEGRATION")
        client = rt.llm
        if not client.configured:
            print(wrap("Status: not configured. Agents run in simulation "
                       "mode with deterministic replies."))
            print(wrap("To give every agent real reasoning, set these "
                       "before starting Novyra:"))
            print(wrap("  export NOVYRA_LLM_URL=https://api.mistral.ai/v1/"
                       "chat/completions"))
            print(wrap("  export NOVYRA_LLM_KEY=<your api key>"))
            print(wrap("  export NOVYRA_LLM_MODEL=mistral-small-latest"))
            print(wrap("Any OpenAI-compatible endpoint works, including "
                       "local Ollama or LM Studio."))
            return
        print(wrap("Status: configured. Endpoint: %s, model: %s."
                   % (client.url, client.model)))
        print(wrap("Running a live ping..."))
        reply = client.chat("You are a system health check for Novyra.",
                            "Reply with the single word OK.")
        if reply:
            print(wrap("LLM answered: %s" % reply[:200]))
        else:
            print(wrap("LLM call failed: %s. Agents will fall back to "
                       "simulation." % client.last_error))

    def cmd_db(self, rest):
        rt = self.rt
        hdr("DATABASE")
        print(wrap("Path: %s" % rt.db.path))
        roster = json.loads(rt.db.kv_get("agents_json", "[]"))
        print(wrap("Agents: %d active in this session, %d on the persisted "
                   "roster" % (len(rt.agents), len(roster))))
        print(wrap("Rows: %d tasks, %d bets, %d memory notes, %d "
                   "decisions, %d approvals, %d messages, %d watched "
                   "actions, %d log entries"
                   % (len(rt.db.tasks_all()), len(rt.db.bets_all()),
                      rt.db.memory_count_all(),
                      len(rt.db.decisions_all()),
                      rt.db.approvals_count(), rt.db.messages_count(),
                      rt.db.actions_count(), rt.db.log_count())))

    def cmd_reset(self, rest):
        rt = self.rt
        if not rt.permissions.request(
                "reset operational data",
                "wipe tasks, bets, memories, decisions, approvals, "
                "messages, actions and logs; the agent roster stays"):
            print(wrap("Declined. Nothing was deleted."))
            return
        rt.reset_data()
        print(wrap("[Guardian Agent] Operational data wiped. Bankroll "
                   "reset to %s. The agent roster stays as it was."
                   % money(rt.book.bankroll)))

    def cmd_stop(self, rest):
        rt = self.rt
        if rt.permissions.emergency_stop:
            print(wrap("Emergency stop is already engaged."))
            return
        if rt.permissions.request("emergency stop",
                                  "halt every agent and freeze all "
                                  "operations"):
            rt.permissions.halt()
            print(wrap("[Guardian Agent] Emergency stop engaged. All agents "
                       "frozen. 'resume' (with approval) restarts them.",
                       "  " + C.RED))
        else:
            print(wrap("Cancelled. Agents keep running."))

    def cmd_resume(self, rest):
        rt = self.rt
        if not rt.permissions.emergency_stop:
            print(wrap("Systems are already running."))
            return
        if rt.permissions.request("resume operations",
                                  "bring all agents back online"):
            rt.permissions.do_resume()
            print(wrap("[Guardian Agent] Systems resumed. All agents back "
                       "online.", "  " + C.GREEN))
        else:
            print(wrap("Declined. Emergency stop stays engaged."))

    def cmd_demo(self, rest):
        self.run_demo()

    def cmd_exit(self, rest):
        print(wrap("[CEO] Logging off. All state is saved to the database. "
                   "See you next session."))
        raise SystemExit(0)

    # ------------------------------------------------------------- dispatch

    def watchdog(self):
        """Guardian light patrol after every command: count new
        warning/error events since the last patrol and flag them."""
        rt = self.rt
        cursor = int(rt.db.kv_get("watchdog_cursor", "0"))
        row = rt.db.conn.execute(
            "SELECT COUNT(*) AS n, MAX(id) AS m FROM log "
            "WHERE id > ? AND level IN ('warn', 'error')",
            (cursor,)).fetchone()
        if row["m"]:
            rt.db.kv_set("watchdog_cursor", str(row["m"]))
        if row["n"]:
            print(wrap("[Guardian Agent] %d new warning/error event(s) "
                       "since my last patrol. Run 'guardian' for a full "
                       "integrity scan." % row["n"], "  " + C.RED))

    def dispatch(self, line):
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        handler = self.table.get(cmd)
        try:
            if handler:
                handler(rest)
            else:
                reply = self.rt.ceo.think(line)
                print_block("CEO Agent", reply)
        except SystemExit:
            raise
        except NovyraError as e:
            print(wrap("Error: %s" % e, "  " + C.RED))
            self.rt.db.log("CLI", "error", "%s: %s" % (cmd, e),
                           level="error")
        except Exception as e:
            print(wrap("Unexpected error in '%s': %s (%s). Details are in "
                       "the audit log, the session keeps running."
                       % (cmd, e, type(e).__name__), "  " + C.RED))
            try:
                self.rt.db.log("CLI", "crash",
                               "%s: %s: %s" % (cmd, type(e).__name__, e),
                               level="error")
            except Exception:
                pass
        try:
            self.watchdog()
        except Exception:
            pass

    # ---------------------------------------------------------------- repl

    def banner(self):
        print(C.BOLD + C.MAGENTA + r"""
    _   _                 ______
    | \ | | _____   ____ _/ ____| __ _ _   _   _  __
    |  \| |/ _ \ \ / / _` \___ \ / _` | | | \ \ / / /
    | |\  | (_) \ V / (_| |___) | (_| | |_| |\ V / /
    |_| \_|\___/ \_/ \__,_|____/ \__,_|\__, | \_/ /
                                        |___/     /_/  v%s
""" % VERSION + C.RESET)
        print(wrap("A multi-agent company operating system. You are the "
                   "Owner. The CEO Agent coordinates %d blueprint entries "
                   "across 6 departments plus shared systems."
                   % self.rt.blueprint_size()))
        print(wrap("Type 'help' for commands, 'demo' for a guided tour, or "
                   "just talk to the CEO."))
        print(wrap("Mode: simulation with synthetic data. No real money "
                   "moves and no external betting services are called.",
                   "  " + C.DIM))
        print(wrap("Autonomous simulated betting: %s."
                   % ("ENABLED (scans act on their own)"
                      if self.rt.sim_autonomy
                      else "disabled (each simulated bet needs your "
                           "approval)"),
                   "  " + C.DIM))
        if self.rt.demo:
            print(wrap("DEMO MODE: approvals are auto-granted and the "
                       "database is a throwaway in-memory copy. Nothing "
                       "you do here is saved.", "  " + C.RED))
        if self.rt.permissions.emergency_stop:
            print(wrap("EMERGENCY STOP IS ENGAGED from a previous "
                       "session. All agents are halted until you run "
                       "'resume' with approval.", "  " + C.RED))

    def repl(self):
        self.banner()
        while True:
            try:
                line = input("%snovyra>%s " % (C.BOLD + C.GREEN,
                                              C.RESET)).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                self.cmd_exit("")
            if not line:
                continue
            self.dispatch(line)

    def run_demo(self):
        self.banner()
        script = [
            "status",
            "agents",
            "activate Betting Department",
            "autonomy on",
            "scan",
            "scan",
            "bets",
            "report",
            "meeting Betting Department",
            "do Data Collector review this week's fixtures",
            "propose Ecommerce Manager",
            "guardian",
            "messages",
            "log 20",
        ]
        for cmd in script:
            print("\n%s%snovyra> %s%s" % (C.DIM, C.BOLD, cmd, C.RESET))
            self.dispatch(cmd)
        print(wrap("[CEO] That concludes the demo. In interactive mode you "
                   "approve actions yourself, and everything persists to "
                   "the database. Type 'help' for everything.",
                   "  " + C.DIM))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    C.off()
    demo = "--demo" in argv
    rt = Runtime(demo=demo)
    cli = CLI(rt)
    try:
        if demo:
            cli.run_demo()
        else:
            cli.repl()
    except SystemExit:
        pass
    finally:
        rt.db.close()


if __name__ == "__main__":
    main()