import os

from neo4j import Driver, GraphDatabase

_neo4j_driver: Driver | None = None


def get_neo4j_driver() -> Driver:
    global _neo4j_driver
    if _neo4j_driver is None:
        _neo4j_driver = GraphDatabase.driver(
            os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            auth=(
                os.getenv("NEO4J_USER", "neo4j"),
                os.getenv("NEO4J_PASSWORD", ""),
            ),
            max_connection_pool_size=50,
        )
    return _neo4j_driver


def close_neo4j() -> None:
    global _neo4j_driver
    if _neo4j_driver is not None:
        _neo4j_driver.close()
        _neo4j_driver = None
