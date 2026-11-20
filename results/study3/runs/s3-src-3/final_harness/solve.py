"""Text-to-SQL harness: draft -> execute -> evidence-backed repair.

Contract (fixed): solve(task, env) -> {"sql": str}
  task  {"id", "db_id", "question", "hint"}      (hint is BIRD's evidence; "" for Spider)
  env.chat(messages, max_tokens=None) -> str     policy call (capped per task: env.max_calls)
  env.schema(db_id) -> str                       CREATE statements of the database
  env.execute(sql, max_rows=20) -> dict          read-only run: {ok, columns, rows, n_rows, error}
  env.descriptions(db_id) -> str                 column descriptions if the dataset ships them, else ""
  env.prompt(name) -> str                        text of a file in this harness
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
    vals = []
    for m in LITERAL.finditer(sql or ""):
        v = m.group(1).replace("''", "'").strip()
        if v and len(v) >= 3 and not v.isdigit() and v.upper() not in ("T", "F"):
            if v not in vals:
                vals.append(v)
    return vals[:3]


def _tables_and_columns(schema):
    """Return list of (table, [columns]) parsed from CREATE TABLE statements."""
    out = []
    for m in re.finditer(r"CREATE\s+TABLE\s+[`\[\"]?(\w+)[`\]\"]?\s*\((.*?)\)\s*;", schema or "", re.S | re.I):
        table, body = m.group(1), m.group(2)
        cols = []
        depth = 0
        cur = ""
        parts = []
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        for p in parts:
            p = p.strip()
            cm = re.match(r"[`\[\"]?(\w+)[`\]\"]?", p)
            if cm and cm.group(1).upper() not in (
                "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT", "KEY"
            ):
                cols.append(cm.group(1))
        out.append((table, cols))
    return out


def _probe_values(env, schema, sql):
    """Sample real stored values resembling the string literals used in the draft."""
    lits = _literals(sql)
    if not lits:
        return ""
    tables = _tables_and_columns(schema)
    notes = []
    budget = 6
    for lit in lits:
        core = re.sub(r"[^A-Za-z0-9 ]", " ", lit).split()
        key = max(core, key=len) if core else lit
        key = key[:24]
        found = False
        for table, cols in tables:
            if found or budget <= 0:
                break
            for col in cols:
                if budget <= 0:
                    break
                probe = (
                    f'SELECT DISTINCT "{col}" FROM "{table}" '
                    f"WHERE LOWER(\"{col}\") LIKE '%{key.lower()}%' LIMIT 5"
                )
                try:
                    r = env.execute(probe, max_rows=5)
                except Exception:
                    continue
                budget -= 1
                if r.get("ok") and r.get("rows"):
                    vals = [str(row[0]) for row in r["rows"]]
                    notes.append(
                        f"Literal '{lit}' does not match exactly. Values containing '{key}' "
                        f"in {table}.{col}: {vals}"
                    )
                    found = True
                    break
    return "\n".join(notes)


def _result_note(res):
    if not res.get("ok"):
        return f"ERROR: {res.get('error')}"
    if res.get("n_rows", 0) == 0:
        return "The query executed but returned ZERO rows."
    return None


def solve(task, env):
    schema = env.schema(task["db_id"])
    user = f"Database: {task['db_id']}\n\nSchema:\n{schema}\n\nQuestion: {task['question']}"
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"
    desc = ""
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        desc = ""
    if desc.strip():
        user += f"\n\nColumn descriptions:\n{desc.strip()[:2000]}"

    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user},
    ]
    reply = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
    sql = extract_sql(reply)

    try:
        res = env.execute(sql, max_rows=20)
    except Exception as e:
        res = {"ok": False, "error": str(e), "rows": [], "n_rows": 0}

    note = _result_note(res)
    if note is None:
        return {"sql": sql}

    # Draft errored or came back empty: gather evidence and ask for one repair.
    evidence = ""
    try:
        evidence = _probe_values(env, schema, sql)
    except Exception:
        evidence = ""

    repair = (
        f"Your draft query:\n```sql\n{sql}\n```\n{note}\n"
    )
    if evidence:
        repair += f"\nActual stored values found in the database:\n{evidence}\n"
    repair += (
        "\nThe question almost certainly has a non-empty answer. Diagnose why the draft fails and "
        "write a corrected query. Common causes:\n"
        "- String literals must match stored data: values may differ in case, have trailing/leading "
        "spaces, or be embedded in longer text. Prefer case-insensitive substring matching "
        "(LOWER(col) LIKE '%...%') using a distinctive keyword from the question rather than exact "
        "equality on long phrases.\n"
        "- The filter may be on the wrong column (e.g. a code vs a name/description column); check "
        "which column actually stores the mentioned value.\n"
        "- NOT IN over a subquery whose column can contain NULL returns no rows; use NOT EXISTS or "
        "add WHERE col IS NOT NULL. For 'has no friends/relationships', remember the entity can "
        "appear on either side of the relation table.\n"
        "- Join keys may be mismatched (wrong id column or wrong direction).\n"
        "Verify your fix mentally against the schema, then return ONLY the corrected query in a "
        "single ```sql block."
    )
    messages.append({"role": "assistant", "content": reply})
    messages.append({"role": "user", "content": repair})
    try:
        reply2 = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
    except Exception:
        return {"sql": sql}
    sql2 = extract_sql(reply2)
    if not sql2:
        return {"sql": sql}
    try:
        res2 = env.execute(sql2, max_rows=20)
    except Exception:
        res2 = {"ok": False}
    if res2.get("ok") and (res2.get("n_rows", 0) > 0 or not res.get("ok")):
        return {"sql": sql2}
    if res2.get("ok") and not res.get("ok"):
        return {"sql": sql2}
    # Prefer whichever executes; if both empty, trust the repaired one only if it ran cleanly.
    if res2.get("ok"):
        return {"sql": sql2}
    return {"sql": sql}