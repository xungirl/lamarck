"""Text-to-SQL harness: draft query, execute it, and spend the second policy
call on a repair turn when the draft errors, returns no rows, or looks cut off.

Contract (fixed): solve(task, env) -> {"sql": str}
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
START_KW = re.compile(r"^\s*(WITH|SELECT)\b", re.I)


def extract_sql(text):
    text = text or ""
    blocks = [b.strip() for b in SQL_BLOCK.findall(text) if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    # Unclosed code fence (truncated generation): take what follows the fence.
    m = re.search(r"```(?:sql|sqlite)?\s*(.*)$", text, re.S | re.I)
    if m and START_KW.match(m.group(1)):
        return m.group(1).strip().rstrip(";").strip()
    # Bare SQL somewhere in the reply.
    m = re.search(r"(?:^|\n)\s*(WITH|SELECT)\b", text, re.I)
    if m:
        return text[m.start():].strip().rstrip(";").strip()
    return text.strip().rstrip(";").strip()


def looks_truncated(sql):
    if not sql:
        return True
    if sql.count("(") != sql.count(")"):
        return True
    if sql.count("'") % 2 == 1 or sql.count('"') % 2 == 1:
        return True
    if re.search(r"\b(SELECT|FROM|WHERE|AND|OR|BY|ON|JOIN|HAVING|LIMIT|AS|IN|LIKE|=|<|>|,)\s*$", sql, re.I):
        return True
    return False


def build_user(task, env):
    user = (
        f"Database: {task['db_id']}\n\nSchema:\n{env.schema(task['db_id'])}"
    )
    desc = ""
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        desc = ""
    if desc.strip():
        user += f"\n\nColumn descriptions:\n{desc.strip()[:4000]}"
    user += f"\n\nQuestion: {task['question']}"
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"
    return user


def repair_message(sql, res, truncated):
    lines = ["Your previous draft was:", "```sql", sql, "```"]
    if truncated:
        lines.append("It appears incomplete or malformed (cut off mid-statement). Rewrite it as one short, complete query.")
    if res is None:
        lines.append("It could not be executed.")
    elif not res.get("ok"):
        lines.append(f"Execution FAILED with error: {res.get('error')}")
        lines.append("Fix the syntax or the table/column names (use only names present in the schema).")
    elif res.get("n_rows", 0) == 0:
        lines.append("It executed successfully but returned 0 rows.")
        lines.append(
            "The question almost certainly has a non-empty answer, so a filter value or join is probably wrong. "
            "Common causes: string literals that do not exactly match stored values (case, spacing, abbreviations, "
            "suffixes like 'Bachelors' vs 'Bachelor'), matching the wrong column, case-sensitive joins on text keys, "
            "or an over-restrictive combination of conditions. "
            "If needed, first run a small probe query such as SELECT DISTINCT <column> FROM <table> LIMIT 20 "
            "to see the actual stored values, then write the corrected final query using them."
        )
    else:
        lines.append(f"It returned {res.get('n_rows')} rows; columns: {res.get('columns')}.")
        lines.append("Re-check that it answers the question exactly (right columns, right comparison semantics).")
    lines.append(
        "Reply with ONLY the corrected final query in a single ```sql code block. "
        "Keep it minimal: select exactly the columns asked for, nothing extra."
    )
    return "\n".join(lines)


def solve(task, env):
    max_tokens = env.config.get("max_tokens", 800)
    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": build_user(task, env)},
    ]
    reply = env.chat(messages, max_tokens=max_tokens)
    sql = extract_sql(reply)

    truncated = looks_truncated(sql)
    res = None
    try:
        res = env.execute(sql, max_rows=20)
    except Exception:
        res = None

    need_repair = truncated or res is None or not res.get("ok") or res.get("n_rows", 0) == 0
    if not need_repair:
        return {"sql": sql}

    messages.append({"role": "assistant", "content": reply})
    messages.append({"role": "user", "content": repair_message(sql, res, truncated)})
    try:
        reply2 = env.chat(messages, max_tokens=max_tokens)
    except Exception:
        return {"sql": sql}
    sql2 = extract_sql(reply2)
    if not sql2:
        return {"sql": sql}

    # If the model used the repair turn as a probe query, inspect its output
    # locally (no extra policy call) so the choice below is informed.
    res2 = None
    try:
        res2 = env.execute(sql2, max_rows=20)
    except Exception:
        res2 = None

    def usable(r):
        return r is not None and r.get("ok") and r.get("n_rows", 0) > 0

    if usable(res2) or not usable(res):
        # Prefer the repaired query unless it is unusable and the draft worked.
        if looks_truncated(sql2) and not truncated:
            return {"sql": sql}
        return {"sql": sql2}
    return {"sql": sql}