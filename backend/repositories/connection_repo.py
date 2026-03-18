from db import Database


class ConnectionRepository:
    def __init__(self, db: Database):
        self.db = db

    def create_connection(self, from_space_id: str, to_space_id: str) -> dict:
        result = self.db.execute_write(
            """
            MATCH (a:Space {id: $from_space_id}), (b:Space {id: $to_space_id})
            MERGE (a)-[:CONNECTS_TO]->(b)
            RETURN a.id AS from_space_id, b.id AS to_space_id
            """,
            {"from_space_id": from_space_id, "to_space_id": to_space_id},
        )
        return result[0] if result else None

    def get_connection(self, from_space_id: str, to_space_id: str) -> dict | None:
        result = self.db.execute(
            """
            MATCH (a:Space {id: $from_id})-[:CONNECTS_TO]->(b:Space {id: $to_id})
            RETURN a.id AS from_space_id, b.id AS to_space_id
            """,
            {"from_id": from_space_id, "to_id": to_space_id},
        )
        return result[0] if result else None

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

    def list_connections_for_space(self, space_id: str) -> list[dict]:
        result = self.db.execute(
            """
            MATCH (a:Space {id: $id})-[:CONNECTS_TO]->(b:Space)
            RETURN b.id AS other_space_id, b.display_name AS other_space_name, 'outgoing' AS direction
            UNION
            MATCH (a:Space)-[:CONNECTS_TO]->(b:Space {id: $id})
            RETURN a.id AS other_space_id, a.display_name AS other_space_name, 'incoming' AS direction
            """,
            {"id": space_id},
        )
        return [dict(row) for row in result]

    def list_connections_for_floor(self, floor_id: str) -> list[dict]:
        result = self.db.execute(
            """
            MATCH (a:Space {floor_id: $floor_id})-[:CONNECTS_TO]->(b:Space {floor_id: $floor_id})
            RETURN a.id AS from_id, b.id AS to_id,
                   a.centroid_x AS from_cx, a.centroid_y AS from_cy,
                   b.centroid_x AS to_cx, b.centroid_y AS to_cy
            """,
            {"floor_id": floor_id},
        )
        return [dict(row) for row in result]

    def list_connections_for_campus(self, campus_id: str) -> list[dict]:
        result = self.db.execute(
            """
            MATCH (a:Space {campus_id: $campus_id})-[:CONNECTS_TO]->(b:Space)
            RETURN a.id AS from_space_id, b.id AS to_space_id
            """,
            {"campus_id": campus_id},
        )
        return [dict(row) for row in result]
