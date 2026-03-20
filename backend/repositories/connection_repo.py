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

    _CONN_TYPES = [
        "DOOR_STANDARD", "DOOR_AUTOMATIC", "DOOR_LOCKED", "DOOR_EMERGENCY",
        "PASSAGE", "OPEN", "STAIRCASE", "ELEVATOR", "ESCALATOR", "RAMP",
    ]

    def list_connections_for_space(self, space_id: str) -> list[dict]:
        # Determine if this space is itself a connection node
        type_result = self.db.execute(
            "MATCH (s:Space {id: $id}) RETURN s.space_type AS space_type",
            {"id": space_id},
        )
        if not type_result:
            return []

        space_type = type_result[0]["space_type"]

        if space_type in self._CONN_TYPES:
            # This IS a door/connection node — return the spaces it bridges
            result = self.db.execute(
                """
                MATCH (dest:Space)-[:CONNECTS_TO]->(s:Space {id: $id})
                WHERE NOT dest.space_type IN $conn_types
                RETURN DISTINCT dest.id AS other_space_id, dest.display_name AS other_space_name,
                       s.id AS door_node_id, s.space_type AS door_type,
                       s.is_accessible AS door_accessible, s.display_name AS door_name
                """,
                {"id": space_id, "conn_types": self._CONN_TYPES},
            )
        else:
            # Regular space — traverse through door nodes to find destinations
            result = self.db.execute(
                """
                MATCH (s:Space {id: $id})-[:CONNECTS_TO]->(door:Space)-[:CONNECTS_TO]->(dest:Space)
                WHERE door.space_type IN $conn_types AND dest.id <> $id
                RETURN DISTINCT dest.id AS other_space_id, dest.display_name AS other_space_name,
                       door.id AS door_node_id, door.space_type AS door_type,
                       door.is_accessible AS door_accessible, door.display_name AS door_name
                """,
                {"id": space_id, "conn_types": self._CONN_TYPES},
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
