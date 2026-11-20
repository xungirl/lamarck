"""Text-to-SQL harness: draft -> execute -> grounded repair.

Contract (fixed): solve(task, env) -> {"sql": str}
  task  {"id", "db_id", "question", "hint"}
  env.chat(messages, max_tokens=None) -> str     capped per task by env.max_calls
  env.schema(db_id) -> str
  env.execute(sql, max_rows=20) -> dict          {ok, columns, rows, n_rows, error}
  env.descriptions(db_id) -> str
  env.prompt(name) -> str
  env.config -> dict
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
TABLE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?([A-Za-z_][\w$]*)", re.I)
COL_RE = re.compile(r"^[\"'`\[]?([A-Za-z_][\w$]*)[\"'`\]]?\s+(.*)$")
NON_COL = re.compile(
    r"^(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT|KEY|INDEX)\b", re.I)
TEXT_TYPE = re.compile(r"CHAR|CLOB|TEXT", re.I)
ANY_TYPE = re.compile(r"^\s*(BLOB)?\s*$", re.I)
LIT_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")
STOP = set("""a an the of for in on at to and or with without by from as is are was were be been
that this these those all any each every some most more least other others who whom whose which what
when where why how many much list show find give tell name names id ids number count total average
sum mean max min maximum minimum highest lowest greatest smallest top bottom first last next only
both either neither not no nor than then there their them they it its his her he she you your we our
us i me my do does did done have has had having will would can could should may might must shall
between among across per via about into out up down over under above below before after during
same different distinct such also very just get got make made take taken using use used based
called known given returned return returns please write query sql database table column row rows
value values record records entry entries information info detail details data item items thing
things one two three four five six seven eight nine ten year years month months day days time date
percent percentage ratio rate""".split())


def extract_sql(text):
    """Pull the final SQL statement out of a model reply."""
    text = text or ""
    blocks = [b.strip() for b in SQL_BLOCK.findall(text) if b.strip()]
    cand = blocks[-1] if blocks else text.strip()
    if not blocks:
        # unfenced reply: keep from the first SQL keyword onwards
        m = re.search(r"\b(WITH|SELECT)\b", cand, re.I)
        if m:
            cand = cand[m.start():]
    cand = cand.strip()
    # drop trailing prose lines after the statement
    lines = cand.split("\n")
    keep = []
    for ln in lines:
        s = ln.strip()
        if keep and s and not re.match(r"^[\w`\"'\(\)\*,\.\s=<>\+\-/%\|\:\[\]@#$&!?;]*$", s):
            break
        keep.append(ln)
    cand = "\n".join(keep).strip()
    cand = re.sub(r";\s*$", "", cand).strip()
    return cand


def _tables(schema):
    """[(table_name, [(col, type), ...]), ...] parsed from CREATE statements."""
    out = []
    parts = re.split(r"(?=CREATE\s+TABLE)", schema or "", flags=re.I)
    for part in parts:
        m = TABLE_RE.match(part.strip())
        if not m:
            continue
        name = m.group(1)
        body_start = part.find("(", m.end() - 1)
        if body_start < 0:
            out.append((name, []))
            continue
        depth = 0
        end = body_start
        for i in range(body_start, len(part)):
            if part[i] == "(":
                depth += 1
            elif part[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        body = part[body_start + 1:end]
        # split on top-level commas
        chunks, depth, cur = [], 0, ""
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                chunks.append(cur)
                cur = ""
            else:
                cur += ch
        chunks.append(cur)
        cols = []
        for c in chunks:
            c = c.strip()
            if not c or NON_COL.match(c):
                continue
            cm = COL_RE.match(c)
            if not cm:
                continue
            rest = cm.group(2)
            tm = re.match(r"\s*([A-Za-z]+)", rest)
            ctype = tm.group(1) if tm else ""
            cols.append((cm.group(1), ctype))
        out.append((name, cols))
    return out


def _literals(task):
    """Candidate string values mentioned in the question / hint."""
    text = (task.get("question") or "") + " " + (task.get("hint") or "")
    vals, seen = [], set()
    for m in LIT_RE.finditer(text):
        v = (m.group(1) or m.group(2) or "").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            vals.append(v)
    # unquoted multi-word proper nouns, e.g. the Rise of the Blue Beetle
    for m in re.finditer(r"\b([A-Z][\w'-]*(?:\s+[A-Za-z][\w'-]*){1,8})\b", task.get("question") or ""):
        v = m.group(1).strip()
        words = v.split()
        if len(words) < 2 or all(w.lower() in STOP for w in words):
            continue
        if v.lower() not in seen:
            seen.add(v.lower())
            vals.append(v)
    return vals[:6]


def _probe(env, schema, values):
    """Look up how the mentioned values are actually stored (generic LIKE probes)."""
    if not values:
        return ""
    tabs = _tables(schema)
    notes, budget = [], 14
    for val in values:
        core = val.strip()
        words = [w for w in re.split(r"[^A-Za-z0-9]+", core) if len(w) > 2 and w.lower() not in STOP]
        pats = []
        if words:
            pats.append("%" + words[0] + "%")
            if len(words) > 1:
                pats.append("%" + words[-1] + "%")
        pats.append("%" + core + "%")
        found = False
        for tname, cols in tabs:
            if found or budget <= 0:
                break
            for col, ctype in cols:
                if found or budget <= 0:
                    break
                if not (TEXT_TYPE.search(ctype or "") or ANY_TYPE.match(ctype or "")):
                    continue
                for pat in pats[:2]:
                    budget -= 1
                    q = ('SELECT DISTINCT "%s" FROM "%s" WHERE "%s" LIKE \'%s\' LIMIT 5'
                         % (col, tname, col, pat.replace("'", "''")))
                    try:
                        r = env.execute(q, max_rows=5)
                    except Exception:
                        break
                    if not r.get("ok"):
                        break
                    rows = [str(x[0]) for x in (r.get("rows") or []) if x and x[0] is not None]
                    if rows:
                        shown = ", ".join(repr(s) for s in rows[:5])
                        notes.append("value %r -> %s.%s actually stores: %s" % (val, tname, col, shown))
                        found = True
                        break
        if not found and budget > 0:
            notes.append("value %r was not found in any text column (check spelling/case/wording)" % val)
    return "\n".join(notes[:8])


def _result_note(res):
    if not res.get("ok"):
        return "ERROR: %s" % (res.get("error") or "unknown error")
    rows = res.get("rows") or []
    if not rows:
        return "executed OK but returned 0 rows"
    head = "; ".join(str(r) for r in rows[:5])
    return "executed OK, %s rows, columns=%s, first rows: %s" % (
        res.get("n_rows", len(rows)), res.get("columns"), head)


def solve(task, env):
    cfg = env.config or {}
    max_tokens = cfg.get("max_tokens", 900)
    schema = env.schema(task["db_id"])
    user = "Database: %s\n\nSchema:\n%s\n" % (task["db_id"], schema)
    desc = ""
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        desc = ""
    if desc.strip():
        user += "\nColumn descriptions:\n%s\n" % desc.strip()[:2500]
    user += "\nQuestion: %s" % task["question"]
    if task.get("hint"):
        user += "\nHint (authoritative, apply it literally): %s" % task["hint"]
    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user},
    ]
    reply = env.chat(messages, max_tokens=max_tokens)
    sql = extract_sql(reply)
    if not sql:
        return {"sql": "SELECT 1"}

    calls_left = getattr(env, "max_calls", 2) - 1
    if calls_left <= 0 or not cfg.get("verify", True):
        return {"sql": sql}

    try:
        res = env.execute(sql, max_rows=20)
    except Exception:
        return {"sql": sql}

    ok = bool(res.get("ok"))
    empty = ok and not (res.get("rows") or [])
    if ok and not empty:
        return {"sql": sql}

    # Draft failed or produced nothing: gather grounding evidence, then repair once.
    evidence = "Your draft query:\n%s\n\nResult: %s" % (sql, _result_note(res))
    probes = _probe(env, schema, _literals(task)) if empty else ""
    if probes:
        evidence += "\n\nActual stored values found in this database:\n%s" % probes
    evidence += (
        "\n\nWrite a corrected query for the SAME question. Fix the cause of the failure: "
        "use the exact stored spelling/case/encoding of filter values (e.g. flags may be 1/0 or 'T'/'F', "
        "degree or category labels may be singular/plural or abbreviated), check join keys and join direction, "
        "and make sure you selected the column the question asks for. "
        "Keep the requested output columns unchanged unless they caused the error. "
        "Return only one ```sql block."
    )
    messages = messages + [
        {"role": "assistant", "content": reply},
        {"role": "user", "content": evidence},
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
        return {"sql": sql2}
    if not res2.get("ok"):
        return {"sql": sql}
    if empty and not (res2.get("rows") or []):
        return {"sql": sql}
    return {"sql": sql2}