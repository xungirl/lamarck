"""Text-to-SQL harness: draft -> execute -> diagnose -> one repair call.

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
CREATE_TBL = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`\[]?([A-Za-z_][A-Za-z0-9_]*)["`\]]?\s*\((.*)',
    re.S | re.I,
)
COL_LINE = re.compile(r'^\s*["`\[]?([A-Za-z_][A-Za-z0-9_]*)["`\]]?\s+([A-Za-z]|\()')
CONSTRAINT_KW = {
    "primary", "foreign", "unique", "check", "constraint", "key", "index",
}


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    return (text or "").strip().rstrip(";").strip()


def _table_columns(schema):
    """Best-effort {table: [columns]} parsed from CREATE statements."""
    out = {}
    for m in CREATE_TBL.finditer(schema or ""):
        name, body = m.group(1), m.group(2)
        cols = []
        depth = 0
        line = ""
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            if ch == "," and depth == 0:
                cm = COL_LINE.match(line)
                if cm and cm.group(1).lower() not in CONSTRAINT_KW:
                    cols.append(cm.group(1))
                line = ""
            else:
                line += ch
        cm = COL_LINE.match(line)
        if cm and cm.group(1).lower() not in CONSTRAINT_KW:
            cols.append(cm.group(1))
        out[name] = cols
    return out


def _probe_values(env, sql, schema):
    """For string literals used in the draft, show what is actually stored.

    Generic: find text-ish columns whose name shares a token with the tables
    referenced by the draft, then LIKE-probe with the literal's core word.
    """
    lits = [l.replace("''", "'") for l in LITERAL.findall(sql)]
    lits = [l for l in lits if l and l.upper() not in ("T", "F", "Y", "N")]
    if not lits:
        return ""
    tables = _table_columns(schema)
    ref = [t for t in tables if re.search(r"\b" + re.escape(t) + r"\b", sql, re.I)]
    if not ref:
        ref = list(tables)
    text_cols = []
    for t in ref:
        for c in tables[t]:
            if re.search(r"name|text|title|type|summary|detail|description|code|language|status|country|city|program|degree|channel|series", c, re.I):
                text_cols.append((t, c))
    lines = []
    seen = set()
    for lit in lits[:3]:
        core = re.sub(r"[%_]", "", lit).strip()
        words = [w for w in re.split(r"\s+", core) if len(w) >= 3]
        probe = words[0] if words else core
        if not probe:
            continue
        for t, c in text_cols[:14]:
            key = (t, c, probe.lower())
            if key in seen:
                continue
            seen.add(key)
            q = (
                "SELECT DISTINCT \"%s\" FROM \"%s\" WHERE \"%s\" LIKE '%%%s%%' LIMIT 6"
                % (c, t, c, probe.replace("'", "''"))
            )
            try:
                r = env.execute(q, max_rows=6)
            except Exception:
                continue
            if r.get("ok") and r.get("rows"):
                vals = [row[0] for row in r["rows"]]
                lines.append("%s.%s contains values like %r (your filter used %r)" % (t, c, vals, lit))
            if len(lines) >= 8:
                break
        if len(lines) >= 8:
            break
    return "\n".join(lines)


GUIDANCE = (
    "Checklist while repairing:\n"
    "- Only reference tables/columns that exist in the schema above; verify exact spelling.\n"
    "- String comparisons in SQLite are case-sensitive by default and stored values may differ "
    "from the question's wording (singular/plural, abbreviations, extra spaces, casing). Prefer "
    "matching the ACTUAL stored values shown below, or use LIKE / LOWER(...) to be tolerant. "
    "Do not keep trailing spaces inside LIKE patterns unless the data really has them.\n"
    "- For anti-joins (NOT IN / NOT EXISTS), make sure the subquery selects the correct id column "
    "that actually exists, and consider both directions of a relationship table when relevant.\n"
    "- Select exactly the columns the question asks for. If the question asks about whole entities "
    "(e.g. 'which countries...', 'show the details...'), returning all columns of that entity's table "
    "(SELECT T1.*) is often what is expected rather than just a name.\n"
    "- Apply hint formulas literally; watch NULLs, ties with LIMIT, and DISTINCT only when needed."
)


def solve(task, env):
    schema = env.schema(task["db_id"])
    user = f"Database: {task['db_id']}\n\nSchema:\n{schema}\n\nQuestion: {task['question']}"
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"
    desc = ""
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        pass
    if desc:
        user += f"\n\nColumn descriptions:\n{desc[:3000]}"

    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user},
    ]
    reply = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
    sql = extract_sql(reply)
    if not sql:
        return {"sql": sql}

    try:
        res = env.execute(sql, max_rows=20)
    except Exception:
        res = {"ok": True, "n_rows": 1}

    needs_repair = (not res.get("ok")) or res.get("n_rows", 0) == 0
    if not needs_repair:
        return {"sql": sql}

    feedback = ["Your draft query was:", "```sql\n" + sql + "\n```"]
    if not res.get("ok"):
        feedback.append("It FAILED with error: %s" % res.get("error", "unknown"))
    else:
        feedback.append(
            "It executed but returned 0 rows. That usually means a filter value does not match "
            "what is stored, a join condition is wrong, or a referenced column is wrong."
        )
    probes = _probe_values(env, sql, schema)
    if probes:
        feedback.append("Actual stored values found by probing the database:\n" + probes)
    feedback.append(GUIDANCE)
    feedback.append(
        "Write a corrected query that answers the original question. Keep any parts of the draft "
        "that were right. Return ONLY the final query in a single ```sql code block."
    )

    messages.append({"role": "assistant", "content": reply})
    messages.append({"role": "user", "content": "\n\n".join(feedback)})
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
    # Prefer the repaired query if it runs; if it errors, fall back to the draft.
    if res2.get("ok") or not res.get("ok"):
        return {"sql": sql2}
    return {"sql": sql}