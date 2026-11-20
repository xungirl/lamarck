"""Text-to-SQL harness: grounded draft -> execute check -> targeted repair.

Contract (fixed): solve(task, env) -> {"sql": str}
  task  {"id", "db_id", "question", "hint"}
  env.chat(messages, max_tokens=None) -> str     capped per task: env.max_calls
  env.schema(db_id) -> str
  env.execute(sql, max_rows=20) -> dict          {ok, columns, rows, n_rows, error}
  env.descriptions(db_id) -> str
  env.prompt(name) -> str
  env.config -> dict
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
LITERAL = re.compile(r"'([^']*)'")
STOP = set(
    """select from where and or not in is null as on join left right inner outer full cross
    union all except intersect distinct group by order having limit offset asc desc count sum
    avg min max cast int integer real text float case when then else end like between exists
    lower upper substr length trim coalesce ifnull round abs total group_concat instr replace
    t1 t2 t3 t4 t5 a b c d e f g h i j k l m n o p q r s t u v w x y z asc desc with recursive
    true false date time strftime julianday""".split()
)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    txt = (text or "").strip()
    # fall back to the last statement-looking line
    m = re.findall(r"(?is)\b(select|with)\b.*", txt)
    if m:
        return m[-1].rstrip(";").strip()
    return txt.rstrip(";").strip()


def _tables(schema):
    """table name -> list of column names, from CREATE statements."""
    out = {}
    for m in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?([A-Za-z_0-9]+)[\"'`\]]?\s*\((.*?)\)\s*;",
        schema or "",
        re.S | re.I,
    ):
        name, body = m.group(1), m.group(2)
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
            if not p:
                continue
            first = p.split()[0].strip('"`[]\'')
            if first.lower() in ("primary", "foreign", "unique", "check", "constraint"):
                continue
            cols.append(first)
        out[name] = cols
    return out


def _sample_values(env, schema, sql, budget=3):
    """Show real stored values for columns referenced near a string literal."""
    tabs = _tables(schema)
    low = sql.lower()
    used = [t for t in tabs if re.search(r"\b" + re.escape(t.lower()) + r"\b", low)]
    lits = [v.strip() for v in LITERAL.findall(sql) if v.strip()]
    targets = []
    for t in used:
        for col in tabs[t]:
            cl = col.lower()
            if cl in STOP:
                continue
            mentioned = re.search(r"\b" + re.escape(cl) + r"\b", low)
            if not mentioned:
                continue
            score = 0
            if re.search(re.escape(cl) + r"\s*(=|like|!=|<>|in)", low):
                score += 2
            if any(w and w in cl for lit in lits for w in re.findall(r"[a-Za-Z_]{3,}", lit.lower())):
                score += 3
            if score:
                targets.append((score, t, col))
    targets.sort(reverse=True)
    lines = []
    for _, t, col in targets[:budget]:
        q = 'SELECT DISTINCT "%s" FROM "%s" WHERE "%s" IS NOT NULL LIMIT 8' % (col, t, col)
        try:
            r = env.execute(q, max_rows=8)
        except Exception:
            continue
        if r.get("ok") and r.get("rows"):
            vals = [str(x[0])[:60] for x in r["rows"]]
            lines.append("%s.%s sample values: %s" % (t, col, " | ".join(vals)))
    return "\n".join(lines)


def solve(task, env):
    cfg = env.config or {}
    max_tokens = cfg.get("max_tokens", 800)
    schema = env.schema(task["db_id"])
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        desc = ""

    user = "Database: %s\n\nSchema:\n%s\n" % (task["db_id"], schema)
    if desc.strip():
        user += "\nColumn descriptions:\n%s\n" % desc.strip()[:2500]
    user += "\nQuestion: %s" % task["question"]
    if task.get("hint"):
        user += "\nHint: %s" % task["hint"]
    user += "\n\nWrite the query now."

    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user},
    ]
    reply = env.chat(messages, max_tokens=max_tokens)
    sql = extract_sql(reply)
    if not sql:
        return {"sql": sql}

    # Cheap self-check: only spend the second call when something looks wrong.
    try:
        res = env.execute(sql, max_rows=5)
    except Exception:
        res = {"ok": False, "error": "execution failed", "rows": [], "n_rows": 0}

    ok = bool(res.get("ok"))
    nrows = res.get("n_rows", len(res.get("rows") or []))
    needs_repair = (not ok) or nrows == 0
    if not needs_repair:
        return {"sql": sql}

    fb = ["Your draft query was:", "```sql", sql, "```"]
    if not ok:
        fb.append("It failed to execute. Error: %s" % str(res.get("error"))[:400])
    else:
        fb.append(
            "It executed but returned ZERO rows. That usually means a literal value does not "
            "match what is stored (spacing/casing/wording), a join key is wrong, or a NOT IN "
            "subquery contains NULLs. Re-read the question: an empty answer is rarely intended."
        )
    samples = ""
    try:
        samples = _sample_values(env, schema, sql)
    except Exception:
        samples = ""
    if samples:
        fb.append("Actual stored values in the database:\n" + samples)
    fb.append(
        "Return a corrected query in a single ```sql block. Loosen string matching "
        "(LIKE '%keyword%' / lower(...)) when the exact value is unknown, use EXCEPT or "
        "NOT EXISTS for negation, and select all columns of the entity if the question asks "
        "for its details without naming attributes."
    )

    messages = messages + [
        {"role": "assistant", "content": reply},
        {"role": "user", "content": "\n".join(fb)},
    ]
    try:
        reply2 = env.chat(messages, max_tokens=max_tokens)
    except Exception:
        return {"sql": sql}
    sql2 = extract_sql(reply2)
    if not sql2:
        return {"sql": sql}
    try:
        res2 = env.execute(sql2, max_rows=5)
    except Exception:
        res2 = {"ok": False}
    if res2.get("ok"):
        return {"sql": sql2}
    return {"sql": sql if ok else sql2}