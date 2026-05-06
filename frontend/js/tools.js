/* ═══════════════════════════════════════════════
   Edit Tool Modes
   ═══════════════════════════════════════════════ */
function setMode(mode) {
  state.mode = mode;
  state.connectFrom = null;

  if (mode !== 'addSpace') resetPendingPolygon();

  for (const btn of document.querySelectorAll('.tool-bar button')) {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  }

  $('add-space-form').style.display = mode === 'addSpace' ? 'block' : 'none';
  $('connect-form').style.display = mode === 'connect' ? 'block' : 'none';

  canvas.classList.toggle('crosshair', mode === 'addSpace');

  if (mode === 'addSpace' && state.selectedFloorId && !state.bounds) {
    fitToCanvas();
  }

  if (mode === 'connect') {
    $('connect-status').textContent = 'Click the first space (start), then click the second space (end).';
    $('connect-status').className = 'tool-hint';
  }

  render();
}

$('tool-select').addEventListener('click', () => setMode('select'));
$('tool-add-space').addEventListener('click', () => setMode('addSpace'));
$('tool-connect').addEventListener('click', () => setMode('connect'));

/* ═══════════════════════════════════════════════
   Shape Selector (Rectangle / Circle / Polygon)
   ═══════════════════════════════════════════════ */
function currentShape() { return $('new-space-shape').value; }

function resetPendingPolygon() {
  state.pendingPolygon = [];
  state.pendingCursorWorld = null;
}

function updateShapeFields() {
  const shape = currentShape();
  $('shape-fields-rectangle').style.display = shape === 'rectangle' ? 'block' : 'none';
  $('shape-fields-circle').style.display    = shape === 'circle'    ? 'block' : 'none';
  $('shape-fields-polygon').style.display   = shape === 'polygon'   ? 'block' : 'none';

  const hint = $('add-space-hint');
  if (shape === 'rectangle') {
    hint.textContent = 'Click on the map to place the space at that location.';
  } else if (shape === 'circle') {
    hint.textContent = 'Click on the map to place a circular space centered there.';
  } else {
    hint.innerHTML = 'Click to add a vertex. Double-click (or press <strong>Enter</strong> / Finish) to close the polygon. <strong>Esc</strong> cancels.';
  }

  resetPendingPolygon();
  render();
}

$('new-space-shape').addEventListener('change', updateShapeFields);

$('finish-polygon-btn').addEventListener('click', (e) => {
  e.preventDefault();
  finishPendingPolygon();
});

async function finishPendingPolygon() {
  if (state.pendingPolygon.length < 3) {
    notify('Polygon needs at least 3 vertices', 'error');
    return;
  }
  const poly = closedPolygon(state.pendingPolygon);
  const [cx, cy] = polygonCentroid(poly);
  resetPendingPolygon();
  await createSpaceFromPolygon(poly, cx, cy);
}

function cancelPendingPolygon() {
  if (state.pendingPolygon.length === 0) return;
  resetPendingPolygon();
  render();
}

/* ═══════════════════════════════════════════════
   Add Space on Click
   ═══════════════════════════════════════════════ */
async function handleAddSpaceClick(canvasX, canvasY) {
  if (!state.selectedFloorId) { notify('Select a floor first', 'error'); return; }

  const name = $('new-space-name').value.trim();
  if (!name) { notify('Enter a space name first', 'error'); $('new-space-name').focus(); return; }

  const [rawWx, rawWy] = canvasToWorld(canvasX, canvasY);
  const shape = currentShape();

  if (shape === 'rectangle') {
    const w = parseFloat($('new-space-w').value) || 4;
    const h = parseFloat($('new-space-h').value) || 3;
    const halfW = w / 2, halfH = h / 2;
    const [wx, wy] = applySnap(rawWx, rawWy, halfW, halfH, null);
    state.snapLines = [];
    const polygon = [
      [wx - halfW, wy - halfH],
      [wx + halfW, wy - halfH],
      [wx + halfW, wy + halfH],
      [wx - halfW, wy + halfH],
      [wx - halfW, wy - halfH],
    ];
    await createSpaceFromPolygon(polygon, wx, wy);
    return;
  }

  if (shape === 'circle') {
    const r = parseFloat($('new-space-radius').value) || 2;
    const [wx, wy] = [rawWx, rawWy];
    state.snapLines = [];
    const polygon = approximateCircle(wx, wy, r, 32);
    await createSpaceFromPolygon(polygon, wx, wy);
    return;
  }

  if (shape === 'polygon') {
    let vx = rawWx, vy = rawWy;
    if (state.pendingPolygon.length > 0) {
      const last = state.pendingPolygon[state.pendingPolygon.length - 1];
      [vx, vy] = snapToAxis(last[0], last[1], rawWx, rawWy);
    }
    state.pendingPolygon.push([vx, vy]);
    render();
  }
}

async function createSpaceFromPolygon(polygon, centroidX, centroidY) {
  const name = $('new-space-name').value.trim();
  if (!name) { notify('Enter a space name first', 'error'); return; }

  let parentSpaceId = null;
  let bestZIndex = -1;
  for (const s of state.spaces) {
    if (!s.polygon || s.polygon.length < 3) continue;
    if (pointInPolygon(centroidX, centroidY, s.polygon) && (s.z_index || 0) > bestZIndex) {
      parentSpaceId = s.id;
      bestZIndex = s.z_index || 0;
    }
  }

  const spaceData = {
    id: uuid(),
    display_name: name,
    space_type: $('new-space-type').value,
    floor_id: state.selectedFloorId,
    parent_space_id: parentSpaceId,
    building_id: state.selectedBuildingId,
    campus_id: state.selectedCampusId,
    floor_index: state.currentFloorIndex,
    centroid_x: centroidX,
    centroid_y: centroidY,
    polygon,
    is_accessible: true,
    is_navigable: true,
    is_outdoor: false,
    tags: [],
  };

  try {
    await api('/spaces', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(spaceData),
    });
    notify(`Space "${name}" added`);
    await reloadCurrentFloor();
    $('new-space-name').value = '';
    $('new-space-name').focus();
  } catch (e) { /* handled */ }
}

/* ═══════════════════════════════════════════════
   Connect Spaces on Click
   ═══════════════════════════════════════════════ */
async function handleConnectClick(canvasX, canvasY) {
  const space = hitTest(canvasX, canvasY);
  if (!space) { notify('Click on a space to select it', 'error'); return; }

  if (!state.connectFrom) {
    state.connectFrom = space;
    $('connect-status').innerHTML = `From: <span class="connect-from-badge">${space.display_name}</span><br>Now click the second space.`;
    render();
    return;
  }

  if (space.id === state.connectFrom.id) {
    notify('Cannot connect a space to itself', 'error');
    return;
  }

  const spaceType = $('new-conn-type').value;
  const accessible = $('new-conn-accessible').checked;
  const from = state.connectFrom;

  const connData = {
    from_space_id: from.id,
    to_space_id: space.id,
    space_type: spaceType,
    display_name: spaceType.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
    is_accessible: accessible,
  };

  try {
    await api('/connections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(connData),
    });

    notify(`Connected "${from.display_name}" to "${space.display_name}"`);
    state.connectFrom = null;
    $('connect-status').textContent = 'Click the first space (start), then click the second space (end).';
    $('connect-status').className = 'tool-hint';

    // Refresh floor display (door nodes appear as spaces now)
    await reloadCurrentFloor();

    // Refresh GDS projection
    api('/navigate/refresh-graph', { method: 'POST' }).catch(() => {});
  } catch (e) { /* handled */ }
}
