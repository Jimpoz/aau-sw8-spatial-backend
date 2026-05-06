/* ═══════════════════════════════════════════════
   Configuration & State
   ═══════════════════════════════════════════════ */
const API = '/api/v1';

/* ═══════════════════════════════════════════════
   Auth (Slice 3)
   ───────────────────────────────────────────────
   - Token is held in localStorage so authoring sessions survive page reloads.
   - On boot we probe `/auth/me`; if it succeeds the user is signed in, if it
     401s the token is bad and we show login, if it 404s the backend is in
     shadow mode (auth router not mounted) and we let the page run as before.
   - `authedFetch` attaches `Authorization: Bearer <jwt>` when a token is set.
   - 401/403 from any other call clears the token and re-shows the login modal.
   ═══════════════════════════════════════════════ */
const AUTH_TOKEN_KEY = 'ariadne.mapmaker.jwt';
const auth = {
  token: null,
  user: null,             // { id, email, full_name }
  organizationId: null,
  role: null,
  enforcementOn: false,   // toggled true when /auth/me returns 200 OR 401
};

const state = {
  organizations: [],
  campuses: [],
  buildings: [],
  floors: [],
  spaces: [],
  connections: [],
  selectedOrganizationId: null,
  selectedCampusId: null,
  selectedBuildingId: null,
  selectedFloorId: null,
  currentFloorIndex: null,
  currentBuildingId: null,
  hoveredSpace: null,
  // Edit mode
  mode: 'select',      // 'select' | 'addSpace' | 'connect' | 'moveSpace' | 'placeDuplicate'
  connectFrom: null,
  // Move mode
  moveSpace: null,
  moveOrigPoly: null,
  moveOrigCentroid: null,
  // Duplicate placement mode
  pendingDuplicate: null,
  pendingDupOrigPoly: null,
  // Polygon-drawing mode (within addSpace when shape=polygon)
  pendingPolygon: [],       // accumulated vertices in world coords [[x,y], ...]
  pendingCursorWorld: null, // live cursor world coords for preview segment
  // Context menu
  contextMenu: null,
  // Snap indicators
  snapLines: [],
  // Canvas transform
  pan: { x: 0, y: 0 },
  zoom: 1,
  baseScale: 1,
  bounds: null,
  isDragging: false,
  dragStart: { x: 0, y: 0 },
  panStart: { x: 0, y: 0 },
  // Info modal editing
  _editingSpace: null,
};

/* ═══════════════════════════════════════════════
   Space Type Colors
   ═══════════════════════════════════════════════ */
const COLORS = {
  ROOM_GENERIC:'#B3D9FF', ROOM_OFFICE:'#A8CCF0', ROOM_CLASSROOM:'#B3D9FF',
  ROOM_LECTURE_HALL:'#9EC5E8', ROOM_LAB:'#8BB8E0', ROOM_MEETING:'#B3D9FF',
  ROOM_STORAGE:'#C8DFF5', ROOM_UTILITY:'#C8DFF5',
  CORRIDOR:'#E0E0E0', CORRIDOR_SEGMENT:'#E0E0E0',
  LOBBY:'#D8D8D8', WAITING_AREA:'#D8D8D8', RECEPTION:'#D8D8D8',
  ENTRANCE:'#90EE90', ENTRANCE_SECONDARY:'#A8F0A8', EXIT_EMERGENCY:'#FFB3B3',
  STAIRCASE:'#FFD699',
  ELEVATOR:'#FFCC80',
  ESCALATOR:'#FFD699', RAMP:'#FFD699',
  BRIDGE:'#FFD699', TUNNEL:'#D0D0D0', COVERED_WALKWAY:'#D8E8D8',
  OUTDOOR_PATH:'#C8F0C8', OUTDOOR_PLAZA:'#C8F0C8',
  OUTDOOR_COURTYARD:'#C8F0C8', OUTDOOR_STAIRS:'#C8F0C8', PARKING:'#D0E8D0',
  RESTROOM:'#FFB3D9', RESTROOM_ACCESSIBLE:'#FFB3D9',
  CAFETERIA:'#FFEB99', CAFE:'#FFEB99', LIBRARY:'#FFEB99',
  GYM:'#FFEB99', AUDITORIUM:'#FFEB99', SHOP:'#FFEB99',
  INACCESSIBLE:'#808080', UNKNOWN:'#F0F0F0',
  DOOR_STANDARD:'#D4A574', DOOR_AUTOMATIC:'#C9976A',
  DOOR_LOCKED:'#BF8A60', DOOR_EMERGENCY:'#FF9999',
  PASSAGE:'#E8D5C0',
  OPEN:'#E0E0E0',
};

// Fetched from backend at startup via /api/v1/enums/space-types
let SPACE_TYPES = [];
let CONN_SPACE_TYPES = [];

/* ═══════════════════════════════════════════════
   DOM References
   ═══════════════════════════════════════════════ */
const $ = id => document.getElementById(id);
const canvas = $('map-canvas');
const ctx = canvas.getContext('2d');
const tooltip = $('tooltip');
const emptyState = $('empty-state');
const legend = $('legend');
