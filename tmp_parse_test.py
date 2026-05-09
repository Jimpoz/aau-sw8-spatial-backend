from backend.services.dxf_import_service import DxfImportService, _collect_segments_from_entities, _snap_and_dedupe_segments, _polygonize_from_doc, _read_dxf_robust
from pathlib import Path
import json
p = Path('..') / 'ACM-stue.dxf'
b = p.read_bytes()
svc = DxfImportService()
try:
    s = svc.parse(b, 'ACM-stue.dxf', campus_id='campus1', campus_name='C', building_id='b1', building_name='B', floor_id='f1', floor_index=0, floor_display_name='Ground')
    spaces = s['campus']['buildings'][0]['floors'][0]['spaces']
    print('rooms_detected:', len(spaces))
    types = [sp.get('space_type') for sp in spaces]
    print('types sample:', types[:10])
    print('first space polygon length:', len(spaces[0]['polygon']) if spaces else 'none')
    print('warnings:', s.get('_warnings', [])[:10])
    print('_diagnostics:', json.dumps(s.get('_diagnostics', {}), indent=2) )
    diag = s.get('_diagnostics', {})
    print('diag raw_room_count:', diag.get('raw_room_count'))
    print('diag cleaned_room_count:', diag.get('cleaned_room_count'))
    doc, dxf_warnings = _read_dxf_robust(b)
    raw_segs = _collect_segments_from_entities(doc.modelspace())
    print('raw segment count:', len(raw_segs))
    sample = raw_segs[:5]
    print('sample segments endpoints:', [[list(s.coords)[0], list(s.coords)[-1]] for s in sample])
    xs = [c for s in raw_segs for c in (s.coords[0][0], s.coords[-1][0])] if raw_segs else []
    ys = [c for s in raw_segs for c in (s.coords[0][1], s.coords[-1][1])] if raw_segs else []
    if xs and ys:
        dx = max(xs) - min(xs)
        dy = max(ys) - min(ys)
        diag = (dx ** 2 + dy ** 2) ** 0.5
        snap_tol = max(1e-3, diag * 1e-6)
    else:
        snap_tol = 1e-3
    snapped = _snap_and_dedupe_segments(raw_segs, snap_tol)
    print('snapped/deduped segment count:', len(snapped))
    poly_preview = _polygonize_from_doc(doc)
    print('polygonize preview count:', len(poly_preview))
except Exception as e:
    print('ERROR', e)
