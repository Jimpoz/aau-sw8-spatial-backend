from db import Database
from models.connection import ConnectionCreate


class ConnectionRepository:
    def __init__(self, db: Database):
        self.db = db

    def create_connection(self, data: ConnectionCreate) -> dict:
        props = {k: v for k, v in data.model_dump().items() if v is not None}
        result = self.db.execute_write(
            """
            MATCH (a:Space {id: $from_space_id}), (b:Space {id: $to_space_id})
            MERGE (a)-[r:CONNECTS_TO]->(b)
            SET r += $props
            RETURN properties(r) AS r
            """,
            {
                "from_space_id": data.from_space_id,
                "to_space_id": data.to_space_id,
                "props": props,
            },
        )
        return result[0]["r"] if result else None

    def get_connection(self, from_space_id: str, to_space_id: str) -> dict | None:
        result = self.db.execute(
            """
            MATCH (:Space {id: $from_id})-[r:CONNECTS_TO]->(:Space {id: $to_id})
            RETURN properties(r) AS r
            """,
            {"from_id": from_space_id, "to_id": to_space_id},
        )
        return result[0]["r"] if result else None

    def delete_connection(self, from_space_id: str, to_space_id: str) -> bool:
        result = self.db.execute_write(
            """
            MATCH (:Space {id: $from_id})-[r:CONNECTS_TO]->(:Space {id: $to_id})
            DELETE r
            RETURN count(r) AS deleted
            """,
            {"from_id": from_space_id, "to_id": to_space_id},
        )
        return result[0]["deleted"] > 0 if result else False

    def list_connections_from(self, space_id: str) -> list[dict]:
        result = self.db.execute(
            """
            MATCH (:Space {id: $id})-[r:CONNECTS_TO]->(b:Space)
            RETURN properties(r) AS r, b.id AS to_space_id
            """,
            {"id": space_id},
        )
        return [r["r"] for r in result]

    def list_connections_for_campus(self, campus_id: str) -> list[dict]:
        result = self.db.execute(
            """
            MATCH (a:Space {campus_id: $campus_id})-[r:CONNECTS_TO]->(b:Space)
            RETURN properties(r) AS r
            """,
            {"campus_id": campus_id},
        )
        return [r["r"] for r in result]
