"""Import a campus map from a JSON file.

Usage (from backend/ directory):
    python scripts/import_map.py path/to/map.json
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from db import get_db
from models.map_import import MapImportSchema
from services.import_service import ImportService
from services.gds_service import GdsService


def main(path: str) -> None:
    with open(path) as f:
        data = json.load(f)

    schema = MapImportSchema.model_validate(data)
    db = get_db()

    print(f"[import_map] Importing campus '{schema.campus.name}' ({schema.campus.id})...")
    result = ImportService(db).import_map(schema)
    print(
        f"[import_map] Done: {result['spaces_imported']} spaces, "
        f"{result['connections_imported']} connections."
    )

    print("[import_map] Refreshing GDS navigation graph projection...")
    ok = GdsService(db).refresh_projection()
    print(f"[import_map] GDS: {'refreshed' if ok else 'not available (GDS plugin missing?)'}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/import_map.py <map.json>")
        sys.exit(1)
    main(sys.argv[1])
