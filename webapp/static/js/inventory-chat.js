// 보물찾기(app.js)와 관리자 콘솔(admin.js)이 쓰는 것과 같은 localStorage 키를 그대로 쓴다.
// 한 번 로그인하면 다른 화면도 다시 로그인하지 않아도 되게 하기 위해서다 (2026-09-23).
const REP_TOKEN_KEY = "rs_rep_token"; // app.js 와 공유
const ADMIN_TOKEN_KEY = "rs_admin_token"; // admin.js 와 공유
let activeTokenKind = ""; // "admin" | "rep" | "" — 지금 세션이 어느 쪽 키를 쓰는지

let chatMap = null;
let chatMarkers = []; // maplibregl.Marker[]
let chatMeMarker = null;
let lastChatMapData = null;
let lastChatOrigin = null;
let storeLabelsOn = null;
let pendingQuestion = "";
let lastStoreCode = ""; // "그 판매점", "거기" 같은 말이 가리킬 직전 응답의 대표 매장
let areaShape = null; // null | "rect" | "circle" — 지금 켜져 있거나 마지막으로 그린 모양
let areaDrawing = false;
let areaStart = null;
let areaLast = null;
let areaBounds = null;
let areaCircle = null;
let areaBoundOnce = false;
let inventoryUser = {
  username: "",
  role: "super",
  dealer_id: "",
  dealer_name: "",
  can_see_all: true,
};
let lastCoords = null;
let hqDealerId = "";
let modelCatalog = [];
let mapProductShorts = [];
let mapModelNames = [];
let pickedProductShorts = [];
let pickedModelNames = [];
let mapPinColor = "";
let mapColorRules = []; // [{min, max, color}] — 사용자가 직접 정한 보유기간 구간별 색
let mapAgedOnly = false;
let catalogPicked = false;
let mapIncludeRetail = false;
let mapIncludePartner = true;
let lastHqDealers = [];

const MAP_STYLE_URL = "https://tiles.openfreemap.org/styles/liberty";
// MapLibre는 [lng, lat] 순서를 쓴다 (Leaflet의 [lat, lng]와 반대).
const FOCUS_CENTER = [127.7, 37.55];
const FOCUS_ZOOM = 10;
const FOCUS_BOUNDS = [
  [126.35, 36.85],
  [129.25, 38.45],
];
const MAP_MAX_BOUNDS = [
  [126.05, 36.6],
  [129.55, 38.7],
];

function $(id) {
  return document.getElementById(id);
}

function appUrl(path) {
  const base = (window.APP_BASE || "").replace(/\/$/, "");
  if (!path.startsWith("/")) path = `/${path}`;
  return `${base}${path}`;
}

function getStoredTokens() {
  return {
    admin: localStorage.getItem(ADMIN_TOKEN_KEY) || "",
    rep: localStorage.getItem(REP_TOKEN_KEY) || "",
  };
}

function getToken() {
  // 세션 종류가 정해져 있으면(로그인·복원 성공 후) 그 키만 쓴다.
  const { admin, rep } = getStoredTokens();
  if (activeTokenKind === "rep") return rep;
  if (activeTokenKind === "admin") return admin;
  return admin || rep;
}

function setToken(token, kind) {
  if (!token) {
    // 재고 화면에서 로그아웃하면 같은 세션인 보물찾기/관리자 콘솔도 함께 로그아웃된다.
    localStorage.removeItem(ADMIN_TOKEN_KEY);
    localStorage.removeItem(REP_TOKEN_KEY);
    activeTokenKind = "";
    return;
  }
  if (kind === "rep") {
    localStorage.setItem(REP_TOKEN_KEY, token);
  } else {
    localStorage.setItem(ADMIN_TOKEN_KEY, token);
  }
  activeTokenKind = kind;
}

function authHeaders(extra) {
  const headers = { ...(extra || {}) };
  const token = getToken();
  if (token) headers["X-Admin-Token"] = token;
  return headers;
}

function escHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function friendlyError(err) {
  const msg = String((err && err.message) || err || "").trim();
  if (!msg) return "요청에 실패했습니다.";
  if (/failed to fetch|networkerror|load failed|network request failed/i.test(msg)) {
    return "서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.";
  }
  if (/<!DOCTYPE/i.test(msg) || /^\s*</.test(msg) || /bad gateway|502|503|504/i.test(msg)) {
    return "서버가 잠시 응답하지 않습니다. 잠시 후 다시 시도해 주세요.";
  }
  if (msg.length > 180) return `${msg.slice(0, 180)}…`;
  return msg;
}

