"""
DXF / DWG floor-plan import. SOME ISSUES STILL
"""
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional
from collections import Counter
import re

import ezdxf
from ezdxf import recover as ezdxf_recover
from ezdxf.document import Drawing
from shapely.geometry import Point, Polygon, LineString
from shapely.validation import make_valid
from shapely.ops import polygonize, unary_union
import math


_DEFAULT_LAYER_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("ROOM_OFFICE", ("OFFICE", "OFC")),
    ("ROOM_CLASSROOM", ("CLASSROOM", "CLASS", "LECTURE")),
    ("ROOM_LECTURE_HALL", ("AUDITORIUM", "AUDITORIA")),
    ("ROOM_LAB", ("LAB", "LABORATORY")),
    ("ROOM_MEETING", ("MEETING", "CONF", "CONFERENCE", "BOARDROOM")),
    ("RESTROOM", ("REST", "WC", "TOILET", "BATH", "LAVATORY")),
    ("CORRIDOR", ("CORRIDOR", "HALL", "HALLWAY", "PASSAGE")),
    ("STAIRCASE", ("STAIR",)),
    ("ELEVATOR", ("ELEV", "LIFT")),
    ("ENTRANCE", ("ENTRANCE", "ENTRY", "LOBBY", "VESTIBULE")),
    ("CAFETERIA", ("CAFE", "CAFETERIA", "KITCHEN", "DINING")),
    ("LIBRARY", ("LIBRARY",)),
    ("SHOP", ("SHOP", "STORE", "RETAIL")),
]
_MIN_ROOM_AREA = 0.5
_LAT_RANGE = (-90.0, 90.0)
_LNG_RANGE = (-180.0, 180.0)
_POLYGONIZE_SNAP_TOLERANCE = 1e-3
_OUTER_AREA_RATIO = 8.0
_DOOR_CONNECTION_THRESHOLD = 0.5
""" DXF FILE LIMITS """
_MAX_DXF_BYTES = 80 * 1024 * 1024            
_MAX_FINAL_ROOMS = 8000                
_MAX_TEXT_ENTITIES = 5000                     


def _classify_layer(
    layer_name: str,
    overrides: Optional[dict[str, str]] = None,
) -> str:
    """Map a DXF layer name to a SpaceType. The optional `overrides`
    mapping wins outright (exact case-insensitive match); the default
    pattern list is the fallback."""
    name = (layer_name or "").strip()
    if overrides:
        for k, v in overrides.items():
            if k and k.upper() == name.upper():
                return v
    upper = name.upper()
    for space_type, patterns in _DEFAULT_LAYER_PATTERNS:
        for p in patterns:
            if p in upper:
                return space_type
    return "ROOM_GENERIC"


def _polygon_from_lwpolyline(entity) -> Optional[list[tuple[float, float]]]:
    if not entity.closed:
        return None
    coords = [(float(pt[0]), float(pt[1])) for pt in entity.get_points()]
    return coords if len(coords) >= 3 else None


def _polygon_from_polyline(entity) -> Optional[list[tuple[float, float]]]:
    if not getattr(entity, "is_closed", False):
        return None
    try:
        coords = [
            (float(v.dxf.location[0]), float(v.dxf.location[1]))
            for v in entity.vertices
        ]
    except Exception:
        return None
    return coords if len(coords) >= 3 else None


def _walk_polygons(entities, depth: int = 0, inherited_layer: Optional[str] = None) -> list[dict]:
    """Recursive walk over an entity collection — modelspace, or the
    virtual children of an INSERT — collecting closed polylines as
    `{layer, polygon}` dicts.

    `inherited_layer` is used so children inside an INSERT that are on
    layer "0" inherit the parent INSERT's layer for classification
    purposes.
    """
    out: list[dict] = []
    if depth > 6:
        # Defensive: protect against pathological deeply-nested blocks.
        return out
    for entity in entities:
        try:
            dxftype = entity.dxftype()
        except Exception:
            continue
        # Determine effective layer: entity layer unless it's the
        # special '0' layer in which case fall back to inherited_layer.
        eff_layer = None
        try:
            raw_layer = getattr(entity.dxf, "layer", None)
            eff_layer = raw_layer if (raw_layer and str(raw_layer).upper() != "0") else inherited_layer
        except Exception:
            eff_layer = inherited_layer

        if dxftype == "LWPOLYLINE":
            poly = _polygon_from_lwpolyline(entity)
            if poly:
                out.append({"layer": eff_layer or (getattr(entity.dxf, "layer", "") or ""), "polygon": poly, "source": "closed_polyline"})
        elif dxftype == "POLYLINE":
            poly = _polygon_from_polyline(entity)
            if poly:
                out.append({"layer": eff_layer or (getattr(entity.dxf, "layer", "") or ""), "polygon": poly, "source": "closed_polyline"})
        elif dxftype == "INSERT":
            # Block reference. Recurse into its virtual children and
            # pass the INSERT's layer as inherited_layer so children on
            # layer "0" pick it up.
            try:
                child_layer = getattr(entity.dxf, "layer", None) or inherited_layer
                out.extend(_walk_polygons(entity.virtual_entities(), depth + 1, inherited_layer=child_layer))
            except Exception:
                continue
    return out


def _approximate_arc_points(center_x: float, center_y: float, radius: float, start_deg: float, end_deg: float, steps: int = 16) -> list[tuple[float, float]]:
    """Approximate an ARC/CIRCLE by sampling points along the circular arc."""
    # Normalize angles
    start = float(start_deg) % 360.0
    end = float(end_deg) % 360.0
    if end <= start:
        end += 360.0
    points: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = start + (end - start) * (i / steps)
        rad = math.radians(t)
        x = center_x + radius * math.cos(rad)
        y = center_y + radius * math.sin(rad)
        points.append((float(x), float(y)))
    return points


def _collect_segments_from_entities(entities, depth: int = 0) -> list[LineString]:
    """Collect linear segments from an entity iterable (modelspace or
    virtual_entities). Recurses into INSERTs. Returns raw LineString
    segments (coordinates in document units)."""
    segments: list[LineString] = []
    if depth > 8:
        return segments
    for e in entities:
        try:
            t = e.dxftype()
        except Exception:
            continue
        try:
            if t == "LINE":
                s = e.dxf.start
                ed = e.dxf.end
                segments.append(LineString([(float(s[0]), float(s[1])), (float(ed[0]), float(ed[1]))]))
            elif t == "LWPOLYLINE":
                pts = [(float(p[0]), float(p[1])) for p in e.get_points()]
                if len(pts) >= 2:
                    for a, b in zip(pts, pts[1:]):
                        segments.append(LineString([a, b]))
                    if getattr(e, "closed", False) and pts[0] != pts[-1]:
                        segments.append(LineString([pts[-1], pts[0]]))
            elif t == "POLYLINE":
                try:
                    pts = [(float(v.dxf.location[0]), float(v.dxf.location[1])) for v in e.vertices]
                except Exception:
                    pts = []
                if len(pts) >= 2:
                    for a, b in zip(pts, pts[1:]):
                        segments.append(LineString([a, b]))
                    if getattr(e, "is_closed", False) and pts[0] != pts[-1]:
                        segments.append(LineString([pts[-1], pts[0]]))
            elif t == "CIRCLE":
                c = e.dxf.center
                r = float(e.dxf.radius)
                pts = _approximate_arc_points(float(c[0]), float(c[1]), r, 0.0, 360.0, steps=32)
                for a, b in zip(pts, pts[1:]):
                    segments.append(LineString([a, b]))
            elif t == "ARC":
                c = e.dxf.center
                r = float(e.dxf.radius)
                start = float(e.dxf.start_angle)
                end = float(e.dxf.end_angle)
                pts = _approximate_arc_points(float(c[0]), float(c[1]), r, start, end, steps=20)
                for a, b in zip(pts, pts[1:]):
                    segments.append(LineString([a, b]))
            elif t == "INSERT":
                try:
                    segments.extend(_collect_segments_from_entities(e.virtual_entities(), depth + 1))
                except Exception:
                    continue
        except Exception:
            continue
    return segments


