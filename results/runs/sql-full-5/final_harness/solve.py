"""H1 text-to-SQL harness with self-correction for empty/error results."""

import re
import json

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)

def extract_sql(text):
    """Extract SQL from code blocks or plain text, stripping trailing semicolons."""
    if not text:
        return ""
    
    # Try code block first
    blocks = [b.strip() for b in SQL_BLOCK.findall(text) if b.strip()]
    if blocks:
        sql = blocks[-1]
    else:
        # Fallback: take the whole string but try to isolate SQL-like parts
        # Simple heuristic: remove markdown artifacts if any
        sql = text.strip()
        
    # Clean up: remove leading/trailing whitespace and semicolons
    sql = sql.strip().rstrip(';').strip()
    
    # Remove inline comments if they are on their own line or after --
    # But be careful not to remove '--' inside strings. 
    # For now, just strip basic trailing comments if obvious, 
    # otherwise rely on SQLite parser to ignore them usually.
    lines = []
    for line in sql.split('\n'):
        # Naive comment removal: if line starts with --, skip it
        stripped = line.strip()
        if stripped.startswith('--'):
            continue
        lines.append(line)
        
    return '\n'.join(lines).strip()

def solve(task, env):
    db_id = task['db_id']
    question = task['question']
    hint = task.get('hint', '')
    
    # Get schema and descriptions
    schema = env.schema(db_id)
    desc = env.descriptions(db_id)
    
    system_prompt = env.prompt("system_prompt.md")
    
    # Construct initial user prompt
    user_content = f"Database: {db_id}\n\nSchema:\n{schema}"
    if desc:
        user_content += f"\n\nDescriptions:\n{desc}"
    user_content += f"\n\nQuestion: {question}"
    if hint:
        user_content += f"\nHint: {hint}"
        
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]
    
    # First attempt
    reply = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
    sql = extract_sql(reply)
    
    if not sql:
        return {"sql": ""}
        
    # Execute to check validity
    exec_res = env.execute(sql, max_rows=20)
    
    # Check for failure modes: Error or Empty Result (unless aggregation implies empty is valid? 
    # Usually empty result means wrong filters/joins in Spider/BIRD context unless COUNT/SUM is used)
    is_error = not exec_res.get('ok', False)
    is_empty = exec_res.get('n_rows', 0) == 0
    
    # Heuristic: If it's an error OR it returned 0 rows AND doesn't look like an aggregate-only query,
    # we might want to retry. 
    # Note: SELECT COUNT(*) ... returns 1 row even if count is 0. 
    # So n_rows==0 is a strong signal of wrong WHERE/JOIN for non-aggregate queries.
    # We assume if n_rows==0, it's likely a mistake unless the question asks for something that genuinely has no matches (rare in eval sets).
    
    needs_retry = is_error or is_empty
    
    if needs_retry:
        # Prepare feedback for second attempt
        feedback = "The generated SQL resulted in "
        if is_error:
            feedback += f"an error: {exec_res.get('error', 'Unknown error')}."
        else:
            feedback += "0 rows. Please check the JOIN conditions, WHERE clauses, and column names carefully against the schema."
            
        feedback += "\n\nPlease generate a corrected SQL query."
        
        # Add previous attempt and feedback to history
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": feedback})
        
        # Second attempt
        reply2 = env.chat(messages, max_tokens=env.config.get("max_tokens", 800))
        sql2 = extract_sql(reply2)
        
        if sql2:
            # Optionally verify again, but contract limits calls. 
            # We trust the second attempt more as it had specific feedback.
            # However, if sql2 also fails, maybe stick to sql1 if sql1 was just empty but syntactically correct?
            # Let's check sql2 quickly if possible without burning tokens? No, execute is free but chat is limited.
            # We can execute sql2 to see if it's better than sql1.
            exec_res2 = env.execute(sql2, max_rows=20)
            
            if exec_res2.get('ok', False) and exec_res2.get('n_rows', 0) > 0:
                return {"sql": sql2}
            elif exec_res2.get('ok', False) and not is_error:
                # sql2 works, returns rows (or 0 but ok). Prefer it over broken sql1.
                return {"sql": sql2}
            elif is_error and exec_res2.get('ok', False):
                # sql1 errored, sql2 runs. Prefer sql2.
                return {"sql": sql2}
                
            # If both are problematic, return the one that ran without error (if any), else latest
            if not is_error:
                return {"sql": sql}
            return {"sql": sql2}
            
    return {"sql": sql}