async function api(path, options = {}) {
  const opts = { ...options };
  const headers = authHeaders(opts.headers || {});
  if (!(opts.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(appUrl(`/api${path}`), { ...opts, headers });
  const raw = await res.text();
  let data = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch (_) {
      data = null;
    }
  }
  if (res.status === 401) {
    // 이 화면에서 쓰던 토큰만 지운다. 다른 화면(보물찾기/관리자)의 로그인은 건드리지 않는다.
    if (activeTokenKind === "rep") localStorage.removeItem(REP_TOKEN_KEY);
    else if (activeTokenKind === "admin") localStorage.removeItem(ADMIN_TOKEN_KEY);
    activeTokenKind = "";
    showLoggedOut();
    throw new Error((data && data.message) || "대리점 로그인이 필요합니다.");
  }
  if (res.status === 403 && data && data.error === "NO_DEALER") {
    // 로그인 자체는 유효하다(보물찾기는 계속 쓸 수 있다). 재고 화면만 못 쓰는 것이라 토큰은 지우지 않는다.
    const err = new Error(data.message);
    err.code = "NO_DEALER";
    throw err;
  }
  if (res.status === 403 && data && data.error === "PASSWORD_CHANGE_REQUIRED") {
    // 초기 비밀번호를 바꾸기 전까지는 변경 화면만 쓸 수 있다.
    showPasswordChange({ ...inventoryUser, must_change_password: true });
    throw new Error(data.message);
  }
  if (!res.ok) {
    const fromJson = data && (data.message || data.error);
    const looksHtml = raw && /^\s*</.test(raw);
    const gateway = res.status === 502 || res.status === 503 || res.status === 504;
    throw new Error(
      fromJson ||
        (looksHtml || gateway
          ? "서버가 잠시 응답하지 않습니다. 잠시 후 다시 시도해 주세요."
          : raw && raw.length > 180
            ? `요청에 실패했습니다. (${res.status})`
            : raw || `요청에 실패했습니다. (${res.status})`)
    );
  }
  return data;
}

function showLoggedOut() {
  $("screen-chat-login").classList.remove("hidden");
  const passwordScreen = $("screen-chat-password");
  if (passwordScreen) passwordScreen.classList.add("hidden");
  $("chat-app").classList.add("hidden");
  $("chatNav").classList.add("hidden");
  const uploadBar = $("inventoryUploadBar");
  if (uploadBar) uploadBar.classList.add("hidden");
  const filterBar = $("inventoryFilterBar");
  if (filterBar) filterBar.classList.add("hidden");
  const asOf = $("asOfBadge");
  if (asOf) {
    asOf.textContent = "";
    asOf.classList.add("hidden");
  }
}

// 초기 비밀번호를 쓰는 동안에는 서버가 재고 API를 막는다. 변경 화면만 보여준다.
function showPasswordChange(user) {
  inventoryUser = user || inventoryUser;
  $("screen-chat-login").classList.add("hidden");
  $("screen-chat-password").classList.remove("hidden");
  $("chat-app").classList.add("hidden");
  $("chatNav").classList.add("hidden");
  const uploadBar = $("inventoryUploadBar");
  if (uploadBar) uploadBar.classList.add("hidden");
  const filterBar = $("inventoryFilterBar");
  if (filterBar) filterBar.classList.add("hidden");
}

async function handleChangePassword() {
  const msg = $("chatPasswordMessage");
  const current = $("chatCurrentPassword").value;
  const next = $("chatNewPassword").value;
  const confirm = $("chatNewPasswordConfirm").value;
  msg.classList.remove("error");
  if (!current || !next) {
    msg.textContent = "현재/새 비밀번호를 입력해주세요.";
    return;
  }
  if (next.length < 4) {
    msg.textContent = "새 비밀번호는 4자 이상이어야 합니다.";
    return;
  }
  if (next !== confirm) {
    msg.textContent = "새 비밀번호 확인이 일치하지 않습니다.";
    return;
  }
  // 대리점 직원은 사원 계정, SKT는 관리자 계정이라 바꾸는 API가 다르다.
  const path = inventoryUser.role === "dealer" ? "/auth/change-password" : "/admin/change-password";
  try {
    await api(path, {
      method: "POST",
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
    ["chatCurrentPassword", "chatNewPassword", "chatNewPasswordConfirm"].forEach((id) => {
      $(id).value = "";
    });
    $("screen-chat-password").classList.add("hidden");
    const me = await api("/inventory/me");
    await showLoggedIn(me);
    greet();
  } catch (err) {
    msg.textContent = friendlyError(err);
    msg.classList.add("error");
  }
}

function showLoggedIn(user) {
  inventoryUser = user || inventoryUser;
  $("screen-chat-password").classList.add("hidden");
  $("screen-chat-login").classList.add("hidden");
  $("chat-app").classList.remove("hidden");
  $("chatNav").classList.remove("hidden");
  const uploadBar = $("inventoryUploadBar");
  // SKT 직원은 조회 전용이라 업로드를 숨긴다. (예전 응답에는 can_upload 가 없어 기본 허용)
  if (uploadBar) uploadBar.classList.toggle("hidden", inventoryUser.can_upload === false);
  const registerBtn = $("registerStoreBtn");
  // 직영점 등록도 재고 업로드와 같은 권한(대리점 직원 누구나 + SKT는 총괄만)이다.
  if (registerBtn) registerBtn.classList.toggle("hidden", inventoryUser.can_upload === false);
  const filterBar = $("inventoryFilterBar");
  if (filterBar) filterBar.classList.remove("hidden");
  $("chatUser").textContent = inventoryUserLabel(inventoryUser);
  const adminLink = $("chatAdminLink");
  if (adminLink) adminLink.classList.toggle("hidden", !inventoryUser.can_see_all);
  const hqPanel = $("hqPanel");
  if (hqPanel) hqPanel.classList.toggle("hidden", !inventoryUser.can_see_all);
  const sktWrap = $("sktDealerWrap");
  if (sktWrap) sktWrap.classList.toggle("hidden", !inventoryUser.can_see_all);
  $("chat-app").classList.toggle("is-hq", !!inventoryUser.can_see_all);
  document.querySelectorAll("[data-all-dealers]").forEach((el) => {
    el.classList.toggle("hidden", !inventoryUser.can_see_all);
  });
  if (inventoryUser.can_see_all) loadHqSummary();
  const ready = loadCatalog();
  ensureChatMap();
  return ready;
}

function inventoryUserLabel(user) {
  if (!user) return "";
  if (user.role === "dealer") {
    const roleLabel = user.dealer_role === "manager" ? "관리자" : "직원";
    return `${user.dealer_name || ""} · ${user.name || user.username} (${roleLabel})`;
  }
  if (user.role === "staff") return `SKT 직원 · ${user.username} (조회 전용)`;
  return `SKT 총괄 · ${user.username}`;
}

function stockPinColor(point) {
  const days = point && point.max_hold_days;
  if (mapColorRules && mapColorRules.length) {
    for (const rule of mapColorRules) {
      const lo = rule.min == null ? -Infinity : rule.min;
      const hi = rule.max == null ? Infinity : rule.max;
      if (days != null && days >= lo && days <= hi) return rule.color;
    }
    // 구간 중 어디에도 안 걸리면(예: 정의 안 한 기간) 기본색으로 표시한다.
  }
  if (mapPinColor) return mapPinColor;
  if (days != null && days >= 30) return "#dc2626";
  if (days != null && days >= 15) return "#d97706";
  return "#2563eb";
}

function isTransientServerError(err) {
  const msg = friendlyError(err);
  return /응답하지 않습니다|연결하지 못했습니다|파일이 크거나 서버가 바쁩/.test(msg);
}

function addBotError(err) {
  if (isTransientServerError(err)) return;
  addBot(friendlyError(err));
}

function looksLikeAddress(text, address) {
  const value = String(text || "").trim();
  const addr = String(address || "").trim();
  if (!value) return false;
  if (addr && value.replace(/\s+/g, "") === addr.replace(/\s+/g, "")) return true;
  return /^(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)/.test(
    value.replace(/\s+/g, "")
  ) && /(로|길|동|대로|번길)\s*\d/.test(value);
}

function storeNameOf(point) {
  const name = String((point && point.name) || "").trim();
  const holder = String((point && point.holder_name) || "").trim();
  const addr = String((point && (point.address || point.detail_address)) || "").trim();
  if (name && !looksLikeAddress(name, addr)) return name;
  if (holder && !looksLikeAddress(holder, addr)) return holder;
  return "";
}

function storeCaptionHtml(point) {
  const stores = (point && point.stores) || [point];
  const first = stores[0] || {};
  const code = escHtml((first.store_code || point.store_code || "").trim());
  const name = escHtml(storeNameOf(first) || storeNameOf(point));
  const extra = stores.length > 1 ? ` 외 ${stores.length - 1}곳` : "";
  return `<span class="stock-code">${code}</span>${name ? ` ${name}` : ""}${extra}`;
}

function storeCaptionText(point) {
  const stores = (point && point.stores) || [point];
  const first = stores[0] || {};
  const code = (first.store_code || point.store_code || "").trim();
  const name = storeNameOf(first) || storeNameOf(point);
  const label = [code, name].filter(Boolean).join(" ");
  if (stores.length > 1) return `${label} 외 ${stores.length - 1}곳`;
  return label;
}

function clusterMapPoints(points) {
  const groups = new Map();
  for (const p of points || []) {
    const addr = String(p.address || "").replace(/\s+/g, "");
    const key = addr || `${Number(p.lat).toFixed(5)},${Number(p.lng).toFixed(5)}`;
    let g = groups.get(key);
    if (!g) {
      g = {
        lat: p.lat,
        lng: p.lng,
        address: p.address || "",
        detail_address: p.detail_address || "",
        region: p.region,
        qty: 0,
        aged_qty: 0,
        max_hold_days: null,
        shared: false,
        stores: [],
        dealers: [],
        models: [],
        distance_meters: p.distance_meters,
      };
      groups.set(key, g);
    }
    g.stores.push(p);
    g.qty += p.qty || 0;
    g.aged_qty += p.aged_qty || 0;
    if (p.max_hold_days != null) {
      g.max_hold_days = g.max_hold_days == null ? p.max_hold_days : Math.max(g.max_hold_days, p.max_hold_days);
    }
    if (p.shared) g.shared = true;
    if (p.distance_meters != null) {
      g.distance_meters = g.distance_meters == null
        ? p.distance_meters
        : Math.min(g.distance_meters, p.distance_meters);
    }
  }
  const out = [];
  for (const g of groups.values()) {
    g.stores.sort((a, b) => (b.qty || 0) - (a.qty || 0) || String(a.store_code).localeCompare(String(b.store_code)));
    g.store_code = g.stores[0].store_code;
    g.name = g.stores[0].name;
    g.holder_name = g.stores[0].holder_name;
    const dealerMap = new Map();
    const modelMap = new Map();
    for (const s of g.stores) {
      for (const d of s.dealers || []) {
        const key = d.dealer_id || d.dealer_code || d.dealer_name;
        const prev = dealerMap.get(key) || { ...d, qty: 0, aged_qty: 0 };
        prev.qty += d.qty || 0;
        prev.aged_qty += d.aged_qty || 0;
        dealerMap.set(key, prev);
      }
      for (const m of s.models || []) {
        modelMap.set(m.model, (modelMap.get(m.model) || 0) + (m.qty || 0));
      }
    }
    g.dealers = [...dealerMap.values()].sort((a, b) => (b.qty || 0) - (a.qty || 0));
    g.models = [...modelMap.entries()]
      .map(([model, qty]) => ({ model, qty }))
      .sort((a, b) => b.qty - a.qty);
    if (g.dealers.length > 1) g.shared = true;
    out.push(g);
  }
  return out;
}

function haversineDistanceMeters(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const toRad = (deg) => (deg * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

// MapLibre는 Leaflet의 L.circle 같은 지오메트리 원이 없어서 다각형으로 근사한다.
function circlePolygonCoords(lat, lng, radiusMeters, steps = 64) {
  const R = 6371000;
  const latRad = (lat * Math.PI) / 180;
  const coords = [];
  for (let i = 0; i <= steps; i++) {
    const angle = (i / steps) * 2 * Math.PI;
    const dx = (radiusMeters * Math.cos(angle)) / (R * Math.cos(latRad));
    const dy = (radiusMeters * Math.sin(angle)) / R;
    coords.push([lng + (dx * 180) / Math.PI, lat + (dy * 180) / Math.PI]);
  }
  return coords;
}

function circleFeature(lat, lng, radiusMeters) {
  return {
    type: "Feature",
    properties: {},
    geometry: { type: "Polygon", coordinates: [circlePolygonCoords(lat, lng, radiusMeters)] },
  };
}

function rectFeature(startLat, startLng, endLat, endLng) {
  const south = Math.min(startLat, endLat);
  const north = Math.max(startLat, endLat);
  const west = Math.min(startLng, endLng);
  const east = Math.max(startLng, endLng);
  return {
    type: "Feature",
    properties: {},
    geometry: {
      type: "Polygon",
      coordinates: [
        [
          [west, south],
          [east, south],
          [east, north],
          [west, north],
          [west, south],
        ],
      ],
    },
  };
}

// points: [lat, lng][] → MapLibre fitBounds가 쓰는 [[west,south],[east,north]]
function boundsFromPoints(points) {
  let west = Infinity;
  let south = Infinity;
  let east = -Infinity;
  let north = -Infinity;
  for (const [lat, lng] of points) {
    west = Math.min(west, lng);
    east = Math.max(east, lng);
    south = Math.min(south, lat);
    north = Math.max(north, lat);
  }
  return [
    [west, south],
    [east, north],
  ];
}

function stockMarkerElement(point, nearest = false, showLabel = true) {
  const qty = typeof point === "number" ? point : (point && point.qty) || 0;
  const shared = point && point.shared;
  const cls = `stock-pin${nearest ? " nearest" : ""}${shared ? " shared" : ""}`;
  const caption = showLabel && typeof point === "object" && point ? storeCaptionHtml(point) : "";
  const el = document.createElement("div");
  el.className = "stock-marker";
  el.innerHTML = `<div class="stock-marker-inner"><div class="${cls}" style="background:${stockPinColor(point)}">${qty}</div>${caption ? `<div class="stock-caption">${caption}</div>` : ""}</div>`;
  return el;
}

function formatKm(meters) {
  if (meters == null) return "";
  if (meters < 1000) return `${Math.round(meters)}m`;
  return `${(meters / 1000).toFixed(1)}km`;
}

function fitLandscapeFocus(map) {
  if (!map) return false;
  map.resize();
  const el = map.getContainer();
  const size = { x: el.clientWidth, y: el.clientHeight };
  if (!size.x || !size.y || size.x < 80 || size.y < 80) return false;
  map.fitBounds(FOCUS_BOUNDS, { padding: 20, maxZoom: 11, animate: false });
  if (map.getZoom() < 9.5) {
    map.jumpTo({ center: FOCUS_CENTER, zoom: FOCUS_ZOOM });
  }
  return true;
}

function scheduleLandscapeFocus(map, attempt = 0) {
  if (!map || attempt > 12) return;
  if (fitLandscapeFocus(map)) return;
  setTimeout(() => scheduleLandscapeFocus(map, attempt + 1), 80);
}

const AREA_SOURCE_ID = "area-shape";

function ensureAreaLayer(map) {
  if (map.getSource(AREA_SOURCE_ID)) return true;
  if (!map.isStyleLoaded()) return false;
  map.addSource(AREA_SOURCE_ID, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: `${AREA_SOURCE_ID}-fill`,
    type: "fill",
    source: AREA_SOURCE_ID,
    paint: { "fill-color": "#8b5cf6", "fill-opacity": 0.12 },
  });
  map.addLayer({
    id: `${AREA_SOURCE_ID}-line`,
    type: "line",
    source: AREA_SOURCE_ID,
    paint: { "line-color": "#7c3aed", "line-width": 2 },
  });
  return true;
}

function setAreaFeature(map, feature) {
  if (!ensureAreaLayer(map)) return;
  map.getSource(AREA_SOURCE_ID).setData({ type: "FeatureCollection", features: feature ? [feature] : [] });
}

function ensureChatMap() {
  if (chatMap) {
    setTimeout(() => chatMap.resize(), 80);
    return chatMap;
  }
  chatMap = new maplibregl.Map({
    container: "chatMap",
    style: MAP_STYLE_URL,
    center: FOCUS_CENTER,
    zoom: FOCUS_ZOOM,
    minZoom: 9,
    maxZoom: 16,
    maxBounds: MAP_MAX_BOUNDS,
    maxPitch: 0, // 평면 유지 (회전은 됨, 틸트는 안 됨)
    attributionControl: { compact: true },
  });
  chatMap.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");
  chatMap.on("load", () => ensureAreaLayer(chatMap));
  chatMap.on("zoomend", () => {
    const show = chatMap.getZoom() >= 12;
    if (show !== storeLabelsOn && lastChatMapData) {
      renderChatMap(lastChatMapData, lastChatOrigin, true);
    }
  });
  bindAreaDraw(chatMap);
  const pane = $("chatMap");
  if (pane && typeof ResizeObserver !== "undefined") {
    let fitted = false;
    const ro = new ResizeObserver(() => {
      chatMap.resize();
      if (!fitted && fitLandscapeFocus(chatMap)) fitted = true;
    });
    ro.observe(pane);
  }
  scheduleLandscapeFocus(chatMap);
  return chatMap;
}

function setAreaMode(on, shape = "rect") {
  areaShape = on ? shape : null;
  const rectBtn = $("areaSelectBtn");
  const circleBtn = $("areaCircleBtn");
  const pane = document.querySelector(".inventory-map-pane");
  if (!rectBtn || !circleBtn || !chatMap) return;
  rectBtn.classList.toggle("active", areaShape === "rect");
  circleBtn.classList.toggle("active", areaShape === "circle");
  rectBtn.textContent = areaShape === "rect" ? "드래그해서 영역을 그리세요" : "영역 선택";
  circleBtn.textContent = areaShape === "circle" ? "드래그해서 반경을 그리세요" : "원형 선택";
  if (areaShape) {
    pane.classList.add("is-drawing");
    chatMap.dragPan.disable();
    chatMap.boxZoom.disable();
  } else {
    pane.classList.remove("is-drawing");
    chatMap.dragPan.enable();
    chatMap.boxZoom.enable();
  }
}

function clearArea() {
  areaDrawing = false;
  areaStart = null;
  areaBounds = null;
  areaCircle = null;
  if (chatMap) setAreaFeature(chatMap, null);
  const clearBtn = $("areaClearBtn");
  if (clearBtn) clearBtn.classList.add("hidden");
  hideAreaTable();
  setAreaMode(false);
  loadDefaultMap();
}

function bindAreaDraw(map) {
  if (areaBoundOnce) return;
  areaBoundOnce = true;
  map.on("mousedown", (e) => {
    if (!areaShape) return;
    if (e.originalEvent) e.originalEvent.preventDefault();
    areaDrawing = true;
    areaStart = e.lngLat;
    areaLast = e.lngLat;
    if (areaShape === "circle") {
      setAreaFeature(map, circleFeature(areaStart.lat, areaStart.lng, 1));
    } else {
      setAreaFeature(map, rectFeature(areaStart.lat, areaStart.lng, areaStart.lat, areaStart.lng));
    }
  });
  map.on("mousemove", (e) => {
    if (!areaDrawing || !areaStart) return;
    areaLast = e.lngLat;
    if (areaShape === "circle") {
      const radiusMeters = haversineDistanceMeters(areaStart.lat, areaStart.lng, e.lngLat.lat, e.lngLat.lng);
      setAreaFeature(map, circleFeature(areaStart.lat, areaStart.lng, radiusMeters));
    } else {
      setAreaFeature(map, rectFeature(areaStart.lat, areaStart.lng, e.lngLat.lat, e.lngLat.lng));
    }
  });
  const finish = () => {
    if (!areaDrawing || !areaStart) return;
    areaDrawing = false;
    const end = areaLast || areaStart;
    if (areaShape === "circle") {
      const radiusMeters = haversineDistanceMeters(areaStart.lat, areaStart.lng, end.lat, end.lng);
      if (radiusMeters < 30) {
        setAreaMode(false);
        setAreaFeature(map, null);
        return;
      }
      setAreaFeature(map, circleFeature(areaStart.lat, areaStart.lng, radiusMeters));
      areaCircle = { lat: areaStart.lat, lng: areaStart.lng, radius_km: radiusMeters / 1000 };
      areaBounds = null;
      setAreaMode(false);
      const clearBtn = $("areaClearBtn");
      if (clearBtn) clearBtn.classList.remove("hidden");
      applyAreaCircle(areaCircle);
      return;
    }
    const south = Math.min(areaStart.lat, end.lat);
    const north = Math.max(areaStart.lat, end.lat);
    const west = Math.min(areaStart.lng, end.lng);
    const east = Math.max(areaStart.lng, end.lng);
    if (south === north || west === east) {
      setAreaMode(false);
      setAreaFeature(map, null);
      return;
    }
    setAreaFeature(map, rectFeature(areaStart.lat, areaStart.lng, end.lat, end.lng));
    areaBounds = { south, west, north, east };
    areaCircle = null;
    setAreaMode(false);
    const clearBtn = $("areaClearBtn");
    if (clearBtn) clearBtn.classList.remove("hidden");
    applyAreaBounds(areaBounds);
  };
  map.on("mouseup", () => finish());
  document.addEventListener("mouseup", () => finish());
}

function hideAreaTable() {
  const wrap = $("areaTableWrap");
  const pane = document.querySelector(".inventory-map-pane");
  if (wrap) wrap.classList.add("hidden");
  if (pane) pane.classList.remove("has-area-table");
  if (chatMap) setTimeout(() => chatMap.resize(), 80);
}

function renderAreaTable(data) {
  const wrap = $("areaTableWrap");
  const table = $("areaTable");
  const pane = document.querySelector(".inventory-map-pane");
  const head = wrap && wrap.querySelector(".inventory-area-table-head");
  if (!wrap || !table) return;
  const models = (data && (data.area_model_totals || data.model_totals)) || [];
  const rows = models.filter((m) => m.qty);
  const tbody = table.querySelector("tbody");
  const tfoot = table.querySelector("tfoot");
  tbody.innerHTML = "";
  tfoot.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="4">이 영역에 판매점 재고가 없습니다.</td></tr>`;
  } else {
    for (const m of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${escHtml(m.model)}</td><td>${Number(m.qty || 0).toLocaleString("ko-KR")}</td><td>${Number(m.stores || 0).toLocaleString("ko-KR")}</td><td>${Number(m.aged_qty || 0).toLocaleString("ko-KR")}</td>`;
      tbody.appendChild(tr);
    }
    const qty = rows.reduce((s, m) => s + (m.qty || 0), 0);
    const aged = rows.reduce((s, m) => s + (m.aged_qty || 0), 0);
    tfoot.innerHTML = `<tr><td>합계 ${rows.length}기종</td><td>${qty.toLocaleString("ko-KR")}</td><td></td><td>${aged.toLocaleString("ko-KR")}</td></tr>`;
  }
  if (head) {
    const stores = (data.points || []).length;
    head.textContent = `선택한 영역 재고 · ${stores}곳`;
  }
  wrap.classList.remove("hidden");
  if (pane) pane.classList.add("has-area-table");
  if (chatMap) setTimeout(() => chatMap.resize(), 80);
}

async function applyAreaBounds(bbox) {
  try {
    const params = mapQueryParams();
    params.set("south", String(bbox.south));
    params.set("west", String(bbox.west));
    params.set("north", String(bbox.north));
    params.set("east", String(bbox.east));
    const data = await api(`/inventory/map?${params}`);
    applyMapMeta(data);
    renderChatMap(data, null, true);
    renderAreaTable(data);
  } catch (err) {
    addBotError(err);
  }
}

async function applyAreaCircle(circle) {
  try {
    const params = mapQueryParams();
    params.set("circle_lat", String(circle.lat));
    params.set("circle_lng", String(circle.lng));
    params.set("circle_radius_km", String(circle.radius_km));
    const data = await api(`/inventory/map?${params}`);
    applyMapMeta(data);
    renderChatMap(data, null, true);
    renderAreaTable(data);
  } catch (err) {
    addBotError(err);
  }
}

function modelsTableHtml(models) {
  const rows = (models || []).filter((m) => m && m.qty);
  if (!rows.length) {
    return `<div class="muted small">보유기종이 없습니다.</div>`;
  }
  const body = rows
    .map(
      (m) =>
        `<tr><td>${escHtml(m.model)}</td><td>${Number(m.qty || 0).toLocaleString("ko-KR")}</td></tr>`
    )
    .join("");
  const total = rows.reduce((sum, m) => sum + (m.qty || 0), 0);
  return `<div class="map-popup-table-wrap"><table class="map-popup-table">
    <thead><tr><th>기종</th><th>대수</th></tr></thead>
    <tbody>${body}</tbody>
    <tfoot><tr><td>합계 ${rows.length}기종</td><td>${total.toLocaleString("ko-KR")}</td></tr></tfoot>
  </table></div>`;
}

function renderChatMap(data, origin, keepView = false) {
  const map = ensureChatMap();
  if (!map) return;
  if (!map.isStyleLoaded()) {
    map.once("load", () => renderChatMap(data, origin, keepView));
    return;
  }
  lastChatMapData = data;
  lastChatOrigin = origin;
  chatMarkers.forEach((m) => m.remove());
  chatMarkers = [];
  if (chatMeMarker) {
    chatMeMarker.remove();
    chatMeMarker = null;
  }
  const points = clusterMapPoints((data && data.points) || []);
  const nearestCode = data && data.nearest && data.nearest.store_code;
  const showLabel = map.getZoom() >= 12;
  storeLabelsOn = showLabel;
  const bounds = [];
  let nearestMarker = null;
  for (const p of points) {
    if (p.lat == null || p.lng == null) continue;
    const isNearest = nearestCode && (p.stores || []).some((s) => s.store_code === nearestCode);
    const dist = p.distance_meters != null ? `<div class="distance">${formatKm(p.distance_meters)}</div>` : "";
    const dealers = (p.dealers || []).map((d) => `${escHtml(d.dealer_name)} ${d.qty}대`).join(" · ");
    const storeRows = (p.stores || [p])
      .map((s) => {
        const title = storeNameOf(s);
        return `<div class="store-line"><span class="store-code">${escHtml(s.store_code)}</span>${
          title ? ` ${escHtml(title)}` : ""
        } <span class="muted">${s.qty}대</span></div>`;
      })
      .join("");
    const addr = [p.address, p.detail_address].filter(Boolean).join(" ");
    const popup = new maplibregl.Popup({ maxWidth: "360px", offset: 18 }).setHTML(
      `<div class="map-popup">
      <div class="store-list">${storeRows}</div>
      <div class="distance">${p.qty}대${p.aged_qty ? ` · 30일+ ${p.aged_qty}대` : ""}</div>
      ${modelsTableHtml(p.models)}
      ${dealers ? `<div class="muted small">${dealers}</div>` : ""}${dist}
      ${addr ? `<div class="muted small store-address">${escHtml(addr)}</div>` : ""}</div>`
    );
    const el = stockMarkerElement(p, isNearest, showLabel);
    if (!showLabel) el.title = storeCaptionText(p);
    const marker = new maplibregl.Marker({ element: el, anchor: "left", offset: [6, 0] })
      .setLngLat([p.lng, p.lat])
      .setPopup(popup)
      .addTo(map);
    chatMarkers.push(marker);
    bounds.push([p.lat, p.lng]);
    if (isNearest) nearestMarker = marker;
  }
  if (origin) {
    const dot = document.createElement("div");
    dot.className = "me-dot";
    chatMeMarker = new maplibregl.Marker({ element: dot })
      .setLngLat([origin.lng, origin.lat])
      .setPopup(new maplibregl.Popup({ offset: 12 }).setText("내 위치"))
      .addTo(map);
    bounds.push([origin.lat, origin.lng]);
  }
  if (keepView) {
    setTimeout(() => map.resize(), 80);
    return;
  }
  fitChatMap(map, bounds, origin, nearestMarker, data);
}

function fitChatMap(map, bounds, origin, nearestMarker, data) {
  if (origin && bounds.length) {
    map.fitBounds(boundsFromPoints(bounds), { padding: 40, maxZoom: 14 });
    if (nearestMarker) nearestMarker.togglePopup();
  } else if (nearestMarker && data && data.nearest) {
    map.jumpTo({ center: [data.nearest.lng, data.nearest.lat], zoom: 14 });
    nearestMarker.togglePopup();
  } else if (bounds.length === 1) {
    map.jumpTo({ center: [bounds[0][1], bounds[0][0]], zoom: 14 });
  } else if (bounds.length > 1 && data && data.dealer_id) {
    map.fitBounds(boundsFromPoints(bounds), { padding: 40, maxZoom: 13 });
  } else {
    fitLandscapeFocus(map);
  }
  setTimeout(() => map.resize(), 80);
}

function addBubble(role, text, tables) {
  const log = $("chatLog");
  const el = document.createElement("div");
  el.className = `chat-bubble ${role}`;
  if (text) {
    const p = document.createElement("div");
    p.textContent = text;
    el.appendChild(p);
  }
  for (const table of tables || []) {
    el.appendChild(buildChatTable(table));
  }
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function buildChatTable(table) {
  const wrap = document.createElement("div");
  wrap.className = "chat-table-wrap";
  if (table.title) {
    const title = document.createElement("div");
    title.className = "chat-table-title";
    title.textContent = table.title;
    wrap.appendChild(title);
  }
  const el = document.createElement("table");
  el.className = "chat-table";
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const col of table.columns || []) {
    const th = document.createElement("th");
    th.textContent = col;
    hr.appendChild(th);
  }
  thead.appendChild(hr);
  el.appendChild(thead);
  const tbody = document.createElement("tbody");
  for (const row of table.rows || []) {
    const tr = document.createElement("tr");
    (row || []).forEach((cell, i) => {
      const td = document.createElement("td");
      td.textContent = formatTableCell(cell, i === 0);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
  el.appendChild(tbody);
  if (table.footer && table.footer.length) {
    const tfoot = document.createElement("tfoot");
    const tr = document.createElement("tr");
    table.footer.forEach((cell, i) => {
      const td = document.createElement("td");
      td.textContent = formatTableCell(cell, i === 0);
      tr.appendChild(td);
    });
    tfoot.appendChild(tr);
    el.appendChild(tfoot);
  }
  wrap.appendChild(el);
  return wrap;
}

function formatTableCell(value, first) {
  if (value == null || value === "") return first ? "" : "—";
  if (typeof value === "number") return value.toLocaleString("ko-KR");
  return String(value);
}

function addBot(text, tables) {
  addBubble("bot", text, tables);
}

function addUser(text) {
  addBubble("user", text);
}

function isTransientError(err) {
  const msg = String((err && err.message) || err || "");
  return /응답하지 않습니다|연결하지 못했습니다|찾지 못했|JOB_NOT_FOUND|failed to fetch|networkerror|load failed|502|503|504/i.test(
    msg
  );
}

async function apiQuiet(path, options = {}) {
  try {
    return { ok: true, data: await api(path, options) };
  } catch (err) {
    return { ok: false, error: err };
  }
}

function newUploadJobId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, "");
  return `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}0000000000000000`.slice(0, 32);
}

function buildUploadForm(file, jobId) {
  const form = new FormData();
  form.append("file", file);
  form.append("job_id", jobId);
  return form;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function setChatNotice(text) {
  const log = $("chatLog");
  if (!log) return;
  let el = $("chatUploadNotice");
  if (!text) {
    if (el) el.remove();
    return;
  }
  if (/응답하지 않습니다/.test(text)) text = "파일을 저장하는 중...";
  if (!el) {
    el = document.createElement("div");
    el.id = "chatUploadNotice";
    el.className = "chat-bubble bot";
    el.appendChild(document.createElement("div"));
    const greetEl = log.querySelector(".chat-bubble.bot");
    if (greetEl) greetEl.after(el);
    else log.appendChild(el);
  }
  el.firstElementChild.textContent = text;
  log.scrollTop = log.scrollHeight;
}

async function postInventoryFile(file, jobId) {
  return apiQuiet("/inventory/excel", { method: "POST", body: buildUploadForm(file, jobId) });
}

async function waitForUploadJob(jobId, file) {
  let lastPost = -8;
  for (let i = 0; i < 360; i++) {
    const result = await apiQuiet(`/inventory/excel/status?job_id=${encodeURIComponent(jobId)}`);
    if (result.ok) {
      const data = result.data || {};
      if (data.message) setChatNotice(data.message);
      if (data.status === "done") return data.summary || data;
      if (data.status === "error") throw new Error(data.message || "업로드에 실패했습니다.");
    } else if (isTransientError(result.error)) {
      setChatNotice("파일을 저장하는 중...");
      if (i - lastPost >= 8) {
        lastPost = i;
        await postInventoryFile(file, jobId);
      }
    } else {
      throw result.error;
    }
    await sleep(1000);
  }
  throw new Error("저장이 아직 끝나지 않았습니다. 잠시 후 새로고침해 보세요.");
}

function openRetailStoreModal() {
  const modal = $("retailStoreModal");
  if (!modal) return;
  ["retailStoreCode", "retailStoreName", "retailStoreAddress", "retailStoreDetailAddress", "retailStoreDealerCode"].forEach(
    (id) => {
      if ($(id)) $(id).value = "";
    }
  );
  const msg = $("retailStoreMessage");
  if (msg) {
    msg.textContent = "";
    msg.classList.remove("error");
  }
  const dealerWrap = $("retailStoreDealerWrap");
  if (dealerWrap) dealerWrap.classList.toggle("hidden", !inventoryUser.can_see_all);
  if (inventoryUser.can_see_all) {
    const datalist = $("dealerCodeListInv");
    if (datalist) {
      datalist.innerHTML = "";
      for (const d of lastHqDealers) {
        if (!d.dealer_code) continue;
        const opt = document.createElement("option");
        opt.value = d.dealer_code;
        opt.textContent = d.dealer_name || d.dealer_code;
        datalist.appendChild(opt);
      }
    }
  }
  modal.classList.remove("hidden");
}

function closeRetailStoreModal() {
  const modal = $("retailStoreModal");
  if (modal) modal.classList.add("hidden");
}

function openAddressSearch() {
  if (!window.daum || !window.daum.Postcode) {
    const msg = $("retailStoreMessage");
    if (msg) msg.textContent = "주소 검색을 불러오지 못했습니다. 잠시 후 다시 시도해주세요.";
    return;
  }
  new window.daum.Postcode({
    oncomplete(data) {
      const addr = data.roadAddress || data.jibunAddress || data.address || "";
      $("retailStoreAddress").value = addr;
      $("retailStoreDetailAddress").focus();
    },
  }).open();
}

async function handleRetailStoreSubmit() {
  const msg = $("retailStoreMessage");
  const btn = $("retailStoreSubmitBtn");
  const storeCode = $("retailStoreCode").value.trim();
  const name = $("retailStoreName").value.trim();
  const address = $("retailStoreAddress").value.trim();
  const detailAddress = $("retailStoreDetailAddress").value.trim();
  const dealerCode = inventoryUser.can_see_all ? $("retailStoreDealerCode").value.trim() : "";
  msg.classList.remove("error");
  if (!storeCode || !name || !address) {
    msg.textContent = "매장코드, 매장명, 기본주소를 입력해주세요.";
    msg.classList.add("error");
    return;
  }
  if (inventoryUser.can_see_all && !dealerCode) {
    msg.textContent = "소속대리점코드를 입력해주세요.";
    msg.classList.add("error");
    return;
  }
  if (btn) btn.disabled = true;
  msg.textContent = "등록하는 중...";
  try {
    const body = { store_code: storeCode, name, address, detail_address: detailAddress };
    if (dealerCode) body.dealer_code = dealerCode;
    const data = await api("/inventory/retail-store", { method: "POST", body: JSON.stringify(body) });
    msg.classList.toggle("error", !data.geocoded);
    msg.textContent = data.message || "등록했습니다.";
    if (data.geocoded) {
      setTimeout(closeRetailStoreModal, 900);
      loadInventoryMap(lastCoords).catch(() => {});
    }
  } catch (err) {
    msg.textContent = friendlyError(err);
    msg.classList.add("error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleInventoryUpload() {
  const input = $("inventoryFile");
  const btn = $("inventoryUploadBtn");
  if (!input) return;
  const file = input.files && input.files[0];
  if (!file) {
    setChatNotice("xlsx 파일을 선택해주세요.");
    return;
  }
  const jobId = newUploadJobId();
  setChatNotice("파일을 받는 중...");
  if (btn) btn.disabled = true;
  try {
    let started = null;
    for (let attempt = 0; attempt < 6; attempt++) {
      const posted = await postInventoryFile(file, jobId);
      if (posted.ok) {
        started = posted.data;
        break;
      }
      if (!isTransientError(posted.error)) throw posted.error;
      setChatNotice("파일을 저장하는 중...");
      await sleep(1500);
    }
    const data = await waitForUploadJob((started && started.job_id) || jobId, file);
    const name = data.dealer_name || inventoryUser.dealer_name || "";
    setChatNotice(`${name} 재고 현황을 업데이트 했습니다`.trim());
    catalogPicked = false;
    await loadCatalog();
    if (inventoryUser.can_see_all) loadHqSummary();
    try {
      await loadInventoryMap(lastCoords);
    } catch (mapErr) {
      addBotError(mapErr);
    }
  } catch (e) {
    const msg = friendlyError(e);
    setChatNotice(
      /응답하지 않습니다|연결하지 못했습니다/.test(msg)
        ? "저장이 아직 끝나지 않았습니다. 잠시 후 새로고침해 보세요."
        : msg
    );
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleLogin() {
  const username = $("chatUsername").value.trim();
  const password = $("chatPassword").value;
  const err = $("chatLoginError");
  err.classList.add("hidden");
  if (!username || !password) {
    err.textContent = "아이디와 비밀번호를 입력해주세요.";
    err.classList.remove("hidden");
    return;
  }
  try {
    const data = await api("/inventory/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    // 대리점 직원은 보물찾기와, SKT 계정은 관리자 콘솔과 같은 로그인 키를 쓴다.
    setToken(data.token, data.role === "dealer" ? "rep" : "admin");
    $("chatPassword").value = "";
    if (data.must_change_password) {
      showPasswordChange(data);
      return;
    }
    await showLoggedIn(data);
    greet();
  } catch (e) {
    err.textContent = friendlyError(e);
    err.classList.remove("hidden");
  }
}

function toggleResetBox() {
  const box = $("chatResetBox");
  const willShow = box.classList.contains("hidden");
  box.classList.toggle("hidden", !willShow);
  $("chatForgotBtn").textContent = willShow ? "닫기" : "비밀번호를 잊으셨나요?";
  if (willShow) {
    $("chatResetCode").value = $("chatUsername").value.trim();
    $("chatResetMessage").textContent = "";
    $("chatResetMessage").classList.remove("error");
  }
}

async function handleResetPassword() {
  const msg = $("chatResetMessage");
  const employeeCode = $("chatResetCode").value.trim();
  const phone = $("chatResetPhone").value.trim();
  const newPassword = $("chatResetPassword").value;
  const confirmPassword = $("chatResetPasswordConfirm").value;
  msg.classList.remove("error");

  if (!employeeCode || !phone || !newPassword) {
    msg.textContent = "고유ID, 전화번호 뒤 4자리, 새 비밀번호를 입력해주세요.";
    msg.classList.add("error");
    return;
  }
  if (!/^\d{4}$/.test(phone.replace(/\D/g, "").slice(-4))) {
    msg.textContent = "전화번호 뒤 4자리를 숫자로 입력해주세요.";
    msg.classList.add("error");
    return;
  }
  if (newPassword.length < 4) {
    msg.textContent = "새 비밀번호는 4자 이상이어야 합니다.";
    msg.classList.add("error");
    return;
  }
  if (newPassword !== confirmPassword) {
    msg.textContent = "새 비밀번호 확인이 일치하지 않습니다.";
    msg.classList.add("error");
    return;
  }

  const btn = $("chatResetBtn");
  btn.disabled = true;
  msg.textContent = "확인 중...";
  try {
    await api("/auth/reset-password", {
      method: "POST",
      body: JSON.stringify({ employee_code: employeeCode, phone, new_password: newPassword }),
    });
    ["chatResetPhone", "chatResetPassword", "chatResetPasswordConfirm"].forEach((id) => {
      $(id).value = "";
    });
    $("chatUsername").value = employeeCode;
    $("chatPassword").value = "";
    toggleResetBox();
    const loginError = $("chatLoginError");
    loginError.textContent = "비밀번호를 바꿨습니다. 새 비밀번호로 로그인해주세요.";
    loginError.classList.remove("hidden");
  } catch (err) {
    msg.textContent = friendlyError(err);
    msg.classList.add("error");
  } finally {
    btn.disabled = false;
  }
}

async function handleLogout() {
  try {
    await api("/inventory/logout", { method: "POST" });
  } catch (_) {
    /* ignore */
  }
  // 같은 세션을 공유하므로 보물찾기/관리자 콘솔에서도 함께 로그아웃된다.
  setToken("");
  showLoggedOut();
}

function greet() {
  $("chatLog").innerHTML = "";
  const dealer = inventoryUser.dealer_name;
  if (dealer && !inventoryUser.can_see_all) {
    addBot(
      "위 드롭다운에서 대표상품명·모델명을 고른 뒤 조회를 누르면 그 기종만 지도에 나옵니다. 채팅은 질문한 내용 그대로 답합니다."
    );
  } else {
    addBot(
      "올린 대리점 재고를 함께 봅니다. 왼쪽에서 대리점을 고를 수 있고, 위 드롭다운에서 대표상품명·모델명을 고른 뒤 조회를 누르면 그 기종만 지도에 나옵니다. 채팅은 질문한 내용 그대로 답합니다."
    );
  }
  requestMyLocation({ announce: false });
}

async function loadInventoryMap(coords) {
  const params = mapQueryParams(coords);
  const data = await api(`/inventory/map?${params}`);
  applyMapMeta(data);
  renderChatMap(data, coords || null);
  return data;
}

function mapQueryParams(coords) {
  const params = new URLSearchParams();
  for (const value of mapProductShorts) params.append("product_short", value);
  for (const value of mapModelNames) params.append("model_name", value);
  if (!mapProductShorts.length && !mapModelNames.length) params.set("model", "ALL");
  if (mapAgedOnly) params.set("aged_only", "1");
  if (mapPinColor) params.set("pin_color", mapPinColor);
  params.set("include_partner", mapIncludePartner ? "1" : "0");
  params.set("include_retail", mapIncludeRetail ? "1" : "0");
  if (coords) {
    params.set("lat", String(coords.lat));
    params.set("lng", String(coords.lng));
    params.set("radius_km", "20");
  }
  if (inventoryUser.can_see_all && hqDealerId) params.set("dealer_id", hqDealerId);
  return params;
}

function applyMapMeta(data) {
  if (!data) return;
  if (Object.prototype.hasOwnProperty.call(data, "pin_color")) {
    mapPinColor = data.pin_color || "";
  }
  if (Object.prototype.hasOwnProperty.call(data, "aged_only")) {
    mapAgedOnly = !!data.aged_only;
  }
  // 구간별 색상은 채팅으로만 정한다 — 조회 버튼으로 직접 지도를 불러오면 초기화한다.
  mapColorRules = [];
  if (data.as_of_date || (data.uploads && data.uploads.length)) renderAsOf(data);
}

function formatAsOf(raw) {
  return String(raw || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((day) => (day.length === 8 ? `${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6, 8)}` : day))
    .join(" · ");
}

function renderAsOf(data) {
  const el = $("asOfBadge");
  if (!el) return;
  const uploads = data.uploads || [];
  let text = "";
  if (uploads.length > 1) {
    text = uploads
      .map((u) => {
        const name = u.dealer_name || "";
        const day = formatAsOf(u.as_of_date);
        if (name && day) return `${name} ${day}`;
        return day || name;
      })
      .filter(Boolean)
      .join(" · ");
  } else {
    text = formatAsOf((uploads[0] && uploads[0].as_of_date) || data.as_of_date);
  }
  el.textContent = text ? `기준일 ${text}` : "";
  el.classList.toggle("hidden", !el.textContent);
}

async function loadCatalog() {
  const qs = inventoryUser.can_see_all && hqDealerId ? `?dealer_id=${encodeURIComponent(hqDealerId)}` : "";
  try {
    const data = await api(`/inventory/catalog${qs}`);
    modelCatalog = (data.products || []).slice().sort((a, b) =>
      String(a.product_short || "").localeCompare(String(b.product_short || ""), "ko", {
        numeric: true,
        sensitivity: "base",
      })
    );
    renderAsOf(data);
    fillProductSelect();
  } catch (_) {
    modelCatalog = [];
    fillProductSelect();
  }
}

function compactSearchText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[\s\-_/]/g, "");
}

function digitSearchText(value) {
  return String(value || "").replace(/\D/g, "");
}

function nameMatchesQuery(text, query) {
  const q = compactSearchText(query);
  if (!q) return true;
  const compact = compactSearchText(text);
  const digits = digitSearchText(text);
  const qDigits = digitSearchText(query);
  if (compact.includes(q)) return true;
  if (qDigits && digits.includes(qDigits)) return true;
  return false;
}

function bindMenuSearch(search, onInput) {
  search.type = "search";
  search.className = "multi-pick-search";
  search.setAttribute("autocomplete", "off");
  search.addEventListener("click", (e) => e.stopPropagation());
  search.addEventListener("mousedown", (e) => e.stopPropagation());
  search.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") e.preventDefault();
  });
  search.addEventListener("input", onInput);
}

function fillProductSelect() {
  const menu = $("productShortMenu");
  if (!menu) return;
  const keep = new Set(pickedProductShorts);
  const prevQuery = (menu.querySelector(".multi-pick-search") || {}).value || "";
  menu.innerHTML = "";
  const search = document.createElement("input");
  search.placeholder = "번호·이름 검색";
  search.value = prevQuery;
  bindMenuSearch(search, () => filterPickMenu(menu));
  menu.appendChild(search);
  for (const p of modelCatalog) {
    const label = document.createElement("label");
    label.className = "multi-pick-item";
    const models = (p.models || []).map((m) => m.model_name).join(" ");
    label.dataset.search = `${p.product_short || ""} ${models}`;
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = p.product_short;
    box.checked = keep.has(p.product_short);
    const qty = Number(p.qty || 0).toLocaleString("ko-KR");
    label.appendChild(box);
    label.appendChild(document.createTextNode(` ${p.product_short} `));
    const meta = document.createElement("span");
    meta.className = "muted";
    meta.textContent = `${qty}대`;
    label.appendChild(meta);
    menu.appendChild(label);
  }
  const empty = document.createElement("div");
  empty.className = "multi-pick-empty muted small hidden";
  empty.textContent = "검색 결과가 없습니다.";
  menu.appendChild(empty);
  filterPickMenu(menu);
  pickedProductShorts = pickedProductShorts.filter((v) => modelCatalog.some((p) => p.product_short === v));
  updateMultiPickLabel("productShortBtn", pickedProductShorts, "전체");
  fillModelSelect();
}

function filterPickMenu(menu) {
  if (!menu) return;
  const q = (menu.querySelector(".multi-pick-search") || {}).value || "";
  let shown = 0;
  menu.querySelectorAll(".multi-pick-item").forEach((el) => {
    const hit = nameMatchesQuery(el.dataset.search || "", q);
    el.classList.toggle("hidden", !hit);
    if (hit) shown += 1;
  });
  const empty = menu.querySelector(".multi-pick-empty");
  if (empty) empty.classList.toggle("hidden", !!shown);
}

function selectedProductModels() {
  const products = pickedProductShorts.length
    ? modelCatalog.filter((p) => pickedProductShorts.includes(p.product_short))
    : [];
  const merged = new Map();
  for (const p of products) {
    for (const m of p.models || []) {
      const prev = merged.get(m.model_name) || { model_name: m.model_name, qty: 0 };
      prev.qty += m.qty || 0;
      merged.set(m.model_name, prev);
    }
  }
  return [...merged.values()].sort((a, b) =>
    String(a.model_name || "").localeCompare(String(b.model_name || ""), "ko", {
      numeric: true,
      sensitivity: "base",
    })
  );
}

function fillModelSelect() {
  const menu = $("modelNameMenu");
  const btn = $("modelNameBtn");
  if (!menu || !btn) return;
  const models = selectedProductModels();
  const keep = new Set(pickedModelNames);
  const prevQuery = (menu.querySelector(".multi-pick-search") || {}).value || "";
  menu.innerHTML = "";
  btn.disabled = !models.length;
  if (!models.length) {
    pickedModelNames = [];
    updateMultiPickLabel("modelNameBtn", [], "대표상품 먼저");
    return;
  }
  const search = document.createElement("input");
  search.placeholder = "번호·이름 검색";
  search.value = prevQuery;
  bindMenuSearch(search, () => filterPickMenu(menu));
  menu.appendChild(search);
  for (const m of models) {
    const label = document.createElement("label");
    label.className = "multi-pick-item";
    label.dataset.search = m.model_name || "";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = m.model_name;
    box.checked = keep.has(m.model_name);
    const qty = Number(m.qty || 0).toLocaleString("ko-KR");
    label.appendChild(box);
    label.appendChild(document.createTextNode(` ${m.model_name} `));
    const meta = document.createElement("span");
    meta.className = "muted";
    meta.textContent = `${qty}대`;
    label.appendChild(meta);
    menu.appendChild(label);
  }
  const empty = document.createElement("div");
  empty.className = "multi-pick-empty muted small hidden";
  empty.textContent = "검색 결과가 없습니다.";
  menu.appendChild(empty);
  filterPickMenu(menu);
  pickedModelNames = pickedModelNames.filter((v) => models.some((m) => m.model_name === v));
  updateMultiPickLabel("modelNameBtn", pickedModelNames, "해당 기종 전체");
}

function updateMultiPickLabel(btnId, values, emptyText) {
  const btn = $(btnId);
  if (!btn) return;
  if (!values.length) {
    btn.textContent = emptyText;
    return;
  }
  if (values.length === 1) {
    btn.textContent = values[0];
    return;
  }
  btn.textContent = `${values[0]} 외 ${values.length - 1}개`;
}

function readChecked(menuId) {
  const menu = $(menuId);
  if (!menu) return [];
  return [...menu.querySelectorAll("input[type=checkbox]:checked")].map((el) => el.value);
}

function placeMultiPickMenu(menu) {
  if (!menu || menu.classList.contains("hidden")) return;
  const host = menu.closest(".multi-pick");
  const btn = host && host.querySelector(".multi-pick-btn");
  if (!btn) return;
  const r = btn.getBoundingClientRect();
  const width = Math.max(240, r.width);
  let left = r.left;
  if (left + width > window.innerWidth - 8) left = Math.max(8, window.innerWidth - width - 8);
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(r.bottom + 4)}px`;
  menu.style.minWidth = `${Math.round(width)}px`;
}

function closeMultiPickMenus(exceptId) {
  document.querySelectorAll(".multi-pick-menu").forEach((el) => {
    if (el.id !== exceptId) el.classList.add("hidden");
  });
}

function toggleMultiPick(menuId) {
  const menu = $(menuId);
  if (!menu) return;
  const willOpen = menu.classList.contains("hidden");
  closeMultiPickMenus(willOpen ? menuId : "");
  menu.classList.toggle("hidden", !willOpen);
  if (willOpen) {
    placeMultiPickMenu(menu);
    const search = menu.querySelector(".multi-pick-search");
    if (search) setTimeout(() => search.focus(), 0);
  }
}

function resetMapStyle() {
  mapPinColor = "";
  mapColorRules = [];
  mapAgedOnly = false;
}

function onProductShortChange() {
  pickedProductShorts = readChecked("productShortMenu");
  pickedModelNames = [];
  updateMultiPickLabel("productShortBtn", pickedProductShorts, "전체");
  fillModelSelect();
}

function onModelNameChange() {
  pickedModelNames = readChecked("modelNameMenu");
  updateMultiPickLabel("modelNameBtn", pickedModelNames, "해당 기종 전체");
}

function applyMapLookup() {
  mapProductShorts = pickedProductShorts.slice();
  mapModelNames = pickedModelNames.slice();
  catalogPicked = true;
  resetMapStyle();
  closeMultiPickMenus("");
  addThinking();
  loadInventoryMap(lastCoords)
    .then(() => removeThinking())
    .catch((err) => {
      removeThinking();
      addBotError(err);
    });
}

function uploadLabel(uploads, dealerId) {
  const rows = (uploads || []).filter((u) => !dealerId || u.dealer_id === dealerId);
  if (!rows.length) return "아직 없음";
  const u = rows[0];
  const qty = Number(u.row_count || 0).toLocaleString("ko-KR");
  return `${qty}행${u.as_of_date ? ` · ${u.as_of_date}` : ""}`;
}

async function loadHqSummary() {
  const box = $("hqDealerList");
  const allMeta = $("hqAllMeta");
  const status = $("hqStatus");
  if (!box || !inventoryUser.can_see_all) return;
  try {
    const data = await api("/inventory/summary");
    const dealers = data.dealers || data.by_dealer || [];
    const uploads = data.uploads || [];
    lastHqDealers = dealers;
    if (allMeta) {
      allMeta.textContent = `${Number(data.total_qty || 0).toLocaleString("ko-KR")}대 · ${Number(data.store_count || 0).toLocaleString("ko-KR")}곳`;
    }
    if (status) {
      status.textContent = `등록 ${Number(data.dealer_count || dealers.length || 0)} · 업로드 ${Number(data.uploaded_count || dealers.filter((d) => d.has_upload).length)}`;
    }
    const q = (($("hqSearch") && $("hqSearch").value) || "").trim().toLowerCase();
    const filtered = dealers.filter((d) => {
      if (!q) return true;
      const blob = `${d.dealer_name || ""} ${d.dealer_code || ""}`.toLowerCase();
      return blob.includes(q);
    });
    box.innerHTML = "";
    for (const d of filtered) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `hq-row${hqDealerId === d.dealer_id ? " active" : ""}`;
      btn.setAttribute("data-dealer", d.dealer_id || "");
      const aged = Number(d.aged_qty || 0);
      const uploaded = d.has_upload ? uploadLabel(uploads, d.dealer_id) : "아직 없음";
      btn.innerHTML = `<strong>${escHtml(d.dealer_name || "미지정")}</strong>
        <span class="muted small">${Number(d.qty || 0).toLocaleString("ko-KR")}대 · ${Number(d.stores || 0)}곳${aged ? ` · 30일+ ${aged.toLocaleString("ko-KR")}` : ""}</span>
        <span class="muted small">${escHtml(uploaded)}</span>`;
      box.appendChild(btn);
    }
    if (!filtered.length) {
      box.innerHTML = `<p class="muted small">해당하는 대리점이 없습니다.</p>`;
    }
    const allBtn = $("hqAllBtn");
    if (allBtn) allBtn.classList.toggle("active", !hqDealerId);
    fillSktDealerSelect(dealers);
  } catch (_) {
    box.textContent = "요약을 불러오지 못했습니다.";
  }
}

function fillSktDealerSelect(dealers) {
  const sel = $("sktDealerSelect");
  if (!sel) return;
  sel.innerHTML = `<option value="">전체 대리점</option>`;
  for (const d of dealers) {
    const opt = document.createElement("option");
    opt.value = d.dealer_id || "";
    const qty = Number(d.qty || 0).toLocaleString("ko-KR");
    opt.textContent = `${d.dealer_name || "미지정"}${d.dealer_code ? ` (${d.dealer_code})` : ""} · ${qty}대`;
    sel.appendChild(opt);
  }
  sel.value = hqDealerId;
}

function applyHqDealer(dealerId) {
  hqDealerId = dealerId || "";
  const sel = $("sktDealerSelect");
  if (sel) sel.value = hqDealerId;
  document.querySelectorAll("#hqPanel [data-dealer]").forEach((el) => {
    el.classList.toggle("active", (el.getAttribute("data-dealer") || "") === hqDealerId);
  });
  catalogPicked = false;
  return loadCatalog()
    .then(() => loadInventoryMap(lastCoords))
    .catch((err) => addBotError(err));
}

function handleHqClick(ev) {
  const filterBtn = ev.target.closest("[data-hq-filter]");
  if (filterBtn) return;
  const btn = ev.target.closest("[data-dealer]");
  if (!btn) return;
  applyHqDealer(btn.getAttribute("data-dealer") || "");
}

async function requestMyLocation(options = {}) {
  const announce = !!options.announce;
  const btn = $("myLocationBtn");
  if (btn) btn.classList.add("active");
  if (!navigator.geolocation) {
    if (announce) addBot("이 브라우저는 위치 정보를 지원하지 않아 전체 재고를 표시합니다.");
    await loadInventoryMap(null).catch((err) => addBotError(err));
    return;
  }
  navigator.geolocation.getCurrentPosition(
    async (pos) => {
      lastCoords = { lat: pos.coords.latitude, lng: pos.coords.longitude };
      try {
        const data = await loadInventoryMap(lastCoords);
        if ((!data.points || !data.points.length) && lastCoords) {
          const nationwide = await loadInventoryMap(null);
          renderChatMap(nationwide, lastCoords);
          return;
        }
      } catch (err) {
        addBotError(err);
      }
    },
    async (err) => {
      if (btn) btn.classList.remove("active");
      try {
        const data = await loadInventoryMap(null);
        if (announce) {
          addBot(
            `위치를 쓰지 못해 전체 재고를 표시합니다. (${err.message || "권한 거부"}) 브라우저에서 위치 권한을 허용하면 근처 재고를 볼 수 있습니다.`
          );
        }
      } catch (e) {
        addBotError(e);
      }
    },
    { enableHighAccuracy: true, timeout: 12000 }
  );
}

async function loadDefaultMap() {
  try {
    await loadInventoryMap(lastCoords);
  } catch (_) {
    /* 지도는 부가 */
  }
}

function askWithLocation(text) {
  addBot("지금 위치를 확인하는 중입니다.");
  if (!navigator.geolocation) {
    addBot("이 브라우저는 위치 정보를 지원하지 않습니다. 위치 없이 지역명으로 물어봐 주세요.");
    return;
  }
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      sendQuestion(text, { lat: pos.coords.latitude, lng: pos.coords.longitude });
    },
    (err) => {
      addBot(`위치를 가져오지 못했습니다: ${err.message}. 브라우저에서 위치 권한을 허용해 주세요.`);
    },
    { enableHighAccuracy: true, timeout: 12000 }
  );
}

function addThinking() {
  const log = $("chatLog");
  removeThinking();
  const el = document.createElement("div");
  el.className = "chat-bubble bot thinking";
  el.id = "chatThinking";
  el.textContent = "조회 중입니다";
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function removeThinking() {
  const el = $("chatThinking");
  if (el) el.remove();
}

function extractPrimaryStoreCode(data) {
  // "그 판매점", "거기" 처럼 다음 질문이 가리킬 만한, 이번 응답이 콕 집은 매장 하나를 고른다.
  // 지역/전체처럼 매장이 여러 곳이면 어느 걸 가리키는지 알 수 없으니 기억하지 않는다.
  if (!data || !data.map) return "";
  if (data.map.nearest && data.map.nearest.store_code) return data.map.nearest.store_code;
  const points = data.map.points || [];
  if (points.length === 1 && points[0].store_code) return points[0].store_code;
  return "";
}

async function sendQuestion(text, coords) {
  pendingQuestion = text;
  addThinking();
  try {
    const body = { text };
    if (coords) {
      body.lat = coords.lat;
      body.lng = coords.lng;
    }
    if (areaBounds) body.bbox = areaBounds;
    if (inventoryUser.can_see_all && hqDealerId) body.dealer_id = hqDealerId;
    if (lastStoreCode) body.last_store_code = lastStoreCode;
    const data = await api("/inventory/ask", {
      method: "POST",
      body: JSON.stringify(body),
    });
    removeThinking();
    if (data.needs_location && !coords) {
      askWithLocation(text);
      return;
    }
    if (data.needs_area && !areaBounds) {
      addBot(data.answer || "지도에서 영역을 먼저 선택해 주세요.");
      setAreaMode(true);
      return;
    }
    addBot(data.answer || "답을 만들지 못했습니다.", data.tables);
    if (data.map) {
      mapPinColor = data.map.pin_color || "";
      mapColorRules = data.map.pin_color_rules || [];
      mapAgedOnly = !!data.map.aged_only;
      renderChatMap(data.map, coords || null);
      if (areaBounds || (data.map && data.map.bbox)) renderAreaTable(data.map);
    }
    const primary = extractPrimaryStoreCode(data);
    if (primary) lastStoreCode = primary;
  } catch (err) {
    removeThinking();
    addBot("답을 가져오지 못했습니다. 다시 물어봐 주세요.");
  }
}

function submitChat(text) {
  const q = (text || "").trim();
  if (!q) return;
  addUser(q);
  $("chatInput").value = "";
  sendQuestion(q);
}

async function restore() {
  const { admin, rep } = getStoredTokens();
  if (!admin && !rep) {
    showLoggedOut();
    return;
  }
  // 보물찾기나 관리자 콘솔에서 이미 로그인했다면 그 토큰으로 바로 들어간다.
  // 둘 다 있으면 관리자 쪽을 먼저 시도한다 (죽은 토큰이면 사원 쪽으로 넘어간다).
  for (const [kind, token] of [
    ["admin", admin],
    ["rep", rep],
  ]) {
    if (!token) continue;
    activeTokenKind = kind;
    try {
      const me = await api("/inventory/me");
      if (me.must_change_password) {
        showPasswordChange(me);
        return;
      }
      await showLoggedIn(me);
      greet();
      return;
    } catch (err) {
      if (err && err.code === "NO_DEALER") {
        // 유효한 로그인이지만(보물찾기는 계속 쓸 수 있다) 이 화면은 못 쓴다. 토큰은 지우지 않는다.
        activeTokenKind = "";
        showLoggedOut();
        const loginError = $("chatLoginError");
        if (loginError) {
          loginError.textContent = err.message;
          loginError.classList.remove("hidden");
        }
        return;
      }
      // 이 토큰은 죽었다. 다음 후보로 넘어간다 (api() 가 이미 해당 키를 지웠다).
    }
  }
  activeTokenKind = "";
  showLoggedOut();
}

document.addEventListener("DOMContentLoaded", () => {
  $("chatLoginBtn").addEventListener("click", handleLogin);
  if ($("chatForgotBtn")) $("chatForgotBtn").addEventListener("click", toggleResetBox);
  if ($("chatResetBtn")) $("chatResetBtn").addEventListener("click", handleResetPassword);
  if ($("chatChangePasswordBtn")) $("chatChangePasswordBtn").addEventListener("click", handleChangePassword);
  if ($("chatPasswordLogoutBtn")) $("chatPasswordLogoutBtn").addEventListener("click", handleLogout);
  $("inventoryUploadBtn").addEventListener("click", handleInventoryUpload);
  const registerBtn = $("registerStoreBtn");
  if (registerBtn) registerBtn.addEventListener("click", openRetailStoreModal);
  const retailCloseBtn = $("retailStoreCloseBtn");
  if (retailCloseBtn) retailCloseBtn.addEventListener("click", closeRetailStoreModal);
  const retailAddress = $("retailStoreAddress");
  if (retailAddress) retailAddress.addEventListener("click", openAddressSearch);
  const retailSubmitBtn = $("retailStoreSubmitBtn");
  if (retailSubmitBtn) retailSubmitBtn.addEventListener("click", handleRetailStoreSubmit);
  const retailModal = $("retailStoreModal");
  if (retailModal) {
    retailModal.addEventListener("click", (e) => {
      if (e.target === retailModal) closeRetailStoreModal();
    });
  }
  const holderPartnerChk = $("holderPartnerChk");
  const holderRetailChk = $("holderRetailChk");
  if (holderPartnerChk) {
    holderPartnerChk.addEventListener("change", () => {
      mapIncludePartner = holderPartnerChk.checked;
      addThinking();
      loadInventoryMap(lastCoords)
        .then(() => removeThinking())
        .catch((err) => {
          removeThinking();
          addBotError(err);
        });
    });
  }
  if (holderRetailChk) {
    holderRetailChk.addEventListener("change", () => {
      mapIncludeRetail = holderRetailChk.checked;
      addThinking();
      loadInventoryMap(lastCoords)
        .then(() => removeThinking())
        .catch((err) => {
          removeThinking();
          addBotError(err);
        });
    });
  }
  const lookupBtn = $("mapLookupBtn");
  if (lookupBtn) lookupBtn.addEventListener("click", applyMapLookup);
  const productBtn = $("productShortBtn");
  const modelBtn = $("modelNameBtn");
  if (productBtn) productBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleMultiPick("productShortMenu");
  });
  if (modelBtn) modelBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (modelBtn.disabled) return;
    toggleMultiPick("modelNameMenu");
  });
  const productMenu = $("productShortMenu");
  const modelMenu = $("modelNameMenu");
  if (productMenu) {
    productMenu.addEventListener("click", (e) => e.stopPropagation());
    productMenu.addEventListener("change", (e) => {
      if (e.target && e.target.classList.contains("multi-pick-search")) return;
      onProductShortChange();
    });
  }
  if (modelMenu) {
    modelMenu.addEventListener("click", (e) => e.stopPropagation());
    modelMenu.addEventListener("change", (e) => {
      if (e.target && e.target.classList.contains("multi-pick-search")) return;
      onModelNameChange();
    });
  }
  document.addEventListener("click", () => closeMultiPickMenus(""));
  window.addEventListener("resize", () => {
    document.querySelectorAll(".multi-pick-menu:not(.hidden)").forEach(placeMultiPickMenu);
  });
  const hqPanel = $("hqPanel");
  if (hqPanel) hqPanel.addEventListener("click", handleHqClick);
  const hqSearch = $("hqSearch");
  if (hqSearch) hqSearch.addEventListener("input", () => loadHqSummary());
  const sktSel = $("sktDealerSelect");
  if (sktSel) {
    sktSel.addEventListener("change", () => applyHqDealer(sktSel.value || ""));
  }
  $("chatUsername").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("chatPassword").focus();
  });
  $("chatPassword").addEventListener("keydown", (e) => {
    if (e.key === "Enter") handleLogin();
  });
  $("chatLogoutBtn").addEventListener("click", handleLogout);
  $("chatForm").addEventListener("submit", (e) => {
    e.preventDefault();
    submitChat($("chatInput").value);
  });
  document.querySelectorAll(".chat-suggestions [data-q]").forEach((btn) => {
    btn.addEventListener("click", () => submitChat(btn.getAttribute("data-q")));
  });
  $("areaSelectBtn").addEventListener("click", () => {
    if (areaShape === "rect") setAreaMode(false);
    else {
      ensureChatMap();
      setAreaMode(true, "rect");
    }
  });
  $("areaCircleBtn").addEventListener("click", () => {
    if (areaShape === "circle") setAreaMode(false);
    else {
      ensureChatMap();
      setAreaMode(true, "circle");
    }
  });
  $("areaClearBtn").addEventListener("click", clearArea);
  $("myLocationBtn").addEventListener("click", () => requestMyLocation({ announce: true }));
  $("chatMicBtn").addEventListener("click", () => {
    addBot("음성 질문/답변은 다음 단계에서 붙입니다. 지금은 글로 물어봐 주세요.");
  });
  restore();
});
