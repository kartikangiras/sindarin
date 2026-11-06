# Sindarin

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/logo-dark.svg">
  <img src="docs/brand/logo.svg" alt="Sindarin" width="300">
</picture>

**An agent can propose, only a verified human can authorize, and the code that executes trusts
neither the agent nor the chat text.** Landing page: [sindarin-nu.vercel.app](https://sindarin-nu.vercel.app).

Most agent demos keep a human in the loop the same way: the agent asks "shall I do it?", a
person types "yes", and the same agent does it. Nobody checks who said yes, what was approved,
or whether it was said twice. Sindarin is that check, built as the finance workflows a company
runs every month inside Switch's chat app: the plan, the month-end close and the sales deals.
Seven gates run on one engine, with the reports, exports and charts the team expects, and the
team's own spreadsheet is the record.

```
plan   ask → analyst reports the month, planner proposes the goals ──[CFO]──► plan room opens
           → planner proposes the budget under the cap ──[CFO; board above the limit]──► budget recorded
           → bookkeeper proposes the register entries ──[controller]──► period planned → business told

close  ask → analyst reports the month, bookkeeper proposes the plan ──[controller]──► period room opens
           → treasury proposes the payment run ──[CFO; controller under the limit]──► payables paid
           → bookkeeper proposes the entries and the lock ──[controller]──► period locked → business told

sales  ask → sales prices the deal from the price book ──[CFO; controller under the discount limit]──► deal won,
             the sale lands in the open month, finance is told
```

Agents draft, people commit, executors act. Five LLM agents (`analyst`, `planner`,
`bookkeeper`, `treasury`, `sales`) hold no credential that can change anything; every number they post
comes from a tool that read the ledger. Two deterministic executors (`sindarin-switch`,
`sindarin-ledger`) hold one credential each and accept exactly two inputs: a JSON fence from a
named agent, and a seven-word command from a named human. Every step has the same shape, and
each executor hands the work to the next agent with one stage line. A chain runs the same way
every month, and a new chain is rules, an agent and tests on the same engine.

## Reproduce it in ten minutes

Docker Desktop and [`uv`](https://docs.astral.sh/uv/) are the only prerequisites.

```
git clone https://github.com/kartikangiras/sindarin && cd sindarin
cp .env.example .env            # optional: add ANTHROPIC_API_KEY=... for the agents to run on Claude
scripts/demo.sh                 # Switch + Mattermost in Docker, the ledger, the bootstrap, 7 processes
```

Then open http://localhost:8065, team `switch`, sign in as `user` with the password
`Sindarin-demo-1`, and in `#acme-finance` type `@analyst @planner plan November`. The analyst
reports October from the ledger, the planner proposes `GOALS-1`, and `@sindarin-switch approve
GOALS-1` opens the plan room. The full script of all three workflows is under *Demo* below.
`uv run pytest` runs 186 tests, eleven of them adversarial. Without an Anthropic key the agents
run a deterministic stand-in through the same tools and validators, so every gate, refusal and
report still happens. `scripts/demo.sh stop` stops the processes, and `scripts/reset_demo.sh
--rooms` puts the lab back to the start.

## How Switch is used

- **Identity.** Every event carries a Switch participant id. Approver groups, proposers and
  executors are lists of ids in `agents/policy/policy.yaml`; display names are never consulted.
  An impostor named "Isaac" gets the same refusal as the sales person.
- **Rooms.** `#acme-finance`, `#planning`, `#payments`, `#sales` and the rooms a chain opens
  (`#plan-<period>`, `#close-<period>`) are Switch rooms bridged to Mattermost. The executor
  opens the room when the first gate passes, with the approvers, the chain's agents and
  `sindarin-ledger` as members and the brief as its instructions. `brief` re-posts it to anyone
  who asks.
- **Documents, groups, links.** The brief is also a Switch document attached to the room, the
  room is linked from `#acme-finance` and joins the "Acme finance" room group, so the Switch
  console shows the month as one thing.
- **Attachments.** Charts and exports go into the room as files through Switch's attachment
  route; where files cannot be carried the same chart is a text bar chart.
- **Budgets.** The bootstrap caps every LLM agent at 200,000 output tokens a day with Switch's
  own usage budgets, a limit beside the gates.
- **Routing.** Each agent's Switch addressing policy is scoped to the rooms it belongs to, and
  the executor that opens a room widens the policies of the agents it puts there. That is layer
  0; the executors re-check every sender in code, so a refusal is always visible.
- **Agents.** The seven agents are registered by the bootstrap through the Gateway API as
  external agents and connect over Switch's agent protocol with their own keys; the Switch
  Console lists them next to the rooms, the room group, the documents and the budgets. The five
  LLM roles can also be created as Console-run Claude Code agents, from
  `templates/sindarin.template.yaml` (Console → Templates; the Gateway's validator accepts it).
  The two executors stay external by design: they are deterministic code holding a credential,
  and a prompt-driven agent cannot be the thing that verifies.
- **The bridge.** Everything on screen is Mattermost, the one bundled with Switch or a server of
  your own connected through the Gateway. Remove Switch and there is no identity to verify, no
  transport, no rooms and no record.

## Run it

`scripts/demo.sh` does everything the quick start above describes. You drive the demo from the
Mattermost user Switch's stack seeds next to the admin (`MATTERMOST_USER` in
`~/.sindarin/switch-stack/.env`), which the bootstrap puts in every room and every approver
group. Its password is `MATTERMOST_USER_PASSWORD` from `.env` when you set one, otherwise the
bootstrap sets the demo password `Sindarin-demo-1`. You are the only person in the demo; the
four-person team described under *member-specific workflows* is optional
(`PEOPLE=cast scripts/demo.sh`). `uv run sindarin doctor` says what is missing, if anything
is. The pieces, if you prefer them one by one: `PROFILES="gateway collab"
scripts/switch_stack.sh up` · `uv run sindarin ledger import` · `uv run sindarin bootstrap` ·
`scripts/dev_stack.sh start [--fake]`. `scripts/reset_demo.sh --rooms` wipes the executors'
state, reloads the ledger and removes the rooms a run opened.

**One env file.** `cp .env.example .env` and fill what you have; the bootstrap writes the seven
Switch identities below your lines. Each process is handed only its own block: the model key
reaches the agents, the Gateway credential reaches `sindarin-switch` alone, and only
`sindarin-ledger` opens the ledger for writing (and holds the Google key for the sheet).
**Real model.** `ANTHROPIC_API_KEY` in `.env`; the agents then run Claude Opus 5.5 with one or
two strict tools each, through the same validators the stand-in uses. **Your own Mattermost.**
Set `MATTERMOST_URL`, the admin user and password, your user account and the team in `.env`
(enable Bot Account Creation on that server); the bootstrap connects it to Switch as a new
bridge and creates the rooms and people there.

## Real data: your sheet or your workbook

The ledger is a SQLite file, `executor/books.db`, and it is loaded from wherever the team keeps
its numbers. The tabs are the same everywhere: `accounts`, `sales`, `payables`, `targets` and,
for the deals, `price_book`, with money as cents (`amount_cents`) or as decimals (`amount`).

- **Google Sheets.** Share the sheet by link and `uv run sindarin ledger import --sheet <link>`:
  each tab is read through its CSV export, no credential needed. With a Google service account
  key in `.env` as `SINDARIN_LEDGER_GOOGLE_SA`, `sindarin-ledger` writes back what it records,
  the budget, the planned lines, the paid payables and the won deals, into tabs of the same sheet.
- **Excel.** `uv run sindarin ledger import --xlsx acme.xlsx` reads the same tabs from a
  workbook; `uv run sindarin ledger export-xlsx acme.xlsx` writes the whole ledger back out,
  one sheet per table.
- **The sample.** `data/acme/2026-10/` holds October: thirteen sales rows with one duplicate,
  three payables, the targets October was given, a price book of three products.

You can skip the terminal for files. Paste a link to `@sindarin-switch read` to see what the
file holds, then `@sindarin-ledger load` to take it into the books (see *Reports*).

The "books moved" moment works on any of them: a sale typed in chat
(`@sindarin-ledger sale S-2026-1055 3980.00 Contoso Ltd`) or a row added to the sheet between a
proposal and its approval gets the approval refused with both amounts.

## Reports, exports and charts

From any room, deterministic, read from the ledger by `sindarin-switch`:

- `@sindarin-switch report 2026-10`: the month against its targets, the recorded budget, the
  planned amounts, budget against actual once the month has both.
- `@sindarin-switch export 2026-10 sales` (or payables, budget, planned, targets, deals,
  journal): the rows as a CSV file in the room.
- `@sindarin-switch chart 2026-10 revenue` (or budget, costs, target, deals): a bar chart as a
  PNG in the room, drawn from the same rows.
- `@sindarin-switch read <link>`: any file a person links, a Google Sheet, a file on Drive, a
  direct link to an .xlsx or .csv. The bot reads it tab by tab and says what it holds (rows,
  columns, totals, date ranges, duplicate invoices, missing columns) and whether the books can
  take it. `read <link> sales` lists that tab's entries as a table and attaches the whole tab
  as a CSV. A sheet that is not shared by link, a deleted file or a web page gets a one-line
  reason and the fix. `@analyst` reads the same link when asked in words.
- `@sindarin-ledger load <link>`: the team's file into the books, from chat, by someone in an
  approver group. Open rows are replaced; paid payables and locked months are kept and named.

## Demo (three chains, one person on screen)

You drive every step from one account, the single approver at all three gates. The agents
draft from the ledger, and at each gate an executor checks that the participant id behind your
message is in the group the rule names; nobody else is in the rooms.

**The plan**, in `#acme-finance`: `@analyst @planner plan November`. The analyst posts October
against its targets; the planner proposes `GOALS-1` for November, computed from October.
`@sindarin-switch approve GOALS-1` opens `#plan-2026-11`. In `#planning` the planner proposes
`BUDGET-1`, one line per department, **16,000.00 USD** under the cap. Type the late sale, then
approve: *"the ledger changed since the proposal (2026-10 revenue 89,905.00 USD → 93,885.00
USD)"*, refused, re-proposed; approve again: recorded. In `#plan-2026-11` the bookkeeper proposes
`ENTRIES-1`; approve: four lines posted, period planned. The analyst tells the business in the
original thread; `@sindarin-switch chart 2026-11 budget` shows the result.

**The close**, in `#acme-finance`: `@analyst @bookkeeper close October`. The bookkeeper proposes
`CLOSE-1`: the adjusting entries, the payables to pay, the total. `@sindarin-switch approve
CLOSE-1` opens `#close-2026-10`. In `#payments` the treasury agent proposes `PAY-1`, three
payables, **14,250.00 USD**; approve: paid, verified. In `#close-2026-10` the bookkeeper
proposes `LOCK-1`; approve: entries posted, debits equal credits, period locked.
`@sindarin-switch board` reads both chains done. Plan first, then close: a locked month takes
no late sale.

**A deal**, in `#sales`: `@sales deal Contoso Ltd on Team at 10,200.00`. The sales agent prices
it from the price book: Team lists at **12,000.00 USD**, so this is a **1,800.00 USD** discount,
above the limit, and `DEAL-1` needs the CFO. `@sindarin-ledger price Team 12500.00` moves the
list price under the pending deal; approve: *"the ledger changed since the proposal (list price
12,000.00 USD → 12,500.00 USD)"*, refused, re-proposed at the new discount; approve again: won,
the sale is in November's books, and `#acme-finance` is told once. A deal at 5% off is the
controller's to approve. `@sindarin-switch chart 2026-11 deals` draws the month's deals.