def _snap_and_dedupe_segments(segments: list[LineString], snap_tol: float) -> list[LineString]:
    """Snap segment endpoints to a coarse grid (precision snap_tol),
    dedupe identical/degenerate segments, and return cleaned
    LineStrings suitable for polygonization.
    This is intentionally conservative: snap_tol should be small for
    precise results.
    """
    if not segments:
        return []

    cell_map: dict[tuple[int, int], list[tuple[float, float]]] = {}
    pts_keys: list[tuple[int, int]] = []
    for s in segments:
        try:
            coords = list(s.coords)
        except Exception:
            continue
        for x, y in (coords[0], coords[-1]):
            key = (int(round(x / snap_tol)), int(round(y / snap_tol)))
            cell_map.setdefault(key, []).append((float(x), float(y)))
            pts_keys.append(key)

    canonical: dict[tuple[int, int], tuple[float, float]] = {}
    for k, pts in cell_map.items():
        sx = sum(p[0] for p in pts)
        sy = sum(p[1] for p in pts)
        n = len(pts)
        canonical[k] = (sx / n, sy / n)

    seen: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    out: list[LineString] = []
    for s in segments:
        try:
            coords = list(s.coords)
            a = coords[0]
            b = coords[-1]
            ka = (int(round(a[0] / snap_tol)), int(round(a[1] / snap_tol)))
            kb = (int(round(b[0] / snap_tol)), int(round(b[1] / snap_tol)))
            pa = canonical.get(ka, (float(a[0]), float(a[1])))
            pb = canonical.get(kb, (float(b[0]), float(b[1])))
            if pa == pb:
                continue
            key = (pa, pb) if pa <= pb else (pb, pa)
            if key in seen:
                continue
            seen.add(key)
            out.append(LineString([pa, pb]))
        except Exception:
            continue

    return out


