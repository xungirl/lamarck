"""Text-to-SQL harness: draft -> execute/validate -> optional single repair call.

Contract (fixed): solve(task, env) -> {"sql": str}
  task  {"id", "db_id", "question", "hint"}
  env.chat(messages, max_tokens=None) -> str     policy calls capped at env.max_calls
  env.schema(db_id) -> str
  env.execute(sql, max_rows=20) -> dict          {ok, columns, rows, n_rows, error}
  env.descriptions(db_id) -> str
  env.prompt(name) -> str
  env.config -> dict
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
LITERAL = re.compile(r"'((?:[^']|'')*)'")
IDENT = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]*)\.([A-Za-z_][A-Za-z_0-9]*)\b")
BARE_CMP = re.compile(
    r"\b([A-Za-z_][A-Za-z_0-9]*)\s*(?:=|!=|<>|\bLIKE\b)\s*'((?:[^']|'')*)'", re.I
)
FROM_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z_0-9]*)(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z_0-9]*))?", re.I)
KEYWORDS = {
    "select", "from", "where", "join", "inner", "left", "right", "outer", "on",
    "group", "order", "by", "limit", "as", "and", "or", "not", "in", "is",
    "null", "like", "having", "union", "distinct", "case", "when", "then",
    "else", "end", "asc", "desc", "cross", "natural", "using", "offset",
}


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    t = (text or "").strip()
    m = re.search(r"(?is)\b(select|with)\b.*", t)
    if m:
        t = m.group(0)
    return t.rstrip(";").strip()


def _safe_execute(env, sql, max_rows=20):
    try:
        r = env.execute(sql, max_rows=max_rows)
        if isinstance(r, dict):
            return r
    except Exception:
        pass
    return {"ok": False, "columns": [], "rows": [], "n_rows": 0, "error": "execution failed"}


def _table_aliases(sql):
    """alias/table-name -> real table name."""
    out = {}
    for tbl, alias in FROM_JOIN.findall(sql):
        if tbl.lower() in KEYWORDS:
            continue
        out[tbl.lower()] = tbl
        if alias and alias.lower() not in KEYWORDS:
            out[alias.lower()] = tbl
    return out


def _schema_columns(schema):
    """table -> [column names] parsed from CREATE statements (best effort)."""
    cols = {}
    for m in re.finditer(r"CREATE\s+TABLE\s+[`\"]?([A-Za-z_0-9]+)[`\"]?\s*\((.*?)\)\s*;", schema or "", re.S | re.I):
        tbl, body = m.group(1), m.group(2)
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
            cm = re.match(r"[`\"]?([A-Za-z_][A-Za-z_0-9]*)[`\"]?", p)
            if cm and cm.group(1).lower() not in (
                "primary", "foreign", "unique", "check", "constraint", "key"
            ):
                names.append(cm.group(1))
        cols[tbl.lower()] = (tbl, names)
    return cols


def _literal_targets(sql, aliases, schema_cols):
    """Map each string literal to candidate (table, column) pairs."""
    targets = {}
    for m in BARE_CMP.finditer(sql):
        col, val = m.group(1), m.group(2)
        # qualified form immediately before?
        start = max(0, m.start() - 60)
        prefix = sql[start:m.start()]
        qm = re.search(r"([A-Za-z_][A-Za-z_0-9]*)\.$", prefix)
        cands = []
        if qm:
            tbl = aliases.get(qm.group(1).lower())
            if tbl:
                cands.append((tbl, col))
        if not cands:
            for tl, (tbl, names) in schema_cols.items():
                for n in names:
                    if n.lower() == col.lower():
                        cands.append((tbl, n))
        if cands:
            targets.setdefault(val, [])
            for c in cands:
                if c not in targets[val]:
                    targets[val].append(c)
    # literals used inside IN (...) lists
    for m in re.finditer(r"([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)?)\s+IN\s*\(([^()]*?)\)", sql, re.I):
        colref, body = m.group(1), m.group(2)
        vals = LITERAL.findall(body)
        if not vals:
            continue
        if "." in colref:
            a, c = colref.split(".", 1)
            tbl = aliases.get(a.lower())
            cands = [(tbl, c)] if tbl else []
        else:
            cands = []
            for tl, (tbl, names) in schema_cols.items():
                for n in names:
                    if n.lower() == colref.lower():
                        cands.append((tbl, n))
        for v in vals:
            targets.setdefault(v, [])
            for c in cands:
                if c not in targets[v]:
                    targets[v].append(c)
    return targets


def _probe_values(env, sql, schema, budget):
    """For each string literal in the draft, show what the column really stores."""
    lits = [v for v in dict.fromkeys(LITERAL.findall(sql)) if v.strip()]
    if not lits:
        return [], budget
    aliases = _table_aliases(sql)
    schema_cols = _schema_columns(schema)
    targets = _literal_targets(sql, aliases, schema_cols)
    notes = []
    for val in lits:
        if budget <= 0:
            break
        cands = targets.get(val) or []
        core = re.sub(r"[%_]", "", val).strip()
        if not core:
            continue
        for tbl, col in cands[:2]:
            if budget <= 0:
                break
            frag = core.split()[0] if core.split() else core
            probe = (
                f'SELECT DISTINCT "{col}" FROM "{tbl}" '
                f"WHERE \"{col}\" LIKE '%{frag}%' LIMIT 8"
            )
            r = _safe_execute(env, probe, max_rows=8)
            budget -= 1
            if not r.get("ok"):
                continue
            vals = [row[0] for row in r.get("rows", []) if row]
            exact = any(isinstance(v, str) and v == val for v in vals)
            if exact and len(vals) == 1:
                continue
            shown = ", ".join(repr(v) for v in vals[:8]) if vals else "(no similar value found)"
            notes.append(
                f"- Column {tbl}.{col}: you filtered on {val!r}. Values actually stored that look related: {shown}."
            )
            break
    return notes, budget


def _probe_empty_join(env, sql, budget):
    """When a join returns nothing, show sample rows of the joined tables."""
    notes = []
    aliases = _table_aliases(sql)
    tables = list(dict.fromkeys(aliases.values()))
    if len(tables) < 2:
        return notes, budget
    for tbl in tables[:3]:
        if budget <= 0:
            break
        r = _safe_execute(env, f'SELECT * FROM "{tbl}" LIMIT 3', max_rows=3)
        budget -= 1
        if r.get("ok") and r.get("rows"):
            cols = ", ".join(str(c) for c in r.get("columns", []))
            rows = "; ".join(str(row) for row in r["rows"][:3])
            notes.append(f"- Sample rows of {tbl} ({cols}): {rows}")
    return notes, budget


def _build_user(task, env, schema):
    user = f"Database: {task['db_id']}\n\nSchema:\n{schema}\n\nQuestion: {task['question']}"
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"
    desc = ""
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        desc = ""
    if desc.strip():
        user += "\n\nColumn descriptions:\n" + desc.strip()[:2000]
    return user


def solve(task, env):
    cfg = env.config or {}
    schema = env.schema(task["db_id"])
    system = env.prompt("system_prompt.md")
    user = _build_user(task, env, schema)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    reply = env.chat(messages, max_tokens=cfg.get("max_tokens", 800))
    sql = extract_sql(reply)
    if not sql:
        return {"sql": "SELECT 1"}
    if not cfg.get("enable_repair", True):
        return {"sql": sql}

    result = _safe_execute(env, sql)
    ok = bool(result.get("ok"))
    nrows = result.get("n_rows", 0) or 0
    err = result.get("error") or ""

    notes = []
    budget = int(cfg.get("max_probes", 6))
    needs_repair = False

    if not ok:
        needs_repair = True
        notes.append(f"- Your query failed to execute. SQLite error: {err}")
    else:
        if nrows == 0:
            needs_repair = True
            notes.append("- Your query executed but returned ZERO rows. The question expects data, so a filter value, a join key or a condition is probably wrong.")
            if cfg.get("probe_zero_row_joins", True):
                jn, budget = _probe_empty_join(env, sql, budget)
                notes.extend(jn)
        if cfg.get("probe_literals", True):
            vn, budget = _probe_values(env, sql, schema, budget)
            if vn:
                needs_repair = True
                notes.extend(vn)

    if not needs_repair:
        return {"sql": sql}

    preview = ""
    if ok and nrows:
        preview = (
            f"\nCurrent result columns: {result.get('columns')}\n"
            f"First rows: {result.get('rows', [])[:5]}\n"
        )

    repair_user = (
        user
        + "\n\nYour previous query was:\n```sql\n" + sql + "\n```\n"
        + preview
        + "\nDiagnostics from running it against the real database:\n"
        + "\n".join(notes)
        + "\n\nWrite a corrected query. Use the exact stored values shown above when they reveal a mismatch "
        "(casing, spacing, abbreviation); fix join keys using the schema's foreign keys; and make sure the "
        "selected columns match exactly what the question asks for (full rows of the main table when it asks "
        "for 'details'/all information about an entity). Keep everything that was already correct. "
        "Return only the final query in one ```sql block."
    )
    try:
        reply2 = env.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": repair_user},
            ],
            max_tokens=cfg.get("repair_max_tokens", 700),
        )
    except Exception:
        return {"sql": sql}
    sql2 = extract_sql(reply2)
    if not sql2:
        return {"sql": sql}
    r2 = _safe_execute(env, sql2)
    if not r2.get("ok"):
        return {"sql": sql}
    if ok and nrows > 0 and (r2.get("n_rows", 0) or 0) == 0:
        return {"sql": sql}
    return {"sql": sql2}