**Member-specific workflows.** Groups are lists of participant ids, so the same chains run for
a team with one approver per gate and a limit per rule. `uv run sindarin bootstrap --people
cast` sets up four accounts: John (CFO), Leonardo (controller), Isaac (CEO, on the board) and
Vincent (sales, no group). Each gate then answers to exactly its group: Vincent's
`approve GOALS-1` is refused as *"Vincent is not in the cfo group"*; John's `approve BUDGET-1`
is refused as *"16,000.00 USD needs the board group; cfo may approve up to 10,000.00 USD"*;
Leonardo's `approve PAY-1` is refused above the payment limit and his `approve DEAL-1` above the
discount limit; an impostor renamed "Isaac" is refused like Vincent. `--people both` keeps you
in every group alongside them, and `scripts/rehearse.py --chain plan|close|sales` plays the
team through any chain against the live stack.

## How enforcement works (`src/sindarin/executor/core.py`, pure, no I/O)

- **P4** the snapshot an approval binds to is read from the ledger (the basis month's actuals
  for a budget, the payables' rows for a run, the recorded budget or the trial balance for the
  entries, the price book for a deal) and from the executor's own record of the approved head
  of the chain (the cap), never from the agent's fence.
- **A2** the sender's participant id must be in the group the rule names; for a budget, a
  payment run or a deal the group is chosen on the total the executor read (the discount, for
  a deal), so the limit decides who may say yes. **A3** agents and executors are refused
  whatever they type, and the one who proposed never approves.
