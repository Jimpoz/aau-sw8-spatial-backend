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
        """Project navigable Space nodes and CONNECTS_TO relationships into GDS."""
        try:
            self.db.execute_write(
                """
                CALL gds.graph.project(
                    $name,
                    {Space: {properties: ['is_navigable']}},
                    {CONNECTS_TO: {
                        orientation: 'NATURAL',
                        properties: ['weight', 'is_accessible']
                    }}
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
