from backend.services.dxf_import_service import DxfImportService
from pathlib import Path
import json
p = Path('..') / 'tmp_test.dxf'
b = p.read_bytes()
svc = DxfImportService()
try:
    s = svc.parse(b, 'tmp_test.dxf', campus_id='campus1', campus_name='C', building_id='b1', building_name='B', floor_id='f1', floor_index=0, floor_display_name='Ground')
    spaces = s['campus']['buildings'][0]['floors'][0]['spaces']
    print('rooms_detected:', len(spaces))
    types = [sp.get('space_type') for sp in spaces]
    print('types sample:', types[:10])
    print('first space polygon length:', len(spaces[0]['polygon']) if spaces else 'none')
    print('warnings:', s.get('_warnings', [])[:10])
except Exception as e:
    print('ERROR', e)
