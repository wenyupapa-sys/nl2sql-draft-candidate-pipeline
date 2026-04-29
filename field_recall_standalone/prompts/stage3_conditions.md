You are extracting structured filter conditions from a text-to-SQL question.

# Task

- Extract explicit or strongly implied filter conditions from the question and evidence.
- Include phrase, operator, and value.
- If the operator is unclear, use the closest simple operator such as =, >, <, LIKE, BETWEEN, IN.
- Pay attention to sample values in the schema — they hint at the exact form of values in the database.

# Output Format

JSON only:
{{
  "conditions": [
    {{"phrase": "Los Angeles County", "operator": "=", "value": "Los Angeles County"}}
  ]
}}

# Question

{question}

# Evidence

{evidence}

# Schema

{schema_summary}
