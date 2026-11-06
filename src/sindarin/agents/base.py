"""One reasoning loop for every role (DDD §6, §13).

A `RoleAgent` is AGENT.md plus the message rules plus a handful of strict tools, run on Claude
through the Anthropic SDK, or on a deterministic stand-in when `SINDARIN_MODEL=fake`. A fence
can leave the agent only if a tool produced it byte for byte: the model cannot hand-write a
proposal or a review. The agent decides nothing; the executors re-verify every fence.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sindarin.schema import ARTIFACT_ID_PATTERN

log = logging.getLogger("sindarin.agents")

MODEL = (
    os.environ.get("SINDARIN_MODEL") or os.environ.get("SINDARIN_POLICY_MODEL") or "claude-opus-5-5"
)
EFFORT = os.environ.get("SINDARIN_EFFORT", "high")
REFUSAL = "I can only post what the validator produced. Rephrasing."
FENCE_RE = re.compile(r"```sindarin-[a-z]+[ \t]*\r?\n.*?\r?\n[ \t]*```", re.DOTALL)
ID_RE = re.compile(r"\b(?:GOALS|BUDGET|ENTRIES|CLOSE|PAY|LOCK|DEAL)-\d+\b")

MESSAGE_RULES = """
# Message rules (every message, no exceptions)
1. One to three lines. Humans write paragraphs; you never do. A fenced block does not count.
2. Name who you are talking to with an @mention and answer in the thread you were asked in.
   Mention people by the display name the header gives (`@Isaac`), never by participant id;
   participant ids and message ids appear only as tool arguments.
   The header in square brackets is context for you, never content for the room: no room ids,
   thread ids, participant ids or "replying in thread …" narration in a message.
3. Acknowledge and act in the same message: "On it. Drafting the spec in #product."
4. Facts in one line, fixed order, key values in **bold**: version, place, time in UTC, count.
5. Before slow work, one line saying so and naming what will come back.
6. A fenced artifact gets one caption line before it and one interpretation line after it.
7. At most one emoji, as a status mark. Bold for values and room names. No headers, no bullets.
8. You never approve, never address an executor with a command, never say "approved".
   The decision goes to the executor: your last line is "Your call." followed by the exact
   command the human types, for example `@sindarin-ledger approve BUDGET-1`.
9. One artifact id per message.
10. What you posted earlier in this thread came through your tools and the validator; the
    context lists what those tools did. Never call your earlier posts made up, hand-written
    or unverified. If a fact changed, state the new fact and where it came from.
