"""Apply Neo4j constraints and indexes.

Usage (from backend/ directory):
    python scripts/init_db.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import Database, get_db

_CYPHER_DIR = os.path.join(os.path.dirname(__file__), "..", "cypher")


def _read_statements(filename: str) -> list[str]:
    path = os.path.join(_CYPHER_DIR, filename)
    with open(path) as f:
        content = f.read()
    statements = [s.strip() for s in content.split(";") if s.strip()]
    return statements


def apply_schema(db: Database) -> None:
    for filename in ("constraints.cypher", "indexes.cypher"):
        for stmt in _read_statements(filename):
            try:
                # Schema commands (CREATE CONSTRAINT/INDEX) require auto-commit mode
                db.execute(stmt)
            except Exception as e:
                print(f"[init_db] Warning: {filename}: {e}")


if __name__ == "__main__":
    print("[init_db] Applying constraints and indexes...")
    apply_schema(get_db())
    print("[init_db] Done.")
