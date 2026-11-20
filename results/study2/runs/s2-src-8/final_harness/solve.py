"""Text-to-SQL harness: draft -> execute -> (optional) repair with evidence.

Contract (fixed): solve(task, env) -> {"sql": str}
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
STR_LIT = re.compile(r"'((?:[^']|'')*)'")


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    t = (text or "").strip()
    m = re.search(r"(?is)\b(select|with)\b.*", t)
    if m:
        t = m.group(0)
    return t.rstrip(";").strip()


def _tables(sql):
    names = re.findall(r"(?i)\b(?:from|join)\s+([A-Za-z_][A-Za-z_0-9]*)", sql or "")
    out = []
    for n in names:
        if n.lower() not in ("select",) and n not in out:
            out.append(n)
    return out


def _sample_values(env, sql, limit_tables=4, per_col=6):
    """Collect a few real values of text columns in tables the draft touches."""
    lines = []
    for tbl in _tables(sql)[:limit_tables]:
        probe = env.execute(f'SELECT * FROM "{tbl}" LIMIT 3', max_rows=3)
        if not probe.get("ok"):
            continue
        cols = probe.get("columns") or []
        rows = probe.get("rows") or []
        text_idx = [i for i, c in enumerate(cols)
                    if any(isinstance(r[i], str) for r in rows if i < len(r))]
        for i in text_idx[:8]:
            col = cols[i]
            res = env.execute(
                f'SELECT DISTINCT "{col}" FROM "{tbl}" WHERE "{col}" IS NOT NULL LIMIT {per_col}',
                max_rows=per_col)
            if res.get("ok") and res.get("rows"):
                vals = ", ".join(repr(r[0]) for r in res["rows"])
                lines.append(f"{tbl}.{col}: {vals}")
    return "\n".join(lines[:24])


def _feedback(env, sql, res):
    parts = []
    if not res.get("ok"):
        parts.append(f"The query FAILED with error: {res.get('error')}")
    else:
        n = res.get("n_rows", 0)
        parts.append(f"The query executed but returned {n} rows.")
        if n:
            parts.append(f"Columns: {res.get('columns')}")
            parts.append(f"First rows: {res.get('rows')[:5]}")
        else:
            parts.append(
                "Zero rows usually means a filter is too strict: a literal does not match the "
                "stored text exactly (case, spacing, suffixes), the wrong column was compared, "
                "or a join key is wrong. Below are real sample values from the tables you used:")
            samples = _sample_values(env, sql)
            if samples:
                parts.append(samples)
    parts.append(
        "Re-examine the question and schema, fix the cause, and return ONE corrected SQLite query "
        "in a single ```sql block. Prefer case-insensitive / substring matching (LIKE '%...%' or "
        "COLLATE NOCASE) when the stored spelling may differ. Keep the selected columns exactly "
        "what the question asks for. If after checking you believe the original query is already "
        "correct, return it unchanged.")
    return "\n".join(parts)


def solve(task, env):
    user = (
        f"Database: {task['db_id']}\n\nSchema:\n{env.schema(task['db_id'])}\n\nQuestion: {task['question']}"
    )
    desc = ""
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        desc = ""
    if desc.strip():
        user += f"\n\nColumn descriptions:\n{desc.strip()[:2000]}"
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"

    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user},
    ]
    max_tokens = env.config.get("max_tokens", 800)
    reply = env.chat(messages, max_tokens=max_tokens)
    sql = extract_sql(reply)
    if not sql:
        return {"sql": sql}

    res = env.execute(sql, max_rows=20)
    needs_repair = (not res.get("ok")) or res.get("n_rows", 0) == 0
    if not needs_repair:
        return {"sql": sql}

    messages.append({"role": "assistant", "content": reply})
    messages.append({"role": "user", "content": _feedback(env, sql, res)})
    try:
        reply2 = env.chat(messages, max_tokens=max_tokens)
    except Exception:
        return {"sql": sql}
    sql2 = extract_sql(reply2)
    if not sql2:
        return {"sql": sql}
    res2 = env.execute(sql2, max_rows=20)
    # Accept the repair if it runs and is non-empty, or if the original errored.
    if res2.get("ok") and (res2.get("n_rows", 0) > 0 or not res.get("ok")):
        return {"sql": sql2}
    if res2.get("ok") and not res.get("ok"):
        return {"sql": sql2}
    return {"sql": sql}