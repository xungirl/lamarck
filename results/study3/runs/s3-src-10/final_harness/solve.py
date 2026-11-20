"""Text-to-SQL harness: draft -> execute -> optional single repair call.

Contract (fixed): solve(task, env) -> {"sql": str}
  task  {"id", "db_id", "question", "hint"}      (hint is BIRD's evidence; "" for Spider)
  env.chat(messages, max_tokens=None) -> str     policy call (capped per task: env.max_calls)
  env.schema(db_id) -> str                       CREATE statements of the database
  env.execute(sql, max_rows=20) -> dict          read-only run: {ok, columns, rows, n_rows, error}
  env.descriptions(db_id) -> str                 column descriptions if the dataset ships them, else ""
  env.prompt(name) -> str                        text of a file in this harness (e.g. "system_prompt.md")
  env.config -> dict                             parsed config.json
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    raw = (text or "").strip()
    # Fall back to the last statement-looking line if prose surrounds bare SQL.
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if re.match(r"(?i)^(select|with)\b", ln):
            return "\n".join(lines[i:]).rstrip(";").strip()
    return raw.rstrip(";").strip()


def _fmt_rows(res):
    try:
        cols = ", ".join(str(c) for c in res.get("columns") or [])
        rows = res.get("rows") or []
        head = "; ".join(str(r) for r in rows[:5])
        return f"columns: [{cols}]\nfirst rows: {head}"
    except Exception:
        return ""


def solve(task, env):
    cfg = env.config or {}
    max_tokens = cfg.get("max_tokens", 800)

    user = (
        f"Database: {task['db_id']}\n\nSchema:\n{env.schema(task['db_id'])}\n\nQuestion: {task['question']}"
    )
    desc = ""
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        desc = ""
    if desc.strip():
        user += f"\n\nColumn descriptions:\n{desc.strip()[:4000]}"
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"

    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user},
    ]
    reply = env.chat(messages, max_tokens=max_tokens)
    sql = extract_sql(reply)
    if not sql:
        return {"sql": sql}

    # Execution check: only spend the second call when something looks wrong.
    try:
        res = env.execute(sql, max_rows=20)
    except Exception:
        res = None

    if not isinstance(res, dict):
        return {"sql": sql}

    ok = res.get("ok")
    n_rows = res.get("n_rows")
    if ok and n_rows:
        return {"sql": sql}  # plausible answer, keep the draft

    if ok and not n_rows:
        feedback = (
            "Your query executed successfully but returned ZERO rows. For this question an empty "
            "result is almost certainly wrong. Likely causes: a string literal that does not match "
            "the stored values (case, trailing/leading spaces, abbreviations, partial text), an "
            "over-restrictive WHERE, or a wrong join key/column.\n"
            "Re-check the literal values against the schema and question wording: prefer LIKE with "
            "%...% around the distinctive part of the text (trim stray spaces), and compare "
            "case-insensitively when unsure. Also reconsider whether the question expects all "
            "columns of an entity rather than one attribute.\n"
            "Return ONE corrected SQLite query in a single ```sql code block."
        )
    else:
        feedback = (
            f"Your query failed with error: {res.get('error')}\n"
            "Fix the table/column names and syntax using the schema exactly as given. "
            "Return ONE corrected SQLite query in a single ```sql code block."
        )

    messages.append({"role": "assistant", "content": reply})
    messages.append({"role": "user", "content": feedback})
    try:
        reply2 = env.chat(messages, max_tokens=max_tokens)
    except Exception:
        return {"sql": sql}
    sql2 = extract_sql(reply2)
    if not sql2:
        return {"sql": sql}

    # Accept the repair unless it is clearly worse than a working draft.
    try:
        res2 = env.execute(sql2, max_rows=20)
    except Exception:
        res2 = None
    if isinstance(res2, dict) and not res2.get("ok") and ok:
        return {"sql": sql}
    return {"sql": sql2}