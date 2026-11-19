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
    if not text:
        return ""
    
    # Try to find code blocks first
    blocks = [b.strip() for b in SQL_BLOCK.findall(text) if b.strip()]
    if blocks:
        # Return the last non-empty block (often the final answer after reasoning)
        sql = blocks[-1]
        # Clean up trailing semicolons and whitespace
        return sql.rstrip(";").strip()
    
    # Fallback: Look for lines starting with SELECT/WITH/INSERT etc if no blocks found
    # This handles cases where model outputs raw SQL without markdown
    lines = text.split('\n')
    sql_lines = []
    capture = False
    for line in lines:
        stripped = line.strip().upper()
        if stripped.startswith(('SELECT', 'WITH', 'INSERT', 'UPDATE', 'DELETE', 'CREATE')):
            capture = True
        
        if capture:
            sql_lines.append(line)
            # Heuristic stop: if we see double newline or empty line after capturing content, maybe stop? 
            # But simple SQL might span multiple lines. Let's just grab everything from first SQL keyword onwards
            # unless it looks like prose.
            
    if sql_lines:
        candidate = '\n'.join(sql_lines).strip()
        # Remove trailing prose if any (hard to detect perfectly, but strip semicolon helps)
        return candidate.rstrip(';').strip()

    # Last resort: return original text cleaned
    return text.strip().rstrip(";").strip()

def solve(task, env):
    user = (
        f"Database: {task['db_id']}\n\nSchema:\n{env.schema(task['db_id'])}\n\nQuestion: {task['question']}"
    )
    if task.get("hint"):
        user += f"\nHint: {task['hint']}"
    
    reply = env.chat(
        [{"role": "system", "content": env.prompt("system_prompt.md")}, {"role": "user", "content": user}],
        max_tokens=env.config.get("max_tokens", 500),
    )
    return {"sql": extract_sql(reply)}