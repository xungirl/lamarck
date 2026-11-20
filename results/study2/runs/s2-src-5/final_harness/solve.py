"""Text-to-SQL harness: one draft call plus an optional cheap, evidence-based repair.

Contract (fixed): solve(task, env) -> {"sql": str}
  task  {"id", "db_id", "question", "hint"}
  env.chat(messages, max_tokens=None) -> str     policy call (capped per task: env.max_calls)
  env.schema(db_id) -> str                       CREATE statements
  env.execute(sql, max_rows=20) -> dict          read-only run {ok, columns, rows, n_rows, error}
  env.descriptions(db_id) -> str                 column descriptions if shipped, else ""
  env.prompt(name) -> str                        text of another harness file
  env.config -> dict                             parsed config.json

Design: most drafts are fine, so they cost exactly one call. Only a draft that errors or
returns zero rows triggers a second call, and that call carries small, schema-grounded
facts about why it may be empty (real stored values, join keys, NULLs in a negated set).
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
CREATE_RE = re.compile(
    r"CREATE\s+(?:TEMP\s+|TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?([\w$]+)",
    re.I,
)
LIT_RE = re.compile(r"'((?:[^']|'')*)'")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
NEG_RE = re.compile(r"\b(NOT\s+IN|EXCEPT)\s*\(?\s*(SELECT\b.*)", re.I | re.S)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    return (text or "").strip().rstrip(";").strip()


def _q(name):
    return '"' + str(name).replace('"', '""') + '"'


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def _run(env, sql, max_rows=5):
    try:
        res = env.execute(sql, max_rows=max_rows)
    except Exception as exc:  # execution must never break the contract
        return {"ok": False, "error": str(exc)}
    return res if isinstance(res, dict) else {"ok": False, "error": "bad result"}


def _tables(schema):
    out = []
    for match in CREATE_RE.finditer(schema or ""):
        start = schema.find("(", match.end())
        if start < 0:
            continue
        depth, end = 0, None
        for i in range(start, len(schema)):
            ch = schema[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        name = match.group(1)
        body = schema[start + 1 : end]
        cols = []
        for line in body.split("\n"):
            cm = re.match(r'\s*[\"\'`\[]?([A-Za-z_][\w$]*)', line)
            if not cm:
                continue
            col = cm.group(1)
            if col.upper() in (
                "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT", "KEY", "INDEX"
            ):
                continue
            if col not in cols:
                cols.append(col)
        fks = re.findall(
            r"FOREIGN\s+KEY\s*\(\s*[\"'`\[]?([\w$]+)[\"'`\]]?\s*\)\s*REFERENCES\s+"
            r"[\"'`\[]?([\w$]+)[\"'`\]]?\s*\(\s*[\"'`\[]?([\w$]+)",
            body,
            re.I,
        )
        out.append({"name": name, "cols": cols, "fks": fks})
    return out


def _core(literal):
    words = re.findall(r"[A-Za-z0-9]+", literal)
    if not words:
        return ""
    return max(words, key=len)


def _value_evidence(env, tables, sql):
    """Real stored values close to the literals the draft used."""
    wanted = {w.lower() for w in IDENT_RE.findall(sql or "")}
    lits = []
    for raw in LIT_RE.findall(sql or ""):
        val = raw.replace("''", "'").strip()
        if len(val) >= 3 and val not in lits:
            lits.append(val)
    lines = []
    for lit in lits[:3]:
        core = _core(lit)
        if len(core) < 3:
            continue
        pattern = "'%" + core.replace("'", "''") + "%'"
        hits = []
        for tab in tables:
            for col in tab["cols"]:
                preferred = col.lower() in wanted
                probe = (
                    "SELECT DISTINCT " + _q(col) + " FROM " + _q(tab["name"])
                    + " WHERE " + _q(col) + " LIKE " + pattern + " LIMIT 3"
                )
                res = _run(env, probe, max_rows=3)
                vals = [r[0] for r in (res.get("rows") or []) if r and r[0] is not None]
                if vals:
                    shown = ", ".join(repr(v)[:60] for v in vals[:3])
                    hits.append((not preferred, tab["name"] + "." + col + ": " + shown))
                if len(hits) >= 6:
                    break
            if len(hits) >= 6:
                break
        hits.sort(key=lambda item: item[0])
        found = "; ".join(text for _, text in hits[:4])
        if found:
            lines.append("- value like " + repr(lit) + " -> " + found)
        else:
            lines.append(
                "- value like " + repr(lit)
                + " -> no similar text anywhere; the filter probably belongs to another column,"
                " or the wording differs (try a shorter LIKE fragment)"
            )
    return lines


def _join_evidence(tables, sql):
    if not re.search(r"\bJOIN\b", sql or "", re.I):
        return []
    names = {t["name"].lower() for t in tables}
    lines = []
    for tab in tables:
        for col, ref_tab, ref_col in tab["fks"]:
            if ref_tab.lower() in names:
                lines.append(
                    "- " + tab["name"] + "." + col + " = " + ref_tab + "." + ref_col
                )
    return lines[:8]


def _null_evidence(env, sql):
    match = NEG_RE.search(sql or "")
    if not match:
        return []
    inner = match.group(2).strip()
    depth = 0
    for i, ch in enumerate(inner):
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                inner = inner[:i]
                break
            depth -= 1
    inner = inner.strip().rstrip(";").strip()
    if not inner.upper().startswith("SELECT"):
        return []
    res = _run(
        env,
        "SELECT COUNT(*) FROM (" + inner + ") AS sub_null_probe WHERE 1 IN (sub_null_probe.*) IS NULL",
        max_rows=1,
    )
    if not res.get("ok"):
        res = _run(
            env,
            "SELECT SUM(CASE WHEN x IS NULL THEN 1 ELSE 0 END) FROM ("
            + re.sub(r"^SELECT\s+(DISTINCT\s+)?", "SELECT ", inner, count=1, flags=re.I)
            + ") AS sub_null_probe(x)",
            max_rows=1,
        )
    rows = res.get("rows") or []
    if res.get("ok") and rows and rows[0] and isinstance(rows[0][0], (int, float)) and rows[0][0]:
        return [
            "- the subquery used for negation contains "
            + str(int(rows[0][0]))
            + " NULL value(s); NOT IN then returns no rows at all - use NOT EXISTS or add IS NOT NULL"
        ]
    return []


def solve(task, env):
    db_id = task["db_id"]
    schema = env.schema(db_id)
    user = "Database: " + str(db_id) + "\n\nSchema:\n" + schema + "\n\nQuestion: " + task["question"]
    if task.get("hint"):
        user += "\nHint: " + task["hint"]
    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user},
    ]
    reply = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
    sql = extract_sql(reply)
    if not sql:
        return {"sql": sql}

    res = _run(env, sql, max_rows=5)
    ok = bool(res.get("ok"))
    if ok and (res.get("n_rows") or len(res.get("rows") or [])) > 0:
        return {"sql": sql}

    notes = []
    if not ok:
        notes.append("- SQLite error: " + str(res.get("error", ""))[:200])
    else:
        notes.append("- the query ran but returned 0 rows")
    try:
        tables = _tables(schema)
        notes += _value_evidence(env, tables, sql)
        notes += _null_evidence(env, sql)
        notes += _join_evidence(tables, sql)
    except Exception:
        pass

    followup = (
        "Your query:\n" + sql + "\n\nExecution feedback:\n" + "\n".join(notes[:12])
        + "\n\nIf a filter value, filter column, join condition or negation is wrong, fix it using the"
        " facts above (match stored values exactly). Keep exactly the columns the question asks for."
        " If the query is already correct and the answer really is empty, repeat it unchanged."
        " Reply with one ```sql block only."
    )
    messages.append({"role": "assistant", "content": reply})
    messages.append({"role": "user", "content": followup})
    try:
        fixed = extract_sql(env.chat(messages, max_tokens=env.config.get("repair_max_tokens", 400)))
    except Exception:
        fixed = ""
    if fixed:
        check = _run(env, fixed, max_rows=5)
        if check.get("ok") and (not ok or (check.get("n_rows") or len(check.get("rows") or [])) > 0):
            return {"sql": fixed}
    return {"sql": sql}