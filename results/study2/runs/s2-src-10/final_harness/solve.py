"""Text-to-SQL harness: draft -> execute -> evidence-backed repair (<= 2 policy calls).

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
IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
STRING_LIT = re.compile(r"'((?:[^']|'')*)'")
STOP = set(
    """select from where group by order having limit offset as on join inner left right full outer
    cross union all distinct and or not in is null like between exists case when then else end asc desc
    count sum avg min max cast coalesce ifnull nullif substr lower upper trim length abs round total
    group_concat instr replace iif julianday date time strftime with recursive over partition row_number
    rank dense_rank true false escape collate nocase natural using except intersect values int integer
    text real blob numeric char varchar primary key foreign references default check unique index table
    view create drop insert update delete set pragma explain query plan top percent ties current_date
    current_time current_timestamp""".split()
)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    t = (text or "").strip()
    m = re.search(r"(?is)\b(select|with)\b.*", t)
    if m:
        t = m.group(0)
    return t.rstrip(";").strip()


def _idents(sql):
    cleaned = STRING_LIT.sub("''", sql or "")
    return [w for w in IDENT.findall(cleaned) if w.lower() not in STOP]


def _tables(schema_text):
    out = []
    for m in re.finditer(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`\[]?([A-Za-z_][\w]*)', schema_text or "", re.I):
        name = m.group(1)
        start = schema_text.find("(", m.end())
        if start < 0:
            continue
        depth, i = 0, start
        while i < len(schema_text):
            c = schema_text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = schema_text[start + 1:i]
        cols = []
        depth = 0
        cur = ""
        parts = []
        for c in body:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            if c == "," and depth == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += c
        parts.append(cur)
        for p in parts:
            p = p.strip()
            cm = re.match(r'["`\[]?([A-Za-z_][\w]*)', p)
            if cm and cm.group(1).lower() not in ("primary", "foreign", "unique", "check", "constraint"):
                cols.append(cm.group(1))
        out.append((name, cols))
    return out


def _fmt(v):
    s = str(v)
    if len(s) > 60:
        s = s[:57] + "..."
    return repr(s)


def _evidence(env, db_id, sql, budget=3500):
    """Sampled real cell values for the tables/columns the draft touches."""
    try:
        schema_text = env.schema(db_id)
    except Exception:
        return ""
    tabs = _tables(schema_text)
    used = set(w.lower() for w in _idents(sql))
    scored = []
    for name, cols in tabs:
        score = (2 if name.lower() in used else 0) + sum(1 for c in cols if c.lower() in used)
        if score:
            scored.append((score, name, cols))
    scored.sort(key=lambda x: -x[0])
    lines = []
    size = 0
    for _, name, cols in scored[:4]:
        pick = [c for c in cols if c.lower() in used][:5]
        if not pick:
            pick = cols[:3]
        if not pick:
            continue
        proj = ", ".join('"%s"' % c for c in pick)
        try:
            res = env.execute('SELECT DISTINCT %s FROM "%s" LIMIT 12' % (proj, name), max_rows=12)
        except Exception:
            continue
        if not res.get("ok") or not res.get("rows"):
            continue
        head = "%s(%s)" % (name, ", ".join(pick))
        vals = ["(" + ", ".join(_fmt(v) for v in row) + ")" for row in res["rows"][:12]]
        chunk = head + " sample values: " + "; ".join(vals)
        if size + len(chunk) > budget:
            chunk = chunk[: max(0, budget - size)]
        if chunk:
            lines.append(chunk)
            size += len(chunk)
        if size >= budget:
            break
    return "\n".join(lines)


def solve(task, env):
    db_id = task["db_id"]
    question = task["question"]
    hint = task.get("hint") or ""
    schema_text = env.schema(db_id)
    max_tokens = env.config.get("max_tokens", 800)
    max_calls = getattr(env, "max_calls", 2)

    user = "Database: %s\n\nSchema:\n%s\n\nQuestion: %s" % (db_id, schema_text, question)
    if hint:
        user += "\nHint (authoritative domain knowledge / formulas - follow it exactly): %s" % hint
    try:
        desc = env.descriptions(db_id)
    except Exception:
        desc = ""
    if desc and desc.strip():
        d = desc.strip()
        user += "\n\nColumn descriptions:\n" + (d[:2500] if len(d) > 2500 else d)

    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user},
    ]
    reply = env.chat(messages, max_tokens=max_tokens)
    sql = extract_sql(reply)
    if not sql:
        return {"sql": "SELECT 1"}

    try:
        res = env.execute(sql, max_rows=20)
    except Exception:
        res = {"ok": False, "error": "execution failed", "rows": [], "n_rows": 0}

    needs_fix = (not res.get("ok")) or (res.get("n_rows", 0) == 0)
    if not needs_fix or max_calls < 2:
        return {"sql": sql}

    if not res.get("ok"):
        problem = "It FAILED with error: %s" % str(res.get("error"))[:300]
    else:
        problem = (
            "It executed but returned ZERO rows. Most likely a filter does not match how values are "
            "actually stored (case, extra/missing spaces, abbreviations, wrong column, wrong join key, "
            "or a NULL trap in NOT IN)."
        )
    ev = _evidence(env, db_id, sql)
    followup = (
        "Your draft query:\n```sql\n%s\n```\n%s\n\n"
        "Real data sampled from the database (use these exact spellings):\n%s\n\n"
        "Rewrite the query so it correctly answers the ORIGINAL question:\n%s\n"
        "%s"
        "Rules for the rewrite:\n"
        "- Match text values to what is really stored: prefer case-insensitive partial matching "
        "(col LIKE '%%value%%') over strict equality when the stored spelling may differ; strip stray "
        "spaces from the search term.\n"
        "- Re-check join keys and the direction of the relationship; check the other side of a "
        "many-to-many link table too.\n"
        "- For 'who has no X' questions prefer NOT EXISTS or a LEFT JOIN ... IS NULL over NOT IN "
        "(NULLs make NOT IN return nothing).\n"
        "- Keep exactly the columns the question asks for; if it asks for 'details' or all information "
        "about an entity, select that table's columns (T1.*), not just its name.\n"
        "- Do not add filters the question did not ask for. If after checking the data an empty result "
        "is genuinely correct, keep the query.\n"
        "Return only the corrected query in a single ```sql block."
    ) % (sql, problem, ev or "(no samples available)", question, ("Hint: %s\n" % hint) if hint else "")

    messages.append({"role": "assistant", "content": reply})
    messages.append({"role": "user", "content": followup})
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
        res2 = {"ok": False, "n_rows": 0}
    if res2.get("ok") and (res2.get("n_rows", 0) > 0 or not res.get("ok")):
        return {"sql": sql2}
    if res2.get("ok") and not res.get("ok"):
        return {"sql": sql2}
    return {"sql": sql2 if res2.get("ok") else sql}