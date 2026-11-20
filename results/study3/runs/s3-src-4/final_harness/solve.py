"""Text-to-SQL harness: draft query, execute-check, one repair call when needed.

Contract (fixed): solve(task, env) -> {"sql": str}
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    return (text or "").strip().rstrip(";").strip()


def _schema_tables(schema):
    """Return {table_name: [(col_name, col_type), ...]} parsed from CREATE statements."""
    tables = {}
    for m in re.finditer(r"CREATE\s+TABLE\s+[`\"]?(\w+)[`\"]?\s*\((.*)\)", schema, re.S | re.I):
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
            first = p.split()[0].strip('`"[]')
            if first.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
                continue
            typ = p.split()[1].upper() if len(p.split()) > 1 else ""
            cols.append((first, typ))
        tables[name] = cols
    return tables


def _value_samples(task, env, schema, limit_tables=4, limit_cols=3, n_vals=12):
    """Sample distinct values of text columns whose names appear in question/hint."""
    text = (task.get("question", "") + " " + task.get("hint", "")).lower()
    words = set(IDENT.findall(text))
    tables = _schema_tables(schema)
    out = []
    used_tables = 0
    for tname, cols in tables.items():
        if used_tables >= limit_tables:
            break
        picked = []
        for cname, ctype in cols:
            toks = set(re.split(r"[_\s]+", cname.lower()))
            is_text = any(k in ctype for k in ("CHAR", "TEXT", "CLOB")) or ctype == ""
            if is_text and (cname.lower() in words or toks & words):
                picked.append(cname)
            if len(picked) >= limit_cols:
                break
        if not picked:
            continue
        used_tables += 1
        for cname in picked:
            try:
                r = env.execute(
                    f'SELECT DISTINCT "{cname}" FROM "{tname}" WHERE "{cname}" IS NOT NULL LIMIT {n_vals}'
                )
            except Exception:
                continue
            if r.get("ok") and r.get("rows"):
                vals = [str(row[0])[:60] for row in r["rows"]]
                out.append(f"{tname}.{cname}: " + ", ".join(repr(v) for v in vals))
    return "\n".join(out)


def solve(task, env):
    schema = env.schema(task["db_id"])
    user = f"Database: {task['db_id']}\n\nSchema:\n{schema}\n\nQuestion: {task['question']}"
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"
    descs = ""
    try:
        descs = env.descriptions(task["db_id"]) or ""
    except Exception:
        pass
    if descs:
        user += f"\nColumn descriptions:\n{descs}"

    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user},
    ]
    max_tokens = env.config.get("max_tokens", 800)
    reply = env.chat(messages, max_tokens=max_tokens)
    sql = extract_sql(reply)

    # Verify the draft; repair once if it fails or returns nothing.
    try:
        res = env.execute(sql)
    except Exception:
        res = {"ok": False, "error": "execution failed"}
    needs_repair = (not res.get("ok")) or res.get("n_rows", 0) == 0
    if needs_repair:
        if not res.get("ok"):
            feedback = f"The query failed with error: {res.get('error', 'unknown error')}"
        else:
            feedback = (
                "The query executed but returned 0 rows. Likely causes: a string literal that does not "
                "match the stored values exactly (case, spacing, abbreviations), a wrong join key, or an "
                "over-restrictive filter."
            )
        samples = ""
        try:
            samples = _value_samples(task, env, schema)
        except Exception:
            samples = ""
        repair_user = (
            f"Your draft query was:\n```sql\n{sql}\n```\n{feedback}\n"
        )
        if samples:
            repair_user += f"\nSample stored values of relevant text columns:\n{samples}\n"
        repair_user += (
            "\nWrite a corrected SQLite query for the original question. Match literals to the actual "
            "stored values (use LIKE '%...%' when the question says a text 'includes'/'contains' something). "
            "Re-check join keys against the schema. Return only one ```sql code block."
        )
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": repair_user})
        try:
            reply2 = env.chat(messages, max_tokens=max_tokens)
            sql2 = extract_sql(reply2)
            if sql2:
                try:
                    res2 = env.execute(sql2)
                except Exception:
                    res2 = {"ok": False}
                # Prefer the repaired query if it runs; prefer non-empty results.
                if res2.get("ok") and (res2.get("n_rows", 0) > 0 or not res.get("ok")):
                    sql = sql2
                elif res2.get("ok") and not res.get("ok"):
                    sql = sql2
        except Exception:
            pass

    return {"sql": sql}