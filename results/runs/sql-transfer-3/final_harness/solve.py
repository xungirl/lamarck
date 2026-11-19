"""H1 text-to-SQL harness: one policy call with the full schema + self-correction for errors/empty."""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    return (text or "").strip().rstrip(";").strip()


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
    
    # Self-correction loop if error OR 0 rows returned
    if not res["ok"] or res["n_rows"] == 0:
        error_msg = res.get("error", "Unknown error") if not res["ok"] else "Query returned 0 rows."
        
        correction_user = f"{user}\n\nPrevious Attempt:\n{sql}\n\nResult: {error_msg}\nPlease debug the query. Common issues:\n1. Syntax errors or unsupported functions.\n2. Wrong table names or column names (check schema carefully).\n3. Exact string matches might need LIKE or different casing/values found in schema samples.\n4. Join conditions might be incorrect.\n5. Filters might be too restrictive.\nReturn the corrected SQL in a ```sql block."
        
        reply2 = env.chat(
            [{"role": "system", "content": env.prompt("system_prompt.md")}, {"role": "user", "content": correction_user}],
            max_tokens=env.config.get("max_tokens", 800),
        )
        sql2 = extract_sql(reply2)
        
        # Only accept new SQL if it's different
        if sql2 and sql2 != sql:
             sql = sql2

    return {"sql": sql}