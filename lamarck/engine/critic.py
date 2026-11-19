"""Deterministic pre-evaluation screen (the cheap half of RRSI's critic).

Runs on the candidate's diff and files before any evaluation budget is spent:

  * leakage denylist: generic (credentials) plus the domain's patterns (task ids,
    evolve-set entity names, database names, ...); checked on ADDED lines only, so a
    candidate is not blamed for text the incumbent already had
  * Python files must compile
  * imports in harness Python files must come from a small allowlist (the harness
    may not open sockets, spawn processes or read the filesystem outside itself)
  * the declared number of independent edits must respect the edit budget b_t

The LLM half of the screen (intent: degenerate edits, grader gaming, bundling,
unbounded loops) is in lamarck.engine.selector.
"""

from __future__ import annotations

import ast
import re

GENERIC_PATTERNS = [
    (r"sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{35}|DASHSCOPE_API_KEY", "credential reference"),
    (r"/data/|LAMARCK_(?:OUT|JOB|TOKEN|GATEWAY)", "reaches outside the harness contract"),
]
ALLOWED_IMPORTS = {
    "re", "json", "math", "string", "textwrap", "itertools", "functools", "collections",
    "dataclasses", "typing", "difflib", "datetime", "statistics", "decimal", "fractions",
    "unicodedata", "hashlib", "copy", "enum", "__future__",
}  # fmt: skip


def added_lines(diff: str) -> str:
    return "\n".join(
        line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
    )


def check_python(name: str, source: str) -> list[str]:
    problems = []
    try:
        tree = ast.parse(source, filename=name)
    except SyntaxError as exc:
        return [f"{name} does not compile: {exc.msg} (line {exc.lineno})"]
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import inside the harness package
            mods = [node.module or ""]
        for m in mods:
            if m.split(".")[0] not in ALLOWED_IMPORTS:
                problems.append(f"{name} imports {m!r}, outside the harness allowlist")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"open", "exec", "eval", "__import__", "compile"}
        ):
            problems.append(f"{name} calls {node.func.id}()")
    return problems


def screen(
    diff: str,
    new_files: dict[str, str | None],
    n_edits: int,
    budget: int | None,
    domain_patterns: list[tuple[str, str]],
) -> list[str]:
    """Reasons to reject the candidate (empty list = passes the deterministic screen)."""
    reasons: list[str] = []
    if not diff.strip():
        reasons.append("empty diff (no-op candidate)")
    added = added_lines(diff)
    for pat, why in GENERIC_PATTERNS + domain_patterns:
        m = re.search(pat, added)
        if m:
            reasons.append(f"{why}: {m.group(0)[:60]!r}")
    for name, src in new_files.items():
        if src is not None and name.endswith(".py"):
            reasons.extend(check_python(name, src))
    if budget is not None and n_edits > budget:
        reasons.append(f"declares {n_edits} independent edits, budget b_t = {budget}")
    return reasons