def _polygonize_from_doc(doc: Drawing) -> list[dict]:
    """Polygonize by collecting segments (recursing into INSERTs),
    snapping endpoints conservatively, merging, and running
    `shapely.ops.polygonize`.
    Returns list of {layer, polygon, source} dicts.
    """
    try:
        msp = doc.modelspace()
    except Exception:
        return []

    raw_segments = _collect_segments_from_entities(msp)
    if not raw_segments:
        return []

    # Adaptive snap: keep small for precision but allow tiny gaps
    # relative to drawing size. Use the configured constant as a
    # conservative floor.
    try:
        xs = [c for s in raw_segments for c in (s.coords[0][0], s.coords[-1][0])]
        ys = [c for s in raw_segments for c in (s.coords[0][1], s.coords[-1][1])]
        if xs and ys:
            dx = max(xs) - min(xs)
            dy = max(ys) - min(ys)
            diag = math.hypot(dx, dy)
            snap_tol = max(_POLYGONIZE_SNAP_TOLERANCE, diag * 1e-6)
        else:
            snap_tol = _POLYGONIZE_SNAP_TOLERANCE
    except Exception:
        snap_tol = _POLYGONIZE_SNAP_TOLERANCE

    segs = _snap_and_dedupe_segments(raw_segments, snap_tol)
    if not segs:
        return []

    try:
        merged = unary_union(segs)
        polys = list(polygonize(merged))
    except Exception:
        return []

    polys = [p for p in polys if not p.is_empty and p.area >= _MIN_ROOM_AREA]
    if not polys:
        return []

    # Remove likely outer building polygon when it's much larger than
    # the rest and contains the others.
    areas = sorted([p.area for p in polys])
    if len(areas) >= 2:
        median = areas[len(areas) // 2]
        largest = max(areas)
        if median > 0 and largest / median > _OUTER_AREA_RATIO:
            largest_poly = max(polys, key=lambda p: p.area)
            contains_count = sum(1 for p in polys if largest_poly.buffer(0).contains(p) and p != largest_poly)
            if contains_count >= max(1, len(polys) // 2):
                polys = [p for p in polys if p != largest_poly]

    out: list[dict] = []
    for poly in polys:
        coords = list(poly.exterior.coords)
        out.append({"layer": "", "polygon": coords, "source": "polygonized_linework"})
    return out


def _polygonize_from_parsed(parsed: dict) -> list[dict]:
    """Construct line segments from the `dxf-json` parsed dict and
    polygonize them to find enclosed polygons."""
    entities = parsed.get("entities") or []
    segments: list[LineString] = []
    for e in entities:
        t = (e.get("type") or "").upper()
        try:
            if t == "LINE":
                # Various field names may be used by dxf-json
                a = e.get("start") or e.get("startPoint") or e.get("from") or e.get("p1")
                b = e.get("end") or e.get("endPoint") or e.get("to") or e.get("p2")
                if not a or not b:
                    continue
                def _pt(x):
                    if isinstance(x, dict):
                        return float(x.get("x")), float(x.get("y"))
                    elif isinstance(x, (list, tuple)) and len(x) >= 2:
                        return float(x[0]), float(x[1])
                    return None
                pa = _pt(a)
                pb = _pt(b)
                if pa and pb:
                    segments.append(LineString([pa, pb]))
            elif t in ("LWPOLYLINE", "POLYLINE"):
                verts = e.get("vertices") or []
                pts = []
                for v in verts:
                    if isinstance(v, dict):
                        x = v.get("x")
                        y = v.get("y")
                    elif isinstance(v, (list, tuple)) and len(v) >= 2:
                        x, y = v[0], v[1]
                    else:
                        continue
                    try:
                        pts.append((float(x), float(y)))
                    except Exception:
                        continue
                if len(pts) >= 2:
                    for a, b in zip(pts, pts[1:]):
                        segments.append(LineString([a, b]))
                    flag = e.get("flag") or e.get("flags") or 0
                    closed = False
                    try:
                        if int(flag) & 1:
                            closed = True
                    except Exception:
                        closed = False
                    if closed and pts[0] != pts[-1]:
                        segments.append(LineString([pts[-1], pts[0]]))
            elif t == "CIRCLE":
                c = e.get("center") or e.get("centerPoint") or e.get("position")
                r = e.get("radius")
                if not c or not r:
                    continue
                try:
                    cx = float(c.get("x")) if isinstance(c, dict) else float(c[0])
                    cy = float(c.get("y")) if isinstance(c, dict) else float(c[1])
                    rr = float(r)
                except Exception:
                    continue
                pts = _approximate_arc_points(cx, cy, rr, 0.0, 360.0, steps=24)
                for a, b in zip(pts, pts[1:]):
                    segments.append(LineString([a, b]))
            elif t == "ARC":
                c = e.get("center") or e.get("centerPoint")
                r = e.get("radius")
                start = e.get("startAngle") or e.get("start") or 0
                end = e.get("endAngle") or e.get("end") or 0
                if not c or not r:
                    continue
                try:
                    cx = float(c.get("x")) if isinstance(c, dict) else float(c[0])
                    cy = float(c.get("y")) if isinstance(c, dict) else float(c[1])
                    rr = float(r)
                    st = float(start)
                    ed = float(end)
                except Exception:
                    continue
                pts = _approximate_arc_points(cx, cy, rr, st, ed, steps=12)
                for a, b in zip(pts, pts[1:]):
                    segments.append(LineString([a, b]))
        except Exception:
            continue

    if not segments:
        return []

    try:
        merged = unary_union(segments)
        polys = list(polygonize(merged))
    except Exception:
        return []

    out: list[dict] = []
    for poly in polys:
        if poly.is_empty:
            continue
        if poly.area < _MIN_ROOM_AREA:
            continue
        coords = list(poly.exterior.coords)
        out.append({"layer": "", "polygon": coords})
    return out


def _validate_and_dedupe(rooms: list[dict]) -> tuple[list[dict], list[str]]:
    """Drop zero-area / invalid polygons, attempt automatic repair on
    polygons shapely flags as invalid, and dedupe coincident polygons.
    Returns the cleaned list plus a human-readable warning trail the
    response can surface to the editor."""
    warnings: list[str] = []
    seen_signatures: set[tuple] = set()
    cleaned: list[dict] = []
    for r in rooms:
        polygon = r["polygon"]
        try:
            shape = Polygon(polygon)
        except Exception:
            warnings.append(f"layer {r['layer']!r}: polygon failed to construct, dropped")
            continue
        if not shape.is_valid:
            try:
                repaired = make_valid(shape)
                # `make_valid` may produce a MultiPolygon when the input
                # self-intersects; pick the largest piece.
                if repaired.geom_type == "MultiPolygon":
                    parts = list(repaired.geoms)
                    parts.sort(key=lambda g: g.area, reverse=True)
                    shape = parts[0] if parts else None
                elif repaired.geom_type == "Polygon":
                    shape = repaired
                else:
                    shape = None
                if shape is None or shape.is_empty:
                    warnings.append(f"layer {r['layer']!r}: polygon could not be repaired, dropped")
                    continue
                warnings.append(f"layer {r['layer']!r}: polygon was self-intersecting and was auto-repaired")
                # Replace the raw vertex list with the repaired shell.
                polygon = list(shape.exterior.coords)
                r = {**r, "polygon": polygon}
            except Exception:
                warnings.append(f"layer {r['layer']!r}: polygon was invalid and could not be repaired, dropped")
                continue
        if shape.area < _MIN_ROOM_AREA:
            # Annotations, hatch fragments, dimension boxes etc.
            continue
        # Deduplicate by rounded vertex sequence (tolerates micro
        # floating-point differences between coincident polygons).
        sig = tuple(sorted((round(x, 4), round(y, 4)) for x, y in polygon))
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        cleaned.append(r)
    return cleaned, warnings


def _extract_texts(doc: Drawing) -> list[dict]:
    """Collect text-like labels from the document.

    Returns list of dicts with keys: x, y, text, layer, source_type.
    Supports TEXT, MTEXT, ATTRIB (from INSERTs) and text inside
    virtual_entities() produced by INSERTs. MTEXT is cleaned of
    common formatting characters.
    """
    def _clean_mtext(raw: str) -> str:
        s = str(raw)
        # Replace newlines with spaces, remove control codes
        s = s.replace("\r", " ").replace("\n", " ")
        s = re.sub(r"\s+", " ", s)
        # Remove common MTEXT formatting sequences (simple heuristic)
        s = re.sub(r"\{\\.*?\}", "", s)
        return s.strip()

    texts: list[dict] = []

    def _walk_text_entities(entities, inherited_layer: Optional[str] = None):
        for e in entities:
            try:
                t = e.dxftype()
            except Exception:
                continue
            eff_layer = None
            try:
                raw_layer = getattr(e.dxf, "layer", None)
                eff_layer = raw_layer if (raw_layer and str(raw_layer).upper() != "0") else inherited_layer
            except Exception:
                eff_layer = inherited_layer

            if t == "TEXT":
                try:
                    insert = e.dxf.insert
                    raw = e.text if hasattr(e, "text") else e.dxf.text
                    if raw:
                        texts.append({"x": float(insert[0]), "y": float(insert[1]), "text": str(raw).strip(), "layer": eff_layer or "", "source_type": "TEXT"})
                except Exception:
                    continue
            elif t == "MTEXT":
                try:
                    insert = e.dxf.insert if hasattr(e.dxf, "insert") else (e.dxf.insert if hasattr(e.dxf, "insert") else None)
                    raw = e.text if hasattr(e, "text") else getattr(e.dxf, "text", "")
                    if raw and insert:
                        texts.append({"x": float(insert[0]), "y": float(insert[1]), "text": _clean_mtext(raw), "layer": eff_layer or "", "source_type": "MTEXT"})
                except Exception:
                    continue
            elif t == "ATTRIB":
                try:
                    insert = e.dxf.insert
                    raw = e.dxf.text if hasattr(e.dxf, "text") else getattr(e, "text", "")
                    if raw:
                        texts.append({"x": float(insert[0]), "y": float(insert[1]), "text": str(raw).strip(), "layer": eff_layer or "", "source_type": "ATTRIB"})
                except Exception:
                    continue
            elif t == "INSERT":
                # Recurse into virtual entities with the INSERT's layer
                try:
                    child_layer = getattr(e.dxf, "layer", None) or inherited_layer
                    _walk_text_entities(e.virtual_entities(), inherited_layer=child_layer)
                except Exception:
                    continue

    _walk_text_entities(doc.modelspace(), inherited_layer=None)
    return texts


def _attach_labels(rooms: list[dict], texts: list[dict]) -> None:
    # Build Shapely polygons and attach all contained texts to each
    polys = []
    for r in rooms:
        try:
            polys.append((r, Polygon(r["polygon"])))
        except Exception:
            polys.append((r, None))

    # Collect labels per room
    labels_per_room: dict[int, list[dict]] = {i: [] for i in range(len(rooms))}
    for t in texts:
        p = Point(t["x"], t["y"])
        for idx, (r, poly) in enumerate(polys):
            if poly is None or not poly.is_valid:
                continue
            try:
                if poly.contains(p):
                    labels_per_room[idx].append(t)
                    break
            except Exception:
                continue

    for idx, (r, poly) in enumerate(polys):
        labels = labels_per_room.get(idx, [])
        if not labels:
            continue
        # Prefer labels containing digits
        chosen = None
        for lab in labels:
            if re.search(r"\d", lab.get("text", "")):
                chosen = lab
                break
        if chosen is None:
            # Prefer shortest non-trivial label
            labs_sorted = sorted([lab for lab in labels if len(lab.get("text", "").strip()) > 0], key=lambda x: len(x.get("text", "")))
            chosen = labs_sorted[0] if labs_sorted else labels[0]

        # Attach chosen label and store others as extra_labels in metadata
        r["label"] = chosen.get("text")
        extra = [lab.get("text") for lab in labels if lab is not chosen]
        if extra:
            meta = r.get("metadata") or {}
            meta.setdefault("extra_labels", [])
            meta["extra_labels"].extend(extra)
            r["metadata"] = meta


def _collect_diagnostics_from_doc(doc: Drawing) -> dict:
    msp = doc.modelspace()
    entity_counts = Counter()
    layer_counts = Counter()
    type_layer_counts = Counter()
    closed_poly_count = 0
    text_count = 0
    insert_count = 0
    hatch_count = 0
    line_arc_count = 0
    for e in msp:
        try:
            t = e.dxftype()
        except Exception:
            continue
        entity_counts[t] += 1
        layer = getattr(e.dxf, "layer", "") or ""
        layer_counts[layer] += 1
        type_layer_counts[(t, layer)] += 1
        if t in ("LWPOLYLINE", "POLYLINE"):
            if (getattr(e, "closed", False) or getattr(e, "is_closed", False)):
                closed_poly_count += 1
        if t in ("TEXT", "MTEXT", "ATTRIB"):
            text_count += 1
        if t == "INSERT":
            insert_count += 1
        if t == "HATCH":
            hatch_count += 1
        if t in ("LINE", "ARC", "CIRCLE"):
            line_arc_count += 1

    return {
        "entity_counts": dict(entity_counts),
        "layer_counts": dict(layer_counts),
        "type_layer_counts": {f"{k[0]}|{k[1]}": v for k, v in type_layer_counts.items()},
        "closed_poly_count": closed_poly_count,
        "text_count": text_count,
        "insert_count": insert_count,
        "hatch_count": hatch_count,
        "line_arc_count": line_arc_count,
    }


def _collect_diagnostics_from_parsed(parsed: dict) -> dict:
    entities = parsed.get("entities") or []
    entity_counts = Counter()
    layer_counts = Counter()
    type_layer_counts = Counter()
    closed_poly_count = 0
    text_count = 0
    insert_count = 0
    hatch_count = 0
    line_arc_count = 0
    for e in entities:
        t = (e.get("type") or "").upper()
        layer = (e.get("layer") or "")
        entity_counts[t] += 1
        layer_counts[layer] += 1
        type_layer_counts[(t, layer)] += 1
        if t in ("LWPOLYLINE", "POLYLINE"):
            verts = e.get("vertices") or []
            flag = e.get("flag") or e.get("flags") or 0
            closed = False
            try:
                if int(flag) & 1:
                    closed = True
            except Exception:
                closed = False
            if not closed and verts and verts[0] == verts[-1]:
                closed = True
            if closed:
                closed_poly_count += 1
        if t in ("TEXT", "MTEXT", "ATTRIB"):
            text_count += 1
        if t == "INSERT":
            insert_count += 1
        if t == "HATCH":
            hatch_count += 1
        if t in ("LINE", "ARC", "CIRCLE"):
            line_arc_count += 1

    return {
        "entity_counts": dict(entity_counts),
        "layer_counts": dict(layer_counts),
        "type_layer_counts": {f"{k[0]}|{k[1]}": v for k, v in type_layer_counts.items()},
        "closed_poly_count": closed_poly_count,
        "text_count": text_count,
        "insert_count": insert_count,
        "hatch_count": hatch_count,
        "line_arc_count": line_arc_count,
    }


def _classify_space(layer_name: Optional[str], label: Optional[str], tags: list[str], overrides: Optional[dict[str, str]] = None) -> tuple[str, bool, float]:
    """Classify a space using: overrides -> layer -> label -> tags -> fallback.
    Returns (space_type, is_navigable, confidence)
    """
    if overrides and layer_name:
        for k, v in overrides.items():
            if k and layer_name and k.upper() == layer_name.upper():
                return v, (v in ("CORRIDOR", "ENTRANCE", "STAIRCASE", "ELEVATOR")), 0.95

    if layer_name:
        t = _classify_layer(layer_name, overrides=overrides)
        if t != "ROOM_GENERIC":
            return t, (t in ("CORRIDOR", "ENTRANCE", "STAIRCASE", "ELEVATOR")), 0.9

    # Try label-based patterns
    txt = (label or "").upper()
    for space_type, patterns in _DEFAULT_LAYER_PATTERNS:
        for p in patterns:
            if p in txt:
                return space_type, (space_type in ("CORRIDOR", "ENTRANCE", "STAIRCASE", "ELEVATOR")), 0.8

    # Tags
    for tag in tags:
        for space_type, patterns in _DEFAULT_LAYER_PATTERNS:
            for p in patterns:
                if p in tag.upper():
                    return space_type, (space_type in ("CORRIDOR", "ENTRANCE", "STAIRCASE", "ELEVATOR")), 0.75

    return "ROOM_GENERIC", False, 0.5


def _extract_hatch_polygons(doc: Drawing) -> tuple[list[dict], list[str]]:
    """Attempt to extract polygons from HATCH entities. This is
    best-effort and will produce warnings when hatch paths/edges are
    not supported by the simple extractor.
    Returns (list_of_room_dicts, warnings)
    """
    out: list[dict] = []
    warnings: list[str] = []
    try:
        msp = doc.modelspace()
    except Exception:
        return out, warnings

    for h in msp.query("HATCH"):
        try:
            loops: list[list[tuple[float, float]]] = []
            # ezdxf exposes paths on Hatch objects; each path has edges
            for path in getattr(h, "paths", []):
                coords: list[tuple[float, float]] = []
                for edge in getattr(path, "edges", []):
                    et = edge.dxftype() if hasattr(edge, "dxftype") else ""
                    if et == "LINE" or et == "LineEdge":
                        try:
                            a = getattr(edge, "start", None) or getattr(edge, "start_point", None)
                            b = getattr(edge, "end", None) or getattr(edge, "end_point", None)
                            if a and b:
                                coords.append((float(a[0]), float(a[1])))
                        except Exception:
                            continue
                    elif et in ("ARC", "ArcEdge"):
                        try:
                            c = getattr(edge, "center", None)
                            r = getattr(edge, "radius", None)
                            st = getattr(edge, "start_angle", None) or getattr(edge, "start", 0)
                            ed = getattr(edge, "end_angle", None) or getattr(edge, "end", 0)
                            if c and r:
                                pts = _approximate_arc_points(float(c[0]), float(c[1]), float(r), float(st), float(ed), steps=8)
                                coords.extend(pts)
                        except Exception:
                            warnings.append(f"HATCH: unsupported arc edge in hatch on layer {getattr(h.dxf, 'layer', '')}")
                            continue
                    else:
                        # Unsupported edge: try to see if it exposes points
                        pts = getattr(edge, "points", None)
                        if pts:
                            for p in pts:
                                try:
                                    coords.append((float(p[0]), float(p[1])))
                                except Exception:
                                    continue
                if coords:
                    # Ensure closed
                    if coords[0] != coords[-1]:
                        coords.append(coords[0])
                    loops.append(coords)
            for poly in loops:
                out.append({"layer": getattr(h.dxf, "layer", "") or "", "polygon": poly, "source": "hatch"})
        except Exception as exc:
            warnings.append(f"HATCH extraction failed: {exc}")
            continue
    return out, warnings


def _infer_connections(spaces: list[dict], doc: Optional[Drawing]) -> tuple[list[dict], list[str]]:
    """Conservative door-based connection inference.
    Scans the document for door-like layers/entities and attempts to
    associate door points with two nearby spaces.
    Returns (connections, warnings).
    """
    warnings: list[str] = []
    connections: list[dict] = []
    if not doc:
        return connections, warnings

    try:
        msp = doc.modelspace()
    except Exception:
        return connections, warnings

    door_layers = set()
    door_points: list[tuple[float, float]] = []
    # Collect potential door geometry
    for e in msp:
        try:
            layer = (getattr(e.dxf, "layer", "") or "").upper()
            name = (getattr(e, "name", None) or getattr(e.dxf, "name", None) or "").upper() if hasattr(e, "dxftype") else ""
            t = e.dxftype()
        except Exception:
            continue
        if "DOOR" in layer or "DOOR" in name or any(k in layer for k in ("DØR", "DR", "A-DOOR")):
            door_layers.add(layer)
            # Get representative point
            try:
                if t == "INSERT":
                    pt = getattr(e.dxf, "insert", None)
                    if pt:
                        door_points.append((float(pt[0]), float(pt[1])))
                elif t == "LINE":
                    s = e.dxf.start
                    ed = e.dxf.end
                    door_points.append(((float(s[0]) + float(ed[0])) / 2.0, (float(s[1]) + float(ed[1])) / 2.0))
                elif t == "CIRCLE":
                    c = e.dxf.center
                    door_points.append((float(c[0]), float(c[1])))
                elif t == "ARC":
                    c = e.dxf.center
                    s_ang = float(e.dxf.start_angle)
                    e_ang = float(e.dxf.end_angle)
                    mid = math.radians((s_ang + e_ang) / 2.0)
                    r = float(e.dxf.radius)
                    door_points.append((float(c[0]) + r * math.cos(mid), float(c[1]) + r * math.sin(mid)))
            except Exception:
                continue

    # Build Shapely shapes for spaces
    shapes = []
    for s in spaces:
        try:
            poly = Polygon(s["polygon"]) if s.get("polygon") else None
        except Exception:
            poly = None
        shapes.append((s.get("id"), poly))

    for pt in door_points:
        p = Point(pt)
        dists = []
        for sid, poly in shapes:
            if poly is None:
                continue
            try:
                d = poly.distance(p)
            except Exception:
                d = float("inf")
            dists.append((sid, d))
        dists.sort(key=lambda x: x[1])
        if len(dists) >= 2 and dists[0][1] <= _DOOR_CONNECTION_THRESHOLD and dists[1][1] <= _DOOR_CONNECTION_THRESHOLD:
            a, b = dists[0][0], dists[1][0]
            if a != b and not any((c.get("from_space_id") == a and c.get("to_space_id") == b) or (c.get("from_space_id") == b and c.get("to_space_id") == a) for c in connections):
                connections.append({
                    "from_space_id": a,
                    "to_space_id": b,
                    "connection_type": "DOORWAY",
                    "is_accessible": True,
                    "door_type": "STANDARD",
                    "requires_access_level": "public",
                    "transition_time_s": 5.0,
                    "weight_override": None,
                    "metadata": {"source": "door_geometry"},
                })
        else:
            # low-confidence door point
            warnings.append(f"Door candidate at {pt} could not be matched to two nearby spaces (nearest distances: {[(d[1]) for d in dists[:2]]})")

    if connections:
        warnings.append(f"Inferred {len(connections)} door connections from door-layer geometry; please review.")
    return connections, warnings


def _convert_dwg_with_oda(dwg_bytes: bytes) -> bytes:
    """ODA File Converter path. Highest fidelity but needs the proprietary
    binary (free download from the Open Design Alliance, not redistributable)."""
    with tempfile.TemporaryDirectory() as in_dir, tempfile.TemporaryDirectory() as out_dir:
        in_path = Path(in_dir) / "input.dwg"
        in_path.write_bytes(dwg_bytes)
        proc = subprocess.run(
            ["ODAFileConverter", in_dir, out_dir, "ACAD2018", "DXF", "0", "1"],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"ODAFileConverter failed (rc={proc.returncode}): "
                f"{proc.stderr.decode('utf-8', errors='ignore')[:400]}"
            )
        out_path = Path(out_dir) / "input.dxf"
        if not out_path.exists():
            raise RuntimeError("ODAFileConverter ran but did not produce a DXF output")
        return out_path.read_bytes()


def _convert_dwg_with_libredwg(dwg_bytes: bytes) -> bytes:
    """LibreDWG (`dwgread`) path. GPL, freely redistributable, available
    in Debian as the `libredwg` package. Lower fidelity than ODA on
    quirky AutoCAD revisions but sufficient for the floor-plan vocabulary
    (LWPOLYLINE, POLYLINE, TEXT, MTEXT, INSERT) that we actually consume."""
    with tempfile.TemporaryDirectory() as work_dir:
        in_path = Path(work_dir) / "input.dwg"
        out_path = Path(work_dir) / "input.dxf"
        in_path.write_bytes(dwg_bytes)
        # `dwgread -O DXF input.dwg -o output.dxf` produces an ASCII DXF.
        proc = subprocess.run(
            ["dwgread", "-O", "DXF", "-o", str(out_path), str(in_path)],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"dwgread failed (rc={proc.returncode}): "
                f"{proc.stderr.decode('utf-8', errors='ignore')[:400]}"
            )
        if not out_path.exists():
            raise RuntimeError("dwgread ran but did not produce a DXF output")
        return out_path.read_bytes()


def _convert_dwg_to_dxf(dwg_bytes: bytes) -> bytes:
    """Best-effort DWG → DXF transcode. Tries the converters in order of
    fidelity (ODA → LibreDWG); raises FileNotFoundError with a clear
    actionable message when neither is installed."""
    if shutil.which("ODAFileConverter"):
        return _convert_dwg_with_oda(dwg_bytes)
    if shutil.which("dwgread"):
        return _convert_dwg_with_libredwg(dwg_bytes)
    raise FileNotFoundError(
        "No DWG converter is installed on the backend. Three ways to fix this:\n"
        "  1) Convert the .dwg to .dxf on your machine and upload the .dxf. "
        "Every CAD tool can do this — LibreCAD, DraftSight, AutoCAD, or the "
        "free standalone ODA File Converter.\n"
        "  2) Rebuild the backend image with the LibreDWG build flag:\n"
        "       docker compose build --build-arg INSTALL_LIBREDWG=1 backend\n"
        "     This is the default in the current Dockerfile — re-running "
        "`docker compose build backend` should produce an image where "
        "DWG just works.\n"
        "  3) For higher-fidelity DWG support, install ODAFileConverter via "
        "the build arg (requires you to mirror the .deb yourself):\n"
        "       docker compose build --build-arg INSTALL_ODA=1 \\\n"
        "         --build-arg ODA_DEB_URL=https://your-mirror/...deb backend\n"
    )


def _sanitize_libredwg_dxf(dxf_bytes: bytes) -> tuple[bytes, list[str]]:
    """Strip malformed code/value pairs from a LibreDWG-produced DXF.

    LibreDWG's DXF writer occasionally emits a non-numeric line where a
    DXF group code should be (typically a font name leaking out of the
    STYLE/TEXT tables). DXF is a strict alternating code/value stream
    where code lines must parse as integers in [0, 1071]; anything else
    is structural corruption that ezdxf's recovery path can't repair.

    The strategy is to walk the file as code/value pairs and, on any
    malformed code line, drop *one* line to resync the pair counter and
    continue. This salvages the rest of the file at the cost of losing
    a handful of entities (almost always table records, not geometry).

    This is a fallback. We only call it when ezdxf's strict and recovery
    paths have already failed on a DWG-converted DXF — running it on
    clean files is safe but pointless.
    """
    text = dxf_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()
    out: list[str] = []
    warnings: list[str] = []
    dropped = 0
    i = 0
    n = len(lines)
    while i < n - 1:
        code_line = lines[i]
        stripped = code_line.strip()
        try:
            code = int(stripped)
            if code < 0 or code > 1071:
                raise ValueError("group code out of range")
        except ValueError:
            # Bad code line — drop it and resync. Don't advance the
            # value index because the malformation is usually a single
            # extra line, so the next line is a valid code.
            dropped += 1
            if dropped <= 5:
                value_peek = lines[i + 1].strip() if i + 1 < n else ""
                warnings.append(
                    f"DWG→DXF sanitizer: dropped malformed line {i + 1}: "
                    f"{stripped!r} (next line: {value_peek!r})"
                )
            i += 1
            continue
        out.append(code_line)
        out.append(lines[i + 1])
        i += 2
    if i < n:
        out.append(lines[i])
    if dropped > 5:
        warnings.append(
            f"DWG→DXF sanitizer: dropped {dropped - 5} additional malformed lines"
        )
    return ("\n".join(out) + "\n").encode("utf-8"), warnings


def _read_dxf_robust(dxf_bytes: bytes) -> tuple[Drawing, list[str]]:
    """Read a DXF (ASCII or binary) using ezdxf's recovery path. The
    recovery path tolerates encoding mismatches, missing headers, and a
    handful of other malformations that the strict reader rejects.
    Returns the document plus a list of recovered-error strings the
    auditor flagged."""
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
        tmp.write(dxf_bytes)
        tmp_path = tmp.name
    try:
        try:
            doc, auditor = ezdxf_recover.readfile(tmp_path)
        except ezdxf.DXFStructureError as exc:
            raise ValueError(
                f"DXF file structure is too damaged to recover: {exc}"
            ) from exc
        warnings: list[str] = []
        for err in getattr(auditor, "errors", [])[:50]:
            warnings.append(f"DXF auditor: {err}")
        for fix in getattr(auditor, "fixes", [])[:50]:
            warnings.append(f"DXF auditor (auto-fixed): {fix}")
        return doc, warnings
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass


def _validate_origin(lat: Optional[float], lng: Optional[float]) -> None:
    if lat is None and lng is None:
        return
    if lat is None or lng is None:
        raise ValueError("origin_lat and origin_lng must be supplied together (or both omitted)")
    if not (_LAT_RANGE[0] <= lat <= _LAT_RANGE[1]):
        raise ValueError(f"origin_lat {lat} out of range {_LAT_RANGE}")
    if not (_LNG_RANGE[0] <= lng <= _LNG_RANGE[1]):
        raise ValueError(f"origin_lng {lng} out of range {_LNG_RANGE}")


class DxfImportService:
    """Translates a DXF (or a DWG that we first transcode) into a
    `MapImportSchema`-compatible dict. The output is fed straight into
    `ImportService.import_map` so the DXF flow shares the same atomic
    Neo4j+PostGIS pipeline as the JSON flow."""

    def parse(
        self,
        file_bytes: bytes,
        filename: str,
        *,
        campus_id: str,
        campus_name: str,
        building_id: str,
        building_name: str,
        floor_id: str,
        floor_index: int,
        floor_display_name: str,
        organization_id: Optional[str] = None,
        organization_name: Optional[str] = None,
        origin_lat: Optional[float] = None,
        origin_lng: Optional[float] = None,
        origin_bearing: float = 0.0,
        layer_mapping: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        _validate_origin(origin_lat, origin_lng)

        if len(file_bytes) > _MAX_DXF_BYTES:
            raise ValueError(
                f"Upload is {len(file_bytes) // (1024*1024)} MB; the parser refuses "
                f"files larger than {_MAX_DXF_BYTES // (1024*1024)} MB to avoid "
                f"out-of-memory hangs. Split the drawing per floor, or raise "
                f"_MAX_DXF_BYTES in dxf_import_service.py if you've sized the host."
            )

        import time
        import logging
        log = logging.getLogger("dxf_import")
        t0 = time.monotonic()
        def _stage(label: str, started: float, **extra) -> None:
            log.warning("[dxf-import] %s took %.2fs %s", label, time.monotonic() - started, extra or "")

        # Resolve the upload to DXF bytes regardless of which CAD format
        # the user sent.
        ext = Path(filename or "").suffix.lower()
        from_dwg = False
        if ext == ".dxf":
            dxf_bytes = file_bytes
        elif ext == ".dwg":
            t = time.monotonic()
            dxf_bytes = _convert_dwg_to_dxf(file_bytes)
            from_dwg = True
            _stage("dwg→dxf transcode", t, output_bytes=len(dxf_bytes))
        else:
            raise ValueError(
                f"Unsupported file extension '{ext}'. Upload a .dxf or a .dwg."
            )

        # Robust read: handles ASCII DXF, binary DXF, and slightly
        # malformed files that the strict reader rejects.
        sanitize_warnings: list[str] = []
        t = time.monotonic()
        try:
            doc, dxf_warnings = _read_dxf_robust(dxf_bytes)
        except ValueError as exc:
            # The recovery path raises ValueError when the DXF is
            # structurally malformed beyond what ezdxf can repair. If we
            # got here via a DWG conversion, the malformation is almost
            # certainly LibreDWG's DXF writer (it occasionally emits a
            # non-numeric line where a group code should be — e.g. a
            # font name from the STYLE table). Run the sanitizer pass
            # and retry; if that still fails, surface an actionable
            # error so the user knows their options.
            if from_dwg and "Invalid group code" in str(exc):
                sanitized_bytes, sanitize_warnings = _sanitize_libredwg_dxf(dxf_bytes)
                try:
                    doc, dxf_warnings = _read_dxf_robust(sanitized_bytes)
                except ValueError as exc2:
                    raise ValueError(
                        "The DWG was transcoded to DXF on the server, but the "
                        "resulting DXF is structurally malformed and the "
                        "auto-sanitizer pass could not salvage it either. "
                        f"Underlying error: {exc2}\n\n"
                        "Two ways to get this DWG imported:\n"
                        "  1) Convert the .dwg to .dxf locally with any CAD tool "
                        "(LibreCAD, DraftSight, BricsCAD, AutoCAD, or the standalone "
                        "ODA File Converter) and re-upload the .dxf.\n"
                        "  2) Install ODAFileConverter into the backend image for "
                        "higher-fidelity DWG support:\n"
                        "       docker compose build --build-arg INSTALL_ODA=1 \\\n"
                        "         --build-arg ODA_DEB_URL=https://your-mirror/...deb backend"
                    ) from exc2
            else:
                raise
        # Surface the sanitizer's warnings alongside the auditor's so
        # the editor sees what was salvaged.
        if sanitize_warnings:
            dxf_warnings = [*sanitize_warnings, *dxf_warnings]
        _stage("ezdxf parse", t)

        # Gather diagnostics
        t = time.monotonic()
        diagnostics = _collect_diagnostics_from_doc(doc)
        _stage("diagnostics", t)

        t = time.monotonic()
        raw_rooms = _walk_polygons(doc.modelspace())
        _stage("walk_polygons", t, count=len(raw_rooms))

        t = time.monotonic()
        hatch_rooms, hatch_warnings = _extract_hatch_polygons(doc)
        _stage("hatch", t, count=len(hatch_rooms))
        if hatch_rooms:
            raw_rooms = [*raw_rooms, *hatch_rooms]

        polygonize_skipped_reason: Optional[str] = None
        t = time.monotonic()
        try:
            poly_rooms = _polygonize_from_doc(doc)
        except Exception as exc:
            poly_rooms = []
            polygonize_skipped_reason = f"polygonization failed: {exc}"
        _stage("polygonize", t, count=len(poly_rooms))
        if poly_rooms:
            raw_rooms = [*raw_rooms, *poly_rooms]

        if not raw_rooms:
            raise ValueError(
                "No closed polylines, hatch boundaries, or recoverable polygon loops found in DXF. "
                "The file may be a model-view drawing, or rooms are drawn as "
                "disconnected annotations rather than enclosed polygons."
            )

        t = time.monotonic()
        rooms, geom_warnings = _validate_and_dedupe(raw_rooms)
        _stage("validate_and_dedupe", t, kept=len(rooms), input=len(raw_rooms))
        if polygonize_skipped_reason:
            geom_warnings.append(polygonize_skipped_reason)
        if not rooms:
            raise ValueError(
                "All polygons in the DXF were rejected as invalid, "
                "zero-area, or noise. Check that rooms are drawn as "
                "closed LWPOLYLINEs/HATCHes and have meaningful area."
            )
        if len(rooms) > _MAX_FINAL_ROOMS:
            raise ValueError(
                f"Too many rooms detected ({len(rooms)}); the parser caps at {_MAX_FINAL_ROOMS} to avoid out-of-memory hangs. "
            )

        t = time.monotonic()
        texts = _extract_texts(doc)
        if len(texts) > _MAX_TEXT_ENTITIES:
            geom_warnings.append(
                f"Found {len(texts)} text entities; truncated to {_MAX_TEXT_ENTITIES} for label attachment."
            )
            texts = texts[:_MAX_TEXT_ENTITIES]
        _attach_labels(rooms, texts)
        _stage("attach_labels", t, texts=len(texts), rooms=len(rooms))

        # Build enriched spaces with metadata, classification, and sizes.
        spaces: list[dict] = []
        for i, r in enumerate(rooms, start=1):
            sid = f"{floor_id}_space_{i}"
            label = r.get("label") or f"Room {i}"
            polygon = r["polygon"]
            src = r.get("source") or "closed_polyline"
            # Centroid and area via shapely when possible
            try:
                shape = Polygon(polygon)
                cx, cy = float(shape.centroid.x), float(shape.centroid.y)
                area = float(shape.area)
                minx, miny, maxx, maxy = shape.bounds
                width = float(maxx - minx)
                length = float(maxy - miny)
            except Exception:
                cx = sum(p[0] for p in polygon) / len(polygon)
                cy = sum(p[1] for p in polygon) / len(polygon)
                try:
                    shape = Polygon(polygon)
                    area = float(shape.area)
                    minx, miny, maxx, maxy = shape.bounds
                    width = float(maxx - minx)
                    length = float(maxy - miny)
                except Exception:
                    area = 0.0
                    width = 0.0
                    length = 0.0

            # Short name heuristic: first token containing a digit
            tokens = re.findall(r"\b[\w-]+\b", label)
            short_name: Optional[str] = None
            for t in tokens:
                if any(ch.isdigit() for ch in t):
                    short_name = t
                    break

            # Tags: layer + label tokens + source
            tags: list[str] = []
            layer = (r.get("layer") or "").strip()
            if layer:
                tags.append(layer.lower())
            for t in tokens:
                tl = t.lower()
                if len(tl) > 1 and tl not in tags:
                    tags.append(tl)
            tags.append(src)

            # Classification
            space_type, is_nav, class_conf = _classify_space(layer, label, tags, overrides=layer_mapping)
            # source-based base confidence
            base_conf = 0.95 if src == "closed_polyline" else 0.85 if src == "hatch" else 0.6
            confidence = min(1.0, base_conf * 0.7 + class_conf * 0.3)

            meta = r.get("metadata") or {}
            meta.setdefault("source_layer", layer or None)
            meta.setdefault("source_geometry_type", src)
            meta.setdefault("confidence", round(confidence, 2))
            meta.setdefault("warnings", [])

            spaces.append({
                "id": sid,
                "display_name": label,
                "short_name": short_name,
                "space_type": space_type,
                "polygon": [[float(x), float(y)] for x, y in polygon],
                "centroid_x": cx,
                "centroid_y": cy,
                "area_m2": round(area, 3),
                "width_m": round(width, 3),
                "length_m": round(length, 3),
                "is_navigable": bool(is_nav),
                "is_accessible": True,
                "is_outdoor": False,
                "capacity": None,
                "tags": tags,
                "metadata": meta,
                "subspaces": [],
            })

        # Compute building/floor bounds from the detected spaces so the
        # JSON matches the richer shape in `test_3.json`.
        xs: list[float] = []
        ys: list[float] = []
        for s in spaces:
            for x, y in s.get("polygon", []):
                xs.append(x)
                ys.append(y)
        if xs and ys:
            min_x, min_y, max_x, max_y = min(xs), min(ys), max(xs), max(ys)
            building_bounds = [[min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y]]
            floor_plan_bounds = building_bounds
        else:
            building_bounds = None
            floor_plan_bounds = None

        schema: dict[str, Any] = {
            "schema_version": "1.0",
            "campus": {
                "id": campus_id,
                "name": campus_name,
                "description": building_name or None,
                "organization_id": organization_id,
                "is_public": False,
                "buildings": [{
                    "id": building_id,
                    "name": building_name,
                    "short_name": (building_name.split()[0] if building_name else None),
                    "organization_id": organization_id,
                    "origin_lat": origin_lat,
                    "origin_lng": origin_lng,
                    "origin_bearing": origin_bearing,
                    "floor_count": 1,
                    "floors": [{
                        "id": floor_id,
                        "floor_index": floor_index,
                        "display_name": floor_display_name,
                        "floor_plan_bounds": floor_plan_bounds,
                        "spaces": spaces,
                    }],
                    "building_bounds": building_bounds,
                }],
                "outdoor_spaces": [],
                "connections": [],
            },
        }
        if organization_id and organization_name:
            schema["organization"] = {
                "id": organization_id,
                "name": organization_name,
                "entity_type": "OTHER",
            }

        t = time.monotonic()
        if len(spaces) > _MAX_FINAL_ROOMS // 2:
            connections, conn_warnings = [], [
                f"Skipped door inference: {len(spaces)} spaces is past the safety threshold; "
                f"author connections in the editor instead."
            ]
        else:
            connections, conn_warnings = _infer_connections(spaces, doc)
        _stage("infer_connections", t, connections=len(connections))
        schema["campus"]["connections"] = connections

        # Diagnostics summary
        diagnostics_out = diagnostics.copy() if isinstance(diagnostics, dict) else {}
        diagnostics_out["raw_room_count"] = len(raw_rooms) if isinstance(raw_rooms, list) else None
        diagnostics_out["cleaned_room_count"] = len(rooms)
        diagnostics_out["parsing_engine"] = "ezdxf"
        schema["_diagnostics"] = diagnostics_out

        # Surface every warning the parsing pipeline produced — DXF
        # auditor recoveries plus geometry repairs — so the editor can
        # see what was salvaged before they trust the import.
        schema["_warnings"] = [*dxf_warnings, *hatch_warnings, *geom_warnings, *conn_warnings]
        # Add a clear hint when only a single room was found
        if diagnostics_out.get("cleaned_room_count", 0) <= 1:
            schema["_warnings"].append(
                "Only one or zero room-like polygons detected; import may be incomplete."
                " Check for HATCH boundaries, unclosed linework, or that the drawing is a model view."
            )

        schema["_classification_summary"] = self._classification_summary(spaces)
        log.warning("[dxf-import] DONE in %.2fs (rooms=%d, connections=%d)",
                    time.monotonic() - t0, len(rooms), len(connections))
        return schema

    def _parse_with_dxfjson(
        self,
        dxf_bytes: bytes,
        filename: str,
        *,
        campus_id: str,
        campus_name: str,
        building_id: str,
        building_name: str,
        floor_id: str,
        floor_index: int,
        floor_display_name: str,
        organization_id: Optional[str] = None,
        organization_name: Optional[str] = None,
        origin_lat: Optional[float] = None,
        origin_lng: Optional[float] = None,
        origin_bearing: float = 0.0,
        layer_mapping: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Attempt to parse the DXF using the Node `dxf-json` parser as a
        fallback. This requires `node` on PATH and the repository script
        `scripts/dxf_json_parse.mjs` to be present (we added this helper
        to the repo). Raises FileNotFoundError when Node/script isn't
        available so the caller can surface an actionable message.
        """
        node_exec = shutil.which("node") or shutil.which("nodejs")
        if not node_exec:
            raise FileNotFoundError("Node.js executable not found on PATH. Install Node.js in the backend image or run the conversion locally.")

        # Locate the helper Node script in parent folders (repo root):
        script_path: Optional[Path] = None
        here = Path(__file__).resolve()
        for p in here.parents:
            cand = p / "scripts" / "dxf_json_parse.mjs"
            if cand.exists():
                script_path = cand
                break
        if script_path is None:
            # Fallback to CWD scripts dir
            cand2 = Path.cwd() / "scripts" / "dxf_json_parse.mjs"
            if cand2.exists():
                script_path = cand2
        if script_path is None:
            raise FileNotFoundError("dxf_json_parse.mjs not found in repository; ensure scripts/dxf_json_parse.mjs is present in the backend image.")

        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
            tmp.write(dxf_bytes)
            tmp_path = tmp.name
        try:
            proc = subprocess.run([node_exec, str(script_path), str(tmp_path)], capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                raise RuntimeError(f"dxf-json parser failed (rc={proc.returncode}): {proc.stderr.strip()[:1000]}")
            parsed = json.loads(proc.stdout)
        finally:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass

        # Extract closed polylines from the parsed output
        entities = parsed.get("entities") or []
        raw_rooms: list[dict] = []
        for e in entities:
            t = (e.get("type") or "").upper()
            if t not in ("LWPOLYLINE", "POLYLINE"):
                continue
            verts = e.get("vertices") or []
            coords: list[tuple[float, float]] = []
            for v in verts:
                if isinstance(v, dict):
                    x = v.get("x")
                    y = v.get("y")
                elif isinstance(v, (list, tuple)) and len(v) >= 2:
                    x, y = v[0], v[1]
                else:
                    continue
                try:
                    coords.append((float(x), float(y)))
                except Exception:
                    continue
            if not coords:
                continue
            flag = e.get("flag") or e.get("flags") or 0
            closed = False
            try:
                if int(flag) & 1:
                    closed = True
            except Exception:
                closed = False
            if not closed and coords and coords[0] == coords[-1]:
                closed = True
            if not closed:
                continue
            raw_rooms.append({"layer": e.get("layer") or "", "polygon": [(x, y) for x, y in coords]})

        if not raw_rooms:
            # Try polygonizing the raw dxf-json entities (LINE/ARC/CIRCLE)
            raw_rooms = _polygonize_from_parsed(parsed)
            if not raw_rooms:
                raise ValueError("dxf-json produced no closed polylines; no rooms detected")

        rooms, geom_warnings = _validate_and_dedupe(raw_rooms)
        if not rooms:
            raise ValueError("All polygons parsed by dxf-json were rejected as invalid or noise")

        # Extract texts
        texts: list[dict] = []
        for e in entities:
            t = (e.get("type") or "").upper()
            if t == "TEXT":
                pt = e.get("startPoint") or e.get("position") or e.get("insertionPoint")
                raw = e.get("text") or ""
            elif t == "MTEXT":
                pt = e.get("insertionPoint") or e.get("startPoint")
                raw = e.get("text") or ""
            else:
                continue
            if not raw:
                continue
            if isinstance(pt, dict):
                x = pt.get("x")
                y = pt.get("y")
            elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                x, y = pt[0], pt[1]
            else:
                continue
            try:
                texts.append({"x": float(x), "y": float(y), "text": str(raw).strip()})
            except Exception:
                continue

        _attach_labels(rooms, texts)

        # Build spaces list
        spaces: list[dict] = []
        for i, r in enumerate(rooms, start=1):
            sid = f"{floor_id}_space_{i}"
            label = r.get("label") or f"Room {i}"
            polygon = r["polygon"]
            try:
                shape = Polygon(polygon)
                cx, cy = float(shape.centroid.x), float(shape.centroid.y)
                area = float(shape.area)
            except Exception:
                cx = sum(p[0] for p in polygon) / len(polygon)
                cy = sum(p[1] for p in polygon) / len(polygon)
                try:
                    area = float(Polygon(polygon).area)
                except Exception:
                    area = 0.0

            tokens = re.findall(r"\b[\w-]+\b", label)
            short_name: Optional[str] = None
            for t in tokens:
                if any(ch.isdigit() for ch in t):
                    short_name = t
                    break

            tags: list[str] = []
            layer = (r.get("layer") or "").strip()
            if layer:
                tags.append(layer.lower())
            for t in tokens:
                tl = t.lower()
                if len(tl) > 1 and tl not in tags:
                    tags.append(tl)

            spaces.append({
                "id": sid,
                "display_name": label,
                "short_name": short_name,
                "space_type": _classify_layer(layer, overrides=layer_mapping),
                "polygon": [[float(x), float(y)] for x, y in polygon],
                "centroid_x": cx,
                "centroid_y": cy,
                "area_m2": round(area, 3),
                "is_navigable": True,
                "is_accessible": True,
                "is_outdoor": False,
                "tags": tags,
            })

        xs: list[float] = []
        ys: list[float] = []
        for s in spaces:
            for x, y in s.get("polygon", []):
                xs.append(x)
                ys.append(y)
        if xs and ys:
            min_x, min_y, max_x, max_y = min(xs), min(ys), max(xs), max(ys)
            building_bounds = [[min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y]]
            floor_plan_bounds = building_bounds
        else:
            building_bounds = None
            floor_plan_bounds = None

        schema: dict[str, Any] = {
            "schema_version": "1.0",
            "campus": {
                "id": campus_id,
                "name": campus_name,
                "description": building_name or None,
                "organization_id": organization_id,
                "is_public": False,
                "buildings": [{
                    "id": building_id,
                    "name": building_name,
                    "short_name": (building_name.split()[0] if building_name else None),
                    "organization_id": organization_id,
                    "origin_lat": origin_lat,
                    "origin_lng": origin_lng,
                    "origin_bearing": origin_bearing,
                    "floor_count": 1,
                    "floors": [{
                        "id": floor_id,
                        "floor_index": floor_index,
                        "display_name": floor_display_name,
                        "floor_plan_bounds": floor_plan_bounds,
                        "spaces": spaces,
                    }],
                    "building_bounds": building_bounds,
                }],
                "outdoor_spaces": [],
                "connections": [],
            },
        }
        if organization_id and organization_name:
            schema["organization"] = {
                "id": organization_id,
                "name": organization_name,
                "entity_type": "OTHER",
            }

        schema["_warnings"] = []
        schema["_classification_summary"] = self._classification_summary(spaces)
        return schema

    @staticmethod
    def _classification_summary(spaces: list[dict]) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in spaces:
            out[s["space_type"]] = out.get(s["space_type"], 0) + 1
        return out


def parse_layer_mapping(raw: Optional[str]) -> Optional[dict[str, str]]:
    """Decode the optional `layer_mapping` form field. Returns None
    when the field is absent or empty; raises ValueError on malformed
    JSON or non-string values so the route can surface a 422."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"layer_mapping is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise ValueError("layer_mapping must be a JSON object {layer_name: SpaceType}")
    out: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError("layer_mapping keys and values must both be strings")
        out[k] = v
    return out
