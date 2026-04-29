from __future__ import annotations

from collections import defaultdict

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope


def _direct_columns(node: exp.Expression) -> list[exp.Column]:
    """
    Collect Column references directly in this expression, stopping at Subquery
    and CTE boundaries so that inner-scope columns do not leak into the outer scope.

    Bug this fixes: sqlglot's scope.columns traverses the full expression tree
    including nested subqueries and CTE bodies, causing columns from inner scopes
    to be falsely attributed to outer tables when aliases collide (e.g. CTE uses
    ``c`` for ``card`` while the outer SELECT uses ``c`` for ``client``, producing
    phantom gold fields like ``client.card_id``).
    """
    cols: list[exp.Column] = []
    if isinstance(node, (exp.Subquery, exp.CTE)):
        return cols  # stop — this subtree belongs to a child scope
    if isinstance(node, exp.Column):
        cols.append(node)
    for child in node.args.values():
        if isinstance(child, exp.Expression):
            cols.extend(_direct_columns(child))
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, exp.Expression):
                    cols.extend(_direct_columns(item))
    return cols


def extract_physical_table_columns(sql: str) -> tuple[set[str], set[tuple[str, str]]]:
    tables: set[str] = set()
    table_columns: set[tuple[str, str]] = set()

    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except Exception:
        return tables, table_columns

    for scope in traverse_scope(tree):
        physical_selected: dict[str, str] = {}
        for alias, (_, source) in scope.selected_sources.items():
            if isinstance(source, exp.Table) and source.name:
                table_name = source.name.lower()
                physical_selected[alias.lower()] = table_name
                tables.add(table_name)

        # Only map unqualified columns when the scope has a single physical source.
        unique_tables = sorted(set(physical_selected.values()))

        # Use _direct_columns instead of scope.columns to avoid inner-subquery leakage.
        external_columns = set(scope.external_columns)
        for column in _direct_columns(scope.expression):
            if column in external_columns and (column.table or len(unique_tables) != 1):
                continue
            if not column.name or column.name == "*":
                continue

            if column.table:
                table_name = physical_selected.get(column.table.lower())
                if table_name:
                    table_columns.add((table_name, column.name.lower()))
            elif len(unique_tables) == 1:
                table_columns.add((unique_tables[0], column.name.lower()))

    return tables, table_columns


def group_columns_by_table(columns: set[tuple[str, str]]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for table, column in columns:
        grouped[table].add(column)
    return grouped
