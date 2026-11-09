Release date: 9 November 2026. Files: novyra.py (the Novyra canvas) and
test_novyra.py (the Novyra Test Suite canvas).

0.2.1 status: the Owner ran the 0.2.1 suite locally and all 58 tests
passed. The run surfaced three issues that 0.2.2 fixes below.
- The bug: with a bankroll like 100.35, the raw 2% cap is 2.007.
The Risk Manager rounded the proposed stake to 2.01 (round-half on
the float), and place() then rejected 2.01 > 2.007 even though the
error message displayed both values as "2.01" — a stake that
appeared to sit exactly at the cap was refused.
- The fix: all monetary limits are now compared in integer
cents through decimal.Decimal (to_cents/from_cents
helpers); no limit is ever evaluated in binary floating point.
  - Stakes are canonicalised to cents with round-half-up, so the
displayed stake, the stored stake and the limit check always
agree.
  - The cap truncates fractional cents (int(0.02 x bankroll_cents)), so it can never be rounded up into a stake
the book itself would refuse.
  - Kelly stakes floor to cents before the cap is applied, and the
Risk Manager proposes min(kelly_cents, cap_cents), so the
pipeline can never propose an unplaceable stake.
  - Win credits round half-up in exact cents
(Decimal(stake) x Decimal(odds) x 100), and PnL is derived
from the same credit the bankroll receives.
- Boundary behaviour (now tested): a stake exactly at the cap is
accepted; one cent below is accepted; one cent above is rejected
with nothing written. Verified at both a clean cap (bankroll
100.00, cap 2.00) and a fractional one (bankroll 100.35, raw cap
2.007), and at the default 10000.00 bankroll (cap 200.00).
- Database.close() is idempotent (tracks a closed flag) and the
object now works as a context manager.
- A failed database open (bad path, locked file) closes the
partially opened connection before raising NovyraError, so no
connection leaks on startup errors.
- New Runtime.close() lifecycle: idempotent, audits the shutdown
in the log, releases the connection; Runtime also works as a
context manager. The CLI now shuts down through Runtime.close()
in main()'s finally block.
- The test suite closes everything it opens: make_runtime()
registers every runtime and a shared teardown closes them all;
runtimes created directly register addCleanup. The suite can be
run with python3 -W error::ResourceWarning test_novyra.py to
prove no resource warnings remain.
Previously only successful placements, settlements and agent-plan
actions reached the actions table. Now every sensitive action
attempt is recorded with its outcome:
- Owner approvals and denials (Permissions.request): the
Owner's decision on every approval-gated command (department
activation, agent builds, settlements, autonomy, stop, resume,
reset) is written with allowed = 1/0.
- Emergency-stop blocks (Permissions.guard): the blocked agent and
the gated action are recorded with allowed = 0.
- Emergency stop and resume themselves (halt/do_resume).
- Autonomy changes (set_autonomy, both on and off).
- Refused bet placements (below minimum, over cap, insufficient
bankroll) and refused settlements (unknown bet, already settled):
the refusal is recorded after the transaction rolls back, so a
failed money operation can never silently vanish.
- Data resets: the wipe clears the watch by design, and the reset
itself is written as the single remaining watched action.
All 57 blueprint entries, all commands, the simulation data and
models, the approval flow and SQLite persistence behaviour are
unchanged. Existing 0.2.1 databases open as-is; no migration step.
- Database.transact() is a new transaction context manager: commit on
success, roll back everything on any failure.
- Bet placement (BettingBook.place) now writes the bankroll
deduction and the bet row inside one transaction. Previously the two
writes committed separately, so a crash or error in between could
deduct a stake with no bet recorded (money leak), or in the worst
case record a bet without the deduction.
- Bet settlement (BettingBook.settle) re-checks that the bet is
still open inside the same transaction that pays out, and the payout
write is guarded by UPDATE ... WHERE status = 'open'. Previously
two racing settlements could both credit the bankroll; now the
second is rejected and nothing is paid twice.
- Helper methods (kv_set, bet_add, bet_settle, message_add,
action_add) gained a commit parameter so they can participate in
a transaction; the default behaviour is unchanged for callers.
- New actions table records every sensitive action attempt with its
outcome (allowed = 1/0): bet placements, settlements, executed and
refused agent actions.
- GuardianAgent.full_scan() is a new deep integrity scan covering:
database integrity (PRAGMA integrity_check), the bankroll ledger
invariant (bankroll = initial + realised PnL - open exposure,
tolerance one cent), settled bets missing a result or PnL (data
corruption), open exposure concentration above 10% of bankroll,
suspicious approval patterns (more denials than grants),
emergency-stop block counts, error-log counts, roster readability
and the autonomy flag. The scan itself is audited.
- GuardianAgent.monitor() (the cheap patrol used in reports) now also
reports blocked actions and the autonomy state.
- A CLI watchdog runs after every command: it counts new
warn/error audit events since its last patrol and points to the
guardian command. The cursor persists across restarts.
- New command: guardian (alias integrity).
- Autonomous simulated bet placement and settlement are now disabled
by default. Scans ask for Owner approval before placing each bet
(place simulated bet request) and never settle with synthetic
results; bets stay open for the Owner to settle with
settle <id> <home|draw|away> (approval-gated, as before).
- New command autonomy on|off: enabling autonomous mode requires
Owner approval; disabling it is always allowed. The state persists
and is shown in status, the banner and Guardian reports.
- Emergency-stop blocks are now counted separately
(blocked_actions) from Owner denials (denied_actions), so
neither hides the other.
- Proposing a department manager (e.g. propose Ecommerce Manager)
now routes through the full department activation approval instead
of silently creating a half-staffed "online" department.
- reset no longer clears the emergency stop; a wipe cannot silently
re-enable halted agents.
- Trust boundary (documented, unchanged): the Python API
(book.place, developer_build) trusts its caller; approval
enforcement lives in the CLI and pipeline layers. No code path in
the app calls these without the appropriate approval.
- The LLM action whitelist (below) deliberately contains no money,
approval, roster or filesystem actions, so an LLM can never spend
funds or escalate privileges.
- New messages table; Bus.send persists sender, recipient, subject,
body and timestamp. Communication history survives restarts.
- New command: messages [n] (alias inbox), with positive-integer
validation like log.
- reset wipes messages along with the other operational data.
- New Agent.act(instruction): the agent plans whitelisted actions and
the runtime executes them deterministically.
- With an LLM configured, the agent must reply with a JSON array of at
most 4 actions from the whitelist: create_task, remember,
message, search_memory, none. Any unknown action, malformed
JSON, non-array or oversized plan invalidates the whole plan and
falls back to the deterministic heuristic (which also executes real
work: it logs tasks and messages). Refused individual actions are
recorded with allowed = 0.
- Field validation: titles max 100 chars, facts max 200, bodies max
500, message recipients must be active agents or the Owner, unknown
recipients are refused and audited.
- New command: do <agent> <instruction>. Emergency stop blocks worker
agents from acting, consistent with say.
- Stakes are rounded to cents on placement; a bet can no longer store
a sub-cent stake that the bankroll (stored as a cents string) cannot
represent, which previously allowed the ledger invariant to drift.
- Win PnL is now derived from the payout itself:
credit = round(stake x odds, 2), pnl = credit - stake, so the
invariant bankroll = initial + realised PnL - open exposure holds
exactly by construction. (0.2.0 computed PnL and the credit
independently, which could disagree by a cent at awkward odds.)
- Cap (2% of bankroll) and minimum stake (1.00) are now re-checked
inside the placement transaction against the live bankroll.
- New CHECK constraints on new databases: status IN ('open', 'settled'), odds > 1.0, 0 < prob < 1, stake >= 1.0, and
allowed IN (0, 1) on actions. Existing 0.2.0 databases keep their
original schema (SQLite applies checks only to newly created tables).
- LLMClient only treats http:///https:// URLs as configured and
records a clear last_error otherwise (the llm command shows it).
- Agents still never claim to execute actions in plain say replies;
execution now happens only through the validated act layer.
- Bug found in the 0.2.0 test suite: test_agent_falls_back_without_llm
expected a task id in the Developer Agent's fallback reply, but
DeveloperAgent.heuristic never created a task, so that test would
have failed. The heuristic now logs the request on the task board
(and the test passes as written).
- cmd_meeting recorded the wrong actor ("Betting Manager") for every
department's guard check; it now names the actual department manager.
- cmd_settle failures are now written to the audit log.
- The db command now also shows message and action counts.
- Demo script updated: it now enables autonomy (auto-approved in
demo), demonstrates do, guardian, messages and a longer log.
- All 57 blueprint entries, all existing commands, the simulation
data and models, and SQLite persistence behaviour are unchanged.
- Betting data remains synthetic and SIMULATED-labeled; there is no
real-money mode, no odds API integration, and no autonomous real
financial action anywhere.
Honest status for 0.2.2: the Python suite (76 tests) could not be
executed in the environment where this release was produced (no
Python runtime is available there). It was statically verified in
full and the changed money logic was executed as a 1:1 TypeScript
port (27 of 27 checks pass, including a 20000-operation ledger
fuzz with 9285 placements and 7333 settlements: the invariant held
exactly in cents and every risk-proposed stake was placeable).
Run it locally and report failures: python3 test_novyra.py. Do
not treat the Python run as passed until you see it pass on your
machine. For reference, the 0.2.1 suite (58 tests) passed fully on
the Owner's machine, which is how the three 0.2.2 issues surfaced.

