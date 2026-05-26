from __future__ import annotations

from typing import List, Optional

from db import Database


def _strip_image(node: dict) -> dict:
    """Return a copy without the heavy image_b64 field — used for
    response shaping where callers don't need the bytes."""
    d = dict(node)
    d.pop("image_b64", None)
    return d


class LandmarkRepository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # Writes

    def create_landmark(
        self,
        *,
        landmark_id: str,
        name: str,
        space_id: str,
        image_b64: str,
        image_width: Optional[int] = None,
        image_height: Optional[int] = None,
        centroid_x: Optional[float] = None,
        centroid_y: Optional[float] = None,
        centroid_lat: Optional[float] = None,
        centroid_lng: Optional[float] = None,
        created_by: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> dict:
        """Create the landmark and attach it to its Space."""
        rows = self.db.execute_write(
            """
            MATCH (s:Space {id: $space_id})
            OPTIONAL MATCH (s)<-[:HAS_SPACE]-(f:Floor)
            OPTIONAL MATCH (f)<-[:HAS_FLOOR]-(b:Building)
            OPTIONAL MATCH (b)<-[:HAS_BUILDING]-(c:Campus)
            OPTIONAL MATCH (c)<-[:HAS_CAMPUS]-(o:Organization)
            MERGE (l:Landmark {id: $landmark_id})
            SET l.name           = $name,
                l.space_id       = $space_id,
                l.floor_id       = f.id,
                l.building_id    = b.id,
                l.campus_id      = c.id,
                l.organization_id= coalesce(o.id, s.organization_id),
                l.image_b64      = $image_b64,
                l.image_width    = $image_width,
                l.image_height   = $image_height,
                l.centroid_x     = $centroid_x,
                l.centroid_y     = $centroid_y,
                l.centroid_lat   = $centroid_lat,
                l.centroid_lng   = $centroid_lng,
                l.created_by     = $created_by,
                l.created_at     = $created_at
            MERGE (s)-[:HAS_LANDMARK]->(l)
            RETURN l.id              AS id,
                   l.name            AS name,
                   l.space_id        AS space_id,
                   l.floor_id        AS floor_id,
                   l.building_id     AS building_id,
                   l.campus_id       AS campus_id,
                   l.organization_id AS organization_id,
                   l.image_width     AS image_width,
                   l.image_height    AS image_height,
                   l.centroid_x      AS centroid_x,
                   l.centroid_y      AS centroid_y,
                   l.centroid_lat    AS centroid_lat,
                   l.centroid_lng    AS centroid_lng,
                   l.created_by      AS created_by,
                   l.created_at      AS created_at
            """,
            {
                "landmark_id": landmark_id,
                "name": name,
                "space_id": space_id,
                "image_b64": image_b64,
                "image_width": image_width,
                "image_height": image_height,
                "centroid_x": centroid_x,
                "centroid_y": centroid_y,
                "centroid_lat": centroid_lat,
                "centroid_lng": centroid_lng,
                "created_by": created_by,
                "created_at": created_at,
            },
        )
        if not rows:
            raise ValueError(f"Space not found: {space_id}")
        return dict(rows[0])

    def delete_landmark(self, landmark_id: str) -> bool:
        rows = self.db.execute_write(
            """
            MATCH (l:Landmark {id: $landmark_id})
            WITH l, l.id AS deleted_id
            DETACH DELETE l
            RETURN deleted_id
            """,
            {"landmark_id": landmark_id},
        )
        return bool(rows)

    def get_landmark(self, landmark_id: str) -> Optional[dict]:
        rows = self.db.execute(
            """
            MATCH (l:Landmark {id: $landmark_id})
            RETURN l.id              AS id,
                   l.name            AS name,
                   l.space_id        AS space_id,
                   l.floor_id        AS floor_id,
                   l.building_id     AS building_id,
                   l.campus_id       AS campus_id,
                   l.organization_id AS organization_id,
                   l.image_width     AS image_width,
                   l.image_height    AS image_height,
                   l.centroid_x      AS centroid_x,
                   l.centroid_y      AS centroid_y,
                   l.centroid_lat    AS centroid_lat,
                   l.centroid_lng    AS centroid_lng,
                   l.created_by      AS created_by,
                   l.created_at      AS created_at
            """,
            {"landmark_id": landmark_id},
        )
        return dict(rows[0]) if rows else None

    def list_for_space(self, space_id: str) -> List[dict]:
        rows = self.db.execute(
            """
            MATCH (:Space {id: $space_id})-[:HAS_LANDMARK]->(l:Landmark)
            RETURN l.id              AS id,
                   l.name            AS name,
                   l.space_id        AS space_id,
                   l.floor_id        AS floor_id,
                   l.building_id     AS building_id,
                   l.campus_id       AS campus_id,
                   l.organization_id AS organization_id,
                   l.image_width     AS image_width,
                   l.image_height    AS image_height,
                   l.centroid_x      AS centroid_x,
                   l.centroid_y      AS centroid_y,
                   l.centroid_lat    AS centroid_lat,
                   l.centroid_lng    AS centroid_lng,
                   l.created_by      AS created_by,
                   l.created_at      AS created_at
            ORDER BY l.created_at DESC
            """,
            {"space_id": space_id},
        )
        return [dict(r) for r in rows]

    def list_for_building(self, building_id: str) -> List[dict]:
        rows = self.db.execute(
            """
            MATCH (l:Landmark {building_id: $building_id})
            RETURN l.id              AS id,
                   l.name            AS name,
                   l.space_id        AS space_id,
                   l.floor_id        AS floor_id,
                   l.building_id     AS building_id,
                   l.campus_id       AS campus_id,
                   l.organization_id AS organization_id,
                   l.image_width     AS image_width,
                   l.image_height    AS image_height,
                   l.centroid_x      AS centroid_x,
                   l.centroid_y      AS centroid_y,
                   l.centroid_lat    AS centroid_lat,
                   l.centroid_lng    AS centroid_lng,
                   l.created_by      AS created_by,
                   l.created_at      AS created_at
            ORDER BY l.created_at DESC
            """,
            {"building_id": building_id},
        )
        return [dict(r) for r in rows]

    def list_with_images_for_facility(self, campus_id: str) -> List[dict]:
        """Used by ml-vision to seed its ORB matcher. Includes the
        base64 image bytes — every other read path strips them."""
        rows = self.db.execute(
            """
            MATCH (l:Landmark {campus_id: $campus_id})
            OPTIONAL MATCH (f:Floor {id: l.floor_id})
            RETURN l.id              AS id,
                   l.name            AS name,
                   l.space_id        AS space_id,
                   l.floor_id        AS floor_id,
                   f.floor_index     AS floor_index,
                   l.building_id     AS building_id,
                   l.campus_id       AS campus_id,
                   l.organization_id AS organization_id,
                   l.image_b64       AS image_b64,
                   l.image_width     AS image_width,
                   l.image_height    AS image_height,
                   l.centroid_x      AS centroid_x,
                   l.centroid_y      AS centroid_y,
                   l.centroid_lat    AS centroid_lat,
                   l.centroid_lng    AS centroid_lng
            """,
            {"campus_id": campus_id},
        )
        return [dict(r) for r in rows]
