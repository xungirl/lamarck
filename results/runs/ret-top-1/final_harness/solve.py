"""H0 text-to-SQL harness: two-step generation with execution-based verification."""

import re

SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.S | re.I)


def extract_sql(text):
    blocks = [b.strip() for b in SQL_BLOCK.findall(text or "") if b.strip()]
    if blocks:
        return blocks[-1].rstrip(";").strip()
    return (text or "").strip().rstrip(";").strip()


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

    # Execute draft to check result count and basic validity
    exec_result = env.execute(draft_sql, max_rows=5)
    n_rows = exec_result.get("n_rows", -1)
    error = exec_result.get("error")
    rows_sample = exec_result.get("rows", [])[:3]

    # Revision strategy: 
    # 1. If error or 0 rows -> Fix bugs.
    # 2. If valid -> Critique for semantic correctness (columns, order, values).
    if env.max_calls >= 2:
        if error or n_rows == 0:
            diag = f"The following query returned {n_rows} rows" + (f" with error: {error}" if error else "") + "."
            if n_rows == 0:
                diag += "\nThis is suspicious — the question likely expects non-empty results."
            
            revision_user = (
                f"Database: {task['db_id']}\n\nSchema:\n{schema}\n\nQuestion: {task['question']}{hint_part}\n\n"
                f"{diag}\n\nDraft query:\n```sql\n{draft_sql}\n```\n\n"
                "Common issues causing empty results: wrong join keys, over-restrictive WHERE clauses, "
                "NULL values breaking NOT IN/IN checks, case-sensitive string mismatches. "
                "Revise the query so it correctly retrieves the expected data. "
                "Return ONLY the corrected query in a ```sql code block."
            )
        else:
            # Semantic refinement pass
            sample_str = str(rows_sample)
            revision_user = (
                f"Database: {task['db_id']}\n\nSchema:\n{schema}\n\nQuestion: {task['question']}{hint_part}\n\n"
                f"Draft query:\n```sql\n{draft_sql}\n```\n\n"
                f"This query returned {n_rows} rows. Sample output: {sample_str}\n\n"
                "Critique this draft against the question requirements:\n"
                "1. Does it return ALL requested columns? (e.g., if asked for names AND details, include both).\n"
                "2. If the question asks to 'rank' or 'order by popularity', does it provide a rank number column if implied, or just sort?\n"
                "3. Do string literals in CASE/WHERE match the question's phrasing exactly (e.g., 'Yes'/'No' vs 'True'/'False' or descriptive labels)?\n"
                "4. Are aggregation functions (COUNT/SUM) applied correctly per the hint's formula?\n"
                "If improvements are needed, rewrite the query. Otherwise, keep it.\n"
                "Return ONLY the final query in a ```sql code block."
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
