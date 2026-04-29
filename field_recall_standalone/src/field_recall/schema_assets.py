from __future__ import annotations

import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Iterable


TYPE_MAP = {
    "text": "TEXT",
    "integer": "INTEGER",
    "number": "REAL",
    "real": "REAL",
    "float": "REAL",
    "double": "REAL",
    "boolean": "INTEGER",
    "bool": "INTEGER",
    "date": "TEXT",
    "time": "TEXT",
    "datetime": "TEXT",
    "year": "INTEGER",
}


class SchemaAssets:
    def __init__(self, dataset_root: str | Path):
        self.root = Path(dataset_root)

    def _first_existing(self, *relative_paths: str) -> Path | None:
        for rel in relative_paths:
            path = self.root / rel
            if path.exists():
                return path
        return None

    @lru_cache(maxsize=1)
    def _load_bird_tables(self) -> list[dict]:
        path = self._first_existing("dev_tables.json", "test_tables.json", "tables.json")
        if path is None:
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    @lru_cache(maxsize=1)
    def _tables_by_db(self) -> dict[str, dict]:
        return {row["db_id"]: row for row in self._load_bird_tables()}

    def load_tables_meta(self, db_id: str) -> dict:
        tables = self._tables_by_db()
        if db_id not in tables:
            raise KeyError(f"Unknown db_id: {db_id}")
        return tables[db_id]

    def load_schema_defs(self, db_id: str) -> dict:
        path = self.root / "schema_defs" / db_id / "definitions.json"
        if not path.exists():
            return {"db_id": db_id, "tables": {}}
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.setdefault("db_id", db_id)
        payload.setdefault("tables", {})
        return payload

    def load_db_descriptions(self, db_id: str) -> dict[str, str]:
        desc_dir = self.root / "db_descriptions" / db_id
        if not desc_dir.exists():
            return {}
        out: dict[str, str] = {}
        for path in desc_dir.glob("*.md"):
            out[path.stem] = path.read_text(encoding="utf-8").strip()
        return out

    @lru_cache(maxsize=1)
    def _load_official_column_meaning(self) -> dict:
        path = self._first_existing("column_meaning.json")
        if path is None:
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _official_column_meaning_text(self, db_id: str, table: str, column: str) -> str:
        payload = self._load_official_column_meaning()
        if not payload:
            return ""

        def _stringify(value: object) -> str:
            if value is None:
                return ""
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, list):
                return " ; ".join(_stringify(item) for item in value if _stringify(item)).strip()
            if isinstance(value, dict):
                for key in (
                    "column_meaning",
                    "meaning",
                    "description",
                    "desc",
                    "summary",
                    "comment",
                    "value",
                ):
                    if key in value:
                        text = _stringify(value.get(key))
                        if text:
                            return text
                return " ; ".join(
                    f"{k}: {_stringify(v)}" for k, v in value.items() if _stringify(v)
                ).strip()
            return str(value).strip()

        def _lookup_casefold(mapping: dict, key: str) -> object | None:
            if key in mapping:
                return mapping[key]
            key_lc = key.lower()
            for cand_key, value in mapping.items():
                if str(cand_key).lower() == key_lc:
                    return value
            return None

        db_payload = _lookup_casefold(payload, db_id)
        if isinstance(db_payload, dict):
            table_payload = _lookup_casefold(db_payload, table)
            if isinstance(table_payload, dict):
                col_payload = _lookup_casefold(table_payload, column)
                text = _stringify(col_payload)
                if text:
                    return text
            # Support flat keys under each db: "table.column" or "table::column".
            for flat_key in (f"{table}.{column}", f"{table}::{column}", f"{table}/{column}"):
                flat_payload = _lookup_casefold(db_payload, flat_key)
                text = _stringify(flat_payload)
                if text:
                    return text

        # Support a global flat mapping: "db.table.column".
        for flat_key in (
            f"{db_id}.{table}.{column}",
            f"{db_id}::{table}::{column}",
            f"{db_id}/{table}/{column}",
        ):
            flat_payload = _lookup_casefold(payload, flat_key)
            text = _stringify(flat_payload)
            if text:
                return text
        return ""

    def _column_records(self, meta: dict) -> list[dict]:
        rows = []
        for idx, ((table_idx, col_name), (_, readable_name), col_type) in enumerate(
            zip(meta["column_names_original"], meta["column_names"], meta["column_types"])
        ):
            rows.append(
                {
                    "global_idx": idx,
                    "table_idx": table_idx,
                    "column_name": col_name,
                    "readable_name": readable_name,
                    "column_type": col_type,
                }
            )
        return rows

    def _quote_ident(self, name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    def load_schema_sql(self, db_id: str) -> str:
        candidates = [
            self.root / "schemas" / f"{db_id}.sql",
            self.root / db_id / "schema.sql",
            self.root / "dev_databases" / db_id / f"{db_id}.sqlite",
            self.root / "test_databases" / db_id / f"{db_id}.sqlite",
            self.root / "databases" / db_id / f"{db_id}.sqlite",
        ]
        for path in candidates:
            if path.suffix == ".sql" and path.exists():
                return path.read_text(encoding="utf-8").strip()
            if path.suffix == ".sqlite" and path.exists():
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    rows = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    ).fetchall()
                finally:
                    conn.close()
                ddl = [r[0].strip() + (";" if not r[0].strip().endswith(";") else "") for r in rows if r[0]]
                if ddl:
                    return "\n\n".join(ddl)
        return self._schema_from_bird_tables(db_id)

    def render_prompt_schema_sql(self, db_id: str) -> str:
        """Return SQL-style schema text for prompts, but without CREATE TABLE prefixes."""
        schema_sql = self.load_schema_sql(db_id)
        lines = []
        for line in schema_sql.splitlines():
            line = re.sub(r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?', '', line, flags=re.IGNORECASE)
            lines.append(line)
        return "\n".join(lines).strip()

    def _schema_from_bird_tables(self, db_id: str) -> str:
        meta = self.load_tables_meta(db_id)
        columns = self._column_records(meta)
        table_names = meta["table_names_original"]
        pk_lookup: dict[int, list[int]] = {}
        for pk_idx in meta.get("primary_keys", []):
            col = columns[pk_idx]
            pk_lookup.setdefault(col["table_idx"], []).append(pk_idx)
        fk_lookup: dict[int, list[tuple[int, int]]] = {}
        for from_idx, to_idx in meta.get("foreign_keys", []):
            from_col = columns[from_idx]
            fk_lookup.setdefault(from_col["table_idx"], []).append((from_idx, to_idx))

        blocks = []
        for table_idx, table_name in enumerate(table_names):
            defs = []
            table_cols = [c for c in columns if c["table_idx"] == table_idx]
            table_pks = pk_lookup.get(table_idx, [])
            composite_pk = len(table_pks) > 1
            for col in table_cols:
                sql_type = TYPE_MAP.get(str(col["column_type"]).lower(), "TEXT")
                part = f"  {self._quote_ident(col['column_name'])} {sql_type}"
                if col["global_idx"] in table_pks and not composite_pk:
                    part += " PRIMARY KEY"
                defs.append(part)
            if composite_pk:
                pk_cols = ", ".join(self._quote_ident(columns[idx]["column_name"]) for idx in table_pks)
                defs.append(f"  PRIMARY KEY ({pk_cols})")
            for from_idx, to_idx in fk_lookup.get(table_idx, []):
                from_col = columns[from_idx]
                to_col = columns[to_idx]
                to_table = table_names[to_col["table_idx"]]
                defs.append(
                    f"  FOREIGN KEY ({self._quote_ident(from_col['column_name'])}) REFERENCES "
                    f"{self._quote_ident(to_table)} ({self._quote_ident(to_col['column_name'])})"
                )
            blocks.append(f"CREATE TABLE {self._quote_ident(table_name)} (\n" + ",\n".join(defs) + "\n);")
        return "\n\n".join(blocks)

    def load_column_meanings_map(self, db_id: str) -> dict[str, str]:
        meta = self.load_tables_meta(db_id)
        table_names = meta["table_names_original"]
        llm_defs = self.load_schema_defs(db_id).get("tables", {})
        meanings: dict[str, str] = {}
        for (table_idx, original_name), (_, readable_name) in zip(
            meta["column_names_original"], meta["column_names"]
        ):
            if table_idx < 0:
                continue
            table_name = table_names[table_idx]
            official_text = self._official_column_meaning_text(db_id, table_name, original_name)
            llm_text = (
                llm_defs.get(table_name, {})
                .get("columns", {})
                .get(original_name, "")
                .strip()
            )
            readable = str(readable_name or "").strip()
            text = official_text or llm_text or readable or str(original_name).strip()
            for key in {
                f"{table_name}.{original_name}",
                f"{table_name.lower()}.{original_name}",
                f"{table_name}.{str(original_name).lower()}",
                f"{table_name.lower()}.{str(original_name).lower()}",
            }:
                meanings[key] = text
        return meanings

    def render_column_meanings_text(self, db_id: str) -> str:
        lines = []
        for key, value in sorted(self.load_column_meanings_map(db_id).items()):
            lines.append(f"- {key}: {value}")
        return "\n".join(lines)

    def render_schema_summary(self, db_id: str, *, include_samples: bool = True) -> str:
        meta = self.load_tables_meta(db_id)
        table_names = meta["table_names_original"]
        llm_defs = self.load_schema_defs(db_id).get("tables", {})
        meanings = self.load_column_meanings_map(db_id)
        rows = self._column_records(meta)
        out: list[str] = []
        for table_idx, table_name in enumerate(table_names):
            out.append(f"## {table_name}")
            table_desc = llm_defs.get(table_name, {}).get("table_description", "").strip()
            if table_desc:
                out.append(f"table_description: {table_desc}")
            for row in rows:
                if row["table_idx"] != table_idx:
                    continue
                desc = (
                    llm_defs.get(table_name, {})
                    .get("columns", {})
                    .get(row["column_name"], "")
                    .strip()
                )
                meaning = meanings.get(f"{table_name}.{row['column_name']}") or meanings.get(
                    f"{table_name.lower()}.{str(row['column_name']).lower()}"
                )
                rendered_desc = meaning or desc or row["readable_name"] or row["column_name"]
                samples = self.sample_values(db_id, table_name, row["column_name"], limit=3) if include_samples else []
                if include_samples and samples:
                    out.append(
                        f"- {row['column_name']} ({row['column_type']}): {rendered_desc} | sample_values={samples}"
                    )
                else:
                    out.append(f"- {row['column_name']} ({row['column_type']}): {rendered_desc}")
        return "\n".join(out)

    def fk_edges(self, db_id: str) -> list[tuple[str, str, str, str]]:
        meta = self.load_tables_meta(db_id)
        table_names = meta["table_names_original"]
        columns = self._column_records(meta)
        edges: list[tuple[str, str, str, str]] = []
        for from_idx, to_idx in meta.get("foreign_keys", []):
            from_col = columns[from_idx]
            to_col = columns[to_idx]
            edges.append(
                (
                    table_names[from_col["table_idx"]].lower(),
                    from_col["column_name"].lower(),
                    table_names[to_col["table_idx"]].lower(),
                    to_col["column_name"].lower(),
                )
            )
        return edges

    def sqlite_path(self, db_id: str) -> Path | None:
        candidates = [
            self.root / "dev_databases" / db_id / f"{db_id}.sqlite",
            self.root / "test_databases" / db_id / f"{db_id}.sqlite",
            self.root / "databases" / db_id / f"{db_id}.sqlite",
            self.root / db_id / f"{db_id}.sqlite",
            self.root / f"{db_id}.sqlite",
        ]
        for path in candidates:
            if path.exists():
                return path
        return None

    def sample_values(self, db_id: str, table: str, column: str, limit: int = 3) -> list[str]:
        sqlite_path = self.sqlite_path(db_id)
        if not sqlite_path:
            return []
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
        try:
            cur = conn.execute(
                f'SELECT DISTINCT "{column}" FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL LIMIT {int(limit * 10)}'
            )
            values = []
            seen = set()
            for row in cur.fetchall():
                val = str(row[0]).strip()
                if not val or val.lower() in seen:
                    continue
                seen.add(val.lower())
                values.append(val[:64])
                if len(values) >= limit:
                    break
            return values
        except Exception:
            return []
        finally:
            conn.close()

    def render_rich_schema(
        self,
        db_id: str,
        field_rewrites: dict | None = None,
        *,
        include_samples: bool = True,
    ) -> str:
        """Render a rich Markdown schema with descriptions, rewrites, samples, and FK edges."""
        meta = self.load_tables_meta(db_id)
        table_names = meta["table_names_original"]
        llm_defs = self.load_schema_defs(db_id).get("tables", {})
        meanings = self.load_column_meanings_map(db_id)
        rows = self._column_records(meta)
        fk = self.fk_edges(db_id)

        # Build FK lookup by table (original case)
        fk_by_table: dict[str, list[str]] = {}
        for from_t, from_c, to_t, to_c in fk:
            # Find original-case names
            from_t_orig = next((t for t in table_names if t.lower() == from_t), from_t)
            to_t_orig = next((t for t in table_names if t.lower() == to_t), to_t)
            from_c_orig = from_c
            to_c_orig = to_c
            for r in rows:
                if r["column_name"].lower() == from_c and table_names[r["table_idx"]].lower() == from_t:
                    from_c_orig = r["column_name"]
                if r["column_name"].lower() == to_c and table_names[r["table_idx"]].lower() == to_t:
                    to_c_orig = r["column_name"]
            fk_by_table.setdefault(from_t_orig, []).append(
                f"- {from_t_orig}.{from_c_orig} -> {to_t_orig}.{to_c_orig}"
            )

        out: list[str] = []
        for table_idx, table_name in enumerate(table_names):
            out.append(f"## {table_name}")
            table_desc = llm_defs.get(table_name, {}).get("table_description", "").strip()
            if table_desc:
                out.append(f"table_description: {table_desc}")
            out.append("")
            for row in rows:
                if row["table_idx"] != table_idx:
                    continue
                col_name = row["column_name"]
                col_type = TYPE_MAP.get(str(row["column_type"]).lower(), "TEXT")
                desc = (
                    llm_defs.get(table_name, {})
                    .get("columns", {})
                    .get(col_name, "")
                    .strip()
                )
                meaning = meanings.get(f"{table_name}.{col_name}") or meanings.get(
                    f"{table_name.lower()}.{str(col_name).lower()}"
                )
                rendered_desc = meaning or desc or row["readable_name"] or col_name

                parts = [f"- {col_name} ({col_type}): {rendered_desc}"]

                # Add rewrites if available
                if field_rewrites:
                    rw = field_rewrites.get(table_name, {}).get(col_name, [])
                    if rw:
                        parts.append(f"rewrites={rw}")

                # Add sample values
                samples = self.sample_values(db_id, table_name, col_name, limit=3) if include_samples else []
                if include_samples and samples:
                    parts.append(f"samples={samples}")

                out.append(" | ".join(parts))

            # Add FK edges for this table
            if table_name in fk_by_table:
                out.append("")
                out.append("Foreign Keys:")
                out.extend(fk_by_table[table_name])

            out.append("")

        return "\n".join(out).strip()

    def pk_fk_columns(self, db_id: str) -> set[tuple[str, str]]:
        meta = self.load_tables_meta(db_id)
        table_names = meta["table_names_original"]
        columns = self._column_records(meta)
        out: set[tuple[str, str]] = set()
        for pk_idx in meta.get("primary_keys", []):
            col = columns[pk_idx]
            out.add((table_names[col["table_idx"]].lower(), col["column_name"].lower()))
        for from_idx, to_idx in meta.get("foreign_keys", []):
            from_col = columns[from_idx]
            to_col = columns[to_idx]
            out.add((table_names[from_col["table_idx"]].lower(), from_col["column_name"].lower()))
            out.add((table_names[to_col["table_idx"]].lower(), to_col["column_name"].lower()))
        return out
