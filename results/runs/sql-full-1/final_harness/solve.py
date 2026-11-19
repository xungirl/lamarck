"""H0 text-to-SQL harness: two-step generation with execution-based verification."""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    return (text or "").strip().rstrip(";").strip()


def format_rows(rows, max_rows=5):
    """Format sample rows for prompt inclusion."""
    if not rows:
        return "(no rows returned)"
    lines = []
    for i, row in enumerate(rows[:max_rows]):
        # Convert tuples/lists to readable string representation
        row_str = str(row)
        lines.append(f"  Row {i+1}: {row_str}")
    return "\n".join(lines)


def solve(task, env):
    schema = env.schema(task["db_id"])
    hint_part = f"\nHint: {task['hint']}" if task.get("hint") else ""
    
    user_msg = (
        f"Database: {task['db_id']}\n\nSchema:\n{schema}\n\nQuestion: {task['question']}{hint_part}\n\n"
        "Write a SQLite query. Return ONLY the query in a ```sql code block."
    )

    # First call: generate draft
    reply1 = env.chat(
        [{"role": "system", "content": env.prompt("system_prompt.md")},
         {"role": "user", "content": user_msg}],
        max_tokens=env.config.get("max_tokens", 800),
    )
    draft_sql = extract_sql(reply1)

    # Execute draft to check result
    exec_result = env.execute(draft_sql, max_rows=10)
    n_rows = exec_result.get("n_rows", -1)
    error = exec_result.get("error")
    columns = exec_result.get("columns", [])
    rows = exec_result.get("rows", [])

    # Revision strategy: 
    # - If error or 0 rows: revise as before (wrong logic)
    # - If >0 rows: verify output format against question semantics
    needs_revision = False
    revision_reason = ""
    
    if error:
        needs_revision = True
        revision_reason = f"The query failed with error: {error}. Fix the syntax or logic."
    elif n_rows == 0:
        needs_revision = True
        revision_reason = ("The query returned 0 rows. Common issues: wrong join keys, over-restrictive WHERE clauses, "
                          "NULL values breaking NOT IN/IN checks, case-sensitive string mismatches.")
    elif n_rows > 0 and env.max_calls >= 2:
        # Always verify non-empty results for format/content correctness
        needs_revision = True
        revision_reason = ("Verify the query output matches the question's expected format. Check: "
                          "1) Column count and order match what was asked (e.g., if 'rank' is requested, include a rank column). "
                          "2) String literals match exact data values (case-sensitive). "
                          "3) For yes/no questions, ensure output uses appropriate labels if hinted. "
                          "4) Aggregations return single values unless grouped explicitly.")

    if needs_revision and env.max_calls >= 2:
        sample_output = format_rows(rows, max_rows=5)
        cols_info = f"Columns: {columns}" if columns else "No column info available"
        
        revision_user = (
            f"Database: {task['db_id']}\n\nSchema:\n{schema}\n\nQuestion: {task['question']}{hint_part}\n\n"
            f"{revision_reason}\n\nDraft query:\n```sql\n{draft_sql}\n```\n\n"
            f"Current output ({n_rows} rows):\n{cols_info}\n{sample_output}\n\n"
            "Revise the query to correctly answer the question. Pay special attention to:\n"
            "- Exact string matching for filters (case-sensitive)\n"
            "- Including all requested attributes (names, ranks, counts) in SELECT\n"
            "- Proper handling of NULLs in subqueries/NOT IN\n"
            "- Matching output format to natural language expectations\n"
            "Return ONLY the corrected query in a ```sql code block."
        )
        reply2 = env.chat(
            [{"role": "system", "content": env.prompt("system_prompt.md")},
             {"role": "user", "content": revision_user}],
            max_tokens=env.config.get("max_tokens", 800),
        )
        final_sql = extract_sql(reply2)
    else:
        final_sql = draft_sql

    return {"sql": final_sql}