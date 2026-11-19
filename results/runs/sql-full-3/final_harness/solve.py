"""H1 text-to-SQL harness: one policy call with the full schema + self-correction for errors/empty."""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    # Fallback: if no code block, assume the whole text is SQL if it starts with SELECT/WITH etc
    stripped = (text or "").strip().rstrip(";").strip()
    if stripped.upper().startswith(("SELECT", "WITH", "INSERT", "UPDATE", "DELETE")):
        return stripped
    return stripped


def solve(task, env):
    schema = env.schema(task['db_id'])
    user = (
        f"Database: {task['db_id']}\n\nSchema:\n{schema}\n\nQuestion: {task['question']}"
    )
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"
    
    # Initial generation
    reply = env.chat(
        [{"role": "system", "content": env.prompt("system_prompt.md")}, {"role": "user", "content": user}],
        max_tokens=env.config.get("max_tokens", 800),
    )
    sql = extract_sql(reply)
    
    # Execute to check for errors or emptiness
    res = env.execute(sql)
    
    # Self-correction loop if execution fails OR 0 rows returned
    # Priority: Fix syntax/runtime errors first, then logical emptiness
    if not res["ok"]:
        error_msg = res.get("error", "Unknown error")
        correction_user = f"{user}\n\nPrevious Attempt:\n{sql}\n\nExecution Error: {error_msg}\nPlease analyze the error and return a corrected SQLite query. Ensure you use valid SQLite syntax and correct table/column names from the schema.\nReturn the corrected SQL in a ```sql block."
        
        reply2 = env.chat(
            [{"role": "system", "content": env.prompt("system_prompt.md")}, {"role": "user", "content": correction_user}],
            max_tokens=env.config.get("max_tokens", 800),
        )
        sql2 = extract_sql(reply2)
        
        if sql2 and sql2 != sql:
             # Verify the new SQL executes before accepting
             res2 = env.execute(sql2)
             if res2["ok"]:
                 sql = sql2
             else:
                 # If still failing, keep the original attempt or the second? 
                 # Usually the second is closer to intent even if buggy, but if both fail, return second.
                 sql = sql2

    elif res["ok"] and res["n_rows"] == 0:
        correction_user = f"{user}\n\nPrevious Attempt:\n{sql}\n\nResult: 0 rows.\nPlease debug the query. Common issues:\n1. Exact string matches might need LIKE or different casing/values found in schema samples.\n2. Join conditions might be incorrect.\n3. Filters might be too restrictive.\nCheck the schema carefully for actual values or relationships.\nReturn the corrected SQL in a ```sql block."
        
        reply2 = env.chat(
            [{"role": "system", "content": env.prompt("system_prompt.md")}, {"role": "user", "content": correction_user}],
            max_tokens=env.config.get("max_tokens", 800),
        )
        sql2 = extract_sql(reply2)
        
        # Only accept new SQL if it's different and still executes successfully
        if sql2 and sql2 != sql:
             res2 = env.execute(sql2)
             if res2["ok"]:
                 sql = sql2

    return {"sql": sql}