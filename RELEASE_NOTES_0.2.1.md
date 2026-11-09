# Novyra 0.2.1 Release Notes

Release date: 7 November 2026. Files: `novyra.py` (the Novyra canvas) and\
`test_novyra.py` (the Novyra Test Suite canvas).

## Changelog: every important modification

### 1. Atomic database operations (money safety)

- `Database.transact()` is a new transaction context manager: commit on\
  success, roll back everything on any failure.
- **Bet placement** (`BettingBook.place`) now writes the bankroll\
  deduction and the bet row inside one transaction. Previously the two\
  writes committed separately, so a crash or error in between could\
  deduct a stake with no bet recorded (money leak), or in the worst\
  case record a bet without the deduction.
- **Bet settlement** (`BettingBook.settle`) re-checks that the bet is\
  still open inside the same transaction that pays out, and the payout\
  write is guarded by `UPDATE ... WHERE status = 'open'`. Previously\
  two racing settlements could both credit the bankroll; now the\
  second is rejected and nothing is paid twice.
- Helper methods (`kv_set`, `bet_add`, `bet_settle`, `message_add`,\
  `action_add`) gained a `commit` parameter so they can participate in\
  a transaction; the default behaviour is unchanged for callers.

### 2. Guardian Agent: active monitoring

- New `actions` table records every sensitive action attempt with its\
  outcome (`allowed` = 1/0): bet placements, settlements, executed and\
  refused agent actions.
- `GuardianAgent.full_scan()` is a new deep integrity scan covering:\
  database integrity (`PRAGMA integrity_check`), the bankroll ledger\
  invariant (`bankroll = initial + realised PnL - open exposure`,\
  tolerance one cent), settled bets missing a result or PnL (data\
  corruption), open exposure concentration above 10% of bankroll,\
  suspicious approval patterns (more denials than grants),\
  emergency-stop block counts, error-log counts, roster readability\
  and the autonomy flag. The scan itself is audited.
- `GuardianAgent.monitor()` (the cheap patrol used in reports) now also\
  reports blocked actions and the autonomy state.
- A **CLI watchdog** runs after every command: it counts new\
  warn/error audit events since its last patrol and points to the\
  `guardian` command. The cursor persists across restarts.
- New command: `guardian` (alias `integrity`).

### 3. Owner approval cannot be bypassed

- Autonomous simulated bet placement and settlement are now **disabled**\
  **by default**. Scans ask for Owner approval before placing each bet\
  (`place simulated bet` request) and never settle with synthetic\
  results; bets stay open for the Owner to settle with\
  `settle <id> <home|draw|away>` (approval-gated, as before).
- New command `autonomy on|off`: enabling autonomous mode requires\
  Owner approval; disabling it is always allowed. The state persists\
  and is shown in `status`, the banner and Guardian reports.
- Emergency-stop blocks are now counted separately\
  (`blocked_actions`) from Owner denials (`denied_actions`), so\
  neither hides the other.
- Proposing a department manager (e.g. `propose Ecommerce Manager`)\
  now routes through the full department activation approval instead\
  of silently creating a half-staffed "online" department.
- `reset` no longer clears the emergency stop; a wipe cannot silently\
  re-enable halted agents.
- Trust boundary (documented, unchanged): the Python API\
  (`book.place`, `developer_build`) trusts its caller; approval\
  enforcement lives in the CLI and pipeline layers. No code path in\
  the app calls these without the appropriate approval.
- The LLM action whitelist (below) deliberately contains no money,\
  approval, roster or filesystem actions, so an LLM can never spend\
  funds or escalate privileges.

### 4. Persistent communication history

- New `messages` table; `Bus.send` persists sender, recipient, subject,\
  body and timestamp. Communication history survives restarts.
- New command: `messages [n]` (alias `inbox`), with positive-integer\
  validation like `log`.
- `reset` wipes messages along with the other operational data.

### 5. Agents can execute meaningful tasks (LLM action layer)

- New `Agent.act(instruction)`: the agent plans whitelisted actions and\
  the runtime executes them deterministically.
- With an LLM configured, the agent must reply with a JSON array of at\
  most 4 actions from the whitelist: `create_task`, `remember`,\
  `message`, `search_memory`, `none`. Any unknown action, malformed\
  JSON, non-array or oversized plan invalidates the whole plan and\
  falls back to the deterministic heuristic (which also executes real\
  work: it logs tasks and messages). Refused individual actions are\
  recorded with `allowed = 0`.
- Field validation: titles max 100 chars, facts max 200, bodies max\
  500, message recipients must be active agents or the Owner, unknown\
  recipients are refused and audited.
- New command: `do <agent> <instruction>`. Emergency stop blocks worker\
  agents from acting, consistent with `say`.

### 6. Betting calculations, bankroll and settlement review

- Stakes are rounded to cents on placement; a bet can no longer store\
  a sub-cent stake that the bankroll (stored as a cents string) cannot\
  represent, which previously allowed the ledger invariant to drift.
- Win PnL is now derived from the payout itself:\
  `credit = round(stake x odds, 2)`, `pnl = credit - stake`, so the\
  invariant `bankroll = initial + realised PnL - open exposure` holds\
  exactly by construction. (0.2.0 computed PnL and the credit\
  independently, which could disagree by a cent at awkward odds.)
- Cap (2% of bankroll) and minimum stake (1.00) are now re-checked\
  inside the placement transaction against the live bankroll.
