"""A hand-written tool-calling agent loop (no framework).

    messages = [system, user]
    repeat up to max_steps:
        response = LLM(messages, tools)            # with timeout + retry
        if response has no tool calls: return its text  (stop_reason="final")
        for each tool call:
            result = execute(call)                 # validate, permission check, timeout + retry
            messages += tool result
    stop_reason="max_steps"

The whole run is also wrapped in a wall-clock deadline (stop_reason="timeout").
Every LLM call and tool call is written to the trace.

Optional harness hooks (hooks.py, used by the RRSI harness search) can wrap the user message,
rewrite tool descriptions, block a tool call, reformat tool results, add local client tools and
ask for one more look at a final answer. With `hooks=None` (the default) none of that code runs.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from .hooks import Hooks
from .providers import LLMError, LLMProvider, TransientLLMError
from .tools import ToolExecutor, TransientToolError
from .trace import Tracer
from .types import LLMResponse, Message, ToolCall, ToolResult, ToolSpec, Usage

StopReason = Literal["final", "max_steps", "timeout", "error"]

MAX_TOOL_RESULT_CHARS = 4000  # keep one huge tool result from blowing up the context window


@dataclass(frozen=True)
class AgentConfig:
    system_prompt: str
    max_steps: int = 8
    llm_timeout_s: float = 60.0
    tool_timeout_s: float = 15.0
    run_timeout_s: float = 180.0
    max_retries: int = 2  # extra attempts after the first, for transient errors only
    backoff_base_s: float = 0.5
    allowed_tools: frozenset[str] | None = None  # None = every tool the server exposes
    hooks: Hooks | None = None  # harness hooks (see hooks.py); None = plain loop
    max_final_checks: int = 0  # how often after_final may send the model back (hooks only)
    max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS


@dataclass
class ToolCallRecord:
    step: int
    name: str
    arguments: dict[str, Any] | None  # None if the model sent invalid JSON
    is_error: bool
    result: str
    latency_ms: float
    attempts: int
    blocked: bool = False  # refused by the harness's before_tool_call hook, never forwarded
    client: bool = False  # a harness client tool, executed locally


@dataclass
class AgentResult:
    final_answer: str
    stop_reason: StopReason
    steps: int
    llm_calls: int
    tool_calls: list[ToolCallRecord]
    usage: Usage
    latency_ms: float
    error: str | None = None
    messages: list[Message] = field(default_factory=list)
    hook_errors: int = 0


async def with_retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    timeout_s: float,
    max_retries: int,
    backoff_base_s: float,
    transient: tuple[type[BaseException], ...],
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> tuple[T, int]:
    """Run `fn` with a per-attempt timeout; retry transient failures with exponential
    backoff and jitter. Returns (result, attempts). Non-transient errors propagate at once."""
    attempt = 0
    while True:
        attempt += 1
        try:
            async with asyncio.timeout(timeout_s):
                return await fn(), attempt
        except (TimeoutError, *transient) as exc:  # type: ignore[misc]
            if attempt > max_retries:
                raise
            if on_retry:
                on_retry(attempt, exc)
            await asyncio.sleep(backoff_base_s * 2 ** (attempt - 1) * (0.5 + random.random()))


class Agent:
    def __init__(
        self, provider: LLMProvider, tools: ToolExecutor, config: AgentConfig, tracer: Tracer | None = None
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.config = config
        self.tracer = tracer or Tracer()

    async def run(self, user_prompt: str) -> AgentResult:
        cfg = self.config
        t0 = time.perf_counter()
        specs = [s for s in await self.tools.list_tools() if cfg.allowed_tools is None or s.name in cfg.allowed_tools]
        state = _RunState(user_prompt=user_prompt)
        user_text = user_prompt
        if cfg.hooks is not None:
            specs = self._hooked_specs(specs, state)
            user_text = self._hook(state, "user_message", user_prompt, str, user_prompt)
        messages: list[Message] = [
            {"role": "system", "content": cfg.system_prompt},
            {"role": "user", "content": user_text},
        ]
        self.tracer.emit(
            "run_start",
            provider=self.provider.name,
            model=self.provider.model,
            prompt=user_prompt,
            tools=[s.name for s in specs],
            max_steps=cfg.max_steps,
        )
        try:
            async with asyncio.timeout(cfg.run_timeout_s):
                await self._loop(messages, specs, state)
        except TimeoutError:
            state.stop_reason, state.error = "timeout", f"run exceeded {cfg.run_timeout_s}s"
        except (LLMError, TransientLLMError) as exc:
            state.stop_reason, state.error = "error", str(exc)

        result = AgentResult(
            final_answer=state.final_answer,
            stop_reason=state.stop_reason,
            steps=state.steps,
            llm_calls=state.llm_calls,
            tool_calls=state.records,
            usage=state.usage,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            error=state.error,
            messages=messages,
            hook_errors=state.hook_errors,
        )
        self.tracer.emit(
            "run_end",
            stop_reason=result.stop_reason,
            steps=result.steps,
            llm_calls=result.llm_calls,
            tool_calls=len(result.tool_calls),
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            latency_ms=result.latency_ms,
            final_answer=result.final_answer,
            error=result.error,
        )
        return result

    async def _loop(self, messages: list[Message], specs: Sequence[ToolSpec], state: _RunState) -> None:
        for step in range(1, self.config.max_steps + 1):
            state.steps = step
            response = await self._call_llm(messages, specs, step, state)
            messages.append(response.to_message())
            if not response.tool_calls:
                answer = response.content or ""
                if (
                    self.config.hooks is not None
                    and state.final_checks < self.config.max_final_checks
                    and step < self.config.max_steps
                ):
                    nudge = self._hook(state, "after_final", None, (str, type(None)), answer, self._ctx(state, step))
                    if nudge and nudge.strip():
                        state.final_checks += 1
                        self.tracer.emit("final_check", step=step, answer=answer, nudge=nudge)
                        messages.append({"role": "user", "content": nudge})
                        continue
                state.final_answer, state.stop_reason = answer, "final"
                return
            # Sequential on purpose: write order matters and results are easier to reason about.
            for call in response.tool_calls:
                record = await self._execute(call, specs, step, state)
                state.records.append(record)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": record.result})
        state.stop_reason = "max_steps"

    async def _call_llm(
        self, messages: list[Message], specs: Sequence[ToolSpec], step: int, state: _RunState
    ) -> LLMResponse:
        cfg = self.config
        t0 = time.perf_counter()

        def on_retry(attempt: int, exc: BaseException) -> None:
            self.tracer.emit("llm_retry", step=step, attempt=attempt, error=f"{type(exc).__name__}: {exc}")

        async def attempt() -> LLMResponse:
            state.llm_calls += 1  # every provider request counts, including failed attempts (cost!)
            return await self.provider.complete(messages, specs)

        try:
            response, attempts = await with_retry(
                attempt,
                timeout_s=cfg.llm_timeout_s,
                max_retries=cfg.max_retries,
                backoff_base_s=cfg.backoff_base_s,
                transient=(TransientLLMError,),
                on_retry=on_retry,
            )
        except TimeoutError as exc:
            raise TransientLLMError(f"LLM call timed out after {cfg.llm_timeout_s}s") from exc
        state.usage.add(response.usage)
        self.tracer.emit(
            "llm_call",
            step=step,
            attempts=attempts,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            finish_reason=response.finish_reason,
            content=response.content,
            tool_calls=[{"name": c.name, "arguments": c.arguments} for c in response.tool_calls],
        )
        return response

    async def _execute(
        self, call: ToolCall, specs: Sequence[ToolSpec], step: int, state: _RunState | None = None
    ) -> ToolCallRecord:
        t0 = time.perf_counter()
        args: dict[str, Any] | None = None
        attempts = 0
        allowed = {s.name for s in specs}
        hooks = self.config.hooks
        blocked = client = False
        try:
            parsed = json.loads(call.arguments or "{}")
            if not isinstance(parsed, dict):
                raise ValueError("arguments must be a JSON object")
            args = parsed
        except ValueError as exc:
            result = ToolResult(True, f"invalid_arguments: could not parse JSON arguments ({exc})")
        else:
            if call.name not in allowed:
                # Defence in depth: never forward a call to a tool the model was not given.
                result = ToolResult(True, f"permission_denied: tool {call.name!r} is not available")
            elif hooks is not None and state is not None:
                block = self._hook(
                    state, "before_tool_call", None, (str, type(None)), call.name, dict(args), self._ctx(state, step)
                )
                local = {t.name: t for t in hooks.client_tools}
                if block and block.strip():
                    blocked = True
                    result = ToolResult(True, f"blocked_by_harness: {block}")
                elif call.name in local:
                    client = True
                    result = self._run_client_tool(local[call.name].fn, args, state)
                else:
                    result, attempts = await self._call_tool_with_retry(call.name, args, step)
            else:
                result, attempts = await self._call_tool_with_retry(call.name, args, step)

        text = result.text
        if hooks is not None and state is not None:
            text = self._hook(
                state,
                "format_tool_result",
                text,
                str,
                call.name,
                dict(args or {}),
                text,
                result.is_error,
                self._ctx(state, step),
            )
        record = ToolCallRecord(
            step=step,
            name=call.name,
            arguments=args,
            is_error=result.is_error,
            result=text[: self.config.max_tool_result_chars],
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            attempts=attempts,
            blocked=blocked,
            client=client,
        )
        self.tracer.emit(
            "tool_call",
            step=step,
            name=call.name,
            arguments=args,
            raw_arguments=call.arguments,
            is_error=record.is_error,
            result=record.result,
            latency_ms=record.latency_ms,
            attempts=attempts,
        )
        return record

    # ------------------------------------------------------------------ hooks (harness search only)

    def _hook(self, state: _RunState, name: str, default: Any, kind: Any, *args: Any) -> Any:
        """Call hook `name`; on an exception or a wrong return type record a hook_error and use `default`."""
        try:
            out = getattr(self.config.hooks, name)(*args)
            if not isinstance(out, kind):
                raise TypeError(f"{name} returned {type(out).__name__}")
            return out
        except Exception as exc:  # noqa: BLE001 - harness code is untrusted; the run must go on
            state.hook_errors += 1
            self.tracer.emit("hook_error", hook=name, error=f"{type(exc).__name__}: {exc}"[:500])
            return default

    def _hooked_specs(self, specs: list[ToolSpec], state: _RunState) -> list[ToolSpec]:
        assert self.config.hooks is not None
        out = [
            replace(s, description=self._hook(state, "tool_description", s.description, str, s.name, s.description))
            for s in specs
        ]
        names = {s.name for s in out}
        for t in self.config.hooks.client_tools:
            if t.name not in names:  # a client tool can never shadow a server tool
                out.append(ToolSpec(t.name, t.description, t.parameters, read_only=True))
                names.add(t.name)
        return out

    def _run_client_tool(
        self, fn: Callable[[dict[str, Any]], str], args: dict[str, Any], state: _RunState
    ) -> ToolResult:
        try:
            out = fn(dict(args))
            if not isinstance(out, str):
                raise TypeError(f"client tool returned {type(out).__name__}")
            return ToolResult(False, out[: self.config.max_tool_result_chars])
        except Exception as exc:  # noqa: BLE001
            state.hook_errors += 1
            self.tracer.emit("hook_error", hook="client_tool", error=f"{type(exc).__name__}: {exc}"[:500])
            return ToolResult(True, f"client_tool_error: {type(exc).__name__}: {exc}"[:500])

    def _ctx(self, state: _RunState, step: int) -> dict[str, Any]:
        """A fresh, plain-data view of the run for hooks."""
        return {
            "user_prompt": state.user_prompt,
            "step": step,
            "max_steps": self.config.max_steps,
            "calls": [
                {"name": r.name, "args": dict(r.arguments or {}), "is_error": r.is_error, "result": r.result[:2000]}
                for r in state.records
            ],
        }

    async def _call_tool_with_retry(self, name: str, args: dict[str, Any], step: int) -> tuple[ToolResult, int]:
        cfg = self.config

        def on_retry(attempt: int, exc: BaseException) -> None:
            self.tracer.emit("tool_retry", step=step, name=name, attempt=attempt, error=f"{type(exc).__name__}: {exc}")

        try:
            # Retrying a write is safe only because write tools are idempotent (issue_refund
            # takes an idempotency key and the retry resends the exact same arguments).
            return await with_retry(
                lambda: self.tools.call_tool(name, args),
                timeout_s=cfg.tool_timeout_s,
                max_retries=cfg.max_retries,
                backoff_base_s=cfg.backoff_base_s,
                transient=(TransientToolError,),
                on_retry=on_retry,
            )
        except (TimeoutError, TransientToolError) as exc:
            return ToolResult(True, f"tool_unavailable: {type(exc).__name__} after retries"), cfg.max_retries + 1


@dataclass
class _RunState:
    user_prompt: str = ""
    final_checks: int = 0
    hook_errors: int = 0
    final_answer: str = ""
    stop_reason: StopReason = "error"
    steps: int = 0
    llm_calls: int = 0
    records: list[ToolCallRecord] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
