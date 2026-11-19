"""Load an evolvable harness directory into an AgentConfig.

A harness directory (the repository's `harness/` is H0) contains:

    system_prompt.md   the system prompt
    tool_policy.md     optional extra policy text, appended to the system prompt when non-empty
    config.json        loop knobs: max_steps, max_final_checks, max_tool_result_chars
    hooks.py           optional functions / CLIENT_TOOLS (see agent/hooks.py); missing names fall back
                       to pass-through defaults
    __init__.py        makes the directory a package, so hooks.py may import sibling modules

Everything else (model, temperature, timeouts, retries, the tool allow-list, the MCP server, the
scorer) is the fixed environment. Knobs are clamped to environment caps, so a harness cannot buy
itself more steps than the environment allows.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .hooks import ClientTool, DefaultHooks
from .loop import MAX_TOOL_RESULT_CHARS, AgentConfig

# Environment caps on the harness knobs (the harness may lower them, never raise them).
CAPS: dict[str, tuple[int, int]] = {
    "max_steps": (1, 12),
    "max_final_checks": (0, 2),
    "max_tool_result_chars": (500, 8000),
}
DEFAULTS: dict[str, int] = {"max_steps": 8, "max_final_checks": 0, "max_tool_result_chars": MAX_TOOL_RESULT_CHARS}
HOOK_NAMES = ("user_message", "tool_description", "before_tool_call", "format_tool_result", "after_final")


class HarnessError(Exception):
    """The harness directory is malformed (missing prompt, bad config.json, bad CLIENT_TOOLS)."""


class ModuleHooks(DefaultHooks):
    """Adapts a hooks.py module: functions it defines override the pass-through defaults."""

    def __init__(self, module: ModuleType | None) -> None:
        self.module = module
        tools: list[ClientTool] = []
        for spec in getattr(module, "CLIENT_TOOLS", None) or []:
            try:
                tools.append(
                    ClientTool(str(spec["name"]), str(spec["description"]), dict(spec["parameters"]), spec["fn"])
                )
            except (KeyError, TypeError) as exc:
                raise HarnessError(f"bad CLIENT_TOOLS entry: {exc!r}") from exc
            if not callable(tools[-1].fn):
                raise HarnessError(f"CLIENT_TOOLS entry {tools[-1].name!r} has no callable fn")
        self.client_tools = tuple(tools)

    def __getattribute__(self, name: str) -> Any:
        if name in HOOK_NAMES:
            module = object.__getattribute__(self, "module")
            fn = getattr(module, name, None) if module is not None else None
            if callable(fn):
                return fn
        return object.__getattribute__(self, name)


@dataclass(frozen=True)
class LoadedHarness:
    path: Path
    system_prompt: str
    knobs: dict[str, int]
    hooks: ModuleHooks

    def agent_config(self, **env: Any) -> AgentConfig:
        """AgentConfig for this harness; `env` carries environment-owned fields (timeouts, allow-list)."""
        return AgentConfig(
            system_prompt=self.system_prompt,
            max_steps=self.knobs["max_steps"],
            max_final_checks=self.knobs["max_final_checks"],
            max_tool_result_chars=self.knobs["max_tool_result_chars"],
            hooks=self.hooks,
            **env,
        )


def _clamp(name: str, value: Any) -> int:
    lo, hi = CAPS[name]
    try:
        v = int(value)
    except (TypeError, ValueError) as exc:
        raise HarnessError(f"config.json: {name} must be an integer") from exc
    return max(lo, min(hi, v))


def _import_hooks(path: Path) -> ModuleType | None:
    if not (path / "hooks.py").is_file():
        return None
    # A unique package name per directory, so two harness checkouts never share module state.
    name = "harness_" + hashlib.sha1(str(path).encode()).hexdigest()[:12]
    init = path / "__init__.py"
    if init.is_file():
        spec = importlib.util.spec_from_file_location(name, init, submodule_search_locations=[str(path)])
        if spec is None or spec.loader is None:
            raise HarnessError(f"cannot import {path}")
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[name] = pkg
        spec.loader.exec_module(pkg)
        return importlib.import_module(f"{name}.hooks")
    spec = importlib.util.spec_from_file_location(f"{name}_hooks", path / "hooks.py")
    if spec is None or spec.loader is None:
        raise HarnessError(f"cannot import {path / 'hooks.py'}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_harness(path: str | Path, *, prompt_override: str | Path | None = None) -> LoadedHarness:
    """Load a harness directory. `prompt_override` swaps in another system prompt file (used to
    evaluate the hand-tuned prompt as the HAND reference on top of H0's other components)."""
    path = Path(path).resolve()
    prompt_file = Path(prompt_override) if prompt_override else path / "system_prompt.md"
    if not prompt_file.is_file():
        raise HarnessError(f"missing system prompt {prompt_file}")
    prompt = prompt_file.read_text()  # verbatim, exactly as the prompts/*.md configs send it
    policy_file = path / "tool_policy.md"
    policy = policy_file.read_text().strip() if policy_file.is_file() else ""
    if policy:
        prompt = f"{prompt.rstrip()}\n\n{policy}\n"
    raw: dict[str, Any] = {}
    if (path / "config.json").is_file():
        try:
            raw = json.loads((path / "config.json").read_text())
        except json.JSONDecodeError as exc:
            raise HarnessError(f"config.json is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise HarnessError("config.json must be a JSON object")
    knobs = {k: _clamp(k, raw.get(k, DEFAULTS[k])) for k in DEFAULTS}
    return LoadedHarness(path, prompt, knobs, ModuleHooks(_import_hooks(path)))
