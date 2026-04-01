from typing import Any


class Neo4jQueryRunner:
    """Small adapter that supports both Neo4j Session and Driver objects."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def run(self, query: str, **params: Any) -> list[Any]:
        if hasattr(self._conn, "run"):
            return list(self._conn.run(query, **params))
        if hasattr(self._conn, "session"):
            with self._conn.session() as session:
                return list(session.run(query, **params))
        raise TypeError("conn must be a neo4j Session or Driver")
