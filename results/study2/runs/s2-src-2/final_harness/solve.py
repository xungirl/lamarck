"""Text-to-SQL harness with execution-gated repair.

Contract (fixed): solve(task, env) -> {"sql": str}
Flow: one draft call -> execute it -> if it errors or returns zero rows,
one targeted repair call (max_calls = 2). Passing drafts are untouched.
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    return (text or "").strip().rstrip(";").strip()


def _base_user(task, env):
    user = (
        f"Database: {task['db_id']}\n\nSchema:\n{env.schema(task['db_id'])}\n\nQuestion: {task['question']}"
    )
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"
    return user


REPAIR_GUIDE = """Your previous query did not produce a usable result. Diagnose and fix it.

Common causes of an empty or wrong result:
1. String literals rarely match stored values exactly. The question may contain stray trailing/leading spaces, different capitalization, or an abbreviation of the stored value. Trim whitespace inside quotes, and prefer case-insensitive substring matching, e.g. WHERE col LIKE '%value%' (SQLite LIKE is case-insensitive for ASCII), instead of exact equality, unless you are certain of the stored form.
2. Join keys may be wrong or reversed: check which column actually references which table using the schema's FOREIGN KEY clauses; text columns sometimes store the referenced value directly rather than an id.
3. Filters on flags/categories may use different stored codes than you assumed (e.g. 'T'/'F', 'Yes'/'No', full words vs abbreviations).
4. If the question asks for "details", "information", or about entities such as countries/people/items (rather than explicitly naming specific attributes like names or ids), select all columns of the relevant tables (e.g. SELECT T1.* or SELECT *) instead of a single name column.

Rewrite the query so it truly answers the question. Keep the same intent; do not drop filters just to get rows, and do not invent tables or columns. Return the corrected query in a single ```sql code block."""


def solve(task, env):
    system = env.prompt("system_prompt.md")
    user = _base_user(task, env)
    max_tokens = env.config.get("max_tokens", 800)

    reply = env.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens,
    )
    sql = extract_sql(reply)

    try:
        res = env.execute(sql)
    except Exception:
        res = {"ok": False, "error": "execution failed", "rows": [], "n_rows": 0}

    needs_repair = (not res.get("ok")) or res.get("n_rows", 0) == 0
    if not needs_repair:
        return {"sql": sql}

    if res.get("ok"):
        feedback = "The query executed successfully but returned 0 rows."
    else:
        feedback = f"The query failed with error: {res.get('error', 'unknown error')}"

    repair_user = (
        user
        + "\n\nYour previous query:\n```sql\n"
        + sql
        + "\n```\n\n"
        + feedback
        + "\n\n"
        + REPAIR_GUIDE
    )
    try:
        reply2 = env.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": repair_user},
            ],
            max_tokens=max_tokens,
        )
        sql2 = extract_sql(reply2)
        if sql2:
            res2 = env.execute(sql2)
            # Accept the repair only if it is executable; prefer non-empty results.
            if res2.get("ok") and (res2.get("n_rows", 0) > 0 or not res.get("ok")):
                return {"sql": sql2}
            if res2.get("ok") and res.get("ok"):
                # Both empty: keep the repaired version (better literal handling).
                return {"sql": sql2}
    except Exception:
        pass
    return {"sql": sql}