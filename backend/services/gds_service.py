from db import Database

_PROJECTION_NAME = "navigation-graph"


class GdsService:
    def __init__(self, db: Database):
        self.db = db

    def projection_exists(self, name: str = _PROJECTION_NAME) -> bool:
        try:
            result = self.db.execute(
                "CALL gds.graph.exists($name) YIELD exists RETURN exists",
                {"name": name},
            )
            return result[0]["exists"] if result else False
        except Exception:
            return False

    def drop_projection(self, name: str = _PROJECTION_NAME) -> None:
        try:
            self.db.execute_write(
                "CALL gds.graph.drop($name, false) YIELD graphName RETURN graphName",
                {"name": name},
            )
        except Exception:
            pass

    def create_projection(self, name: str = _PROJECTION_NAME) -> bool:
        """Project navigable Space nodes and CONNECTS_TO relationships into GDS.

        Uses Cypher projection to derive edge weight from target node's traversal_cost.
        """
        try:
            self.db.execute_write(
                """
                CALL gds.graph.project.cypher(
                    $name,
                    'MATCH (s:Space) WHERE s.is_navigable = true RETURN id(s) AS id',
                    'MATCH (s:Space)-[:CONNECTS_TO]->(t:Space)
                     WHERE s.is_navigable = true AND t.is_navigable = true
                     RETURN id(s) AS source, id(t) AS target,
                            coalesce(t.traversal_cost, 1.0) AS weight'
                )
                YIELD graphName
                RETURN graphName
                """,
                {"name": name},
            )
            return True
        except Exception:
            return False

    def refresh_projection(self, name: str = _PROJECTION_NAME) -> bool:
        self.drop_projection(name)
        return self.create_projection(name)
