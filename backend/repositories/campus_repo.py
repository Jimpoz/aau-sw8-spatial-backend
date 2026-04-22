from datetime import datetime, timezone

from db import Database
from core.exceptions import (
    CampusNotFound,
    BuildingNotFound,
    FloorNotFound,
    OrganizationNotFound,
)
from models.campus import (
    CampusCreate,
    BuildingCreate,
    FloorCreate,
    OrganizationCreate,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entity_type_str(value) -> str:
    """Accept enum instances or bare strings."""
    if value is None:
        return "OTHER"
    return value.value if hasattr(value, "value") else str(value)


class OrganizationRepository:
    def __init__(self, db: Database):
        self.db = db

    def create_organization(self, data: OrganizationCreate) -> dict:
        now = _now()
        result = self.db.execute_write(
            """
            MERGE (o:Organization {id: $id})
            SET o.name = $name,
                o.entity_type = $entity_type,
                o.description = $description,
                o.created_at = coalesce(o.created_at, $now),
                o.updated_at = $now
            RETURN o
            """,
            {
                "id": data.id,
                "name": data.name,
                "entity_type": _entity_type_str(data.entity_type),
                "description": data.description,
                "now": now,
            },
        )
        return result[0]["o"]

    def get_organization(self, organization_id: str) -> dict:
        result = self.db.execute(
            "MATCH (o:Organization {id: $id}) RETURN o",
            {"id": organization_id},
        )
        if not result:
            raise OrganizationNotFound(organization_id)
        return result[0]["o"]

    def list_organizations(self) -> list[dict]:
        result = self.db.execute(
            "MATCH (o:Organization) RETURN o ORDER BY o.name"
        )
        return [r["o"] for r in result]

    def list_campuses(self, organization_id: str) -> list[dict]:
        result = self.db.execute(
            """
            MATCH (:Organization {id: $org_id})-[:HAS_CAMPUS]->(c:Campus)
            RETURN c ORDER BY c.name
            """,
            {"org_id": organization_id},
        )
        return [r["c"] for r in result]

    def delete_organization(self, organization_id: str) -> None:
        self.db.execute_write(
            """
            MATCH (o:Organization {id: $id})
            OPTIONAL MATCH (o)-[:HAS_CAMPUS]->(c:Campus)-[:HAS_BUILDING]->(b:Building)-[:HAS_FLOOR]->(f:Floor)-[:HAS_SPACE]->(s:Space)
            DETACH DELETE s, f, b, c, o
            """,
            {"id": organization_id},
        )


class CampusRepository:
    def __init__(self, db: Database):
        self.db = db

    # --- Campus ---

    def create_campus(self, data: CampusCreate) -> dict:
        now = _now()
        params = {
            "id": data.id,
            "name": data.name,
            "description": data.description,
            "organization_id": data.organization_id,
            "now": now,
        }
        if data.organization_id:
            result = self.db.execute_write(
                """
                MATCH (o:Organization {id: $organization_id})
                MERGE (c:Campus {id: $id})
                SET c.name = $name,
                    c.description = $description,
                    c.organization_id = $organization_id,
                    c.created_at = coalesce(c.created_at, $now),
                    c.updated_at = $now
                MERGE (o)-[:HAS_CAMPUS]->(c)
                RETURN c
                """,
                params,
            )
        else:
            result = self.db.execute_write(
                """
                MERGE (c:Campus {id: $id})
                SET c.name = $name,
                    c.description = $description,
                    c.organization_id = $organization_id,
                    c.created_at = coalesce(c.created_at, $now),
                    c.updated_at = $now
                RETURN c
                """,
                params,
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

    def list_campuses(self, organization_id: str | None = None) -> list[dict]:
        if organization_id:
            result = self.db.execute(
                """
                MATCH (c:Campus {organization_id: $organization_id})
                RETURN c ORDER BY c.name
                """,
                {"organization_id": organization_id},
            )
        else:
            result = self.db.execute(
                "MATCH (c:Campus) RETURN c ORDER BY c.name"
            )
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
        organization_id = data.organization_id
        if not organization_id:
            campus_lookup = self.db.execute(
                "MATCH (c:Campus {id: $id}) RETURN c.organization_id AS org_id",
                {"id": data.campus_id},
            )
            if campus_lookup:
                organization_id = campus_lookup[0]["org_id"]

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
                b.organization_id = $organization_id,
                b.created_at = coalesce(b.created_at, $now),
                b.updated_at = $now
            MERGE (campus)-[:HAS_BUILDING]->(b)
            RETURN b
            """,
            {**data.model_dump(), "organization_id": organization_id, "now": now},
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
