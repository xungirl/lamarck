"""Text-to-SQL harness: one generation call + at most one grounded repair call.

Contract (fixed): solve(task, env) -> {"sql": str}
"""

import re
import difflib

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
LITERAL = re.compile(r"'([^']{1,80})'")


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    t = (text or "").strip()
    m = re.search(r"(?is)\b(select|with)\b.*", t)
    if m:
        t = m.group(0)
    return t.rstrip(";").strip()


def _tables(schema):
    return re.findall(r'CREATE\s+TABLE\s+[`"\[]?([A-Za-z_][\w]*)', schema or "", re.I)


def _columns(schema, table):
    m = re.search(
        r'CREATE\s+TABLE\s+[`"\[]?' + re.escape(table) + r'[`"\]]?\s*\((.*?)\)\s*;',
        schema or "",
        re.S | re.I,
    )
    if not m:
        return []
    cols = []
    depth = 0
    cur = ""
    for ch in m.group(1):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            cols.append(cur)
            cur = ""
        else:
            cur += ch
    cols.append(cur)
    out = []
    for c in cols:
        c = c.strip()
        w = re.match(r'[`"\[]?([A-Za-z_][\w]*)', c)
        if not w:
            continue
        name = w.group(1)
        if name.upper() in (
            "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT", "KEY"
        ):
            continue
        out.append(name)
    return out


def _probe_values(env, schema, sql, question, hint, budget=14):
    """Find real stored values resembling literals used in the draft/question."""
    lits = []
    for src in (sql, question or "", hint or ""):
        for v in LITERAL.findall(src or ""):
            v = v.strip()
            if v and v not in lits and not v.isdigit():
                lits.append(v)
    # also pull quoted phrases from the question
    for v in re.findall(r'"([^"]{2,60})"', question or ""):
        v = v.strip()
        if v and v not in lits:
            lits.append(v)
    if not lits:
        return []

    used_tables = [t for t in _tables(schema) if re.search(r"\b" + re.escape(t) + r"\b", sql or "", re.I)]
    tables = used_tables or _tables(schema)
    notes = []
    calls = 0
    for lit in lits[:3]:
        core = re.sub(r"[^A-Za-z0-9 ]", " ", lit).split()
        key = max(core, key=len) if core else lit
        found = []
        for t in tables:
            for c in _columns(schema, t):
                if calls >= budget:
                    break
                q = (
                    'SELECT DISTINCT "%s" FROM "%s" WHERE "%s" LIKE \'%%%s%%\' LIMIT 5'
                    % (c, t, c, key.replace("'", ""))
                )
                try:
                    r = env.execute(q, max_rows=5)
                except Exception:
                    continue
                calls += 1
                if r.get("ok") and r.get("rows"):
                    for row in r["rows"]:
                        v = row[0]
                        if isinstance(v, str) and v not in found:
                            found.append(v)
            if calls >= budget:
                break
        if found:
            close = difflib.get_close_matches(lit, found, n=3, cutoff=0.4) or found[:3]
            notes.append("Literal %r -> actual stored values in %s: %s" % (lit, ", ".join(tables[:4]), close))
        else:
            notes.append("Literal %r matched NO stored value by substring; consider a shorter LIKE keyword or a different column." % lit)
    return notes


def solve(task, env):
    schema = env.schema(task["db_id"])
    desc = ""
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        desc = ""
    user = "Database: %s\n\nSchema:\n%s\n" % (task["db_id"], schema)
    if desc.strip():
        user += "\nColumn descriptions:\n%s\n" % desc.strip()[:2000]
    user += "\nQuestion: %s" % task["question"]
    if task.get("hint"):
        user += "\nHint: %s" % task["hint"]

    sysmsg = {"role": "system", "content": env.prompt("system_prompt.md")}
    reply = env.chat([sysmsg, {"role": "user", "content": user}],
                     max_tokens=env.config.get("max_tokens", 800))
    sql = extract_sql(reply)

    try:
        res = env.execute(sql, max_rows=5)
    except Exception:
        res = {"ok": False, "error": "execution failed", "rows": [], "n_rows": 0}

    needs_repair = (not res.get("ok")) or res.get("n_rows", 0) == 0
    if not needs_repair:
        return {"sql": sql}

    notes = []
    if not res.get("ok"):
        notes.append("SQLite error: %s" % str(res.get("error"))[:300])
    else:
        notes.append("The query executed but returned 0 rows, which is almost always wrong.")
    try:
        notes += _probe_values(env, schema, sql, task["question"], task.get("hint", ""))
    except Exception:
        pass

    fix_user = (
        user
        + "\n\nYour previous query was:\n```sql\n" + sql + "\n```\n\nExecution feedback:\n- "
        + "\n- ".join(notes)
        + "\n\nWrite a corrected query. Use the observed stored values verbatim, fix any wrong column/table/join, "
          "and keep the required output columns (all columns of the entity table when the question does not name attributes). "
          "Return only one ```sql block."
    )
    try:
        reply2 = env.chat([sysmsg, {"role": "user", "content": fix_user}],
                          max_tokens=env.config.get("max_tokens", 800))
    except Exception:
        return {"sql": sql}
    sql2 = extract_sql(reply2)
    if not sql2:
        return {"sql": sql}
    try:
        r2 = env.execute(sql2, max_rows=5)
    except Exception:
        r2 = {"ok": False}
    if r2.get("ok"):
        return {"sql": sql2}
    return {"sql": sql}