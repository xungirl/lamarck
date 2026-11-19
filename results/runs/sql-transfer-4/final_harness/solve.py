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

def extract_sql(text):
    """Extract SQL from LLM response, handling code blocks, prose, and multiple attempts."""
    if not text:
        return ""
    
    # Try to find code blocks first
    sql_blocks = re.findall(r"```(?:sql|sqlite)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if sql_blocks:
        # Take the last non-empty block
        for block in reversed(sql_blocks):
            clean_block = block.strip().rstrip(";").strip()
            if clean_block:
                return clean_block
    
    # Fallback: Find the last occurrence of a SELECT statement
    # This helps when the model writes prose then SQL without backticks
    select_matches = list(re.finditer(r"\bSELECT\b.*?(?:;|\Z)", text, re.DOTALL | re.IGNORECASE))
    if select_matches:
        last_match = select_matches[-1]
        sql_candidate = last_match.group(0).strip().rstrip(";").strip()
        # Basic cleanup: remove trailing markdown artifacts if any slipped through
        sql_candidate = re.sub(r"```\s*$", "", sql_candidate)
        return sql_candidate
        
    # Last resort: return stripped text if it looks like SQL (starts with SELECT/WITH/INSERT etc)
    stripped = text.strip().rstrip(";").strip()
    if re.match(r"^\s*(WITH|SELECT|INSERT|UPDATE|DELETE)\b", stripped, re.IGNORECASE):
        return stripped
        
    return stripped

def validate_and_fix_sql(sql, env, db_id):
    """Attempt to execute SQL. If it fails due to common issues, try simple fixes."""
    if not sql:
        return sql
    
    result = env.execute(sql, db_id=db_id, max_rows=1)
    
    if result["ok"]:
        return sql
        
    error_msg = result.get("error", "")
    
    # Handle "unrecognized token" often caused by leftover markdown or prose
    if "unrecognized token" in error_msg.lower():
        # Try extracting again more aggressively from the raw error context isn't possible here,
        # but we can assume the extraction failed or the model output was messy.
        # We already tried extraction. If it still fails, return original.
        pass
        
    return sql

def solve(task, env):
    user_content = (
        f"Database: {task['db_id']}\n\nSchema:\n{env.schema(task['db_id'])}\n\nQuestion: {task['question']}"
    )
    if task.get("hint"):
        user_content += f"\nHint: {task['hint']}"
        
    messages = [
        {"role": "system", "content": env.prompt("system_prompt.md")},
        {"role": "user", "content": user_content}
    ]
    
    reply = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
    
    sql = extract_sql(reply)
    
    # Optional: Validate via execution if allowed, but contract says only 1 chat call usually.
    # The harness allows env.execute. We can use it to catch gross syntax errors, 
    # but fixing them automatically is hard. We'll just return the extracted SQL.
    
    return {"sql": sql}