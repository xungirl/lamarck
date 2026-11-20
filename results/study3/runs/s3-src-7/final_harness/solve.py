"""Text-to-SQL harness: draft -> execute -> optional data-grounded repair.

Contract (fixed): solve(task, env) -> {"sql": str}
  task  {"id", "db_id", "question", "hint"}
  env.chat(messages, max_tokens=None) -> str     capped per task (env.max_calls)
  env.schema(db_id) -> str
  env.execute(sql, max_rows=20) -> dict          {ok, columns, rows, n_rows, error}
  env.descriptions(db_id) -> str
  env.prompt(name) -> str
  env.config -> dict
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
STR_LIT = re.compile(r"'((?:[^']|'')*)'")


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    t = (text or "").strip()
    # fall back to the last statement-looking line if prose surrounds it
    m = re.search(r"(?is)\b(select|with)\b.*", t)
    if m:
        t = m.group(0)
    return t.strip().rstrip(";").strip()


def _tables(schema):
    """table name -> list of (column, declared type), from CREATE statements."""
    out = {}
    for m in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?([\w]+)[\"'`\]]?\s*\((.*?)\)\s*;",
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
            first = p.split()[0].strip("\"'`[]")
            if first.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
                continue
            typ = p[len(p.split()[0]):].strip().upper()
            cols.append((first, typ))
        out[name] = cols
    return out


def _probe(env, db_id, schema, sql, budget=6):
    """Sample real stored values for text columns the draft touches."""
    tabs = _tables(schema)
    used = set(IDENT.findall(sql or ""))
    lines = []
    for tname, cols in tabs.items():
        if tname not in used:
            continue
        text_cols = [c for c, ty in cols if ("CHAR" in ty or "TEXT" in ty or "CLOB" in ty or ty == "")]
        text_cols = [c for c in text_cols if c in used] or text_cols[:3]
        for c in text_cols[:3]:
            if len(lines) >= budget:
                break
            q = 'SELECT DISTINCT "%s" FROM "%s" WHERE "%s" IS NOT NULL LIMIT 8' % (c, tname, c)
            try:
                r = env.execute(q, max_rows=8)
            except Exception:
                continue
            if r.get("ok") and r.get("rows"):
                vals = [str(row[0]) for row in r["rows"]]
                lines.append("%s.%s sample values: %s" % (tname, c, " | ".join(vals)))
        if len(lines) >= budget:
            break
    return "\n".join(lines)


def _fmt_result(r):
    if not r.get("ok"):
        return "ERROR: %s" % r.get("error")
    if r.get("n_rows", 0) == 0:
        return "0 rows returned."
    head = ", ".join(map(str, r.get("columns") or []))
    rows = "; ".join(str(row) for row in (r.get("rows") or [])[:5])
    return "%d rows. columns: [%s]. first rows: %s" % (r.get("n_rows", 0), head, rows)


def solve(task, env):
    db_id = task["db_id"]
    schema = env.schema(db_id)
    user = "Database: %s\n\nSchema:\n%s\n\nQuestion: %s" % (db_id, schema, task["question"])
    if task.get("hint"):
        user += "\nHint: %s" % task["hint"]
    desc = ""
    try:
        desc = env.descriptions(db_id) or ""
    except Exception:
        pass
    if desc.strip():
        user += "\n\nColumn descriptions:\n" + desc.strip()[:2000]

    sys_msg = {"role": "system", "content": env.prompt("system_prompt.md")}
    messages = [sys_msg, {"role": "user", "content": user}]
    reply = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
    sql = extract_sql(reply)

    max_calls = getattr(env, "max_calls", 1) or 1
    if max_calls < 2:
        return {"sql": sql}

    try:
        res = env.execute(sql, max_rows=20)
    except Exception:
        return {"sql": sql}

    needs_fix = (not res.get("ok")) or res.get("n_rows", 0) == 0
    if not needs_fix:
        return {"sql": sql}

    feedback = "Your query was executed on the real database.\nResult: %s\n" % _fmt_result(res)
    probe = _probe(env, db_id, schema, sql)
    if probe:
        feedback += "\nActual stored values (use these exact spellings/cases):\n%s\n" % probe
    lits = [s for s in STR_LIT.findall(sql) if s]
    if lits:
        feedback += "\nString literals you used: %s\n" % ", ".join(repr(x) for x in lits[:8])
    feedback += (
        "\nIf the result is empty or an error, the query is probably wrong: check exact literal "
        "spellings/case, which column really holds the value, join keys and direction, and whether "
        "a filter should be case-insensitive or a LIKE pattern. Also reconsider the projected columns "
        "if the question asks about whole entities rather than one attribute. "
        "Return the corrected final query in a single ```sql block. If you are confident the empty "
        "result is genuinely correct, return the same query."
    )
    messages = messages + [
        {"role": "assistant", "content": reply},
        {"role": "user", "content": feedback},
    ]
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
        return {"sql": sql}
    if not res2.get("ok"):
        return {"sql": sql}
    return {"sql": sql2}