"""Stage 2 of the DXF → building-JSON pipeline.
"""
from __future__ import annotations

import math
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from services.dxf_import_service import (
    _INSUNITS_TO_METERS,
    _MIN_ROOM_AREA,
    _MIN_ROOM_AREA_M2,
    _MAX_ROOM_AREA_M2,
    _POLYGONIZE_SNAP_TOLERANCE,
    _OUTER_AREA_RATIO,
    _DOOR_CONNECTION_THRESHOLD_M,
    _arc_is_door_candidate,
    _classify_space,
    _drop_outer_envelopes,
    _filter_by_labels,
    _merge_id_type_pairs,
    _polygon_area_m2,
    _validate_and_dedupe,
)


def _resolve_scale(header: dict, bbox_span: float = 0.0) -> tuple[float, list[str]]:
    code = 0
    try:
        code = int(header.get("$INSUNITS", 0) or 0)
    except Exception:
        code = 0
    scale = _INSUNITS_TO_METERS.get(code)
    if scale is not None:
        return scale, [f"DXF $INSUNITS={code} -> {scale} m/unit"]
    if bbox_span > 5000:
        return 0.001, [f"$INSUNITS=0; bbox span {bbox_span:.0f} suggests mm (x0.001)."]
    if bbox_span > 200:
        return 0.01, [f"$INSUNITS=0; bbox span {bbox_span:.0f} suggests cm (x0.01)."]
    return 1.0, [f"$INSUNITS=0; bbox span {bbox_span:.0f} treated as meters."]


def _arc_points(cx: float, cy: float, r: float, start_deg: float, end_deg: float,
                steps: int) -> list[tuple[float, float]]:
    s = float(start_deg) % 360.0
    e = float(end_deg) % 360.0
    if e <= s:
        e += 360.0
    out = []
    for i in range(steps + 1):
        a = math.radians(s + (e - s) * (i / steps))
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out


def _closed_polylines(entities: list[dict]) -> list[dict]:
    out: list[dict] = []
    for e in entities:
        if e.get("type") not in ("LWPOLYLINE", "POLYLINE"):
            continue
        verts = e.get("vertices") or []
        if not e.get("closed"):
            if not verts or verts[0] != verts[-1]:
                continue
        coords = [(float(v["x"]), float(v["y"])) for v in verts]
        if len(coords) < 3:
            continue
        out.append({
            "layer": e.get("layer") or "",
            "polygon": coords,
            "source": "closed_polyline",
        })
    return out


def _hatch_rooms(entities: list[dict]) -> list[dict]:
    out: list[dict] = []
    for e in entities:
        if e.get("type") != "HATCH":
            continue
        for loop in e.get("loops") or []:
            coords = [(float(p["x"]), float(p["y"])) for p in loop]
            if len(coords) < 3:
                continue
            out.append({
                "layer": e.get("layer") or "",
                "polygon": coords,
                "source": "hatch",
            })
    return out


def _segments(entities: list[dict]) -> list[LineString]:
    segs: list[LineString] = []
    for e in entities:
        t = e.get("type")
        try:
            if t == "LINE":
                a, b = e.get("start"), e.get("end")
                if a and b:
                    segs.append(LineString([(a["x"], a["y"]), (b["x"], b["y"])]))
            elif t in ("LWPOLYLINE", "POLYLINE"):
                verts = e.get("vertices") or []
                pts = [(float(v["x"]), float(v["y"])) for v in verts]
                if len(pts) < 2:
                    continue
                for u, v in zip(pts, pts[1:]):
                    segs.append(LineString([u, v]))
                if e.get("closed") and pts[0] != pts[-1]:
                    segs.append(LineString([pts[-1], pts[0]]))
            elif t == "CIRCLE":
                c = e.get("center")
                r = e.get("radius")
                if not c or r is None:
                    continue
                pts = _arc_points(c["x"], c["y"], float(r), 0.0, 360.0, steps=32)
                for u, v in zip(pts, pts[1:]):
                    segs.append(LineString([u, v]))
            elif t == "ARC":
                c = e.get("center")
                r = e.get("radius")
                if not c or r is None:
                    continue
                pts = _arc_points(c["x"], c["y"], float(r),
                                  float(e.get("start_angle", 0.0)),
                                  float(e.get("end_angle", 0.0)),
                                  steps=20)
                for u, v in zip(pts, pts[1:]):
                    segs.append(LineString([u, v]))
        except Exception:
            continue
    return segs


