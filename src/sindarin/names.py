"""Handles the agents and executors go by in the rooms (design v5 §4, §5).

Named by function it talks (an LLM agent, untrusted); named Sindarin it acts (a deterministic
executor, trusted). Identity never depends on these names: every check uses participant ids.
"""

ANALYST = "analyst"
PLANNER = "planner"
BOOKKEEPER = "bookkeeper"
TREASURY = "treasury"
SALES = "sales"
AGENT_ROLES: tuple[str, ...] = (ANALYST, PLANNER, BOOKKEEPER, TREASURY, SALES)

SWITCH_EXECUTOR = "sindarin-switch"
LEDGER_EXECUTOR = "sindarin-ledger"
EXECUTORS: tuple[str, ...] = (SWITCH_EXECUTOR, LEDGER_EXECUTOR)

DISPLAY = {
    ANALYST: "Analyst",
    PLANNER: "Planner",
    BOOKKEEPER: "Bookkeeper",
    TREASURY: "Treasury",
    SALES: "Sales",
    SWITCH_EXECUTOR: "Sindarin Switch",
    LEDGER_EXECUTOR: "Sindarin Ledger",
}

# which executor owns which artifact kind (design v5 §5): the goals and the close plan open
# their rooms (Switch); budgets, register entries, payment runs and locks are written into the
# books (the ledger)
EXECUTOR_OF_KIND = {
    "goals": SWITCH_EXECUTOR,
    "budget": LEDGER_EXECUTOR,
    "entries": LEDGER_EXECUTOR,
    "close": SWITCH_EXECUTOR,
    "pay": LEDGER_EXECUTOR,
    "lock": LEDGER_EXECUTOR,
    "deal": LEDGER_EXECUTOR,
}
