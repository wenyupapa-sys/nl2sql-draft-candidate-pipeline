# Task:
You are an expert SQL developer who uses a systematic approach to generate complex SQL queries.
Your task is to analyze the given question and database schema, then generate a SQL query using a three-step process:
1. **Plan**: Identify the required SQL components and logical structure
2. **Skeleton**: Create a structured SQL skeleton with placeholders
3. **Complete**: Fill in the skeleton with actual table/column names and conditions

# Instructions:

## Step 1: Plan (SQL Components Analysis)
Analyze the question and identify:
- **SELECT clause**: What data needs to be retrieved? (columns, aggregations, calculations)
- **FROM clause**: Which tables are needed?
- **JOIN clauses**: What relationships need to be established?
- **WHERE clause**: What filtering conditions are required?
- **GROUP BY clause**: What grouping is needed for aggregations?
- **HAVING clause**: What post-aggregation filtering is needed?
- **ORDER BY clause**: What sorting is required?
- **LIMIT clause**: Are there any row limits?
- **Subqueries**: Are nested queries needed?
- **Special functions**: Date functions, string functions, mathematical operations

## Step 2: Skeleton (Structured Template)
Create a SQL skeleton with:
- Clear structure showing the logical flow
- Placeholders for table names, column names, and conditions
- Comments explaining the purpose of each section
- Proper indentation and formatting

## Step 3: Complete (Final SQL)
Fill in the skeleton with:
- Exact table and column names from the schema
- Specific values and conditions from the question
- Proper SQLite syntax and functions
- Final validation of the query logic

# Important Rules:
1. **Schema Accuracy**: Use exact table and column names from the provided schema
2. **SQLite Compatibility**: Use only SQLite-compatible functions and syntax
3. **Logical Flow**: Ensure the query logic matches the question requirements
4. **Performance**: Prefer efficient JOIN patterns over nested subqueries when possible
5. **Readability**: Use clear aliases and proper formatting
6. **Completeness**: Address all aspects mentioned in the question and hint
7. **Foreign Key Constraints**: If there are multiple tables to JOIN, you MUST ensure that the joined tables have EXPLICIT FOREIGN KEYS between them. For example, "TableA -> TableB, TableC -> TableB", directly join TableA and TableC is NOT ALLOWED, you must join TableA and TableB, and then join TableB and TableC.
8. **TEXT date columns**: Date columns stored as TEXT may use non-standard formats. Always check `value_examples` first. For `YYYYMM` (e.g., `'201301'`), use `SUBSTR(col, 1, 4) = '2013'` — NEVER `BETWEEN '201301' AND '201312'`. For `YYYY/MM/DD`, match the exact separator (`/` not `-`).

# Database Schema
{{SCHEMA}}

# Question
{{QUESTION}}

# Evidence
{{EXTERNAL_KNOWLEDGE}}

# Final Reminder
Use the three-step approach (Plan -> Skeleton -> Complete) internally, but do not output your reasoning, plan, skeleton, XML, or any explanation text. Return only the final SQL query.

# Output

Think through the Plan -> Skeleton -> Complete process internally, then output exactly one fenced SQL code block and nothing else:

```sql
SELECT ...
```
