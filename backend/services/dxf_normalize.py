"""Stage 1 of the DXF → building-JSON pipeline."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable, Optional

import ezdxf
from ezdxf import recover as ezdxf_recover

from services.dxf_import_service import (
    _convert_dwg_to_dxf,
    _read_dxf_robust,
    _sanitize_libredwg_dxf,
)


_MAX_INSERT_DEPTH = 6


def _xy(point: Any) -> Optional[dict[str, float]]:
    if point is None:
        return None
    try:
        return {"x": float(point[0]), "y": float(point[1])}
    except (TypeError, IndexError, ValueError):
        return None


def _effective_layer(entity: Any, inherited: Optional[str]) -> Optional[str]:
    try:
        raw = getattr(entity.dxf, "layer", None)
    except Exception:
        raw = None
    if raw and str(raw) != "0":
        return str(raw)
    return inherited


def _entity_to_record(e: Any, layer: Optional[str]) -> Optional[dict[str, Any]]:
    t = e.dxftype()
    base: dict[str, Any] = {"type": t, "layer": layer or ""}

    if t == "LWPOLYLINE":
        try:
            verts = [{"x": float(p[0]), "y": float(p[1]),
                      "bulge": float(p[4]) if len(p) > 4 else 0.0}
                     for p in e.get_points()]
        except Exception:
            return None
        base["closed"] = bool(getattr(e, "closed", False))
        base["vertices"] = verts
        return base

    if t == "POLYLINE":
        try:
            verts = [{"x": float(v.dxf.location[0]),
                      "y": float(v.dxf.location[1]),
                      "bulge": float(getattr(v.dxf, "bulge", 0.0) or 0.0)}
                     for v in e.vertices]
        except Exception:
            return None
        base["closed"] = bool(getattr(e, "is_closed", False))
        base["vertices"] = verts
        return base

    if t == "LINE":
        try:
            return {**base, "start": _xy(e.dxf.start), "end": _xy(e.dxf.end)}
        except Exception:
            return None

    if t == "CIRCLE":
        try:
            return {**base, "center": _xy(e.dxf.center), "radius": float(e.dxf.radius)}
        except Exception:
            return None

    if t == "ARC":
        try:
            return {**base,
                    "center": _xy(e.dxf.center),
                    "radius": float(e.dxf.radius),
                    "start_angle": float(e.dxf.start_angle),
                    "end_angle": float(e.dxf.end_angle)}
        except Exception:
            return None

    if t == "TEXT":
        try:
            raw = e.dxf.text if hasattr(e.dxf, "text") else getattr(e, "text", "")
            return {**base,
                    "position": _xy(e.dxf.insert),
                    "text": str(raw or "").strip(),
                    "height": float(getattr(e.dxf, "height", 0.0) or 0.0)}
        except Exception:
            return None

    if t == "MTEXT":
        try:
            raw = e.text if hasattr(e, "text") else getattr(e.dxf, "text", "")
            return {**base,
                    "position": _xy(getattr(e.dxf, "insert", None)),
                    "text": str(raw or "").strip(),
                    "height": float(getattr(e.dxf, "char_height", 0.0) or 0.0)}
        except Exception:
            return None

    if t == "ATTRIB":
        try:
            raw = e.dxf.text if hasattr(e.dxf, "text") else getattr(e, "text", "")
            return {**base,
                    "position": _xy(e.dxf.insert),
                    "text": str(raw or "").strip(),
                    "tag": getattr(e.dxf, "tag", None)}
        except Exception:
            return None

    if t == "HATCH":
        loops: list[list[dict[str, float]]] = []
        try:
            for path in getattr(e, "paths", []):
                pts: list[dict[str, float]] = []
                for edge in getattr(path, "edges", []) or []:
                    et = edge.dxftype() if hasattr(edge, "dxftype") else ""
                    if et in ("LINE", "LineEdge"):
                        a = getattr(edge, "start", None) or getattr(edge, "start_point", None)
                        if a is not None:
                            xy = _xy(a)
                            if xy:
                                pts.append(xy)
                    elif et in ("ARC", "ArcEdge"):
                        c = getattr(edge, "center", None)
                        r = getattr(edge, "radius", None)
                        st = getattr(edge, "start_angle", None) or 0.0
                        en = getattr(edge, "end_angle", None) or 0.0
                        if c is not None and r is not None:
                            st_n = float(st) % 360.0
                            en_n = float(en) % 360.0
                            if en_n <= st_n:
                                en_n += 360.0
                            steps = 8
                            for i in range(steps + 1):
                                tt = st_n + (en_n - st_n) * (i / steps)
                                rad = math.radians(tt)
                                pts.append({
                                    "x": float(c[0]) + float(r) * math.cos(rad),
                                    "y": float(c[1]) + float(r) * math.sin(rad),
                                })
                    else:
                        for p in getattr(edge, "points", []) or []:
                            xy = _xy(p)
                            if xy is not None:
                                pts.append(xy)
                if pts:
                    if pts[0] != pts[-1]:
                        pts.append(pts[0])
                    loops.append(pts)
        except Exception:
            loops = []
        if not loops:
            return None
        return {**base, "loops": loops}

    # INSERT is consumed by the walker via virtual_entities() — no record.
    return None


def _walk(entities: Iterable[Any], depth: int, inherited: Optional[str],
          out: list[dict[str, Any]]) -> None:
    if depth > _MAX_INSERT_DEPTH:
        return
    for e in entities:
        try:
            t = e.dxftype()
        except Exception:
            continue
        layer = _effective_layer(e, inherited)
        if t == "INSERT":
            try:
                _walk(e.virtual_entities(), depth + 1, layer, out)
            except Exception:
                continue
            continue
        rec = _entity_to_record(e, layer)
        if rec is not None:
            out.append(rec)


def _summarise_layers(doc, entities: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    try:
        for lyr in doc.layers:
            name = str(lyr.dxf.name)
            try:
                color = int(getattr(lyr.dxf, "color", 0))
            except Exception:
                color = None
            summary[name] = {"color": color, "entity_count": 0}
    except Exception:
        pass
    for rec in entities:
        ln = rec.get("layer") or ""
        if ln not in summary:
            summary[ln] = {"color": None, "entity_count": 0}
        summary[ln]["entity_count"] += 1
    return summary


def _read_header(doc) -> dict[str, Any]:
    """Subset of header variables downstream cares about."""
    header: dict[str, Any] = {}
    for key in ("$INSUNITS", "$EXTMIN", "$EXTMAX", "$LIMMIN", "$LIMMAX",
                "$LUNITS", "$MEASUREMENT"):
        try:
            val = doc.header.get(key)
        except Exception:
            val = None
        if val is None:
            continue
        if hasattr(val, "__iter__") and not isinstance(val, (str, bytes)):
            try:
                header[key] = [float(v) for v in val]
            except Exception:
                header[key] = list(val)
        else:
            try:
                header[key] = float(val) if isinstance(val, (int, float)) else val
            except Exception:
                header[key] = val
    return header


def _normalize_doc(doc, source_name: str, auditor_warnings: list[str]) -> dict[str, Any]:
    entities: list[dict[str, Any]] = []
    _walk(doc.modelspace(), 0, None, entities)
    return {
        "source_file": source_name,
        "header": _read_header(doc),
        "layers": _summarise_layers(doc, entities),
        "entities": entities,
        "auditor_warnings": auditor_warnings,
    }


def normalize_bytes(file_bytes: bytes, source_name: str = "upload.dxf") -> dict[str, Any]:
    """Normalize a DXF (or DWG) buffer to the pipeline JSON shape.

    DWG inputs are transcoded to DXF first via the same converter chain
    the legacy importer used (ODA File Converter or LibreDWG `dwgread`).
    Raises FileNotFoundError when a DWG is supplied but no converter is
    on PATH — the route surfaces that as a 415.
    """
    ext = Path(source_name or "").suffix.lower()
    from_dwg = False
    if ext == ".dwg":
        dxf_bytes = _convert_dwg_to_dxf(file_bytes)
        from_dwg = True
    else:
        dxf_bytes = file_bytes

    sanitize_warnings: list[str] = []
    try:
        doc, dxf_warnings = _read_dxf_robust(dxf_bytes)
    except ValueError as exc:
        # LibreDWG occasionally emits a DXF with out-of-spec group codes
        # in non-essential blocks. One sanitisation pass salvages it.
        if from_dwg and "Invalid group code" in str(exc):
            sanitized, sanitize_warnings = _sanitize_libredwg_dxf(dxf_bytes)
            doc, dxf_warnings = _read_dxf_robust(sanitized)
        else:
            raise

    if sanitize_warnings:
        dxf_warnings = [*sanitize_warnings, *dxf_warnings]
    return _normalize_doc(doc, Path(source_name).name or "upload.dxf", dxf_warnings)


def normalize_path(dxf_path: Path) -> dict[str, Any]:
    """Path-based entry point for the CLI scripts."""
    try:
        doc, auditor = ezdxf_recover.readfile(str(dxf_path))
    except ezdxf.DXFStructureError as exc:
        raise ValueError(f"DXF structure too damaged to read: {exc}") from exc
    auditor_warnings: list[str] = []
    try:
        for err in (getattr(auditor, "errors", []) or [])[:50]:
            auditor_warnings.append(str(err))
    except Exception:
        pass
    return _normalize_doc(doc, dxf_path.name, auditor_warnings)
