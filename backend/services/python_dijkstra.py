"""Weighted Dijkstra implemented in pure Python over Cypher reads."""
from __future__ import annotations

import heapq
import math
from typing import Any

from db import Database
from services.geometry_service import closest_point_on_polygon, parse_polygon


_WALKING_SPEED_MS = 1.4

_DEFAULT_FLOOR_HEIGHT_M = 3.3

_CONNECTION_TYPES: set[str] = {
    "DOOR_STANDARD", "DOOR_AUTOMATIC", "DOOR_LOCKED", "DOOR_EMERGENCY",
    "PASSAGE",
    "ELEVATOR", "STAIRCASE", "ESCALATOR", "RAMP",
}


def _room_exit_point(
    room: dict,
    door_x: float,
    door_y: float,
) -> tuple[float, float]:
    """Where you'd realistically *leave* a room to traverse the given door.
    Falls back to the room's centroid if the polygon isn't available — i.e.
    we never make the estimate *worse* than today's behaviour."""
    polygon = parse_polygon(room.get("polygon"))
    cx = room.get("centroid_x")
    cy = room.get("centroid_y")
    cx_f = float(cx) if cx is not None else None
    cy_f = float(cy) if cy is not None else None
    if polygon is None:
        return (cx_f or door_x, cy_f or door_y)
    snap = closest_point_on_polygon(polygon, door_x, door_y)
    return snap


def _edge_weight(src: dict, dst: dict) -> float:
    """Edge cost in seconds. Walking-time approximation:

    - Same-floor, same-building: Euclidean distance, but snap the room's
      endpoint to the polygon edge nearest the door when one side of the
      edge is a connector — represents exiting through the actual doorway.
    - Cross-floor / cross-building: 3D Pythagorean distance using a
      per-floor vertical step.

    Connection-type targets carry a fixed traversal penalty (the time it
    takes to actually pass through the elevator, open the door, etc.)."""
    sx, sy = src.get("centroid_x"), src.get("centroid_y")
    dx, dy = dst.get("centroid_x"), dst.get("centroid_y")
    have_centroids = None not in (sx, sy, dx, dy)

    src_fi, dst_fi = src.get("floor_index"), dst.get("floor_index")
    src_bi, dst_bi = src.get("building_id"), dst.get("building_id")
    floor_step = (
        abs(int(src_fi) - int(dst_fi))
        if src_fi is not None and dst_fi is not None
        else 0
    )
    different_building = (
        src_bi is not None and dst_bi is not None and src_bi != dst_bi
    )

    dist_m: float
    if have_centroids and floor_step == 0 and not different_building:
        src_is_conn = src.get("space_type") in _CONNECTION_TYPES
        dst_is_conn = dst.get("space_type") in _CONNECTION_TYPES
        if src_is_conn and not dst_is_conn:
            ex, ey = _room_exit_point(dst, float(sx), float(sy))
            dist_m = math.hypot(float(sx) - ex, float(sy) - ey)
        elif dst_is_conn and not src_is_conn:
            ex, ey = _room_exit_point(src, float(dx), float(dy))
            dist_m = math.hypot(ex - float(dx), ey - float(dy))
        else:
            dist_m = math.hypot(float(sx) - float(dx), float(sy) - float(dy))
    elif have_centroids and floor_step > 0 and not different_building:
        horizontal = math.hypot(float(sx) - float(dx), float(sy) - float(dy))
        vertical = floor_step * _DEFAULT_FLOOR_HEIGHT_M
        dist_m = math.hypot(horizontal, vertical)
    else:
        dist_m = 1.0

    penalty = 0.0
    if dst.get("space_type") in _CONNECTION_TYPES:
        penalty = float(dst.get("traversal_cost") or 0.0)

    return (dist_m / _WALKING_SPEED_MS) + penalty


def find_path(
    db: Database,
    from_id: str,
    to_id: str,
    *,
    accessible_only: bool = False,
    excluded_types: list[str] | None = None,
) -> dict | None:
    """Run weighted Dijkstra from ``from_id`` to ``to_id``."""
    excluded: set[str] = set(excluded_types or [])

    nodes = db.execute(
        """
        MATCH (s:Space)
        WHERE s.is_navigable = true OR s.id IN [$from_id, $to_id]
        RETURN
          s.id AS id,
          s.display_name AS display_name,
          s.space_type AS space_type,
          s.floor_index AS floor_index,
          s.building_id AS building_id,
          s.campus_id AS campus_id,
          s.centroid_x AS centroid_x,
          s.centroid_y AS centroid_y,
          s.centroid_lat AS centroid_lat,
          s.centroid_lng AS centroid_lng,
          s.polygon AS polygon,
          s.is_accessible AS is_accessible,
          s.traversal_cost AS traversal_cost
        """,
        {"from_id": from_id, "to_id": to_id},
    )
    if not nodes:
        return None

    by_id: dict[str, dict] = {n["id"]: dict(n) for n in nodes}
    if from_id not in by_id or to_id not in by_id:
        return None

    edges = db.execute(
        """
        MATCH (s:Space)-[:CONNECTS_TO]->(t:Space)
        WHERE (s.is_navigable = true OR s.id IN [$from_id, $to_id])
          AND (t.is_navigable = true OR t.id IN [$from_id, $to_id])
        RETURN s.id AS from_id, t.id AS to_id
        """,
        {"from_id": from_id, "to_id": to_id},
    )

    endpoints = {from_id, to_id}
    adj: dict[str, list[tuple[str, float]]] = {nid: [] for nid in by_id}
    for e in edges:
        src_id, dst_id = e["from_id"], e["to_id"]
        src, dst = by_id.get(src_id), by_id.get(dst_id)
        if src is None or dst is None:
            continue
        if dst_id not in endpoints:
            if dst.get("space_type") in excluded:
                continue
            if accessible_only and dst.get("is_accessible") is False:
                continue
        adj[src_id].append((dst_id, _edge_weight(src, dst)))

    # Standard binary-heap Dijkstra
    dist: dict[str, float] = {from_id: 0.0}
    prev: dict[str, str | None] = {from_id: None}
    heap: list[tuple[float, str]] = [(0.0, from_id)]
    while heap:
        d, u = heapq.heappop(heap)
        if u == to_id:
            break
        if d > dist.get(u, math.inf):
            continue
        for v, w in adj.get(u, []):
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))

    if to_id not in dist:
        return None

    # Reconstruct path back-to-front
    path_ids: list[str] = []
    cur: str | None = to_id
    while cur is not None:
        path_ids.append(cur)
        cur = prev.get(cur)
    path_ids.reverse()

    path_nodes: list[dict[str, Any]] = []
    for nid in path_ids:
        n = by_id[nid]
        path_nodes.append({
            "id": n["id"],
            "display_name": n.get("display_name"),
            "space_type": n.get("space_type"),
            "floor_index": n.get("floor_index"),
            "building_id": n.get("building_id"),
            "campus_id": n.get("campus_id"),
            "centroid_x": n.get("centroid_x"),
            "centroid_y": n.get("centroid_y"),
            "centroid_lat": n.get("centroid_lat"),
            "centroid_lng": n.get("centroid_lng"),
            "polygon": n.get("polygon"),
            "is_accessible": n.get("is_accessible"),
            "traversal_cost": n.get("traversal_cost"),
        })

    return {"path_nodes": path_nodes, "total_cost": dist[to_id]}