def _snap_dedupe(segs: list[LineString], snap_tol: float) -> list[LineString]:
    if not segs:
        return []
    cell: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for s in segs:
        coords = list(s.coords)
        for x, y in (coords[0], coords[-1]):
            k = (int(round(x / snap_tol)), int(round(y / snap_tol)))
            cell.setdefault(k, []).append((x, y))
    canonical = {k: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v))
                 for k, v in cell.items()}
    seen: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    out: list[LineString] = []
    for s in segs:
        coords = list(s.coords)
        a, b = coords[0], coords[-1]
        ka = (int(round(a[0] / snap_tol)), int(round(a[1] / snap_tol)))
        kb = (int(round(b[0] / snap_tol)), int(round(b[1] / snap_tol)))
        pa, pb = canonical.get(ka, a), canonical.get(kb, b)
        if pa == pb:
            continue
        key = (pa, pb) if pa <= pb else (pb, pa)
        if key in seen:
            continue
        seen.add(key)
        out.append(LineString([pa, pb]))
    return out


def _polygonize(entities: list[dict], unit_scale: float, warnings: list[str]) -> list[dict]:
    raw_segs = _segments(entities)
    if not raw_segs:
        return []
    snap_tol_raw = max(0.020 / unit_scale, _POLYGONIZE_SNAP_TOLERANCE)
    segs = _snap_dedupe(raw_segs, snap_tol_raw)
    if not segs:
        return []
    try:
        merged = unary_union(segs)
        polys = list(polygonize(merged))
    except Exception as exc:
        warnings.append(f"polygonize: shapely failed: {exc}")
        return []
    polys = [p for p in polys if not p.is_empty and p.area >= _MIN_ROOM_AREA]
    if not polys:
        return []

    areas = sorted(p.area for p in polys)
    if len(areas) >= 2:
        median = areas[len(areas) // 2]
        largest = max(areas)
        if median > 0 and largest / median > _OUTER_AREA_RATIO:
            biggest = max(polys, key=lambda p: p.area)
            contains = sum(1 for p in polys
                           if biggest.buffer(0).contains(p) and p is not biggest)
            if contains >= max(1, len(polys) // 2):
                polys = [p for p in polys if p is not biggest]

    return [
        {"layer": "", "polygon": list(p.exterior.coords), "source": "polygonized_linework"}
        for p in polys
    ]


def _texts(entities: list[dict]) -> list[dict]:
    out: list[dict] = []
    for e in entities:
        t = e.get("type")
        if t not in ("TEXT", "MTEXT", "ATTRIB"):
            continue
        raw = (e.get("text") or "").strip()
        if not raw:
            continue
        if t == "MTEXT":
            raw = re.sub(r"\\[A-Za-z][^;]*;?", "", raw)
            raw = raw.replace("\\P", " ").replace("\r", " ").replace("\n", " ")
            raw = re.sub(r"\s+", " ", raw).strip()
        pos = e.get("position")
        if not pos:
            continue
        out.append({
            "x": float(pos["x"]),
            "y": float(pos["y"]),
            "text": raw,
            "layer": e.get("layer") or "",
            "source_type": t,
        })
    return out


def _scale_rooms(rooms: list[dict], scale: float) -> None:
    if scale == 1.0:
        return
    for r in rooms:
        r["polygon"] = [(x * scale, y * scale) for x, y in r["polygon"]]


def _scale_texts(texts: list[dict], scale: float) -> None:
    if scale == 1.0:
        return
    for t in texts:
        t["x"] *= scale
        t["y"] *= scale


def _door_candidates(entities: list[dict], scale: float) -> tuple[list[tuple[float, float]], int]:
    out: list[tuple[float, float]] = []
    arc_hits = 0
    for e in entities:
        t = e.get("type")
        layer_up = (e.get("layer") or "").upper()
        is_door_layer = (
            "DOOR" in layer_up or "DØR" in layer_up
            or layer_up in {"DR", "A-DOOR"}
        )

        if t == "ARC":
            try:
                r_m = float(e.get("radius") or 0.0) * scale
                start = float(e.get("start_angle") or 0.0)
                end = float(e.get("end_angle") or 0.0)
                sweep = abs(end - start)
                if sweep > 360:
                    sweep %= 360
                if sweep > 180:
                    sweep = 360 - sweep
                if _arc_is_door_candidate(r_m, sweep):
                    c = e.get("center")
                    if not c:
                        continue
                    cx = float(c["x"]) * scale
                    cy = float(c["y"]) * scale
                    mid = math.radians((start + end) / 2.0)
                    out.append((cx + r_m * math.cos(mid) * 0.5,
                                cy + r_m * math.sin(mid) * 0.5))
                    arc_hits += 1
            except Exception:
                continue
        elif is_door_layer:
            try:
                if t == "LINE":
                    a, b = e.get("start"), e.get("end")
                    if a and b:
                        out.append(((a["x"] + b["x"]) / 2.0 * scale,
                                    (a["y"] + b["y"]) / 2.0 * scale))
                elif t == "CIRCLE":
                    c = e.get("center")
                    if c:
                        out.append((float(c["x"]) * scale, float(c["y"]) * scale))
            except Exception:
                continue
    return out, arc_hits


def _infer_connections(spaces: list[dict], entities: list[dict],
                       scale: float) -> tuple[list[dict], int]:
    cands, arc_hits = _door_candidates(entities, scale)
    if not cands:
        return [], arc_hits

    polys: list[tuple[str, Polygon]] = []
    for s in spaces:
        try:
            poly = Polygon(s["polygon"])
            if poly.is_valid:
                polys.append((s["id"], poly))
        except Exception:
            continue
    if len(polys) < 2:
        return [], arc_hits

    try:
        from shapely.strtree import STRtree
        tree = STRtree([p for _, p in polys])
        use_tree = True
    except Exception:
        tree = None
        use_tree = False

    seen: set[tuple[str, str]] = set()
    conns: list[dict] = []
    for (x, y) in cands:
        pt = Point(x, y)
        if use_tree:
            try:
                hits = tree.query(pt.buffer(1.0))
                near: list[tuple[str, Polygon]] = []
                for cand in hits:
                    try:
                        ci = int(cand)
                        if 0 <= ci < len(polys):
                            near.append(polys[ci])
                    except (TypeError, ValueError):
                        continue
                if not near:
                    near = polys
            except Exception:
                near = polys
        else:
            near = polys

        dists: list[tuple[str, float]] = []
        for sid, poly in near:
            try:
                d = poly.distance(pt)
            except Exception:
                continue
            if d <= _DOOR_CONNECTION_THRESHOLD_M:
                dists.append((sid, d))
        if len(dists) < 2:
            continue
        dists.sort(key=lambda kv: kv[1])
        a, b = dists[0][0], dists[1][0]
        if a == b:
            continue
        key = (a, b) if a <= b else (b, a)
        if key in seen:
            continue
        seen.add(key)
        conns.append({
            "from_space_id": a,
            "to_space_id": b,
            "connection_type": "DOOR",
            "door_type": "STANDARD",
            "is_accessible": True,
            "door_cx": x,
            "door_cy": y,
            "requires_access_level": None,
            "transition_time_s": None,
            "weight_override": None,
        })
    return conns, arc_hits


_SPACE_KEYS = (
    "space_type", "centroid_x", "centroid_y", "is_accessible",
    "display_name", "tags", "polygon", "area_m2", "organization_id",
    "id", "is_outdoor", "is_navigable", "subspaces",
)


def _build_space(room: dict, organization_id: Optional[str],
                 layer_mapping: Optional[dict[str, str]]) -> dict:
    polygon = room["polygon"]
    try:
        shape = Polygon(polygon)
        cx, cy = float(shape.centroid.x), float(shape.centroid.y)
        area = float(shape.area)
    except Exception:
        cx = sum(p[0] for p in polygon) / len(polygon)
        cy = sum(p[1] for p in polygon) / len(polygon)
        area = _polygon_area_m2(polygon)

    layer = (room.get("layer") or "").strip()
    label = room.get("label") or ""
    space_type, is_nav, _ = _classify_space(layer, label, overrides=layer_mapping)
    if space_type == "ROOM_GENERIC":
        for extra in (room.get("metadata") or {}).get("extra_labels", []) or []:
            et, en, _ = _classify_space(layer, extra, overrides=layer_mapping)
            if et != "ROOM_GENERIC":
                space_type, is_nav = et, en
                break

    return {
        "space_type": space_type,
        "centroid_x": cx,
        "centroid_y": cy,
        "is_accessible": True,
        "display_name": label or f"Room {uuid.uuid4().hex[:6]}",
        "tags": [],
        "polygon": [[float(x), float(y)] for x, y in polygon],
        "area_m2": round(area, 6),
        "organization_id": organization_id,
        "id": str(uuid.uuid4()),
        "is_outdoor": False,
        "is_navigable": bool(is_nav),
        "subspaces": [],
    }


def _infer_floor_meta(source_file: str, fi: Optional[int], fn: Optional[str]) -> tuple[int, str]:
    stem = Path(source_file).stem.lower()
    name_map = {"stue": (0, "Stue"), "kld": (-1, "Kælder"), "kaelder": (-1, "Kælder")}
    inf_idx, inf_name = (None, None)
    for tok, (idx, name) in name_map.items():
        if stem.endswith(tok):
            inf_idx, inf_name = idx, name
            break
    if inf_idx is None:
        m = re.search(r"(\d+)\s*sal\b", stem)
        if m:
            inf_idx = int(m.group(1))
            inf_name = f"{inf_idx}. Sal"
    return (
        fi if fi is not None else (inf_idx if inf_idx is not None else 0),
        fn or inf_name or "Ground",
    )


def convert(
    normalized: dict,
    *,
    organization_id: Optional[str] = "org-aau",
    organization_name: Optional[str] = "Aalborg University",
    organization_entity_type: str = "UNIVERSITY",
    organization_description: Optional[str] = "Aalborg University",
    campus_id: str = "campus-aau-cph",
    campus_name: str = "AAU CPH",
    campus_description: Optional[str] = "A.C. Meyers Vænge 15",
    building_id: str = "bldg-acm15",
    building_name: str = "A.C. Meyers Vænge 15",
    building_short_name: Optional[str] = "A.C.",
    floor_id: Optional[str] = None,
    floor_index: Optional[int] = None,
    floor_display_name: Optional[str] = None,
    origin_bearing: float = 0.0,
    layer_mapping: Optional[dict[str, str]] = None,
) -> tuple[dict, dict]:
    """Convert a normalized DXF dict to a MapImportSchema-compatible dict.

    Returns `(schema, summary)`. `summary` carries the warnings,
    rooms-detected count, door-inference stats and unit scale so the
    caller can echo them to its own response envelope (the route does
    this for `_warnings` / `_classification_summary`).
    """
    entities = normalized.get("entities") or []
    header = normalized.get("header") or {}

    floor_index, floor_display_name = _infer_floor_meta(
        normalized.get("source_file", ""), floor_index, floor_display_name,
    )
    floor_id = floor_id or f"{building_id}-{floor_index}"

    warnings: list[str] = list(normalized.get("auditor_warnings") or [])

    raw_rooms = _closed_polylines(entities)
    raw_rooms.extend(_hatch_rooms(entities))

    bbox_span = 0.0
    for r in raw_rooms:
        xs = [p[0] for p in r["polygon"]]
        ys = [p[1] for p in r["polygon"]]
        if xs and ys:
            bbox_span = max(bbox_span, max(xs) - min(xs), max(ys) - min(ys))
    if bbox_span == 0.0:
        for e in entities:
            if e.get("type") == "LINE":
                a, b = e.get("start"), e.get("end")
                if a and b:
                    bbox_span = max(bbox_span,
                                    abs(float(a["x"]) - float(b["x"])),
                                    abs(float(a["y"]) - float(b["y"])))
    scale, unit_warnings = _resolve_scale(header, bbox_span)
    warnings.extend(unit_warnings)

    poly_rooms = _polygonize(entities, scale, warnings)
    raw_rooms.extend(poly_rooms)

    rooms, geom_warnings = _validate_and_dedupe(raw_rooms)
    warnings.extend(geom_warnings)

    _scale_rooms(rooms, scale)
    rooms = [
        r for r in rooms
        if _MIN_ROOM_AREA_M2 <= _polygon_area_m2(r["polygon"]) <= _MAX_ROOM_AREA_M2
    ]

    texts = _texts(entities)
    _scale_texts(texts, scale)
    rooms, label_warnings, label_stats = _filter_by_labels(
        rooms, texts, overrides=layer_mapping,
    )
    warnings.extend(label_warnings)
    rooms, _envelope_dropped = _drop_outer_envelopes(rooms)

    spaces = [_build_space(r, organization_id, layer_mapping) for r in rooms]

    for s in spaces:
        tokens = re.findall(r"\b[\w-]+\b", s["display_name"])
        for tok in tokens:
            if any(c.isdigit() for c in tok):
                s["short_name"] = tok
                break
    spaces, id_remap, _merged = _merge_id_type_pairs(spaces)
    for s in spaces:
        s.pop("short_name", None)

    conns, arc_hits = _infer_connections(spaces, entities, scale)
    if id_remap:
        for c in conns:
            if c["from_space_id"] in id_remap:
                c["from_space_id"] = id_remap[c["from_space_id"]]
            if c["to_space_id"] in id_remap:
                c["to_space_id"] = id_remap[c["to_space_id"]]
        conns = [c for c in conns if c["from_space_id"] != c["to_space_id"]]

    bidir: list[dict] = []
    for c in conns:
        a, b = c["from_space_id"], c["to_space_id"]
        bidir.append(c)
        rev = dict(c)
        rev["from_space_id"], rev["to_space_id"] = b, a
        bidir.append(rev)

    spaces = [{k: s.get(k) for k in _SPACE_KEYS} for s in spaces]

    org_block = None
    if organization_id:
        org_block = {
            "id": organization_id,
            "name": organization_name or organization_id,
            "entity_type": organization_entity_type,
            "description": organization_description,
        }

    schema: dict[str, Any] = {
        "schema_version": "1.0",
        "campus": {
            "organization_id": organization_id,
            "name": campus_name,
            "is_public": False,
            "description": campus_description,
            "id": campus_id,
            "buildings": [{
                "origin_bearing": origin_bearing,
                "organization_id": organization_id,
                "name": building_name,
                "short_name": building_short_name,
                "id": building_id,
                "floor_count": 1,
                "floors": [{
                    "floor_plan_scale": 1,
                    "floor_index": floor_index,
                    "floor_plan_origin_x": 0,
                    "floor_plan_origin_y": 0,
                    "id": floor_id,
                    "display_name": floor_display_name,
                    "spaces": spaces,
                }],
            }],
            "outdoor_spaces": [],
            "connections": bidir,
        },
    }
    if org_block:
        schema["organization"] = org_block

    classification_summary: dict[str, int] = {}
    for s in spaces:
        st = s.get("space_type") or "UNKNOWN"
        classification_summary[st] = classification_summary.get(st, 0) + 1

    summary = {
        "rooms_detected": len(spaces),
        "doors_inferred": len(conns),
        "arc_candidates_filtered": arc_hits,
        "unit_scale_m": scale,
        "warnings": warnings,
        "label_stats": label_stats,
        "classification_summary": classification_summary,
    }
    return schema, summary
