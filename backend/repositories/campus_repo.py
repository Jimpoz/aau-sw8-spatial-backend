from datetime import datetime, timezone

from db import Database
from core.exceptions import CampusNotFound, BuildingNotFound, FloorNotFound
from models.campus import CampusCreate, BuildingCreate, FloorCreate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CampusRepository:
    def __init__(self, db: Database):
        self.db = db

    # --- Campus ---

    def create_campus(self, data: CampusCreate) -> dict:
        now = _now()
        result = self.db.execute_write(
            """
            MERGE (c:Campus {id: $id})
            SET c.name = $name,
                c.description = $description,
                c.created_at = coalesce(c.created_at, $now),
                c.updated_at = $now
            RETURN c
            """,
            {"id": data.id, "name": data.name, "description": data.description, "now": now},
        )
        return result[0]["c"]

    def get_campus(self, campus_id: str) -> dict:
        result = self.db.execute(
            "MATCH (c:Campus {id: $id}) RETURN c",
            {"id": campus_id},
        )
        if not result:
            raise CampusNotFound(campus_id)
        return result[0]["c"]

    def list_campuses(self) -> list[dict]:
        result = self.db.execute("MATCH (c:Campus) RETURN c ORDER BY c.name")
        return [r["c"] for r in result]

    def delete_campus(self, campus_id: str) -> None:
        self.db.execute_write(
            """
            MATCH (c:Campus {id: $id})
            OPTIONAL MATCH (c)-[:HAS_BUILDING]->(b:Building)-[:HAS_FLOOR]->(f:Floor)-[:HAS_SPACE]->(s:Space)
            DETACH DELETE s, f, b, c
            """,
            {"id": campus_id},
        )

    # --- Building ---

    def create_building(self, data: BuildingCreate) -> dict:
        now = _now()
        result = self.db.execute_write(
            """
            MATCH (campus:Campus {id: $campus_id})
            MERGE (b:Building {id: $id})
            SET b.name = $name,
                b.short_name = $short_name,
                b.address = $address,
                b.origin_lat = $origin_lat,
                b.origin_lng = $origin_lng,
                b.origin_bearing = $origin_bearing,
                b.floor_count = $floor_count,
                b.campus_id = $campus_id,
                b.created_at = coalesce(b.created_at, $now),
                b.updated_at = $now
            MERGE (campus)-[:HAS_BUILDING]->(b)
            RETURN b
            """,
            {**data.model_dump(), "now": now},
        )
        return result[0]["b"]

    def get_building(self, building_id: str) -> dict:
        result = self.db.execute(
            "MATCH (b:Building {id: $id}) RETURN b",
            {"id": building_id},
        )
        if not result:
            raise BuildingNotFound(building_id)
        return result[0]["b"]

    def list_buildings(self, campus_id: str) -> list[dict]:
        result = self.db.execute(
            """
            MATCH (:Campus {id: $campus_id})-[:HAS_BUILDING]->(b:Building)
            RETURN b ORDER BY b.name
            """,
            {"campus_id": campus_id},
        )
        return [r["b"] for r in result]

    # --- Floor ---

    def create_floor(self, data: FloorCreate) -> dict:
        result = self.db.execute_write(
            """
            MATCH (b:Building {id: $building_id})
            MERGE (f:Floor {id: $id})
            SET f.floor_index = $floor_index,
                f.display_name = $display_name,
                f.elevation_m = $elevation_m,
                f.floor_plan_url = $floor_plan_url,
                f.floor_plan_scale = $floor_plan_scale,
                f.floor_plan_origin_x = $floor_plan_origin_x,
                f.floor_plan_origin_y = $floor_plan_origin_y,
                f.building_id = $building_id
            MERGE (b)-[:HAS_FLOOR]->(f)
            RETURN f
            """,
            data.model_dump(),
        )
        return result[0]["f"]

    def get_floor(self, floor_id: str) -> dict:
        result = self.db.execute(
            "MATCH (f:Floor {id: $id}) RETURN f",
            {"id": floor_id},
        )
        if not result:
            raise FloorNotFound(floor_id)
        return result[0]["f"]

    def list_floors(self, building_id: str) -> list[dict]:
        result = self.db.execute(
            """
            MATCH (:Building {id: $building_id})-[:HAS_FLOOR]->(f:Floor)
            RETURN f ORDER BY f.floor_index
            """,
            {"building_id": building_id},
        )
        return [r["f"] for r in result]
