import json

from db import Database
from core.exceptions import SpaceNotFound
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

    def delete_space(self, space_id: str) -> None:
        result = self.db.execute_write(
            "MATCH (s:Space {id: $id}) DETACH DELETE s RETURN count(s) AS deleted",
            {"id": space_id},
        )
        if not result or result[0]["deleted"] == 0:
            raise SpaceNotFound(space_id)

    def get_floor_spaces(self, floor_id: str) -> list[dict]:
        result = self.db.execute(
            "MATCH (:Floor {id: $floor_id})-[:HAS_SPACE]->(s:Space) RETURN s",
            {"floor_id": floor_id},
        )
        return [_from_neo4j(r["s"]) for r in result]

    def get_floor_display(self, floor_id: str) -> list[dict]:
        """Return all top-level spaces with nested subspaces for rendering."""
        result = self.db.execute(
            """
            MATCH (:Floor {id: $floor_id})-[:HAS_SPACE]->(s:Space)
            OPTIONAL MATCH (s)-[:HAS_SUBSPACE*1..]->(sub:Space)
            RETURN s, collect(sub) AS subspaces
            """,
            {"floor_id": floor_id},
        )
        spaces = []
        for r in result:
            space = _from_neo4j(r["s"])
            space["subspaces"] = [_from_neo4j(sub) for sub in r["subspaces"] if sub]
            spaces.append(space)
        return spaces

    def search(self, campus_id: str, query: str) -> list[dict]:
        result = self.db.execute(
            """
            CALL db.index.fulltext.queryNodes('space_search_idx', $query)
            YIELD node, score
            WHERE node.campus_id = $campus_id AND node.is_navigable = true
            RETURN node AS s, score
            ORDER BY score DESC
            LIMIT 20
            """,
            {"campus_id": campus_id, "query": query},
        )
        return [_from_neo4j(r["s"]) for r in result]
