import json

from db import Database
from core.exceptions import SpaceNotFound
from models.enums import CONN_SPACE_TYPES
from models.enums import CONN_SPACE_TYPES
from models.space import SpaceCreate, SpaceUpdate


def _to_neo4j(data: dict) -> dict:
    """Serialize polygon/metadata to JSON strings; remove None values."""
    result = {}
    for k, v in data.items():
        if v is None:
            continue
        if k in ("polygon", "polygon_global", "metadata") and not isinstance(v, str):
            result[k] = json.dumps(v)
        else:
            result[k] = v
    # Store tags as both native array and joined text for fulltext index
    if "tags" in result and isinstance(result["tags"], list):
        result["tags_text"] = " ".join(result["tags"])
    return result


def _from_neo4j(node: dict) -> dict:
    """Deserialize JSON strings back to Python objects."""
    d = dict(node)
    for key in ("polygon", "polygon_global", "metadata"):
        if isinstance(d.get(key), str):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


class SpaceRepository:
    def __init__(self, db: Database):
        self.db = db

    def create_space(self, data: SpaceCreate) -> dict:
        params = _to_neo4j(data.model_dump(exclude={"floor_id", "parent_space_id"}))
        parent_id = data.parent_space_id
        floor_id = data.floor_id

        if parent_id:
            result = self.db.execute_write(
                """
                MATCH (parent:Space {id: $parent_id})
                MERGE (s:Space {id: $id})
                SET s += $props
                MERGE (parent)-[:HAS_SUBSPACE]->(s)
                RETURN s
                """,
                {"parent_id": parent_id, "id": data.id, "props": params},
            )
        elif floor_id:
            result = self.db.execute_write(
                """
                MATCH (f:Floor {id: $floor_id})
                MERGE (s:Space {id: $id})
                SET s += $props
                MERGE (f)-[:HAS_SPACE]->(s)
                RETURN s
                """,
                {"floor_id": floor_id, "id": data.id, "props": params},
            )
        else:
            result = self.db.execute_write(
                """
                MERGE (s:Space {id: $id})
                SET s += $props
                RETURN s
                """,
                {"id": data.id, "props": params},
            )

        return _from_neo4j(result[0]["s"]) if result else None

    def get_space(self, space_id: str) -> dict:
        result = self.db.execute(
            "MATCH (s:Space {id: $id}) RETURN s",
            {"id": space_id},
        )
        if not result:
            raise SpaceNotFound(space_id)
        return _from_neo4j(result[0]["s"])

    def update_space(self, space_id: str, data: SpaceUpdate) -> dict:
        updates = _to_neo4j({k: v for k, v in data.model_dump().items() if v is not None})
        result = self.db.execute_write(
            """
            MATCH (s:Space {id: $id})
            SET s += $updates
            RETURN s
            """,
            {"id": space_id, "updates": updates},
        )
        if not result:
            raise SpaceNotFound(space_id)
        return _from_neo4j(result[0]["s"])

    def delete_space(self, space_id: str) -> list[str]:
        """Delete the space together with every door/passage node that connects
        to it. Returns the list of deleted door IDs so the PostGIS mirror can drop matching connection.
        """
        conn_types = [t.value for t in CONN_SPACE_TYPES]

        exists = self.db.execute(
            "MATCH (s:Space {id: $id}) RETURN s.id AS id",
            {"id": space_id},
        )
        if not exists:
            raise SpaceNotFound(space_id)

        door_rows = self.db.execute(
            """
            MATCH (s:Space {id: $id})-[:CONNECTS_TO]-(d:Space)
            WHERE d.space_type IN $conn_types
            RETURN DISTINCT d.id AS id
            """,
            {"id": space_id, "conn_types": conn_types},
        )
        door_ids = [r["id"] for r in door_rows]

        self.db.execute_write(
            """
            MATCH (s:Space {id: $id})
            OPTIONAL MATCH (s)-[:CONNECTS_TO]-(d:Space)
            WHERE d.space_type IN $conn_types
            DETACH DELETE d, s
            """,
            {"id": space_id, "conn_types": conn_types},
        )
        return door_ids

    def get_floor_spaces(self, floor_id: str) -> list[dict]:
        result = self.db.execute(
            "MATCH (:Floor {id: $floor_id})-[:HAS_SPACE]->(s:Space) RETURN s",
            {"floor_id": floor_id},
        )
        return [_from_neo4j(r["s"]) for r in result]

    def get_floor_spaces_with_subspaces(self, floor_id: str) -> list[dict]:
        """Return top-level spaces with recursively nested subspaces for export."""
        top_result = self.db.execute(
            "MATCH (:Floor {id: $floor_id})-[:HAS_SPACE]->(s:Space) RETURN s",
            {"floor_id": floor_id},
        )
        sub_result = self.db.execute(
            """
            MATCH (:Floor {id: $floor_id})-[:HAS_SPACE]->(root:Space)
            MATCH (root)-[:HAS_SUBSPACE*1..]->(sub:Space)
            MATCH (parent:Space)-[:HAS_SUBSPACE]->(sub)
            RETURN parent.id AS parent_id, sub
            """,
            {"floor_id": floor_id},
        )
        children_map: dict[str, list[dict]] = {}
        for r in sub_result:
            child = _from_neo4j(r["sub"])
            children_map.setdefault(r["parent_id"], []).append(child)

        def attach_subspaces(space: dict) -> dict:
            space["subspaces"] = children_map.get(space["id"], [])
            for child in space["subspaces"]:
                attach_subspaces(child)
            return space

        return [attach_subspaces(_from_neo4j(r["s"])) for r in top_result]

    def get_floor_spaces_with_subspaces(self, floor_id: str) -> list[dict]:
        """Return top-level spaces with recursively nested subspaces for export."""
        top_result = self.db.execute(
            "MATCH (:Floor {id: $floor_id})-[:HAS_SPACE]->(s:Space) RETURN s",
            {"floor_id": floor_id},
        )
        sub_result = self.db.execute(
            """
            MATCH (:Floor {id: $floor_id})-[:HAS_SPACE]->(root:Space)
            MATCH (root)-[:HAS_SUBSPACE*1..]->(sub:Space)
            MATCH (parent:Space)-[:HAS_SUBSPACE]->(sub)
            RETURN parent.id AS parent_id, sub
            """,
            {"floor_id": floor_id},
        )
        children_map: dict[str, list[dict]] = {}
        for r in sub_result:
            child = _from_neo4j(r["sub"])
            children_map.setdefault(r["parent_id"], []).append(child)

        def attach_subspaces(space: dict) -> dict:
            space["subspaces"] = children_map.get(space["id"], [])
            for child in space["subspaces"]:
                attach_subspaces(child)
            return space

        return [attach_subspaces(_from_neo4j(r["s"])) for r in top_result]

    def get_floor_display(self, floor_id: str) -> list[dict]:
        """Return all spaces with z_index for rendering, including subspaces and connection nodes."""
        result = self.db.execute(
            """
            MATCH (:Floor {id: $floor_id})-[:HAS_SPACE]->(s:Space)
            OPTIONAL MATCH path = (s)-[:HAS_SUBSPACE*1..]->(sub:Space)
            OPTIONAL MATCH (directParent:Space)-[:HAS_SUBSPACE]->(sub)
            RETURN s, collect({node: sub, depth: length(path), parent_id: directParent.id, parent_name: directParent.display_name}) AS subspaces_with_depth
            """,
            {"floor_id": floor_id},
        )
        spaces = []
        max_depth = 0
        for r in result:
            space = _from_neo4j(r["s"])
            space["z_index"] = 0
            space["subspaces"] = []
            for item in r["subspaces_with_depth"]:
                if item["node"] is not None:
                    sub = _from_neo4j(item["node"])
                    depth = item["depth"]
                    sub["z_index"] = depth
                    sub["parent_space_id"] = item["parent_id"]
                    sub["parent_space_name"] = item["parent_name"]
                    if depth > max_depth:
                        max_depth = depth
                    space["subspaces"].append(sub)
            spaces.append(space)

        # Connection nodes (doors/passages/vertical) connected to spaces on this floor
        conn_types = [t.value for t in CONN_SPACE_TYPES]
        conn_result = self.db.execute(
            """
            MATCH (:Floor {id: $floor_id})-[:HAS_SPACE]->(s:Space)-[:CONNECTS_TO]-(conn:Space)
            WHERE conn.space_type IN $conn_types
            RETURN DISTINCT conn
            """,
            {"floor_id": floor_id, "conn_types": conn_types},
        )
        conn_z = max_depth + 1
        for r in conn_result:
            node = _from_neo4j(r["conn"])
            node["z_index"] = conn_z
            node["subspaces"] = []
            spaces.append(node)

        # Cross-floor vertical connections: find spaces on this floor linked
        # through a vertical connection node to a space on a different floor
        vertical_types = ["STAIRCASE", "ELEVATOR", "ESCALATOR", "RAMP"]
        vert_result = self.db.execute(
            """
            MATCH (:Floor {id: $floor_id})-[:HAS_SPACE]->(s:Space)-[:CONNECTS_TO]->(conn:Space)-[:CONNECTS_TO]->(remote:Space)
            WHERE conn.space_type IN $vertical_types
            MATCH (remote_floor:Floor)-[:HAS_SPACE]->(remote)
            WHERE remote_floor.id <> $floor_id
            RETURN s.id AS space_id, remote.id AS remote_space_id,
                   remote_floor.floor_index AS remote_floor_index,
                   conn.space_type AS connection_type
            """,
            {"floor_id": floor_id, "vertical_types": vertical_types},
        )
        vert_map: dict[str, list[dict]] = {}
        for r in vert_result:
            vert_map.setdefault(r["space_id"], []).append({
                "to_space_id": r["remote_space_id"],
                "to_floor_index": r["remote_floor_index"],
                "connection_type": r["connection_type"],
            })
        for space in spaces:
            space["vertical_connections"] = vert_map.get(space.get("id"), [])


        # Connection nodes (doors/passages/vertical) connected to spaces on this floor
        conn_types = [t.value for t in CONN_SPACE_TYPES]
        conn_result = self.db.execute(
            """
            MATCH (:Floor {id: $floor_id})-[:HAS_SPACE]->(s:Space)-[:CONNECTS_TO]-(conn:Space)
            WHERE conn.space_type IN $conn_types
            RETURN DISTINCT conn
            """,
            {"floor_id": floor_id, "conn_types": conn_types},
        )
        conn_z = max_depth + 1
        for r in conn_result:
            node = _from_neo4j(r["conn"])
            node["z_index"] = conn_z
            node["subspaces"] = []
            spaces.append(node)

        # Cross-floor vertical connections: find spaces on this floor linked
        # through a vertical connection node to a space on a different floor
        vertical_types = ["STAIRCASE", "ELEVATOR", "ESCALATOR", "RAMP"]
        vert_result = self.db.execute(
            """
            MATCH (:Floor {id: $floor_id})-[:HAS_SPACE]->(s:Space)-[:CONNECTS_TO]->(conn:Space)-[:CONNECTS_TO]->(remote:Space)
            WHERE conn.space_type IN $vertical_types
            MATCH (remote_floor:Floor)-[:HAS_SPACE]->(remote)
            WHERE remote_floor.id <> $floor_id
            RETURN s.id AS space_id, remote.id AS remote_space_id,
                   remote_floor.floor_index AS remote_floor_index,
                   conn.space_type AS connection_type
            """,
            {"floor_id": floor_id, "vertical_types": vertical_types},
        )
        vert_map: dict[str, list[dict]] = {}
        for r in vert_result:
            vert_map.setdefault(r["space_id"], []).append({
                "to_space_id": r["remote_space_id"],
                "to_floor_index": r["remote_floor_index"],
                "connection_type": r["connection_type"],
            })
        for space in spaces:
            space["vertical_connections"] = vert_map.get(space.get("id"), [])

        return spaces

    def search(self, campus_id: str, query: str) -> list[dict]:
        result = self.db.execute(
            """
            CALL db.index.fulltext.queryNodes('space_search_idx', $query)
            YIELD node, score
            WHERE node.campus_id = $campus_id AND node.is_navigable = true
                AND NOT node.space_type STARTS WITH 'DOOR_'
                AND node.space_type <> 'PASSAGE'
                AND NOT node.space_type STARTS WITH 'DOOR_'
                AND node.space_type <> 'PASSAGE'
            RETURN node AS s, score
            ORDER BY score DESC
            LIMIT 20
            """,
            {"campus_id": campus_id, "query": query},
        )
        return [_from_neo4j(r["s"]) for r in result]

    def search_all(self, query: str, limit: int = 50) -> list[dict]:
        result = self.db.execute(
            """
            CALL db.index.fulltext.queryNodes('space_search_idx', $query)
            YIELD node, score
            WHERE node.is_navigable = true
                AND NOT node.space_type STARTS WITH 'DOOR_'
                AND node.space_type <> 'PASSAGE'
            RETURN node AS s, score
            ORDER BY score DESC
            LIMIT $limit
            """,
            {"query": query, "limit": limit},
        )
        return [_from_neo4j(r["s"]) for r in result]

    def nearest_space(self, lat: float, lon: float, limit: int = 1) -> list[dict]:
        result = self.db.execute(
            """
            MATCH (s:Space)
            WHERE s.centroid_lat IS NOT NULL AND s.centroid_lon IS NOT NULL AND s.is_navigable = true
            WITH s, point({latitude: s.centroid_lat, longitude: s.centroid_lon}) AS p
            RETURN s, distance(p, point({latitude: $lat, longitude: $lon})) AS dist
            ORDER BY dist ASC
            LIMIT $limit
            """,
            {"lat": lat, "lon": lon, "limit": limit},
        )
        return [_from_neo4j(r["s"]) for r in result]
