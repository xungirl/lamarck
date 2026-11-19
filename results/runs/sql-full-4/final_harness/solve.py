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
    # Try to find code blocks first
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        # Return the last block found, assuming it's the final answer
        sql = blocks[-1].rstrip(";").strip()
        # Basic cleanup: remove any leading/trailing whitespace or newlines within the block
        lines = sql.split('\n')
        clean_lines = [line for line in lines if line.strip() and not line.strip().startswith('#')]
        return '\n'.join(clean_lines).strip()
    
    # Fallback: try to find SQL starting keywords if no code block
    sql_patterns = [
        r"(?i)(SELECT\s+.*?)(?:;|\Z)",
        r"(?i)(WITH\s+.*?)(?:;|\Z)"
    ]
    for pattern in sql_patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            sql = match.group(1).strip().rstrip(';').strip()
            # Remove potential trailing prose if it looks like a sentence ended
            if sql.endswith('.') or sql.lower().endswith('answer'):
                pass # Keep it, might be part of a string literal, but usually safe to trim obvious prose
            return sql
            
    # Last resort: return stripped text
    return (text or "").strip().rstrip(";").strip()


def solve(task, env):
    user = (
        f"Database: {task['db_id']}\n\nSchema:\n{env.schema(task['db_id'])}\n\nQuestion: {task['question']}"
    )
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"
    reply = env.chat(
        [{"role": "system", "content": env.prompt("system_prompt.md")}, {"role": "user", "content": user}],
        max_tokens=env.config.get("max_tokens", 800),
    )
    return {"sql": extract_sql(reply)}