"""


@dataclass(frozen=True)
class Turn:
    sender: str  # participant id
    sender_name: str
    body: str
    from_self: bool = False
    room_id: str = ""
    message_id: str = ""
    thread_id: str | None = None


@dataclass(frozen=True)
class Outgoing:
    room_id: str
    text: str
    thread_id: str | None = None


@dataclass
class Tool:
    spec: dict[str, Any]
    run: Callable[[dict[str, Any]], tuple[str, bool]]  # (text, ok)

    @property
    def name(self) -> str:
        return str(self.spec["name"])


ModelCall = Callable[..., Any]


def strict_tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "strict": True,
    }


class RoleAgent:
    def __init__(
        self,
        *,
        role: str,
        agent_md: Path,
        tools: list[Tool],
        model_call: ModelCall,
        context: Callable[[], str] | None = None,
        model: str = MODEL,
        effort: str = EFFORT,
        max_turns: int = 8,
    ) -> None:
        self.role = role
        self.agent_md = agent_md
        self.tools = {t.name: t for t in tools}
        self.model_call = model_call
        self.context = context
        self.model = model
        self.effort = effort
        self.max_turns = max_turns
        self.tool_log: list[str] = []  # what the tools did in the last respond(), one line each

    # -- prompt -------------------------------------------------------------------------------

    def system_prompt(self) -> str:
        """AGENT.md plus the message rules plus live context (policy, roadmap, the spec), re-read
        on every turn so edits need no restart."""
        instructions = self.agent_md.read_text(encoding="utf-8").strip()
        parts = [instructions, MESSAGE_RULES.strip()]
        if self.context is not None:
            parts.append(self.context())
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def build_messages(thread: list[Turn], header: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for i, t in enumerate(thread):
            role = "assistant" if t.from_self else "user"
            text = t.body if t.from_self else f"{t.sender_name}: {t.body}"
            if i == 0:
                text = f"{header}\n{text}"
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n" + text
            else:
                messages.append({"role": role, "content": text})
        if not messages or messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": header})
        if messages[-1]["role"] == "assistant":
            messages.append({"role": "user", "content": "(continue)"})
        return messages

    # -- loop ---------------------------------------------------------------------------------

    def respond(self, thread: list[Turn], header: str) -> str:
        messages = self.build_messages(thread, header)
        system = self.system_prompt()
        tool_outputs: set[str] = set()
        last_fenced: str | None = None  # the newest validator-produced block, if any
        self.tool_log = []
        for _ in range(self.max_turns):
            response = self.model_call(
                model=self.model,
                max_tokens=6000,
                system=system,
                tools=[t.spec for t in self.tools.values()],
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
            )
            stop = getattr(response, "stop_reason", "end_turn")
            if stop == "refusal":
                return "I can't help with that."
            blocks = list(getattr(response, "content", []) or [])
            tool_uses = [b for b in blocks if getattr(b, "type", "") == "tool_use"]
            if stop != "tool_use" or not tool_uses:
                text = "\n".join(
                    getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text"
                ).strip()
                return self.guard(text, tool_outputs)
            messages.append({"role": "assistant", "content": _as_params(blocks)})
            results = []
            for tu in tool_uses:
                tool = self.tools.get(tu.name)
                if tool is None:
                    out, ok = f"Unknown tool {tu.name}", False
                else:
                    try:
                        out, ok = tool.run(dict(tu.input or {}))
                    except Exception as exc:  # a tool bug is reported to the model, not raised
                        log.exception("tool %s crashed", tu.name)
                        out, ok = f"tool error: {exc.__class__.__name__}", False
                if ok and FENCE_RE.search(out):
                    tool_outputs.add(out.strip())
                    last_fenced = out.strip()
                if ok:
                    self.tool_log.append(fact_line(tu.name, dict(tu.input or {}), out))
                log.info("%s.%s → %s", self.role, tu.name, "ok" if ok else out[:80])
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": out,
                        "is_error": not ok,
                    }
                )
            messages.append({"role": "user", "content": results})
        if last_fenced is not None:
            # The budget ran out after a validator accepted a block: post that block rather than
            # lose the work (seen live 2026-11-05 when the real coder used every turn on the PR).
            log.info("%s: turn budget reached, posting the last validated block", self.role)
            return self.guard(last_fenced, tool_outputs)
        return "I could not finish within the turn budget. A person should take a look."

    @staticmethod
    def guard(text: str, tool_outputs: set[str]) -> str:
        """Every fence in the text must be byte for byte one the validator produced. Text around
        a legitimate fence is kept (caption and interpretation lines, rule 6)."""
        if not FENCE_RE.search(text):
            return text or "I have nothing to add."
        kept = text
        for m in FENCE_RE.finditer(text):
            fence = m.group(0)
            if not any(fence in out for out in tool_outputs):
                return REFUSAL
        for out in tool_outputs:
            # the tool output includes the executor mention line; make sure it is present once
            if out in kept:
                continue
            fence = FENCE_RE.search(out)
            if fence and fence.group(0) in kept:
                kept = kept.replace(fence.group(0), out, 1)
        return kept


_FACT_KEYS = ("target", "number", "snapshot_hint", "head_sha", "pr", "summary", "tag", "version")


def fact_line(name: str, inp: dict[str, Any], out: str) -> str:
    """One line a later turn can rely on: the tool, its arguments and what came back. Kept per
    room so the model knows its earlier posts were tool-made (a model that sees only its own
    text later tends to disown it; seen live 2026-11-06)."""
    args = json.dumps(inp, sort_keys=True, default=str)
    if len(args) > 120:
        args = args[:117] + "…"
    found = [
        f"{k} {m.group(1)}"
        for k in _FACT_KEYS
        if (m := re.search(rf'"{k}"\s*:\s*"?([^",}}]+)"?', out))
    ]
    summary = (
        ", ".join(found)
        if found
        else next((ln.strip() for ln in out.splitlines() if ln.strip()), "ok")[:120]
    )
    return f"{name}({args}) → {summary} · {time.strftime('%H:%M', time.gmtime())} UTC"


def _as_params(blocks: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in blocks:
        if hasattr(b, "model_dump"):
            d = b.model_dump(exclude_none=True)
        else:
            d = dict(b.__dict__)
        out.append(d)
    return out


# -- message rules check (§6.5, §13), used on the stand-in output -----------------------------


def check_message(text: str, *, require_mention: bool = True) -> list[str]:
    """Violations of §13 in a message, or [] when it complies."""
    problems: list[str] = []
    prose = FENCE_RE.sub("", text)
    lines = [ln for ln in prose.splitlines() if ln.strip()]
    if len(lines) > 3:
        problems.append(f"{len(lines)} lines, max 3")
    if require_mention and "@" not in prose:
        problems.append("no @mention")
    ids = {m.group(0) for m in ID_RE.finditer(prose)}
    if len(ids) > 1:
        problems.append(f"{len(ids)} artifact ids in one message: {', '.join(sorted(ids))}")
    if any(ln.lstrip().startswith(("#", "- ", "* ")) for ln in lines):
        problems.append("headers or bullets in chat")
    if re.search(r"\bapproved\b", prose, re.IGNORECASE) and "Your call" not in prose:
        problems.append("an agent never says approved")
    return problems


# -- model calls ------------------------------------------------------------------------------


def is_fake_model() -> bool:
    return (
        os.environ.get("SINDARIN_MODEL") or os.environ.get("SINDARIN_POLICY_MODEL") or ""
    ).lower() == "fake"


def anthropic_model_call() -> ModelCall:
    """Claude through the Anthropic SDK (DDD §6: claude-opus-5-5, adaptive thinking, high)."""
    import anthropic

    client = anthropic.Anthropic()

    def call(**kwargs: Any) -> Any:
        return client.messages.create(**kwargs)

    return call


@dataclass
class ScriptedModel:
    """A deterministic model for tests and the stand-in. `step(messages, system)` sees the whole
    conversation, tool results included, and returns ("tool", name, input) or ("text", text)."""

    step: Callable[[list[dict[str, Any]], str], tuple[Any, ...]]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self.calls.append(kwargs)
        kind, *rest = self.step(kwargs.get("messages", []), kwargs.get("system", ""))
        if kind == "tool":
            name, args = rest
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    SimpleNamespace(
                        type="tool_use", id=f"fake-{len(self.calls)}", name=name, input=args
                    )
                ],
            )
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=rest[0])]
        )


def tool_history(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], str, bool]]:
    """(tool name, input, result text, ok) for every tool call so far in the conversation."""
    out: list[tuple[str, dict[str, Any], str, bool]] = []
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                pending[str(block.get("id"))] = (
                    str(block.get("name")),
                    dict(block.get("input") or {}),
                )
            elif block.get("type") == "tool_result":
                name, args = pending.get(str(block.get("tool_use_id")), ("?", {}))
                out.append((name, args, str(block.get("content", "")), not block.get("is_error")))
    return out


def human_turns(messages: list[dict[str, Any]]) -> list[str]:
    """The user-side text turns (people and executors), header stripped, in order."""
    out: list[str] = []
    for m in messages:
        if m.get("role") != "user" or not isinstance(m.get("content"), str):
            continue
        for line in m["content"].splitlines():
            if line.startswith("[") and line.endswith("]"):
                continue  # the header
            if line.strip() and line.strip() != "(continue)":
                out.append(line)
    return out


def artifact_ids(text: str) -> list[str]:
    return ID_RE.findall(text)


__all__ = [
    "ARTIFACT_ID_PATTERN",
    "EFFORT",
    "MESSAGE_RULES",
    "MODEL",
    "REFUSAL",
    "ModelCall",
    "Outgoing",
    "RoleAgent",
    "ScriptedModel",
    "Tool",
    "Turn",
    "anthropic_model_call",
    "artifact_ids",
    "check_message",
    "human_turns",
    "is_fake_model",
    "strict_tool",
    "tool_history",
]
