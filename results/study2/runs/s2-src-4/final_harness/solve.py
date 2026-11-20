"""Text-to-SQL harness: draft -> execute -> targeted repair.

Contract (fixed): solve(task, env) -> {"sql": str}
  task  {"id", "db_id", "question", "hint"}      (hint is BIRD's evidence; "" for Spider)
  env.chat(messages, max_tokens=None) -> str     one policy call (capped per task: env.max_calls)
  env.schema(db_id) -> str                       CREATE statements of the database
  env.execute(sql, max_rows=20) -> dict          read-only run: {ok, columns, rows, n_rows, error}
  env.descriptions(db_id) -> str                 column descriptions if the dataset ships them, else ""
  env.prompt(name) -> str                        text of a file in this harness (e.g. "system_prompt.md")
  env.config -> dict                             parsed config.json
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
LITERAL = re.compile(r"'((?:[^']|'')*)'")


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    return (text or "").strip().rstrip(";").strip()


def _literals(sql):
    seen = []
    for m in LITERAL.finditer(sql):
        v = m.group(1).replace("''", "'")
        if v and v not in seen:
            seen.append(v)
    return seen[:4]


def _probe(env, sql):
    """Cheap diagnostics for a draft that errored or returned no rows."""
    lines = []
    lits = _literals(sql)
    # Find equality comparisons involving literals to locate the filtered column.
    col_pat = re.compile(r"([A-Za-z_][\w.]*)\s*(?:=|LIKE)\s*'((?:[^']|'')*)'", re.I)
    cols = {}
    for m in col_pat.finditer(sql):
        col, val = m.group(1), m.group(2).replace("''", "'")
        if val in lits:
            cols.setdefault(val, col)
    from_m = re.search(r"\bFROM\s+([A-Za-z_]\w*)", sql, re.I)
    base = from_m.group(1) if from_m else None
    for val in lits:
        col = cols.get(val)
        target = col if col else None
        if target is None and base:
            continue
        esc = val.replace("'", "''")
        like = f"%{esc.strip()}%"
        try:
            r = env.execute(
                f"SELECT {target} FROM {base} WHERE {target} LIKE '{like}' LIMIT 5",
                max_rows=5,
            )
            if r.get("ok") and r.get("rows"):
                vals = [str(row[0]) for row in r["rows"]]
                lines.append(f"- Values in {target} resembling {val!r}: {vals}")
            elif r.get("ok"):
                r2 = env.execute(f"SELECT DISTINCT {target} FROM {base} LIMIT 8", max_rows=8)
                if r2.get("ok") and r2.get("rows"):
                    vals = [str(row[0]) for row in r2["rows"]]
                    lines.append(f"- No match for {val!r}; sample values of {target}: {vals}")
        except Exception:
            pass
    return "\n".join(lines[:6])


def solve(task, env):
    schema = env.schema(task["db_id"])
    user = f"Database: {task['db_id']}\n\nSchema:\n{schema}\n\nQuestion: {task['question']}"
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"
    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user},
    ]
    reply = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
    sql = extract_sql(reply)

    # Verify by execution; repair only on clear failure signals.
    try:
        res = env.execute(sql, max_rows=20)
    except Exception:
        res = {"ok": False, "error": "execution failed", "rows": [], "n_rows": 0}

    needs_repair = (not res.get("ok")) or res.get("n_rows", 0) == 0
    if needs_repair:
        if res.get("ok"):
            feedback = "The query executed successfully but returned ZERO rows."
        else:
            feedback = f"The query failed with error: {res.get('error', 'unknown')}."
        probes = ""
        try:
            probes = _probe(env, sql)
        except Exception:
            probes = ""
        repair_user = (
            f"{feedback}\n\nYour draft was:\n```sql\n{sql}\n```\n"
            + (f"\nDiagnostics from the database:\n{probes}\n" if probes else "")
            + "\nCommon causes: string literals differ in case or have trailing/leading spaces "
            "(use LIKE '%value%' or TRIM); the filter is on the wrong column; a join key or "
            "direction is wrong; an exclusion subquery checks only one side of a symmetric "
            "relation. Rewrite the query so it correctly answers the question. "
            "Return the corrected query in a single ```sql code block."
        )
        try:
            reply2 = env.chat(
                messages
                + [
                    {"role": "assistant", "content": f"```sql\n{sql}\n```"},
                    {"role": "user", "content": repair_user},
                ],
                max_tokens=env.config.get("max_tokens", 800),
            )
            sql2 = extract_sql(reply2)
            if sql2:
                try:
                    res2 = env.execute(sql2, max_rows=20)
                except Exception:
                    res2 = {"ok": False}
                # Prefer the repaired query if it runs; keep draft only if repair fails to execute.
                if res2.get("ok") or not res.get("ok"):
                    sql = sql2
        except Exception:
            pass

    return {"sql": sql}