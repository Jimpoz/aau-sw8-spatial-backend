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

    # Door/passage spaces that act as intermediate "midpoint" nodes between
    # two rooms (Room→Door→Room). STAIRCASE / ELEVATOR / etc. are connection
    # types in the GDS sense but they're real destinations in the UI, so they
    # are intentionally excluded here.
    _DOOR_LIKE_TYPES = {
        "DOOR_STANDARD", "DOOR_AUTOMATIC", "DOOR_LOCKED",
        "DOOR_EMERGENCY", "PASSAGE",
    }

    def list_connections_for_floor(self, floor_id: str) -> list[dict]:
        """Return one row per *logical* connection on this floor.

        - Room→Door→Room patterns collapse to a single row with `door_id`
          (and `door_cx/cy`, `door_type`) populated.
        - Direct Room→Room edges (no intermediate door Space, e.g. legacy
          imports) emit one row with `door_id=null`.
        """
        rows = self.db.execute(
            """
            MATCH (f:Floor {id: $floor_id})-[:HAS_SPACE]->(a:Space)-[:CONNECTS_TO]->(b:Space)
            MATCH (f)-[:HAS_SPACE]->(b)
            RETURN a.id AS a_id, a.space_type AS a_type,
                   a.centroid_x AS a_cx, a.centroid_y AS a_cy,
                   coalesce(a.is_accessible, true) AS a_acc,
                   b.id AS b_id, b.space_type AS b_type,
                   b.centroid_x AS b_cx, b.centroid_y AS b_cy,
                   coalesce(b.is_accessible, true) AS b_acc
            """,
            {"floor_id": floor_id},
        )

        DOOR_LIKE = self._DOOR_LIKE_TYPES
        # Group door Spaces with their incident endpoints (rooms only).
        door_info: dict[str, dict] = {}
        for r in rows:
            a_id, a_type = r["a_id"], r["a_type"]
            b_id, b_type = r["b_id"], r["b_type"]
            if a_id == b_id:
                continue
            # Identify which endpoint (if any) is the door
            if a_type in DOOR_LIKE and b_type not in DOOR_LIKE:
                d = door_info.setdefault(a_id, {
                    "door_id": a_id, "door_type": a_type,
                    "door_cx": r["a_cx"], "door_cy": r["a_cy"],
                    "is_accessible": True,
                    "endpoints": {},
                })
                d["endpoints"][b_id] = {
                    "cx": r["b_cx"], "cy": r["b_cy"], "acc": bool(r["b_acc"]),
                }
                d["is_accessible"] = d["is_accessible"] and bool(r["a_acc"])
            elif b_type in DOOR_LIKE and a_type not in DOOR_LIKE:
                d = door_info.setdefault(b_id, {
                    "door_id": b_id, "door_type": b_type,
                    "door_cx": r["b_cx"], "door_cy": r["b_cy"],
                    "is_accessible": True,
                    "endpoints": {},
                })
                d["endpoints"][a_id] = {
                    "cx": r["a_cx"], "cy": r["a_cy"], "acc": bool(r["a_acc"]),
                }
                d["is_accessible"] = d["is_accessible"] and bool(r["b_acc"])

        out: list[dict] = []
        seen: set[tuple] = set()

        # Emit one row per Room↔Room pair around each door.
        for d in door_info.values():
            endpoints = list(d["endpoints"].items())
            for i in range(len(endpoints)):
                for j in range(len(endpoints)):
                    if i == j:
                        continue
                    a_id, a = endpoints[i]
                    b_id, b = endpoints[j]
                    key = (a_id, b_id, d["door_id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "from_id": a_id, "to_id": b_id,
                        "from_cx": a["cx"], "from_cy": a["cy"],
                        "to_cx": b["cx"], "to_cy": b["cy"],
                        "door_id": d["door_id"],
                        "door_cx": d["door_cx"], "door_cy": d["door_cy"],
                        "door_type": d["door_type"],
                        "is_accessible": d["is_accessible"] and a["acc"] and b["acc"],
                    })

        # Emit direct Room→Room edges (neither endpoint is a door).
        for r in rows:
            a_id, a_type = r["a_id"], r["a_type"]
            b_id, b_type = r["b_id"], r["b_type"]
            if a_id == b_id:
                continue
            if a_type in DOOR_LIKE or b_type in DOOR_LIKE:
                continue
            key = (a_id, b_id, None)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "from_id": a_id, "to_id": b_id,
                "from_cx": r["a_cx"], "from_cy": r["a_cy"],
                "to_cx": r["b_cx"], "to_cy": r["b_cy"],
                "door_id": None,
                "door_cx": None, "door_cy": None,
                "door_type": None,
                "is_accessible": bool(r["a_acc"]) and bool(r["b_acc"]),
            })

        return out

    def list_connections_for_campus(self, campus_id: str) -> list[dict]:
        result = self.db.execute(
            """
            MATCH (a:Space {campus_id: $campus_id})-[:CONNECTS_TO]->(b:Space)
            RETURN a.id AS from_space_id, b.id AS to_space_id
            """,
            {"campus_id": campus_id},
        )
        return [dict(row) for row in result]