- New CHECK constraints on new databases: `status IN ('open', 'settled')`, `odds > 1.0`, `0 < prob < 1`, `stake >= 1.0`, and\
  `allowed IN (0, 1)` on actions. Existing 0.2.0 databases keep their\
  original schema (SQLite applies checks only to newly created tables).

### 7. LLM integration hardening

- `LLMClient` only treats `http://`/`https://` URLs as configured and\
  records a clear `last_error` otherwise (the `llm` command shows it).
- Agents still never claim to execute actions in plain `say` replies;\
  execution now happens only through the validated `act` layer.

### 8. Other fixes

- **Bug found in the 0.2.0 test suite:** `test_agent_falls_back_without_llm`\
  expected a task id in the Developer Agent's fallback reply, but\
  `DeveloperAgent.heuristic` never created a task, so that test would\
  have failed. The heuristic now logs the request on the task board\
  (and the test passes as written).
- `cmd_meeting` recorded the wrong actor ("Betting Manager") for every\
  department's guard check; it now names the actual department manager.
- `cmd_settle` failures are now written to the audit log.
- The `db` command now also shows message and action counts.
- Demo script updated: it now enables autonomy (auto-approved in\
  demo), demonstrates `do`, `guardian`, `messages` and a longer log.

### Not changed (by design)

- All 57 blueprint entries, all existing commands, the simulation\
  data and models, and SQLite persistence behaviour are unchanged.
- Betting data remains synthetic and SIMULATED-labeled; there is no\
  real-money mode, no odds API integration, and no autonomous real\
  financial action anywhere.

## Test results and verification status

**Honest status: the Python test suite was written and statically**\
**verified in full, but it could not be executed in the environment**\
**where this release was produced (no Python runtime is available**\
**there). Run it locally: `python3 test_novyra.py`. Do not treat these**\
**tests as passed until you see them pass on your machine.**

What was actually verified, and how:

1. **Structure checks (executed):** both files pass a bracket/\
   &#x20;string/indentation-aware scan: all parentheses, brackets and\
   &#x20;braces balance; all string literals (including triple-quoted\
   &#x20;docstrings) are closed; no tabs; every block header ends with a\
   &#x20;colon; all multi-line continuations were reviewed manually.
2. **Symbol cross-check (executed):** every name the test suite\
   &#x20;references on the module (`NovyraError`, `BettingError`, `CLI`,\
   &#x20;`LLMClient`, `Database`, `Runtime`, `match_probs`, `ev_of`,\
   &#x20;`kelly_fraction`, `slug`) and every method the tests call\
   &#x20;(`full_scan`, `watchdog`, `act`, `set_autonomy`, `actions_count`,\
   &#x20;`messages_count`, `blocked_actions`, and the four new CLI commands)\
   &#x20;exists in the source. 14 test classes, 58 test methods.
3. **Algorithm execution tests (executed as a TypeScript port):** the\
   &#x20;pure betting logic (Poisson model, strength model, EV, Kelly, the\
   &#x20;cent-rounding scheme, cap/min-stake validation, single and double\
   &#x20;settlement, and a 2000-operation randomized ledger fuzz) was ported\
   &#x20;1:1 to TypeScript and executed: 32 of 32 checks pass. The maximum\
   &#x20;ledger invariant deviation across 2000 mixed operations is\
   &#x20;3.6e-12 (pure floating-point representation noise, far inside the\
   &#x20;0.005 tolerance the Python assertions use).
4. **Line-by-line review:** every edited region was re-read in the\
   &#x20;final file, including each test's assertions against the actual\
   &#x20;implementation behaviour.

Test inventory (58 cases): maths (4), database (5), betting book (8),\
permissions (4), persistence/restart (2), pipeline (3), roster/bus\
(2), LLM fallback (4), CLI validation (3), **atomicity (4)**,\
**autonomy gating (4)**, **message persistence (3)**, **guardian**\
**integrity/watchdog (8)**, **agent action execution (5)**, plus 2\
unittest scaffolding lines. Bold classes are new in 0.2.1 and cover\
every important fix.

## Setup and installation

Requirements: Python 3.8 or newer. No third-party packages; the app\
uses only the standard library (sqlite3, urllib).

1. Save the Novyra canvas as `novyra.py` and the Novyra Test Suite\
   &#x20;canvas as `test_novyra.py`, in the same folder.
2. Run the tests first:
   ```
    python3 test_novyra.py
   ```
   &#x20;Expect `Ran 58 tests` with 0 failures and 0 errors (one test\
   &#x20;self-skips if `NOVYRA_LLM_URL` is set in your environment).
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
   &#x20;Verify with the `llm` command. Without these, Novyra runs with\
   &#x20;deterministic simulation replies.
5. Suggested first session: `help`, then `demo`, then `activate  Betting Department`, `scan` (approve the simulated bet yourself),\
   &#x20;`bets`, `guardian`, `messages`, `settle <id> home`.

### Migrating from 0.2.0

- Open your existing database as usual; the `messages` and `actions`\
  tables are created automatically. No data is lost.
- Autonomous placement/settlement resets to **off** for everyone:\
  scans now ask per-bet approval. Use `autonomy on` (approval) to get\
  the old behaviour back.
- Emergency stop state is no longer cleared by `reset`.

### What still needs development

- Real betting data feeds and odds APIs (current data is synthetic).
- Model calibration against real historical results.
- Real-money mode with a hardened, audited opt-in (intentionally\
  absent).
- Production LLM key management (env vars are fine for personal use,\
  not for shared machines), scheduling/background runs, REST API and\
  multi-user access.