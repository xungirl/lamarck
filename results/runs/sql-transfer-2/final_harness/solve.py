"""H0 text-to-SQL harness: one policy call with the full schema + data sampling on retry."""

import re
import json

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)
TABLE_RE = re.compile(r"\bFROM\s+([a-zA-Z0-9_]+)|\bJOIN\s+([a-zA-Z0-9_]+)", re.I)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    return (text or "").strip().rstrip(";").strip()


def get_tables_from_sql(sql):
    tables = set()
    for match in TABLE_RE.finditer(sql):
        t = match.group(1) or match.group(2)
        if t:
            tables.add(t.lower())
    return list(tables)[:3] # Limit to top 3 tables to save tokens


def solve(task, env):
    system_content = env.prompt("system_prompt.md")
    
    user_content = (
        f"Database: {task['db_id']}\n\nSchema:\n{env.schema(task['db_id'])}\n\nQuestion: {task['question']}"
    )
    if task.get("hint"):
        user_content += f"\nHint: {task['hint']}"

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content}
    ]

    # First attempt
    reply = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
    sql = extract_sql(reply)
    
    # Validate execution
    result = env.execute(sql, max_rows=1)
    
    # Retry logic: if query failed or returned no rows, ask model to fix it WITH DATA SAMPLES
    if not result["ok"] or result["n_rows"] == 0:
        tables = get_tables_from_sql(sql)
        samples_str = ""
        
        # Fetch sample rows for tables involved in the failed query
        for t in tables:
            try:
                # Simple probe to get real data values
                probe_sql = f"SELECT * FROM {t} LIMIT 3"
                probe_res = env.execute(probe_sql, max_rows=3)
                if probe_res["ok"]:
                    samples_str += f"\nSample rows from '{t}':\n{json.dumps(probe_res['rows'], default=str)}\n"
            except Exception:
                pass
        
        feedback = f"The previous query returned 0 rows or failed.\n"
        if not result["ok"]:
             feedback += f"Error: {result['error']}\n"
        else:
             feedback += "Check your WHERE clauses and JOIN conditions against the actual data values.\n"
             
        if samples_str:
            feedback += f"\nHere is some sample data from the relevant tables to help you correct value mismatches:\n{samples_str}"

        retry_user_content = user_content + f"\n\nPrevious Query:\n```sql\n{sql}\n```\nFeedback: {feedback}\nPlease write a corrected SQLite query."
        
        retry_messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": retry_user_content}
        ]
        
        reply2 = env.chat(retry_messages, max_tokens=env.config.get("max_tokens", 800))
        sql2 = extract_sql(reply2)
        
        return {"sql": sql2}

    return {"sql": sql}
