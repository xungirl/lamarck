"""H0 text-to-SQL harness: one policy call with the full schema.

Contract (fixed): solve(task, env) -> {"sql": str}
  task  {"id", "db_id", "question", "hint"}      (hint is BIRD's evidence; "" for Spider)
  env.chat(messages, max_tokens=None) -> str     one policy call (capped per task: env.max_calls)
  env.schema(db_id) -> str                       CREATE statements of the database
  env.execute(sql, max_rows=20) -> dict          read-only run: {ok, columns, rows, n_rows, error}
  env.descriptions(db_id) -> str                 column descriptions if the dataset ships them, else ""
  env.prompt(name) -> str                        text of a file in this harness (e.g. "system_prompt.md")
  env.config -> dict                             parsed config.json
"""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    return (text or "").strip().rstrip(";").strip()


def solve(task, env):
    schema = env.schema(task['db_id'])
    hint = task.get("hint")
    
    # Step 1: Initial generation
    user_msg_1 = f"Database: {task['db_id']}\n\nSchema:\n{schema}\n\nQuestion: {task['question']}"
    if hint:
        user_msg_1 += f"\nHint: {hint}"
        
    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user_msg_1}
    ]
    
    reply_1 = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
    sql_1 = extract_sql(reply_1)
    
    # Step 2: Validation and Refinement
    # Check if we have calls left (max_calls usually 2, so we used 1, have 1 left)
    if env.max_calls > 1:
        result = env.execute(sql_1, max_rows=1)
        
        # Trigger refinement if execution failed OR returned no rows.
        # Note: We cannot easily detect "logical" errors (like wrong grouping) without ground truth,
        # so we stick to syntactic/emptiness checks to avoid unnecessary retries on valid-looking wrong answers.
        if not result["ok"] or result["n_rows"] == 0:
            error_info = result["error"] if not result["ok"] else "Query returned 0 rows."
            
            # Construct refinement prompt with FULL context (Schema + Hint + Question) 
            # so the model doesn't lose critical information during correction.
            user_msg_2 = (
                f"The previous query was:\n```sql\n{sql_1}\n```\n\n"
                f"It resulted in: {error_info}\n\n"
                f"Please analyze the schema and question again. Common issues might be:\n"
                f"- Incorrect table joins (check foreign keys).\n"
                f"- Wrong filter values (case sensitivity, exact matches vs LIKE).\n"
                f"- Selecting wrong columns (check Hint definitions).\n"
                f"- Using NULL handling incorrectly.\n"
                f"- Missing GROUP BY clauses for aggregate questions.\n\n"
                f"Generate a corrected SQLite query."
            )
            
            # Re-use the original full context to ensure schema/hint are fresh in attention
            full_context_msg = f"{user_msg_1}\n\n{user_msg_2}"
            
            messages_refine = [
                {"role": "system", "content": env.prompt("system_prompt.md")},
                {"role": "user", "content": full_context_msg}
            ]
            
            reply_2 = env.chat(messages_refine, max_tokens=env.config.get("max_tokens", 800))
            sql_2 = extract_sql(reply_2)
            
            return {"sql": sql_2}
            
    return {"sql": sql_1}