What was actually verified for 0.2.2, and how:
1. Structure checks (executed): both files pass a bracket/
 string/indentation-aware scan: all parentheses, brackets and
 braces balance; all string literals (including triple-quoted
 docstrings) are closed; no tabs.
2. Symbol cross-check (executed): every module symbol and every
 method the tests reference exists in the source, including the
 new to_cents/from_cents, close, __enter__/__exit__,
 _closed/closed flags and all new test helpers.
3. Algorithm execution (executed as a TypeScript port): the new
 cents logic was ported 1:1 (Decimal semantics reproduced with
 exact BigInt scaled arithmetic): stake acceptance at exact cap,
 one cent below and one cent above (clean and fractional caps),
 cap truncation, Kelly flooring, half-up win credits, the kv
 money round-trip, and the old 0.2.1 float logic was confirmed to
 reproduce the reported bug (bankroll 100.35: old code proposed
 2.01 and rejected it; new code proposes and accepts 2.00).
 27 of 27 checks pass.
4. Line-by-line review: every edited region was re-read in the
 final file, and all 58 existing tests were re-checked against
 the new behaviour (see regression notes below).
- All money-adjacent assertions (deduction, settlement, invariant)
were recomputed under the cents logic; values are unchanged for
the tested inputs (50.00 @ 2.50 etc. are exact in both schemes).
- One existing assertion was updated on purpose:
test_reset_wipes_messages_and_actions expected 0 watched actions
after a reset; the reset is itself now audited, so exactly 1
action ("reset data") remains. The test verifies this.
- test_place_validation now also causes refused-action rows to be
written (by design); its assertions (nothing placed, nothing
deducted) still hold.
- The atomicity tests still exercise rollback: their simulated
failures raise NovyraError, not BettingError, so the new
refusal auditing does not fire mid-transaction and rollback
behaviour is unchanged.
Test inventory (76 cases): maths (4), database (5), betting book
(8), permissions (4), persistence/restart (2), pipeline (3),
roster/bus (2), LLM fallback (4), CLI validation (3), atomicity (4),
autonomy gating (4), message persistence (3), guardian
integrity/watchdog (8), agent action execution (5), stake limits
(6), resource cleanup (6), audit logging (6). Bold classes
are new in 0.2.2.
Requirements: Python 3.8 or newer. No third-party packages; the app
uses only the standard library (sqlite3, urllib, decimal).
1. Save the Novyra canvas as novyra.py and the Novyra Test Suite
 canvas as test_novyra.py, in the same folder.
