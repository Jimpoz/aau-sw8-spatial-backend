import json
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
    BuildingUpdate,
    FloorCreate,
    FloorUpdate,
    OrganizationCreate,
)
from models.enums import CONN_SPACE_TYPES
from services.geometry_service import (
    local_to_global_coordinates,
    polygon_local_to_global,
    apply_edit_transform,
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

    def delete_organization(self, organization_id: str) -> dict:
        """Delete an Organization and every descendant — Campuses, Buildings,
        Floors, Spaces."""
        exists = self.db.execute(
            "MATCH (o:Organization {id: $id}) RETURN o.id AS id",
            {"id": organization_id},
        )
        if not exists:
            raise OrganizationNotFound(organization_id)

        conn_types = [t.value for t in CONN_SPACE_TYPES]

        campus_rows = self.db.execute(
            "MATCH (:Organization {id: $id})-[:HAS_CAMPUS]->(c:Campus) RETURN c.id AS id",
            {"id": organization_id},
        )
        campus_ids = [r["id"] for r in campus_rows]

        building_rows = self.db.execute(
            """
            MATCH (:Organization {id: $id})-[:HAS_CAMPUS]->(:Campus)-[:HAS_BUILDING]->(b:Building)
            RETURN b.id AS id
            """,
            {"id": organization_id},
        )
        building_ids = [r["id"] for r in building_rows]

        floor_rows = self.db.execute(
            """
            MATCH (:Organization {id: $id})-[:HAS_CAMPUS]->(:Campus)-[:HAS_BUILDING]->(b:Building)-[:HAS_FLOOR]->(f:Floor)
            RETURN b.id AS building_id, f.id AS id
            """,
            {"id": organization_id},
        )
        floor_ids = [r["id"] for r in floor_rows]
        floor_pks = [f"{r['building_id']}_{r['id']}" for r in floor_rows]

        space_rows = self.db.execute(
            """
            MATCH (:Organization {id: $id})-[:HAS_CAMPUS]->(:Campus)-[:HAS_BUILDING]->(:Building)-[:HAS_FLOOR]->(:Floor)-[:HAS_SPACE]->(s:Space)
            OPTIONAL MATCH (s)-[:HAS_SUBSPACE*1..]->(sub:Space)
            RETURN collect(DISTINCT s.id) AS roots, collect(DISTINCT sub.id) AS subs
            """,
            {"id": organization_id},
        )
        space_ids: set[str] = set()
        if space_rows:
            space_ids.update(sid for sid in space_rows[0]["roots"] if sid)
            space_ids.update(sid for sid in space_rows[0]["subs"] if sid)

        door_ids: set[str] = set()
        if space_ids:
            door_rows = self.db.execute(
                """
                MATCH (s:Space)-[:CONNECTS_TO]-(d:Space)
                WHERE s.id IN $space_ids AND d.space_type IN $conn_types
                RETURN DISTINCT d.id AS id
                """,
                {"space_ids": list(space_ids), "conn_types": conn_types},
            )
            door_ids.update(r["id"] for r in door_rows)

        self.db.execute_write(
            """
            MATCH (o:Organization {id: $id})
            OPTIONAL MATCH (o)-[:HAS_CAMPUS]->(c:Campus)
            OPTIONAL MATCH (c)-[:HAS_BUILDING]->(b:Building)
            OPTIONAL MATCH (b)-[:HAS_FLOOR]->(f:Floor)
            OPTIONAL MATCH (f)-[:HAS_SPACE]->(s:Space)
            OPTIONAL MATCH (s)-[:HAS_SUBSPACE*1..]->(sub:Space)
            OPTIONAL MATCH (d:Space) WHERE d.id IN $door_ids
            DETACH DELETE d, sub, s, f, b, c, o
            """,
            {"id": organization_id, "door_ids": list(door_ids)},
        )

        return {
            "organization_id": organization_id,
            "campus_ids": campus_ids,
            "building_ids": building_ids,
            "floor_ids": floor_ids,
            "floor_pks": floor_pks,
            "space_ids": sorted(space_ids | door_ids),
        }


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
            "is_public": bool(data.is_public),
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
                    c.is_public = $is_public,
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
                    c.is_public = $is_public,
                    c.created_at = coalesce(c.created_at, $now),
                    c.updated_at = $now
                RETURN c
                """,
                params,
            )
        return result[0]["c"]

    def list_visible_campuses(self, org_ids: list[str] | None) -> list[dict]:
        ids = list(org_ids or [])
        result = self.db.execute(
            """
            MATCH (c:Campus)
            WHERE coalesce(c.is_public, false) = true
               OR (size($org_ids) > 0 AND c.organization_id IN $org_ids)
            OPTIONAL MATCH (org:Organization {id: c.organization_id})
            RETURN c, org.name AS organization_name
            ORDER BY coalesce(org.name, ''), c.name
            """,
            {"org_ids": ids},
        )
        return [
            {**row["c"], "organization_name": row["organization_name"]}
            for row in result
        ]

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

    def delete_campus(self, campus_id: str) -> dict:
        """Delete a Campus and every descendant — Buildings, Floors, Spaces."""
        exists = self.db.execute(
            "MATCH (c:Campus {id: $id}) RETURN c.id AS id",
            {"id": campus_id},
        )
        if not exists:
            raise CampusNotFound(campus_id)

        conn_types = [t.value for t in CONN_SPACE_TYPES]

        building_rows = self.db.execute(
            "MATCH (:Campus {id: $id})-[:HAS_BUILDING]->(b:Building) RETURN b.id AS id",
            {"id": campus_id},
        )
        building_ids = [r["id"] for r in building_rows]

        floor_rows = self.db.execute(
            """
            MATCH (:Campus {id: $id})-[:HAS_BUILDING]->(b:Building)-[:HAS_FLOOR]->(f:Floor)
            RETURN b.id AS building_id, f.id AS id
            """,
            {"id": campus_id},
        )
        floor_ids = [r["id"] for r in floor_rows]
        floor_pks = [f"{r['building_id']}_{r['id']}" for r in floor_rows]

        space_rows = self.db.execute(
            """
            MATCH (:Campus {id: $id})-[:HAS_BUILDING]->(:Building)-[:HAS_FLOOR]->(:Floor)-[:HAS_SPACE]->(s:Space)
            OPTIONAL MATCH (s)-[:HAS_SUBSPACE*1..]->(sub:Space)
            RETURN collect(DISTINCT s.id) AS roots, collect(DISTINCT sub.id) AS subs
            """,
            {"id": campus_id},
        )
        space_ids: set[str] = set()
        if space_rows:
            space_ids.update(sid for sid in space_rows[0]["roots"] if sid)
            space_ids.update(sid for sid in space_rows[0]["subs"] if sid)

        door_ids: set[str] = set()
        if space_ids:
            door_rows = self.db.execute(
                """
                MATCH (s:Space)-[:CONNECTS_TO]-(d:Space)
                WHERE s.id IN $space_ids AND d.space_type IN $conn_types
                RETURN DISTINCT d.id AS id
                """,
                {"space_ids": list(space_ids), "conn_types": conn_types},
            )
            door_ids.update(r["id"] for r in door_rows)

        self.db.execute_write(
            """
            MATCH (c:Campus {id: $id})
            OPTIONAL MATCH (c)-[:HAS_BUILDING]->(b:Building)
            OPTIONAL MATCH (b)-[:HAS_FLOOR]->(f:Floor)
            OPTIONAL MATCH (f)-[:HAS_SPACE]->(s:Space)
            OPTIONAL MATCH (s)-[:HAS_SUBSPACE*1..]->(sub:Space)
            OPTIONAL MATCH (d:Space) WHERE d.id IN $door_ids
            DETACH DELETE d, sub, s, f, b, c
            """,
            {"id": campus_id, "door_ids": list(door_ids)},
        )

        return {
            "campus_id": campus_id,
            "building_ids": building_ids,
            "floor_ids": floor_ids,
            "floor_pks": floor_pks,
            "space_ids": sorted(space_ids | door_ids),
        }

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
                b.scale_factor = $scale_factor,
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

    def _recompute_space(
        self, s: dict, origin_lat: float, origin_lng: float,
        bearing: float, scale: float,
    ) -> dict:
        """Re-project one space's global geometry from its local polygon +
        the given origin, write it back to Neo4j, and return the values so
        the caller can mirror them into PostGIS."""
        polygon = s.get("polygon")
        if isinstance(polygon, str):
            try:
                polygon = json.loads(polygon)
            except (json.JSONDecodeError, TypeError):
                polygon = None
        cx, cy = s.get("centroid_x"), s.get("centroid_y")

        centroid_lat = centroid_lng = None
        if cx is not None and cy is not None:
            centroid_lat, centroid_lng = local_to_global_coordinates(
                cx, cy, origin_lat, origin_lng, bearing, scale
            )

        polygon_global = None
        if polygon:
            polygon_global = polygon_local_to_global(
                polygon, origin_lat, origin_lng, bearing, scale
            )

        self.db.execute_write(
            """
            MATCH (s:Space {id: $id})
            SET s.centroid_lat = $clat,
                s.centroid_lng = $clng,
                s.polygon_global = $pg
            """,
            {
                "id": s["id"],
                "clat": centroid_lat,
                "clng": centroid_lng,
                "pg": json.dumps(polygon_global) if polygon_global else None,
            },
        )
        return {
            "id": s["id"],
            "centroid_lat": centroid_lat,
            "centroid_lng": centroid_lng,
            "polygon_global": polygon_global,
        }

    def update_building(self, building_id: str, data: BuildingUpdate) -> dict:
        """Reposition / rotate / resize a building rigidly. The same move is
        carried through to every per-floor origin override, so a floor that
        was nudged individually still moves *with* the building. Then every
        space is re-projected from its floor's effective origin."""
        existing = self.db.execute(
            "MATCH (b:Building {id: $id}) RETURN b",
            {"id": building_id},
        )
        if not existing:
            raise BuildingNotFound(building_id)
        b = dict(existing[0]["b"])

        old_lat = b.get("origin_lat")
        old_lng = b.get("origin_lng")
        old_bearing = b.get("origin_bearing") or 0.0
        old_scale = b.get("scale_factor") or 1.0

        new_lat = data.origin_lat if data.origin_lat is not None else old_lat
        new_lng = data.origin_lng if data.origin_lng is not None else old_lng
        new_bearing = (
            data.origin_bearing if data.origin_bearing is not None else old_bearing
        )
        new_scale = (
            data.scale_factor if data.scale_factor is not None else old_scale
        )

        now = _now()
        updated = self.db.execute_write(
            """
            MATCH (b:Building {id: $id})
            SET b.origin_lat = $lat,
                b.origin_lng = $lng,
                b.origin_bearing = $bearing,
                b.scale_factor = $scale,
                b.updated_at = $now
            RETURN b
            """,
            {
                "id": building_id, "lat": new_lat, "lng": new_lng,
                "bearing": new_bearing, "scale": new_scale, "now": now,
            },
        )

        updated_spaces: list[dict] = []
        if new_lat is not None and new_lng is not None:
            # The rigid delta of this building move (pivot = old origin).
            if old_lat is not None and old_lng is not None:
                d_lat = new_lat - old_lat
                d_lng = new_lng - old_lng
                d_bearing = new_bearing - old_bearing
                scale_mult = (new_scale / old_scale) if old_scale else 1.0
            else:
                d_lat = d_lng = d_bearing = 0.0
                scale_mult = 1.0

            # Carry the move through to each per-floor origin override.
            floor_origin: dict[str, tuple[float, float, float, float]] = {}
            floor_rows = self.db.execute(
                "MATCH (:Building {id: $id})-[:HAS_FLOOR]->(f:Floor) RETURN f",
                {"id": building_id},
            )
            for fr in floor_rows:
                f = dict(fr["f"])
                if f.get("origin_lat") is None or f.get("origin_lng") is None:
                    continue
                f_lat, f_lng = apply_edit_transform(
                    f["origin_lat"], f["origin_lng"],
                    old_lat, old_lng, d_lat, d_lng, d_bearing, scale_mult,
                )
                f_bearing = (f.get("origin_bearing") or 0.0) + d_bearing
                f_scale = (f.get("scale_factor") or 1.0) * scale_mult
                self.db.execute_write(
                    """
                    MATCH (f:Floor {id: $id})
                    SET f.origin_lat = $lat, f.origin_lng = $lng,
                        f.origin_bearing = $bearing, f.scale_factor = $scale,
                        f.updated_at = $now
                    """,
                    {
                        "id": f["id"], "lat": f_lat, "lng": f_lng,
                        "bearing": f_bearing, "scale": f_scale, "now": now,
                    },
                )
                floor_origin[f["id"]] = (f_lat, f_lng, f_bearing, f_scale)

            # Re-project every space from its floor's effective origin.
            space_rows = self.db.execute(
                "MATCH (s:Space {building_id: $id}) RETURN s",
                {"id": building_id},
            )
            for row in space_rows:
                s = dict(row["s"])
                origin = floor_origin.get(
                    s.get("floor_id"),
                    (new_lat, new_lng, new_bearing, new_scale),
                )
                updated_spaces.append(self._recompute_space(s, *origin))

        return {"building": updated[0]["b"], "updated_spaces": updated_spaces}

    def update_floor(self, floor_id: str, data: FloorUpdate) -> dict:
        """Reposition / rotate / resize a single floor. Stores a per-floor
        origin override and re-projects only that floor's spaces."""
        existing = self.db.execute(
            """
            MATCH (b:Building)-[:HAS_FLOOR]->(f:Floor {id: $id})
            RETURN f, b
            """,
            {"id": floor_id},
        )
        if not existing:
            raise FloorNotFound(floor_id)
        f = dict(existing[0]["f"])
        b = dict(existing[0]["b"])

        # Resolve each field: explicit update > floor's own override > building.
        def _resolve(field: str, building_default):
            if getattr(data, field) is not None:
                return getattr(data, field)
            if f.get(field) is not None:
                return f.get(field)
            return building_default

        new_lat = _resolve("origin_lat", b.get("origin_lat"))
        new_lng = _resolve("origin_lng", b.get("origin_lng"))
        new_bearing = _resolve("origin_bearing", b.get("origin_bearing") or 0.0)
        new_scale = _resolve("scale_factor", b.get("scale_factor") or 1.0)

        now = _now()
        updated = self.db.execute_write(
            """
            MATCH (f:Floor {id: $id})
            SET f.origin_lat = $lat,
                f.origin_lng = $lng,
                f.origin_bearing = $bearing,
                f.scale_factor = $scale,
                f.updated_at = $now
            RETURN f
            """,
            {
                "id": floor_id, "lat": new_lat, "lng": new_lng,
                "bearing": new_bearing, "scale": new_scale, "now": now,
            },
        )

        updated_spaces: list[dict] = []
        if new_lat is not None and new_lng is not None:
            # Space nodes carry no floor_id property — they hang off the floor
            # via :HAS_SPACE (and nested :HAS_SUBSPACE), so walk the graph.
            space_rows = self.db.execute(
                """
                MATCH (:Floor {id: $id})-[:HAS_SPACE]->(root:Space)
                OPTIONAL MATCH (root)-[:HAS_SUBSPACE*1..]->(sub:Space)
                WITH collect(DISTINCT root) + collect(DISTINCT sub) AS spaces
                UNWIND spaces AS s
                WITH DISTINCT s WHERE s IS NOT NULL
                RETURN s
                """,
                {"id": floor_id},
            )
            for row in space_rows:
                updated_spaces.append(self._recompute_space(
                    dict(row["s"]), new_lat, new_lng, new_bearing, new_scale
                ))

        return {"floor": updated[0]["f"], "updated_spaces": updated_spaces}

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

    def list_visible_buildings(self, org_ids: list[str] | None) -> list[dict]:
        ids = list(org_ids or [])

        # Backfill: if a Building has no origin_lat/lng but any of its
        # Floors does, copy the lowest-floor-index one up to the
        # Building. The mapmaker writes per-floor georef overrides
        # without always stamping the parent building, which leaves
        # the building unfilterable by world coords. This makes
        # /buildings/visible self-healing.
        self.db.execute_write(
            """
            MATCH (b:Building)
            WHERE b.origin_lat IS NULL OR b.origin_lng IS NULL
            MATCH (b)-[:HAS_FLOOR]->(f:Floor)
            WHERE f.origin_lat IS NOT NULL AND f.origin_lng IS NOT NULL
            WITH b, f ORDER BY coalesce(f.floor_index, 0) ASC
            WITH b, head(collect(f)) AS first_floor
            SET b.origin_lat = coalesce(b.origin_lat, first_floor.origin_lat),
                b.origin_lng = coalesce(b.origin_lng, first_floor.origin_lng)
            """,
        )

        result = self.db.execute(
            """
            MATCH (c:Campus)-[:HAS_BUILDING]->(b:Building)
            WHERE coalesce(c.is_public, false) = true
               OR (size($org_ids) > 0 AND c.organization_id IN $org_ids)
            WITH c, b
            WHERE b.origin_lat IS NOT NULL AND b.origin_lng IS NOT NULL
            OPTIONAL MATCH (org:Organization {id: c.organization_id})
            RETURN
              b.id AS id,
              b.name AS name,
              b.short_name AS short_name,
              b.address AS address,
              b.origin_lat AS origin_lat,
              b.origin_lng AS origin_lng,
              c.id AS campus_id,
              c.name AS campus_name,
              c.organization_id AS organization_id,
              org.name AS organization_name,
              coalesce(c.is_public, false) AS is_public
            ORDER BY coalesce(org.name, ''), c.name, b.name
            """,
            {"org_ids": ids},
        )
        return result

    def delete_building(self, building_id: str) -> dict:
        """Delete a building and everything inside it — floors, spaces (incl.
        nested subspaces), and any door/passage node that connected to one of
        those spaces (even if the door was authored cross-floor and lives on
        another floor).

        Returns the IDs that were removed so the PostGIS mirror can drop the
        matching rows — `building_spaces` has no FK to `buildings`, so PostGIS
        can't cascade on its own.
        """
        exists = self.db.execute(
            "MATCH (b:Building {id: $id}) RETURN b.id AS id",
            {"id": building_id},
        )
        if not exists:
            raise BuildingNotFound(building_id)

        conn_types = [t.value for t in CONN_SPACE_TYPES]

        floor_rows = self.db.execute(
            "MATCH (:Building {id: $id})-[:HAS_FLOOR]->(f:Floor) RETURN f.id AS id",
            {"id": building_id},
        )
        floor_ids = [r["id"] for r in floor_rows]

        space_rows = self.db.execute(
            """
            MATCH (:Building {id: $id})-[:HAS_FLOOR]->(:Floor)-[:HAS_SPACE]->(s:Space)
            OPTIONAL MATCH (s)-[:HAS_SUBSPACE*1..]->(sub:Space)
            RETURN collect(DISTINCT s.id) AS roots, collect(DISTINCT sub.id) AS subs
            """,
            {"id": building_id},
        )
        space_ids: set[str] = set()
        if space_rows:
            space_ids.update(sid for sid in space_rows[0]["roots"] if sid)
            space_ids.update(sid for sid in space_rows[0]["subs"] if sid)

        door_ids: set[str] = set()
        if space_ids:
            door_rows = self.db.execute(
                """
                MATCH (s:Space)-[:CONNECTS_TO]-(d:Space)
                WHERE s.id IN $space_ids AND d.space_type IN $conn_types
                RETURN DISTINCT d.id AS id
                """,
                {"space_ids": list(space_ids), "conn_types": conn_types},
            )
            door_ids.update(r["id"] for r in door_rows)

        self.db.execute_write(
            """
            MATCH (b:Building {id: $id})
            OPTIONAL MATCH (b)-[:HAS_FLOOR]->(f:Floor)
            OPTIONAL MATCH (f)-[:HAS_SPACE]->(s:Space)
            OPTIONAL MATCH (s)-[:HAS_SUBSPACE*1..]->(sub:Space)
            OPTIONAL MATCH (d:Space)
              WHERE d.id IN $door_ids
            DETACH DELETE d, sub, s, f, b
            """,
            {"id": building_id, "door_ids": list(door_ids)},
        )

        return {
            "building_id": building_id,
            "floor_ids": floor_ids,
            "space_ids": sorted(space_ids | door_ids),
        }

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
            WITH b, f
            MATCH (b)-[:HAS_FLOOR]->(all:Floor)
            WITH b, f, count(DISTINCT all) AS floor_count
            SET b.floor_count = floor_count
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

    def delete_floor(self, floor_id: str) -> dict:
        exists = self.db.execute(
            """
            MATCH (b:Building)-[:HAS_FLOOR]->(f:Floor {id: $id})
            RETURN b.id AS building_id
            """,
            {"id": floor_id},
        )
        if not exists:
            raise FloorNotFound(floor_id)
        building_id = exists[0]["building_id"]

        conn_types = [t.value for t in CONN_SPACE_TYPES]

        space_rows = self.db.execute(
            """
            MATCH (:Floor {id: $id})-[:HAS_SPACE]->(s:Space)
            OPTIONAL MATCH (s)-[:HAS_SUBSPACE*1..]->(sub:Space)
            RETURN collect(DISTINCT s.id) AS roots, collect(DISTINCT sub.id) AS subs
            """,
            {"id": floor_id},
        )
        space_ids: set[str] = set()
        if space_rows:
            space_ids.update(sid for sid in space_rows[0]["roots"] if sid)
            space_ids.update(sid for sid in space_rows[0]["subs"] if sid)

        door_ids: set[str] = set()
        if space_ids:
            door_rows = self.db.execute(
                """
                MATCH (s:Space)-[:CONNECTS_TO]-(d:Space)
                WHERE s.id IN $space_ids AND d.space_type IN $conn_types
                RETURN DISTINCT d.id AS id
                """,
                {"space_ids": list(space_ids), "conn_types": conn_types},
            )
            door_ids.update(r["id"] for r in door_rows)

        self.db.execute_write(
            """
            MATCH (f:Floor {id: $id})
            OPTIONAL MATCH (f)-[:HAS_SPACE]->(s:Space)
            OPTIONAL MATCH (s)-[:HAS_SUBSPACE*1..]->(sub:Space)
            OPTIONAL MATCH (d:Space)
              WHERE d.id IN $door_ids
            DETACH DELETE d, sub, s, f
            """,
            {"id": floor_id, "door_ids": list(door_ids)},
        )

        self.db.execute_write(
            """
            MATCH (b:Building {id: $building_id})
            OPTIONAL MATCH (b)-[:HAS_FLOOR]->(f:Floor)
            WITH b, count(DISTINCT f) AS floor_count
            SET b.floor_count = floor_count
            """,
            {"building_id": building_id},
        )

        return {
            "floor_id": floor_id,
            "building_id": building_id,
            "space_ids": sorted(space_ids | door_ids),
        }
