"""Text-to-SQL harness: draft query, execute-check, one repair call if needed.

Contract (fixed): solve(task, env) -> {"sql": str}
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
IDENT = re.compile(r"\b(?:FROM|JOIN)\s+[\"'`\[]?([A-Za-z_][A-Za-z0-9_]*)", re.I)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    return (text or "").strip().rstrip(";").strip()


def _tables(sql):
    seen = []
    for t in IDENT.findall(sql or ""):
        if t.lower() not in ("select",) and t not in seen:
            seen.append(t)
    return seen


def _samples(env, sql, limit_tables=3):
    """Small value samples from tables referenced by the draft, to expose real stored values."""
    out = []
    for t in _tables(sql)[:limit_tables]:
        try:
            r = env.execute(f'SELECT * FROM "{t}" LIMIT 3', max_rows=3)
        except Exception:
            continue
        if r.get("ok") and r.get("rows"):
            cols = ", ".join(r.get("columns") or [])
            rows = "; ".join(str(row) for row in r["rows"])
            out.append(f"Table {t} ({cols}): {rows}")
    return "\n".join(out)


def solve(task, env):
    user = (
        f"Database: {task['db_id']}\n\nSchema:\n{env.schema(task['db_id'])}\n\nQuestion: {task['question']}"
    )
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"
    desc = ""
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        pass
    if desc:
        user += f"\nColumn descriptions:\n{desc[:2000]}"

    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user},
    ]
    reply = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
    sql = extract_sql(reply)

    # Verify draft; repair once on error or empty result.
    try:
        res = env.execute(sql, max_rows=5)
    except Exception:
        res = {"ok": False, "error": "execution failed", "rows": [], "n_rows": 0}

    needs_repair = (not res.get("ok")) or res.get("n_rows", 0) == 0
    if needs_repair:
        if not res.get("ok"):
            feedback = f"The query failed with error: {res.get('error')}"
        else:
            feedback = (
                "The query executed but returned 0 rows. This usually means a string literal "
                "does not match stored values (case/spacing/format), a wrong join key, or an "
                "over-restrictive filter. It may also mean the projection is wrong: if the "
                "question asks about entities (e.g. 'which countries'), return the entity's "
                "full row (SELECT T1.*) rather than only a name column."
            )
        samples = _samples(env, sql)
        repair_user = (
            f"{feedback}\n\nYour draft query:\n```sql\n{sql}\n```\n"
            + (f"\nSample rows from the referenced tables (use these to match exact stored values):\n{samples}\n" if samples else "")
            + "\nRe-examine the question and schema above, then output ONE corrected SQLite query "
              "in a single ```sql code block. Keep the projection faithful to what the question asks."
        )
        messages = messages + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": repair_user},
        ]
        try:
            reply2 = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
            sql2 = extract_sql(reply2)
            if sql2:
                try:
                    res2 = env.execute(sql2, max_rows=5)
                except Exception:
                    res2 = {"ok": False}
                # Prefer the repaired query if it runs; keep draft if repair errors.
                if res2.get("ok"):
                    sql = sql2
        except Exception:
            pass

    return {"sql": sql}