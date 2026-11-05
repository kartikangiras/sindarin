"""Handles the agents and executors go by in the rooms (DDD §1.5, §6, §7).

Named by function it talks (an LLM agent, untrusted); named Sindarin it acts (a deterministic
executor, trusted). Identity never depends on these names: every check uses participant ids.
"""

PRODUCT = "product"
CODER = "coder"
REVIEWER = "reviewer"
RELEASE = "release"
AGENT_ROLES: tuple[str, ...] = (PRODUCT, CODER, REVIEWER, RELEASE)

SWITCH_EXECUTOR = "sindarin-switch"
GITHUB_EXECUTOR = "sindarin-github"
EXECUTORS: tuple[str, ...] = (SWITCH_EXECUTOR, GITHUB_EXECUTOR)

DISPLAY = {
    PRODUCT: "Product",
    CODER: "Coder",
    REVIEWER: "Reviewer",
    RELEASE: "Release",
    SWITCH_EXECUTOR: "Sindarin Switch",
    GITHUB_EXECUTOR: "Sindarin GitHub",
}

# which executor owns which artifact kind (DDD §7.1, §7.2)
EXECUTOR_OF_KIND = {"spec": SWITCH_EXECUTOR, "pr": GITHUB_EXECUTOR, "release": GITHUB_EXECUTOR}