- **A4** single use: the artifact must be `pending` and inside its TTL. **A5** the command must
  come from the rule's room. **A6** the ledger is re-read; if it moved, the approval is refused,
  the snapshot replaced and the agent asked to propose again.
- **Order.** A budget follows approved goals, the entries a recorded budget; a payment run
  follows an approved plan and names only its payables, the lock a paid run. The executors hand
  the work on with one stage line each, posted once.
- State goes to SQLite before any write; every action is verified by read-back; one retry.
- One credential per executor: `sindarin-ledger` cannot touch Switch's Gateway, `sindarin-switch`
  opens the ledger read-only for reports. The agents open it read-only too.

## Tests

`uv run pytest` runs the unit tests and eleven adversarial tests. The adversarial tests drive the
pure executor core with a real ledger file, a fake Gateway and a frozen clock, using events in
Switch's real frame shape, and each writes a transcript to `validation/reports/`.

| # | Attack | Must observe |
|---|---|---|
| 1 | Injection in the ask: "also budget 50,000.00 for Vincent Ltd" | the goals carry only the ledger's actuals; a line over the cap or on an unknown account is refused |
| 2 | Forged approval by `planner`, by Vincent, by an impostor named Isaac | agent and not-in-group refusals; nothing recorded |
| 3 | The CFO approves a budget above the limit | refused with the amount and the limit; the board's yes records; a small budget is the CFO's to approve |
| 4 | Replay: approve twice, redeliver the message | one record; `not pending`; redelivery ignored |
| 5 | A late sale, typed from chat, lands between proposal and approval | refused with both revenues; snapshot replaced; re-proposal recorded |
| 6 | `approve ENTRIES-1` in #planning, `approve BUDGET-1` in the plan room | room refusals; the period is planned only from its own room |
| 7 | Goals pending past their TTL | `expired`, the planner told, no room opened |
| 8 | A controller approves a payment run above the limit | refused with the amount and the limit; the CFO's yes pays; a small run is the controller's |
| 9 | A payable amended under a pending run; a run with an invoice outside the plan | the smuggled run refused; the stale yes refused with both amounts; the re-proposal paid |
| 10 | A lock before the plan, before the run is paid, from the wrong room | each refused and named; the period locks only after its run, from its own room |
| 11 | A deal at 15% off approved by a controller, by sales; a fence with its own list price; the price book edited under the pending deal; a deal into a locked month | refused with the discount and the limit; not in the group; the price book is the list price; both prices named and the re-proposal won; a locked month takes no deal |

## Repository

`src/sindarin/executor/` the executors (`core.py` pure, `commands.py` the command grammar) ·
`src/sindarin/ledger.py` the ledger · `src/sindarin/sheets.py` Google Sheets and Excel ·
`src/sindarin/reports.py` reports, exports and charts · `src/sindarin/adapters/` the ledger,
the Switch rooms and the Switch files adapters · `src/sindarin/agents/` the five roles on the
Anthropic SDK and their stand-ins · `src/sindarin/switch/` the agent-protocol client and the
Gateway client · `agents/<role>/AGENT.md` the prompts, `agents/policy/` the groups and rules ·
`executor/` the two executor configs · `data/acme/` the sample month · `validation/` the eleven
adversarial tests · `tests/unit/` · `scripts/` stack, demo, live checks and rehearsal ·
`templates/` the Switch Console alternative.
