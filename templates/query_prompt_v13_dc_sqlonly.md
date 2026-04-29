# Task:
You are an experienced database expert.
You will be given details about the database schema and you need understand the tables and columns.
Then you need to generate a SQL query given the database information, a question and some additional information.

# Instructions:
You will be using a way called "recursive divide-and-conquer approach to SQL query generation from natural language".

Here is a high level description of the steps.
1. **Divide (Decompose Sub-question with Pseudo SQL):** The complex natural language question is recursively broken down into simpler sub-questions. Each sub-question targets a specific piece of information or logic required for the final SQL query.
2. **Conquer (Real SQL for sub-questions):** For each sub-question (and the main question initially), a "pseudo-SQL" fragment is formulated. This pseudo-SQL represents the intended SQL logic but might have placeholders for answers to the decomposed sub-questions.
3. **Combine (Reassemble):** Once all sub-questions are resolved and their corresponding SQL fragments are generated, the process reverses. The SQL fragments are recursively combined by replacing the placeholders in the pseudo-SQL with the actual generated SQL from the lower levels.
4. **Final Output:** This bottom-up assembly culminates in the complete and correct SQL query that answers the original complex question.

# Important Rules:
1. **SELECT Clause:**
    - Only select columns mentioned in the user's question and with the SAME ORDER as the question requires.
    - Avoid unnecessary columns or values.
2. **Handling NULLs:**
    - If a column may contain NULL values, use `JOIN` or `WHERE <column> IS NOT NULL`.
3. **FROM/JOIN Clauses:**
    - Only include tables essential to answer the question.
4. **Thorough Question Analysis:**
    - Address all conditions mentioned in the question.
5. **DISTINCT Keyword:**
    - Use `SELECT DISTINCT` when the question requires unique values (e.g., IDs, URLs).
    - Refer to column statistics ("Total count" and "Distinct count") to determine if `DISTINCT` is necessary.
6. **Column Selection:**
    - Carefully analyze column descriptions and hints to choose the correct column when similar columns exist across tables.
7. **String Concatenation:**
    - Never use `|| ' ' ||` or any other method to concatenate strings in the `SELECT` clause.
8. **JOIN Preference:**
    - Prioritize `INNER JOIN` over nested `SELECT` statements.
9. **SQLite Functions Only:**
    - Use only functions available in SQLite.
10. **Date Processing:**
    - Utilize `STRFTIME()` for date manipulation (e.g., `STRFTIME('%Y', SOMETIME)` to extract the year).
11. **Schema Syntax:**
    - When table name or column name contains whitespace, include quotes (`table_name` or `column_name`) around the table name or column name.
12. **Value Examples:**
    - For key phrases mentioned in the question, we have provided the most similar values within the columns (TEXT-TYPE columns) denoted by "Value Examples".
13. **Foreign Key Constraints:**
    - If there are multiple tables to JOIN, you MUST ensure that the joined tables have EXPLICIT FOREIGN KEYS between them. For example, "TableA -> TableB, TableC -> TableB", directly join TableA and TableC is NOT ALLOWED, you must join TableA and TableB, and then join TableB and TableC.
14. **TEXT date columns:**
    - Date columns stored as TEXT may use non-standard formats. Always check `value_examples` first. For `YYYYMM` (e.g., `'201301'`), use `SUBSTR(col, 1, 4) = '2013'` — NEVER `BETWEEN '201301' AND '201312'`. For `YYYY/MM/DD`, match the exact separator (`/` not `-`).

# Database Schema
{{SCHEMA}}

# Question
{{QUESTION}}

# Evidence
{{EXTERNAL_KNOWLEDGE}}

# Final Reminder
Use the recursive divide-and-conquer approach internally, but DO NOT reveal your reasoning, sub-questions, pseudo-SQL, or intermediate analysis.
Only output the final executable SQLite query.

# Output

Return exactly one SQLite query in a single fenced ```sql code block.
Do not output XML.
Do not output reasoning.
Do not output any explanation text.

```sql
YOUR SQL HERE
```
