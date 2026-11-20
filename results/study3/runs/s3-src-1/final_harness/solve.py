"""Execution-grounded text-to-SQL harness.

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
IDENT = r"[A-Za-z_][A-Za-z_0-9]*"
QUOTED = r'"[^"]*"|`[^`]*`|\[[^\]]*\]'
NAME = r"(?:%s|%s)" % (IDENT, QUOTED)
FROM_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+(%s)" % NAME, re.I)
LITERAL = re.compile(r"'((?:[^']|'')*)'")
EQ_COL = re.compile(r"(%s)\s*(?:=|LIKE|GLOB)\s*'" % NAME, re.I)
KEYWORDS = set(
    """select from where group by order having limit offset union all distinct as on join inner left right
    full outer cross case when then else end and or not in exists like glob between is null asc desc count
    sum avg min max cast coalesce substr length upper lower replace trim round abs date time strftime with
    recursive natural using except intersect values true false""".split()
)


def _unquote(tok):
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] in "\"`[" and tok[-1] in "\"`]":
        return tok[1:-1]
    return tok


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    return (text or "").strip().rstrip(";").strip()


def _tables(sql):
    out = []
    for m in FROM_JOIN.finditer(sql or ""):
        t = _unquote(m.group(1))
        if t.lower() not in KEYWORDS and t not in out:
            out.append(t)
    return out


def _schema_columns(schema):
    """table name -> list of column names, parsed from CREATE TABLE statements."""
    cols = {}
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(%s)\s*\((.*?)\)\s*;" % NAME,
                         schema or "", re.S | re.I):
        table = _unquote(m.group(1))
        body = m.group(2)
        names = []
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
            first = p.split()[0]
            if first.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
                continue
            names.append(_unquote(first))
        cols[table] = names
    return cols


def _probe(env, sql, schema, budget=6):
    """Sample real stored values for the tables/literals used by a failing query."""
    lines = []
    tables = _tables(sql)
    if not tables:
        return ""
    colmap = _schema_columns(schema)
    literals = [m.group(1).replace("''", "'") for m in LITERAL.finditer(sql or "")]
    literals = [l for l in literals if l and "%" not in l and "_" not in l][:3]
    eq_cols = [_unquote(m.group(1)) for m in EQ_COL.finditer(sql or "")]

    def q(name):
        return '"%s"' % name.replace('"', '""')

    # 1) targeted lookups: what does the column actually contain near this literal?
    for lit in literals:
        frag = re.sub(r"[^A-Za-z0-9 ]", " ", lit).split()
        frag = max(frag, key=len) if frag else lit
        done = False
        for t in tables:
            for c in colmap.get(t, []):
                if c in eq_cols or not eq_cols:
                    try:
                        r = env.execute(
                            "SELECT DISTINCT %s FROM %s WHERE %s LIKE '%%%s%%' LIMIT 8"
                            % (q(c), q(t), q(c), frag.replace("'", "''")),
                            max_rows=8,
                        )
                    except Exception:
                        continue
                    if r.get("ok") and r.get("rows"):
                        vals = [row[0] for row in r["rows"]]
                        lines.append("%s.%s values matching %r: %s" % (t, c, lit, vals))
                        done = True
                        budget -= 1
                        break
            if done or budget <= 0:
                break
        if budget <= 0:
            break

    # 2) small unconditional samples of each referenced table (join keys, flags, codes)
    for t in tables:
        if budget <= 0:
            break
        try:
            r = env.execute("SELECT * FROM %s LIMIT 3" % q(t), max_rows=3)
        except Exception:
            continue
        budget -= 1
        if r.get("ok"):
            lines.append("Sample rows of %s (columns %s): %s" % (t, r.get("columns"), r.get("rows")))
    return "\n".join(lines)[:2500]


def solve(task, env):
    schema = env.schema(task["db_id"])
    user = "Database: %s\n\nSchema:\n%s\n\nQuestion: %s" % (task["db_id"], schema, task["question"])
    hint = task.get("hint") or ""
    if hint:
        user += "\nHint: %s" % hint
    desc = ""
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        desc = ""
    if desc.strip():
        user += "\n\nColumn notes:\n%s" % desc.strip()[:2000]

    system = env.prompt("system_prompt.md")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    max_tokens = env.config.get("max_tokens", 800)
    reply = env.chat(messages, max_tokens=max_tokens)
    sql = extract_sql(reply)
    if not sql:
        return {"sql": sql}

    try:
        res = env.execute(sql, max_rows=20)
    except Exception:
        return {"sql": sql}

    ok = bool(res.get("ok"))
    empty = ok and not res.get("rows")
    if ok and not empty:
        return {"sql": sql}

    # One grounded repair attempt with the remaining policy call.
    feedback = []
    if not ok:
        feedback.append("SQLite error: %s" % res.get("error"))
    else:
        feedback.append(
            "The query executed successfully but returned ZERO rows. That usually means a string "
            "literal does not match how the value is stored (case, spacing, punctuation, abbreviation), "
            "a join uses the wrong key/direction, or a filter is too strict."
        )
    try:
        probe = _probe(env, sql, schema)
    except Exception:
        probe = ""
    if probe:
        feedback.append("Actual database contents (use these exact values):\n%s" % probe)
    feedback.append(
        "Rewrite the query so it answers the question using values that really exist in the database. "
        "Keep the same requested output columns. If a literal cannot be matched exactly, compare "
        "case-insensitively or with LIKE on a distinctive fragment. Return only one ```sql block."
    )
    messages = messages + [
        {"role": "assistant", "content": reply},
        {"role": "user", "content": "Your query was:\n```sql\n%s\n```\n\n%s" % (sql, "\n\n".join(feedback))},
    ]
    try:
        reply2 = env.chat(messages, max_tokens=max_tokens)
    except Exception:
        return {"sql": sql}
    sql2 = extract_sql(reply2)
    if not sql2:
        return {"sql": sql}
    try:
        res2 = env.execute(sql2, max_rows=20)
    except Exception:
        return {"sql": sql}
    if res2.get("ok") and (res2.get("rows") or not ok):
        return {"sql": sql2}
    if res2.get("ok") and not ok:
        return {"sql": sql2}
    return {"sql": sql}