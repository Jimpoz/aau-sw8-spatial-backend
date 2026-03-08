from neo4j import GraphDatabase
from core.config import settings


class Database:
    _instance: "Database | None" = None

    def __init__(self):
        self.driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_pool_size=50,
        )

    def close(self):
        self.driver.close()

    def execute(self, query: str, params: dict = None) -> list[dict]:
        with self.driver.session() as session:
            result = session.run(query, params or {})
            return [r.data() for r in result]

    def execute_write(self, query: str, params: dict = None) -> list[dict]:
        def _work(tx):
            result = tx.run(query, params or {})
            return [record.data() for record in result]

        with self.driver.session() as session:
            return session.execute_write(_work)

    @classmethod
    def get_instance(cls) -> "Database":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


def get_db() -> Database:
    return Database.get_instance()
