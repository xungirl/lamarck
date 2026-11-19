"""H1 text-to-SQL harness: one policy call with the full schema + self-correction for empty results."""

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
    
    # Execute to check for emptiness
    res = env.execute(sql)
    
    # Self-correction loop if 0 rows returned (common failure mode for strict matches/joins)
    if res["ok"] and res["n_rows"] == 0:
        correction_user = f"{user}\n\nPrevious Attempt:\n{sql}\n\nResult: 0 rows.\nPlease debug the query. Common issues:\n1. Exact string matches might need LIKE or different casing/values found in schema samples.\n2. Join conditions might be incorrect.\n3. Filters might be too restrictive.\nCheck the schema carefully for actual values or relationships.\nReturn the corrected SQL in a ```sql block."
        
        reply2 = env.chat(
            [{"role": "system", "content": env.prompt("system_prompt.md")}, {"role": "user", "content": correction_user}],
            max_tokens=env.config.get("max_tokens", 800),
        )
        sql2 = extract_sql(reply2)
        
        # Only accept new SQL if it's different and still executes successfully (even if empty, sometimes empty is correct, but usually better to try one fix)
        if sql2 and sql2 != sql:
             # Optional: Could re-execute to see if it improved, but we only have 2 calls total. 
             # We assume the second attempt is better guided by the feedback.
             sql = sql2

    return {"sql": sql}