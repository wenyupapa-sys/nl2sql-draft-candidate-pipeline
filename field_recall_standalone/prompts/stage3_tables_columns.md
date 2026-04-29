You are a database expert performing schema linking.

## Relevant Schema
{schema_summary}

## Column Meanings
{column_meanings}

## External Knowledge
{evidence}

## Question
{question}

## Task
Identify ALL tables and columns needed to answer this question.

Rules:
1. Include ALL tables needed, even bridge/join tables.
2. For each table, list the specific columns needed.
3. If unsure, include MORE columns rather than miss necessary ones.
4. Include columns for JOINs (foreign keys).
5. Include columns mentioned in WHERE, GROUP BY, ORDER BY, HAVING.

Output ONLY valid JSON, no markdown fences:
{{"tables": {{"table1": ["col1", "col2"], "table2": ["col3", "col4"]}}, "reasoning": "..."}}
