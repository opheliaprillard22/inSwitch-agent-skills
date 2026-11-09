"""Agent turn runner.

Encapsulates the LLM-driven turn loop: build the prompt for the calling
agent's role, call the LLM, process its tool calls, feed the results back,
and repeat until the model produces a final text response.

The original ``run_agent_turn`` in app.py had two near-identical
``gen_ai.chat`` blocks — one for the initial call, one for the tool
follow-up call — differing only in the ``previous_response_id`` passed
and the ``turn_kind`` attribute on the span. Both are factorised here
into ``_call_llm``.

The class holds the OpenAI client and the verbosity flag as instance
state, so callers don't have to thread them through every invocation.
The model name is read from ``core.config`` by default but can be
overridden for tests or alternative deployments.

Public API:
    AgentTurnRunner(client, model=MODEL, verbose=False)
    runner.run(agent, user_input, folder_overview, folder_skill, agents)
        -> str
    runner.process_user_input(manager, user_input) -> str
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, List

CONTEXT_COMPACTION_LIMIT = 12000

from tracing import compute_llm_cost, start_span

from core.agent import AgentManager, AgentState, restart_agent_session
from core.config import MAX_LOG_CHARS, MODEL
from core.fs import list_folder_overview
from core.logging_hub import log_event
from core.prompts import (
    build_captain_prompt,
    build_supervisor_prompt,
    build_worker_prompt,
)
from core.skills import extract_frontmatter_name, load_folder_skill
from core.tools import ToolContext, dispatch, get_schemas

if TYPE_CHECKING:
    from openai import OpenAI


class AgentTurnRunner:
    """Drives one agent's turn through an LLM-and-tools loop.

    One instance is created at process startup (in ``core.__main__``)
    and shared by every code path that needs to run an agent: the HTTP
    handler, the CLI REPL, and ``delegate_task`` (via ``ctx.runner``).
    """

    def __init__(
        self,
        client: "OpenAI",
        model: str = MODEL,
        verbose: bool = False,
    ) -> None:
        self.client = client
        self.model = model
        self.verbose = verbose

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def run(
        self,
        agent: AgentState,
        user_input: str,
        folder_overview: str,
        folder_skill: str,
        agents: Dict[str, AgentState],
    ) -> str:
        """Run one turn of ``agent`` against ``user_input``.

        Builds the role-appropriate prompt, calls the LLM, processes any
        tool calls in a loop, and returns the final text. Updates
        ``agent.last_response_id`` for conversation continuity.
        """
        system_prompt, tools = self._build_prompt_and_tools(
            agent, folder_overview, folder_skill
        )
        if system_prompt is None:
            return "Error: sub-agent is missing a folder."

        # Initial call: input is the user message, previous_response_id
        # chains to whatever this agent saw last.
        response = self._call_llm(
            instructions=system_prompt,
            tools=tools,
            input_payload=[{"role": "user", "content": user_input}],
            previous_response_id=agent.last_response_id,
            agent=agent,
            turn_kind=None,
        )
        if isinstance(response, str):  # error path
            return response

        # Tool loop: as long as the model emits tool calls, dispatch
        # them and feed the outputs back in a follow-up call.
        while True:
            tool_calls = _extract_tool_calls(response)
            if not tool_calls:
                break
            tool_outputs = self._process_tool_calls(
                tool_calls,
                agent=agent,
                agents=agents,
                folder_overview=folder_overview,
                folder_skill=folder_skill,
            )
            response = self._call_llm(
                instructions=system_prompt,
                tools=tools,
                input_payload=tool_outputs,
                previous_response_id=response.id,
                agent=agent,
                turn_kind="tool_followup",
            )
            if isinstance(response, str):  # error path
                return response

        agent.last_response_id = response.id
        return _extract_text(response) or "(no response)"

    def process_user_input(self, manager: AgentManager, user_input: str) -> str:
        """Top-level entry: handle session commands, lazy-load the
        captain's SKILL.md if needed, then run a captain turn.

        Currently the only session command short-circuited here is
        ``:restart``; everything else goes through the captain.
        """
        if user_input == ":restart":
            restart_result = restart_agent_session(manager, triggered_by="api")
            killed = ", ".join(restart_result["killed_agents"])
            return (
                f"Restarted agent session. Killed agents: {killed}. "
                "Created clean captain."
            )

        captain = manager.captain()
        if captain.folder_path is None:
            captain.folder_path = "."
        if captain.folder_skill is None:
            try:
                captain.folder_skill = load_folder_skill(captain.folder_path)
                captain.skill_name = (
                    extract_frontmatter_name(captain.folder_skill)
                    or captain.folder_path
                )
                log_event(
                    "skill_loaded",
                    {"agent": captain.name, "skill": captain.skill_name},
                )
            except Exception as exc:
                captain.folder_skill = ""
                log_event(
                    "skill_missing",
                    {
                        "agent": captain.name,
                        "folder": captain.folder_path,
                        "error": str(exc),
                    },
                )

        return self.run(
            agent=captain,
            user_input=user_input,
            folder_overview=list_folder_overview(captain.folder_path),
            folder_skill=captain.folder_skill or "",
            agents=manager.agents,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_prompt_and_tools(
        self,
        agent: AgentState,
        folder_overview: str,
        folder_skill: str,
    ) -> tuple[str | None, List[Dict[str, Any]]]:
        """Pick the prompt builder and tool list for ``agent``'s role.

        Returns ``(None, [])`` if the worker is misconfigured (no folder
        or no skill), to let ``run`` short-circuit with an error message.
        """
        if agent.role == "captain":
            system_prompt = build_captain_prompt(folder_overview, folder_skill)
            tools = get_schemas(include_delegate=True)
            return system_prompt, tools

        if not agent.folder_path or agent.folder_skill is None:
            return None, []

        if agent.role == "supervisor":
            system_prompt = build_supervisor_prompt(
                agent.folder_path,
                agent.folder_skill,
                folder_overview,
            )
            tools = get_schemas(
                include_delegate=False,
                include_supervisor=True,
            )
            return system_prompt, tools

        system_prompt = build_worker_prompt(
            agent.folder_path,
            agent.folder_skill,
            agent.name,
            folder_overview,
        )
        tools = get_schemas(include_delegate=False)
        return system_prompt, tools

    def _call_llm(
        self,
        *,
        instructions: str,
        tools: List[Dict[str, Any]],
        input_payload: List[Dict[str, Any]],
        previous_response_id: str | None,
        agent: AgentState,
        turn_kind: str | None,
    ):
        """Wrap one ``responses.create`` call in a ``gen_ai.chat`` span.

        Factorises the two duplicated blocks of the original
        ``run_agent_turn``. Returns the response on success or an
        error string on failure (the caller pattern-matches on type).
        """
        span_attrs: Dict[str, Any] = {
            "gen_ai.request.model": self.model,
            "agent.name": agent.name,
            "agent.role": agent.role,
        }
        if turn_kind:
            span_attrs["gen_ai.turn_kind"] = turn_kind

        with start_span("gen_ai.chat", span_attrs) as span:
            try:
                response = self.client.responses.create(
                    model=self.model,
                    input=input_payload,
                    instructions=instructions,
                    tools=tools,
                    previous_response_id=previous_response_id,
                )
            except Exception as exc:
                label = "follow-up" if turn_kind == "tool_followup" else "request"
                span.mark_error(f"OpenAI {label} failed: {exc}")
                return f"Error: OpenAI {label} failed: {exc}"

            if getattr(response, "usage", None):
                span.update(
                    {
                        "gen_ai.usage.input_tokens": response.usage.input_tokens,
                        "gen_ai.usage.output_tokens": response.usage.output_tokens,
                        "gen_ai.usage.cost_usd": compute_llm_cost(
                            self.model, response.usage
                        ),
                        "gen_ai.response_id": getattr(response, "id", None),
                        "gen_ai.request_id": getattr(response, "_request_id", None),
                    }
                )
        return response

    def _process_tool_calls(
        self,
        tool_calls: List[Any],
        *,
        agent: AgentState,
        agents: Dict[str, AgentState],
        folder_overview: str,
        folder_skill: str,
    ) -> List[Dict[str, Any]]:
        """Dispatch a batch of tool calls, log them, and shape the
        results for the next ``responses.create`` call.

        Each call is wrapped in a ``tool.call`` span so the trace tree
        reflects which tool failed when it does.
        """
        tool_outputs: List[Dict[str, Any]] = []
        for call in tool_calls:
            call_id = getattr(call, "call_id", None) or getattr(call, "id", None)
            if not call_id:
                continue
            tool_name = getattr(call, "name", "")
            try:
                args = json.loads(getattr(call, "arguments", "") or "{}")
            except json.JSONDecodeError:
                args = {}

            log_event(
                "tool_invocation",
                {"agent": agent.name, "tool": tool_name, "args": args},
            )
            if self.verbose:
                print(f"\n[{agent.name} tool] {tool_name} args={args}")

            with start_span(
                "tool.call",
                {"tool.name": tool_name, "agent.name": agent.name},
            ) as tool_span:
                try:
                    ctx = ToolContext(
                        agent=agent,
                        agents=agents,
                        client=self.client,
                        model=self.model,
                        verbose=self.verbose,
                        runner=self.run,
                        folder_overview=folder_overview,
                        folder_skill=folder_skill,
                    )
                    result = dispatch(tool_name, args, ctx)
                except Exception as exc:
                    result = {"error": str(exc)}
                    tool_span.mark_error(str(exc))
                if isinstance(result, dict) and "error" in result:
                    tool_span.mark_error(str(result["error"]))

            log_event(
                "tool_result",
                {"agent": agent.name, "tool": tool_name, "result": result},
            )
            if self.verbose:
                preview = json.dumps(result)
                if len(preview) > MAX_LOG_CHARS:
                    preview = preview[:MAX_LOG_CHARS] + "...(truncated)"
                print(f"[{agent.name} tool] output={preview}")

            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(_compact_tool_output(result)),
                }
            )
        return tool_outputs


def _reduce_service(service: Any) -> Any:
    """Keep only the fields the service-selection step reads."""
    if not isinstance(service, dict):
        return service
    reduced = {
        "name": service.get("name"),
        "description": service.get("description"),
        "dependencies": service.get("dependencies", []) or [],
    }
    return {key: value for key, value in reduced.items() if value is not None}


def _reduce_instance(instance: Any) -> Any:
    """Keep only the fields the dedup/status steps read."""
    if not isinstance(instance, dict):
        return instance
    reduced = {
        "instance_name": instance.get("instance_name"),
        "service_name": instance.get("service_name"),
        "deployment_status": instance.get("deployment_status"),
        "ready": instance.get("ready"),
        "env_vars": instance.get("env_vars") or {},
    }
    return {key: value for key, value in reduced.items() if value is not None}


def _fit_items(items: List[Any], budget: int) -> tuple[List[Any], int]:
    """Take as many leading items as fit in ``budget`` characters.

    Returns the kept items and how many were left out, so the caller can say
    so explicitly instead of silently shortening a list.
    """
    kept: List[Any] = []
    used = 2  # the enclosing [] of the JSON array
    for index, item in enumerate(items):
        encoded = len(json.dumps(item, ensure_ascii=True, sort_keys=True)) + 1
        if used + encoded > budget and kept:
            return kept, len(items) - index
        used += encoded
        kept.append(item)
    return kept, 0


def _compact_tool_output(result: Any) -> Any:
    """Reduce oversized tool outputs before sending them back to the model.

    Large plan sets and real k8s-manager catalogue/status payloads can exceed
    the model context window when fed back during tool follow-up calls.

    Lists are reduced field-by-field first and only then shortened. 
    Whenever entries are left out, the count is reported in ``summary``.
    """
    if not isinstance(result, dict):
        return result

    serialized = json.dumps(result, ensure_ascii=True, sort_keys=True)
    if len(serialized) <= CONTEXT_COMPACTION_LIMIT:
        return result

    summary: Dict[str, Any] = {
        "truncated": True,
        "original_size_chars": len(serialized),
    }
    compact: Dict[str, Any] = {"summary": summary}

    if "error" in result:
        compact["error"] = result["error"]

    if "matched_instance" in result:
        compact["matched_instance"] = _reduce_instance(result["matched_instance"])

    def remaining_budget() -> int:
        used = len(json.dumps(compact, ensure_ascii=True, sort_keys=True))
        return max(CONTEXT_COMPACTION_LIMIT - used - 256, 512)

    if isinstance(result.get("plans"), list):
        plans = result["plans"]
        summary["total_plans"] = len(plans)
        summary["plan_names"] = [
            item.get("plan_name") for item in plans[:3] if isinstance(item, dict)
        ]
        previews = [
            {
                "plan_name": item.get("plan_name"),
                "service_count": len(item.get("services", [])),
            }
            for item in plans
            if isinstance(item, dict)
        ]
        kept, omitted = _fit_items(previews, remaining_budget())
        compact["plans"] = kept
        if omitted:
            summary["plans_omitted"] = omitted

    if isinstance(result.get("services"), list):
        services = result["services"]
        summary["total_services"] = len(services)
        reduced = [_reduce_service(item) for item in services]
        kept, omitted = _fit_items(reduced, remaining_budget())
        compact["services"] = kept
        if omitted:
            summary["services_omitted"] = omitted

    if isinstance(result.get("instances"), list):
        instances = result["instances"]
        summary["total_instances"] = len(instances)
        reduced = [_reduce_instance(item) for item in instances]
        kept, omitted = _fit_items(reduced, remaining_budget())
        compact["instances"] = kept
        if omitted:
            summary["instances_omitted"] = omitted

    return compact

# ---------------------------------------------------------------------------
# Response parsing helpers (no state — kept as module-level functions)
# ---------------------------------------------------------------------------


def _extract_tool_calls(response: Any) -> List[Any]:
    calls = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", "") == "function_call":
            calls.append(item)
    return calls


def _extract_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return text
    parts = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", "") == "message":
            for content in getattr(item, "content", []):
                if getattr(content, "type", "") == "output_text":
                    parts.append(getattr(content, "text", ""))
    return "\n".join(parts).strip()
