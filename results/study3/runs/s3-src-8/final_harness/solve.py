"""Text-to-SQL harness: draft, execute-check, one repair call if needed.

Contract (fixed): solve(task, env) -> {"sql": str}
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
LITERAL = re.compile(r"'([^']*)'")
IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    t = (text or "").strip()
    m = re.search(r"(?is)\b(select|with)\b.*", t)
    if m:
        t = m.group(0)
    return t.rstrip(";").strip()


def _tables(sql):
    return set(re.findall(r"(?i)\b(?:from|join)\s+([A-Za-z_][A-Za-z_0-9]*)", sql))


def _sample_values(env, sql, literals, limit_tables=3, limit_vals=8):
    """Sample stored values of columns compared against string literals."""
    lines = []
    tables = list(_tables(sql))[:limit_tables]
    seen = set()
    for lit in literals[:4]:
        core = lit.strip().strip("%").strip()
        if not core or len(core) < 2:
            continue
        word = re.split(r"[^A-Za-z0-9]+", core)[0]
        if not word:
            continue
        for tbl in tables:
            try:
                info = env.execute(f"PRAGMA table_info({tbl})", max_rows=60)
            except Exception:
                continue
            if not info.get("ok"):
                continue
            cols = [r[1] for r in info.get("rows", []) if len(r) > 2 and str(r[2]).upper().startswith(("TEXT", "CHAR", "VARCHAR")) or (len(r) > 2 and str(r[2]) == "")]
            for col in cols:
                key = (tbl, col, word.lower())
                if key in seen:
                    continue
                seen.add(key)
                q = (f"SELECT DISTINCT {col} FROM {tbl} WHERE {col} LIKE '%{word}%' LIMIT {limit_vals}")
                try:
                    res = env.execute(q, max_rows=limit_vals)
                except Exception:
                    continue
                if res.get("ok") and res.get("rows"):
                    vals = [str(r[0]) for r in res["rows"]]
                    lines.append(f"{tbl}.{col} values containing '{word}': {vals}")
    return "\n".join(lines[:10])


def _check(env, sql):
    try:
        return env.execute(sql, max_rows=5)
    except Exception as e:
        return {"ok": False, "error": str(e), "rows": [], "n_rows": 0}


def solve(task, env):
    user = (
        f"Database: {task['db_id']}\n\nSchema:\n{env.schema(task['db_id'])}\n\nQuestion: {task['question']}"
    )
    desc = ""
    try:
        desc = env.descriptions(task["db_id"]) or ""
    except Exception:
        pass
    if desc:
        user += f"\n\nColumn descriptions:\n{desc[:2000]}"
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"
    system = env.prompt("system_prompt.md")
    reply = env.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=env.config.get("max_tokens", 800),
    )
    sql = extract_sql(reply)
    res = _check(env, sql)

    needs_repair = (not res.get("ok")) or res.get("n_rows", 0) == 0
    if needs_repair:
        literals = [l for l in LITERAL.findall(sql) if l.strip()]
        samples = _sample_values(env, sql, literals)
        if res.get("ok"):
            problem = "The query executed but returned ZERO rows, which is suspicious."
        else:
            problem = f"The query failed with error: {res.get('error')}"
        repair = (
            user
            + f"\n\nYour previous query:\n```sql\n{sql}\n```\n{problem}\n"
        )
        if samples:
            repair += f"\nActual stored values that may be relevant:\n{samples}\n"
        repair += (
            "\nDiagnose the cause: wrong literal spelling/case/spacing (match the actual stored values above), "
            "wrong join keys or direction, overly strict filters, or wrong table. "
            "If a literal does not match exactly, use the actual stored value or a LIKE pattern. "
            "Rewrite the corrected query. Return only one ```sql code block."
        )
        try:
            reply2 = env.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": repair}],
                max_tokens=env.config.get("max_tokens", 800),
            )
            sql2 = extract_sql(reply2)
            if sql2:
                res2 = _check(env, sql2)
                if res2.get("ok") and (res2.get("n_rows", 0) > 0 or not res.get("ok")):
                    sql = sql2
                elif res2.get("ok") and not res.get("ok"):
                    sql = sql2
        except Exception:
            pass
    return {"sql": sql}