2. Run the tests first:
   ```
    python3 test_novyra.py
   ```
    Expect Ran 76 tests with 0 failures and 0 errors (one test
 self-skips if NOVYRA_LLM_URL is set in your environment).
 To confirm no resource warnings remain:
3. Start the system:
   ```
    python3 novyra.py            # interactive, persists to novyra.db
    python3 novyra.py --demo     # throwaway demo, nothing is saved
    NOVYRA_DB=my.db python3 novyra.py   # custom database location
   ```
4. Optional LLM integration (any OpenAI-compatible endpoint):
   ```
    export NOVYRA_LLM_URL=https://api.mistral.ai/v1/chat/completions
    export NOVYRA_LLM_KEY=your-api-key
    export NOVYRA_LLM_MODEL=mistral-small-latest
   ```
    Verify with the llm command. Without these, Novyra runs with
 deterministic simulation replies.
5. Suggested first session: help, then demo, then activate  Betting Department, scan (approve the simulated bet yourself),
 bets, guardian, messages, settle <id> home.
- Just replace both files; 0.2.1 databases open unchanged. No schema
change, no data migration.
- Behaviour changes to be aware of: half-cent stakes now round
half-up (a stake of 0.995 registers as 1.00 and passes the
minimum), fractional caps truncate (bankroll 100.35 allows at most
2.00, not 2.01), and refused money operations and Owner decisions
now appear in the actions table (the guardian command shows the
counts).
- Open your existing database as usual; the messages and actions
tables are created automatically. No data is lost.
- Autonomous placement/settlement resets to off for everyone:
scans now ask per-bet approval. Use autonomy on (approval) to get
the old behaviour back.
- Emergency stop state is no longer cleared by reset.
- Real betting data feeds and odds APIs (current data is synthetic).
- Model calibration against real historical results.
- Real-money mode with a hardened, audited opt-in (intentionally
absent).
- Production LLM key management (env vars are fine for personal use,
not for shared machines), scheduling/background runs, REST API and
multi-user access.