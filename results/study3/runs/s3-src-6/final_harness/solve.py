"""Text-to-SQL harness: draft -> execute -> (repair once) -> answer.

Contract (fixed): solve(task, env) -> {"sql": str}
  task  {"id", "db_id", "question", "hint"}      (hint is BIRD's evidence; "" for Spider)
  env.chat(messages, max_tokens=None) -> str     policy call (capped per task: env.max_calls)
  env.schema(db_id) -> str                       CREATE statements of the database
  env.execute(sql, max_rows=20) -> dict          read-only run: {ok, columns, rows, n_rows, error}
  env.descriptions(db_id) -> str                 column descriptions if the dataset ships them, else ""
  env.prompt(name) -> str                        text of a file in this harness
  env.config -> dict                             parsed config.json
"""

import json
import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    t = (text or "").strip()
    m = re.search(r"(?is)\b(select|with)\b.*", t)
    if m:
        return m.group(0).rstrip(";").strip()
    return t.rstrip(";").strip()


def _run(env, sql):
    try:
        return env.execute(sql, max_rows=20)
    except Exception as exc:  # harness-side failure must not kill the task
        return {"ok": False, "columns": [], "rows": [], "n_rows": 0, "error": str(exc)}


def _feedback(res):
    if not res.get("ok"):
        return "Execution ERROR: %s" % (res.get("error") or "unknown error")
    rows = res.get("rows") or []
    cols = res.get("columns") or []
    if not rows:
        return ("Executed successfully but returned ZERO rows (columns: %s)."
                % json.dumps(cols))
    return ("Returned %s row(s); columns: %s; first rows: %s"
            % (res.get("n_rows", len(rows)), json.dumps(cols),
               json.dumps(rows[:5], default=str)))


REPAIR_RULES = """Your previous query did not produce a usable answer. Diagnose and fix it, then output ONE corrected SQLite query in a single ```sql block.

Checklist for an empty result (most common cause is an over-strict filter, not missing data):
- Text matching: stored values may differ in case, spacing, punctuation or wording from the question. Prefer case-insensitive substring matching, e.g. lower(col) LIKE lower('%term%'), instead of exact '=' when the question quotes a phrase loosely. Trim stray spaces inside the quoted term.
- Literal values: confirm the exact spelling/format actually stored (codes vs names, 'T'/'F' vs 1/0, date formats, numeric units). If unsure, inspect distinct values with a small probe query instead of guessing again.
- Joins: verify the foreign-key direction and the key columns against the schema; a reversed or wrong-column join silently yields nothing. Consider whether an inner join should be a LEFT JOIN / EXISTS.
- Filters combined with AND may each be individually fine but jointly impossible; relax the least essential condition.
For an execution error: fix the syntax, unknown column/table names (use the schema verbatim), or aggregate/GROUP BY misuse.

Keep the answer shape aligned with the question: do not add or drop requested columns while repairing, and keep DISTINCT / ORDER BY / LIMIT only when the question asks for them. An empty result is acceptable ONLY after you have verified the filters and joins are right."""


def solve(task, env):
    cfg = env.config or {}
    max_tokens = cfg.get("max_tokens", 1200)
    schema = env.schema(task["db_id"])
    user = "Database: %s\n\nSchema:\n%s\n\nQuestion: %s" % (
        task["db_id"], schema, task["question"])
    desc = ""
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        desc = ""
    if desc.strip():
        user += "\n\nColumn descriptions:\n" + desc.strip()
    if task.get("hint"):
        user += "\nHint: %s" % task["hint"]

    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user},
    ]
    reply = env.chat(messages, max_tokens=max_tokens)
    sql = extract_sql(reply)
    if not sql:
        return {"sql": sql}

    res = _run(env, sql)
    needs_repair = (not res.get("ok")) or not (res.get("rows") or [])
    if not needs_repair:
        return {"sql": sql}

    followup = (
        "Draft query:\n```sql\n%s\n```\n\nResult of running it: %s\n\n%s"
        % (sql, _feedback(res), REPAIR_RULES)
    )
    messages = messages + [
        {"role": "assistant", "content": reply},
        {"role": "user", "content": followup},
    ]
    try:
        reply2 = env.chat(messages, max_tokens=max_tokens)
    except Exception:
        return {"sql": sql}
    sql2 = extract_sql(reply2)
    if not sql2:
        return {"sql": sql}

    res2 = _run(env, sql2)
    # Prefer a query that actually returns data; never swap in a broken one.
    if res2.get("ok") and ((res2.get("rows") or []) or not res.get("ok")):
        return {"sql": sql2}
    if res2.get("ok") and not res.get("ok"):
        return {"sql": sql2}
    return {"sql": sql}