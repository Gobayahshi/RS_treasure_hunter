// ---------------------------------------------------------------------------
// 상태 / 상수
// ---------------------------------------------------------------------------
const VISIT_RADIUS_METERS = 30;
const NEARBY_RADIUS_KM = 5;
const MIN_INITIAL_ZOOM = 15;
const DWELL_SECONDS = 5; // confidence.py RULES_CONFIG.min_dwell_seconds 와 맞출 것
const SAMPLE_INTERVAL_MS = 1000;

let rep = null;
let currentPosition = null;
let visitState = null; // { sessionId, store, timerId, elapsedSeconds }
let treasureMap = null;
let treasureMarkers = []; // maplibregl.Marker[]
let meMarker = null; // maplibregl.Marker
let hasAccuracyCircle = false;
let suppressMapMoveLoad = false;
let mapMoveTimer = null;
let mapLoadSeq = 0;
let positionWatchId = null;

// ---------------------------------------------------------------------------
// 유틸
// ---------------------------------------------------------------------------
function $(id) {
  return document.getElementById(id);
}

// 서버는 UTC로 저장한다. 화면에는 한국 시간으로 짧게 보여준다 (예: 9/18 07:30).
function formatCompactDateTime(iso) {
  if (!iso) return "";
  const date = new Date(`${iso}${/[zZ+]/.test(iso) ? "" : "Z"}`);
  if (Number.isNaN(date.getTime())) return "";
  const parts = new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date);
  const get = (type) => parts.find((p) => p.type === type)?.value || "";
  return `${get("month")}/${get("day")} ${get("hour")}:${get("minute")}`;
}

const REASON_LABELS = {
  NO_SAMPLES: "위치 정보가 수집되지 않았습니다",
  R1_MOCK_LOCATION_DETECTED: "가상 위치(모의 GPS) 사용이 감지되었습니다",
  R2_OUT_OF_RADIUS: "매장 반경 밖에서 인증을 시도했습니다",
  R2_PARTIAL_RADIUS_COVERAGE: "매장 반경 안에 머문 시간이 부족합니다",
  R3_LOW_GPS_ACCURACY: "GPS 정확도가 낮습니다",
  R4_INSUFFICIENT_DWELL_TIME: "매장 근처 체류 시간이 부족합니다",
  R5_MOVEMENT_INCONSISTENCY: "이동 경로가 부자연스럽습니다",
  R6_TELEPORT_DETECTED: "직전 위치와 비교해 이동 속도가 비정상적입니다",
  R7_ALREADY_CLAIMED_TODAY: "오늘 이미 인증한 매장입니다 (포인트 미지급)",
  R8_DEVICE_MISMATCH: "등록된 기기와 다릅니다",
  R9_OFF_HOURS_ACTIVITY: "근무시간 외 활동입니다",
};

function formatReasons(reasons) {
  return (reasons || []).map((r) => REASON_LABELS[r] || r).join(", ");
}

function appUrl(path) {
  const base = (window.APP_BASE || "").replace(/\/$/, "");
  if (!path.startsWith("/")) path = `/${path}`;
  return `${base}${path}`;
}

function showScreen(name) {
  document.querySelectorAll(".screen").forEach((el) => el.classList.add("hidden"));
  $(`screen-${name}`).classList.remove("hidden");
  $("topnav").classList.toggle("hidden", name === "login");
  if (name === "map") {
    startPositionWatch();
  } else {
    stopPositionWatch();
  }
}

// 지도 화면에서 "내 위치" 점을 실시간으로 움직인다. 매장 목록/거리는 새로고침 시에만 다시 계산한다.
function startPositionWatch() {
  if (!navigator.geolocation || positionWatchId != null) return;
  positionWatchId = navigator.geolocation.watchPosition(
    (pos) => {
      currentPosition = { lat: pos.coords.latitude, lng: pos.coords.longitude };
      if (meMarker) meMarker.setLngLat([currentPosition.lng, currentPosition.lat]);
      if (hasAccuracyCircle && treasureMap && treasureMap.getSource("me-accuracy")) {
        treasureMap
          .getSource("me-accuracy")
          .setData(circleFeature(currentPosition.lat, currentPosition.lng, VISIT_RADIUS_METERS));
      }
    },
    () => {}, // 실시간 갱신 실패는 조용히 무시한다 (최초 위치는 loadTreasures가 이미 가져옴)
    { enableHighAccuracy: true, maximumAge: 2000, timeout: 10000 }
  );
}

function stopPositionWatch() {
  if (positionWatchId != null && navigator.geolocation) {
    navigator.geolocation.clearWatch(positionWatchId);
    positionWatchId = null;
  }
}

function getOrCreateDeviceId() {
  let id = localStorage.getItem("rs_device_id");
  if (!id) {
    id = `dev-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem("rs_device_id", id);
  }
  return id;
}

function treasurePlaceName(store) {
  if ((store.address || "").startsWith("ADMIN/")) return store.name || "관리자 지정 보물";
  return store.address || store.name;
}

function treasurePlaceSub(store) {
  const n = Number(store.store_count) || 0;
  return n > 1 ? `이 주소 매장 ${n}곳` : "";
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

// MapLibre는 지오메트리 원을 그려주는 기능이 없어서(Leaflet의 L.circle과 달리)
// 위도 보정을 넣은 다각형으로 직접 근사한다.
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

function getCurrentPosition() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error("이 브라우저는 위치 정보를 지원하지 않습니다."));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve(pos),
      (err) => reject(err),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
    );
  });
}

function getRepToken() {
  return localStorage.getItem("rs_rep_token") || "";
}

function setRepToken(token) {
  if (token) localStorage.setItem("rs_rep_token", token);
  else localStorage.removeItem("rs_rep_token");
}

async function api(path, options) {
  const opts = options || {};
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const token = getRepToken();
  if (token) headers["X-Rep-Token"] = token;
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
  if (res.status === 401 && data && data.error === "REP_AUTH_REQUIRED") {
    // 토큰이 만료됐거나 로그아웃된 상태. 로그인 화면으로 되돌린다.
    forceRelogin();
    throw new Error("로그인이 만료되었습니다. 다시 로그인해주세요.");
  }
  if (res.status === 403 && data && data.error === "PASSWORD_CHANGE_REQUIRED") {
    // 초기 비밀번호를 쓰는 동안은 설정 화면 외에는 쓸 수 없다.
    if (rep) {
      rep.must_change_password = true;
      saveRep(rep);
    }
    $("passwordHint").classList.remove("hidden");
    showScreen("settings");
    throw new Error(data.message);
  }
  if (!res.ok) {
    const message = data && (data.message || data.error);
    throw new Error(message || `API ${path} 실패: ${res.status}`);
  }
  return data;
}

function forceRelogin() {
  clearRep();
  setRepToken("");
  rep = null;
  showScreen("login");
}

// ---------------------------------------------------------------------------
// 로그인
// ---------------------------------------------------------------------------
function loadStoredRep() {
  const raw = localStorage.getItem("rs_rep");
  return raw ? JSON.parse(raw) : null;
}

function saveRep(r) {
  localStorage.setItem("rs_rep", JSON.stringify(r));
}

function clearRep() {
  localStorage.removeItem("rs_rep");
}

async function handleLogin() {
  const employeeCode = $("loginCode").value.trim();
  const password = $("loginPassword").value;
  $("loginError").classList.add("hidden");

  if (!employeeCode) {
    $("loginError").textContent = "고유ID를 입력해주세요.";
    $("loginError").classList.remove("hidden");
    return;
  }
  if (!password) {
    $("loginError").textContent = "비밀번호를 입력해주세요.";
    $("loginError").classList.remove("hidden");
    return;
  }

  try {
    const res = await fetch(appUrl("/api/auth/login"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ employee_code: employeeCode, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.message || data.error || `로그인 실패 (${res.status})`);
    }
    setRepToken(data.token || "");
    delete data.token; // 토큰은 별도 키에만 보관한다
    rep = data;
    saveRep(rep);
    $("loginPassword").value = "";
    enterApp();
  } catch (err) {
    $("loginError").textContent = err.message || "로그인에 실패했습니다.";
    $("loginError").classList.remove("hidden");
  }
}

function handleLogout() {
  const token = getRepToken();
  if (token) {
    // 서버 세션도 끊는다. 실패해도 로컬은 지운다.
    fetch(appUrl("/api/auth/logout"), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Rep-Token": token },
    }).catch(() => {});
  }
  forceRelogin();
}

function enterApp() {
  $("repGreeting").textContent = rep.dealer_name ? `${rep.dealer_name} · ${rep.name}` : rep.name;
  if (rep.must_change_password || rep.using_initial_password) {
    // 초기 비밀번호를 바꾸기 전에는 서버가 다른 기능을 막는다.
    $("passwordHint").classList.remove("hidden");
    showScreen("settings");
  } else {
    $("passwordHint").classList.add("hidden");
    showScreen("map");
    loadTreasures();
  }
}

// ---------------------------------------------------------------------------
// 지도(근처 보물 목록)
// ---------------------------------------------------------------------------
const MAP_STYLE_URL = "https://tiles.openfreemap.org/styles/liberty";

// 로그인 직후엔 컨테이너가 막 hidden 이 풀리거나(특히 모바일 레이아웃) 화면이
// 다시 배치되는 중이라, 생성 시점 크기로는 resize() 를 불러도 실제로 다시 그려지지
// 않을 때가 있다. 레이아웃이 완전히 자리잡을 때까지 짧게 여러 번 눌러준다.
function pulseResize(map, durationMs = 2000, intervalMs = 150) {
  const start = Date.now();
  const tick = () => {
    if (!map || Date.now() - start > durationMs) return;
    map.resize();
    setTimeout(tick, intervalMs);
  };
  tick();
}

function ensureMap() {
  if (treasureMap) {
    setTimeout(() => treasureMap.resize(), 50);
    return treasureMap;
  }

  treasureMap = new maplibregl.Map({
    container: "treasureMap",
    style: MAP_STYLE_URL,
    center: [126.978, 37.5665],
    zoom: 14,
    maxPitch: 0, // 평면 유지 (회전은 됨, 틸트는 안 됨)
    attributionControl: { compact: true },
  });
  treasureMap.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");
  // 로그인 직후엔 컨테이너가 막 hidden 이 풀린 상태라 생성 시점의 크기를 0으로 잡을 때가 있다.
  // 스타일 로드가 끝난 뒤(레이아웃이 확실히 자리잡은 뒤) 한 번 더 강제로 맞춘다.
  treasureMap.on("load", () => treasureMap.resize());

  // moveend는 resize()/jumpTo()/fitBounds() 같은 프로그램적인 이동에도 발생해서
  // (originalEvent로 걸러도 못 걸러지는 경우가 있었다 - pulseResize()가 계속 resize()를
  // 부르는 동안 moveend가 반복 발생 -> loadTreasuresAt이 겹쳐 불려 목록이 "불러오는 중"에
  // 멈추는 원인이었다), 사용자가 실제로 드래그한 경우에만 발생하는 dragend로 바꾼다.
  treasureMap.on("dragend", () => {
    if (suppressMapMoveLoad) return;
    if (mapMoveTimer) clearTimeout(mapMoveTimer);
    mapMoveTimer = setTimeout(() => {
      loadTreasuresAt(treasureMap.getCenter().lat, treasureMap.getCenter().lng, {
        fitBounds: false,
        useGpsDistance: true,
      });
    }, 350);
  });

  pulseResize(treasureMap);
  return treasureMap;
}

function radiusKmForMapView(map) {
  const center = map.getCenter();
  const corner = map.getBounds().getNorthEast();
  const meters = haversineDistanceMeters(center.lat, center.lng, corner.lat, corner.lng);
  return Math.min(50, Math.max(0.8, meters / 1000));
}

function treasureMarkerElement(tier, withinRadius) {
  const color = withinRadius ? "#16a34a" : tier === "rare" ? "#d97706" : "#2563eb";
  // 판매점 리스트의 "⭐ 레어" 배지와 같은 노란 별 이모지로 맞춘다.
  const label = tier === "rare" ? "⭐" : "●";
  const el = document.createElement("div");
  el.className = "treasure-marker";
  el.innerHTML = `<div class="treasure-pin" style="background:${color}">${label}</div>`;
  return el;
}

function ensureAccuracyLayer(map) {
  if (map.getSource("me-accuracy")) return true;
  if (!map.isStyleLoaded()) return false;
  map.addSource("me-accuracy", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "me-accuracy-fill",
    type: "fill",
    source: "me-accuracy",
    paint: { "fill-color": "#93c5fd", "fill-opacity": 0.25 },
  });
  map.addLayer({
    id: "me-accuracy-line",
    type: "line",
    source: "me-accuracy",
    paint: { "line-color": "#2563eb", "line-width": 1 },
  });
  hasAccuracyCircle = true;
  return true;
}

function renderTreasureMap(treasures, options = {}) {
  const { fitBounds = true } = options;
  const map = ensureMap();
  if (!map.isStyleLoaded()) {
    map.once("load", () => renderTreasureMap(treasures, options));
    return;
  }

  treasureMarkers.forEach((m) => m.remove());
  treasureMarkers = [];

  if (meMarker) {
    meMarker.remove();
    meMarker = null;
  }

  if (currentPosition) {
    const dot = document.createElement("div");
    dot.className = "me-dot";
    meMarker = new maplibregl.Marker({ element: dot })
      .setLngLat([currentPosition.lng, currentPosition.lat])
      .setPopup(new maplibregl.Popup({ offset: 12 }).setText("내 위치"))
      .addTo(map);

    if (ensureAccuracyLayer(map)) {
      map.getSource("me-accuracy").setData(circleFeature(currentPosition.lat, currentPosition.lng, VISIT_RADIUS_METERS));
    }
  }

  const bounds = [];
  if (currentPosition) bounds.push([currentPosition.lat, currentPosition.lng]);

  for (const t of treasures) {
    const lat = t.store.lat;
    const lng = t.store.lng;
    if (lat == null || lng == null) continue;

    const withinRadius = t.distanceMeters <= VISIT_RADIUS_METERS;
    const tierLabel = t.tier === "rare" ? "⭐ 레어" : "🏅 일반";
    const pointsLabel = t.award_points ? ` · ${t.award_points}P` : "";
    const actionHtml = withinRadius
      ? `<button type="button" class="map-visit-btn" data-store-id="${t.store.id}">보물 캐러가기</button>`
      : `<p class="muted small">매장 근처(30m)로 이동하세요</p>`;

    const popup = new maplibregl.Popup({ offset: 14 }).setHTML(
      `<div class="map-popup">
        <strong>${tierLabel}${pointsLabel}</strong>
        <div class="store-name">${treasurePlaceName(t.store)}</div>
        ${treasurePlaceSub(t.store) ? `<div class="muted small">${treasurePlaceSub(t.store)}</div>` : ""}
        <div class="distance">내 위치에서 ${Math.round(t.distanceMeters)}m</div>
        ${actionHtml}
      </div>`
    );
    popup.on("open", () => {
      const btn = document.querySelector(`.map-visit-btn[data-store-id="${t.store.id}"]`);
      if (btn) {
        btn.onclick = () => startVisit(t.store);
      }
    });

    const marker = new maplibregl.Marker({ element: treasureMarkerElement(t.tier, withinRadius) })
      .setLngLat([lng, lat])
      .setPopup(popup)
      .addTo(map);
    treasureMarkers.push(marker);
    bounds.push([lat, lng]);
  }

  if (fitBounds && bounds.length > 0) {
    suppressMapMoveLoad = true;
    if (bounds.length === 1) {
      map.jumpTo({ center: [bounds[0][1], bounds[0][0]], zoom: 15 });
    } else {
      map.fitBounds(boundsFromPoints(bounds), { padding: 36, maxZoom: 16, animate: false });
      // 보물이 넓게 흩어져 있으면 fitBounds가 너무 멀리 빠지므로, 처음 보이는 화면은 최소 이 정도로 가까이 잡는다.
      if (map.getZoom() < MIN_INITIAL_ZOOM) {
        map.setZoom(MIN_INITIAL_ZOOM);
      }
    }
    setTimeout(() => {
      suppressMapMoveLoad = false;
    }, 500);
  }

  setTimeout(() => map.resize(), 80);
}

async function loadTreasuresAt(lat, lng, options = {}) {
  const { fitBounds = false, useGpsDistance = true, showLoading = true } = options;
  const seq = ++mapLoadSeq;
  const map = ensureMap();
  const radiusKm = treasureMap ? radiusKmForMapView(map) : NEARBY_RADIUS_KM;

  if (showLoading) {
    $("treasureList").innerHTML = '<p class="empty">불러오는 중...</p>';
  }

  try {
    const query = new URLSearchParams({
      lat,
      lng,
      radius_km: radiusKm,
      limit: 50,
    });
    const data = await api(`/treasures/nearby?${query}`);
    if (seq !== mapLoadSeq) return; // 더 최신 요청이 있으면 무시

    const items = data.items.map((t) => {
      const storeLat = t.store.lat;
      const storeLng = t.store.lng;
      const distanceMeters =
        useGpsDistance && currentPosition
          ? haversineDistanceMeters(currentPosition.lat, currentPosition.lng, storeLat, storeLng)
          : t.distance_meters;
      return { ...t, distanceMeters };
    });

    // 목록은 내 위치 기준 가까운 순으로 보여준다.
    items.sort((a, b) => a.distanceMeters - b.distanceMeters);

    // 목록 렌더링을 먼저 한다 - 지도 쪽(MapLibre 마커/팝업)에서 뭔가 던지더라도
    // 목록이 "불러오는 중"에 멈춰 있지 않도록.
    renderTreasureList(items, data.total_in_radius, radiusKm);
    try {
      renderTreasureMap(items, { fitBounds });
    } catch (mapErr) {
      console.error("renderTreasureMap failed", mapErr);
    }
  } catch (err) {
    if (seq !== mapLoadSeq) return;
    $("treasureList").innerHTML = "";
    $("mapError").textContent = `주변 보물을 불러오지 못했습니다: ${err.message || err}`;
    $("mapError").classList.remove("hidden");
  }
}

async function loadTreasures() {
  $("mapError").classList.add("hidden");
  $("treasureList").innerHTML = '<p class="empty">불러오는 중...</p>';
  ensureMap();

  try {
    const pos = await getCurrentPosition();
    currentPosition = { lat: pos.coords.latitude, lng: pos.coords.longitude };
    await loadTreasuresAt(currentPosition.lat, currentPosition.lng, {
      fitBounds: true,
      useGpsDistance: true,
      showLoading: false,
    });
  } catch (err) {
    $("treasureList").innerHTML = "";
    $("mapError").textContent = `위치 정보를 가져오지 못했습니다: ${err.message || err}`;
    $("mapError").classList.remove("hidden");
  }
}

const TREASURE_LIST_LIMIT = 5;

function renderTreasureList(treasures, totalInRadius, radiusKm = NEARBY_RADIUS_KM) {
  const container = $("treasureList");
  container.innerHTML = "";

  if (treasures.length === 0) {
    container.innerHTML = `<p class="empty">이 지도 범위(약 ${radiusKm.toFixed(1)}km) 안에 보물이 없습니다.</p>`;
    return;
  }

  // 목록은 가장 가까운 순으로 최대 5곳만 보여준다 (지도 마커는 그대로 다 보인다).
  const shown = treasures.slice(0, TREASURE_LIST_LIMIT);
  const total = totalInRadius && totalInRadius > treasures.length ? totalInRadius : treasures.length;

  if (total > shown.length) {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = `지도 주변 ${total}곳 중 가까운 ${shown.length}곳 (거리는 내 위치 기준)`;
    container.appendChild(note);
  }

  for (const t of shown) {
    const withinRadius = t.distanceMeters <= VISIT_RADIUS_METERS;
    const el = document.createElement("div");
    el.className = "item-card";
    el.innerHTML = `
      <div class="item-header">
        <span class="tier-badge${t.tier === "rare" ? " tier-rare" : ""}">${t.tier === "rare" ? "⭐ 레어" : "🏅 일반"}${t.award_points ? ` · ${t.award_points}P` : ""}</span>
        <span class="distance">${Math.round(t.distanceMeters)}m</span>
      </div>
      <div class="store-name">${treasurePlaceName(t.store)}</div>
      ${treasurePlaceSub(t.store) ? `<div class="store-address">${treasurePlaceSub(t.store)}</div>` : ""}
      <button class="visit-button" ${withinRadius ? "" : "disabled"}>
        ${withinRadius ? "보물 캐러가기" : "매장 근처로 이동하세요"}
      </button>
    `;
    el.querySelector(".visit-button").addEventListener("click", () => {
      if (withinRadius) startVisit(t.store);
    });
    container.appendChild(el);
  }
}

// ---------------------------------------------------------------------------
// 방문 인증
// ---------------------------------------------------------------------------
async function startVisit(store) {
  showScreen("visit");
  $("visitStoreName").textContent = treasurePlaceName(store);
  $("visitResult").classList.add("hidden");
  $("visitProgressFill").parentElement.classList.remove("hidden");
  $("visitProgressText").classList.remove("hidden");
  $("visitCancelBtn").classList.remove("hidden");
  $("visitProgressFill").style.width = "0%";
  $("visitProgressText").textContent = `0 / ${DWELL_SECONDS}초`;

  try {
    const deviceId = getOrCreateDeviceId();
    const session = await api("/visit-sessions", {
      method: "POST",
      body: JSON.stringify({ store_id: store.id, device_id: deviceId }),
    });

    visitState = { sessionId: session.id, store, elapsedSeconds: 0, timerId: null };

    const pushSample = async () => {
      const pos = await getCurrentPosition();
      currentPosition = { lat: pos.coords.latitude, lng: pos.coords.longitude };
      await api(`/visit-sessions/${visitState.sessionId}/samples`, {
        method: "POST",
        body: JSON.stringify({
          lat: pos.coords.latitude,
          lng: pos.coords.longitude,
          accuracy: pos.coords.accuracy ?? 9999,
          is_mock: false,
        }),
      });
    };

    // 시작 직후 1회 샘플을 먼저 보낸다.
    try {
      await pushSample();
    } catch {
      // 첫 샘플 실패는 이후 주기에서 재시도한다.
    }

    visitState.timerId = setInterval(async () => {
      try {
        await pushSample();
      } catch {
        // 개별 샘플 전송 실패는 무시하고 다음 주기에 재시도한다.
      }

      visitState.elapsedSeconds += SAMPLE_INTERVAL_MS / 1000;
      const progress = Math.min(1, visitState.elapsedSeconds / DWELL_SECONDS);
      $("visitProgressFill").style.width = `${progress * 100}%`;
      $("visitProgressText").textContent = `${Math.min(
        visitState.elapsedSeconds,
        DWELL_SECONDS
      )} / ${DWELL_SECONDS}초`;

      if (visitState.elapsedSeconds >= DWELL_SECONDS) {
        completeVisit();
      }
    }, SAMPLE_INTERVAL_MS);
  } catch (err) {
    alert(`인증 세션을 시작하지 못했습니다: ${err.message || err}`);
    showScreen("map");
  }
}

async function completeVisit() {
  if (!visitState) return;
  clearInterval(visitState.timerId);
  const sessionId = visitState.sessionId;

  $("visitProgressText").textContent = "위치 정보를 검증하는 중...";

  try {
    const result = await api(`/visit-sessions/${sessionId}/complete`, { method: "POST" });
    renderVisitResult(result);
  } catch (err) {
    alert(`인증 처리 중 오류가 발생했습니다: ${err.message || err}`);
    showScreen("map");
  } finally {
    visitState = null;
  }
}

function renderVisitResult(result) {
  const { evaluation, point_ledger_entry, claimed_treasure } = result;
  const approved = evaluation.status === "auto_approved";
  const pending = evaluation.status === "pending_review";
  const status = approved ? "approved" : pending ? "pending" : "rejected";

  $("visitProgressFill").parentElement.classList.add("hidden");
  $("visitProgressText").classList.add("hidden");
  $("visitCancelBtn").classList.add("hidden");
  $("visitResult").classList.remove("hidden");
  $("visitResult").className = `result-${status}`;

  $("visitResultEmoji").textContent = approved ? "🎉" : pending ? "🕵️" : "😥";
  $("visitResultTitle").textContent = approved
    ? "보물을 획득했습니다!"
    : pending
      ? "관리자 검토 대기 중입니다"
      : "인증에 실패했습니다";

  const noteEl = $("visitResultNote");
  if (noteEl) {
    noteEl.textContent = pending
      ? "제출한 위치 정보 중 확인이 필요한 부분이 있어요. SKT 총괄 담당자가 검토해서 승인하면 포인트가 지급됩니다."
      : "";
    noteEl.classList.toggle("hidden", !pending);
  }

  $("visitResultPoints").textContent = point_ledger_entry ? `+${point_ledger_entry.points} 포인트` : "";
  const tierEl = $("visitResultTier");
  tierEl.textContent = claimed_treasure ? (claimed_treasure.tier === "rare" ? "⭐ 레어 보물" : "🏅 일반 보물") : "";
  tierEl.className = claimed_treasure && claimed_treasure.tier === "rare" ? "tier-badge tier-rare" : "tier-badge";
  $("visitResultScore").textContent = `신뢰도 점수: ${Math.round(evaluation.score)}`;
  $("visitResultReasons").textContent = formatReasons(evaluation.reasons);
}

// ---------------------------------------------------------------------------
// 포인트 / 리워드
// ---------------------------------------------------------------------------
function rankMedal(rank) {
  return { 1: "🥇", 2: "🥈", 3: "🥉" }[rank] || `${rank}.`;
}

function renderRankList(containerId, rows, emptyText, lineFn) {
  const container = $(containerId);
  container.innerHTML = "";
  if (!rows.length) {
    container.innerHTML = `<p class="empty rank-empty">${emptyText}</p>`;
    return;
  }
  for (const row of rows) {
    const el = document.createElement("div");
    el.className = "rank-row" + (row.is_me ? " rank-me" : "");
    el.innerHTML = lineFn(row);
    container.appendChild(el);
  }
}

async function loadRankings() {
  const data = await api("/stats/rankings");
  renderRankList(
    "dealerRankList",
    data.dealers || [],
    "아직 포인트가 쌓인 대리점이 없습니다.",
    (row) =>
      `<span>${rankMedal(row.rank)} ${row.name}</span><span class="ledger-points">${row.total_points}P</span>`
  );
  renderRankList(
    "repRankList",
    data.reps || [],
    "아직 포인트를 받은 영업사원이 없습니다.",
    (row) =>
      `<span>${rankMedal(row.rank)} ${row.dealer_name} · ${row.name_masked}${row.is_me ? " (나)" : ""}</span><span class="ledger-points">${row.total_points}P</span>`
  );
}

async function loadRewardsScreen() {
  const data = await api(`/points/${rep.id}`);
  $("totalPoints").textContent = `${data.total}P`;
  const rankEl = $("dealerRankLine");
  if (rankEl) {
    const hasRank = data.dealer_rank && data.dealer_rep_count;
    rankEl.classList.toggle("hidden", !hasRank);
    rankEl.textContent = hasRank
      ? `${rep.dealer_name ? `${rep.dealer_name} ` : ""}내 순위 ${data.dealer_rank}위 (${data.dealer_rep_count}명 중)`
      : "";
  }
  const used = Number(data.used || 0);
  const balanceEl = $("pointBalance");
  if (balanceEl) {
    // 리워드로 쓴 포인트가 있을 때만 사용/잔액을 보여준다.
    balanceEl.classList.toggle("hidden", used <= 0);
    balanceEl.textContent = used > 0 ? `사용 ${used}P · 사용 가능 ${data.balance}P` : "";
  }
  await loadRankings();

  const container = $("ledgerList");
  container.innerHTML = "";
  if (data.ledgers.length === 0) {
    container.innerHTML = '<p class="empty">아직 적립 내역이 없습니다.</p>';
    return;
  }
  for (const l of data.ledgers) {
    const row = document.createElement("div");
    row.className = "ledger-row";
    const label = l.reason.startsWith("VISIT_VERIFIED:") ? l.reason.slice("VISIT_VERIFIED:".length) : l.reason;
    row.innerHTML = `
      <div class="ledger-main">
        <div class="ledger-date">${formatCompactDateTime(l.created_at)}</div>
        <div class="ledger-reason">${label}</div>
      </div>
      <span class="ledger-points">+${l.points}P</span>
    `;
    container.appendChild(row);
  }
}

async function handleChangePassword() {
  const currentPassword = $("currentPassword").value;
  const newPassword = $("newPassword").value;
  const confirmPassword = $("newPasswordConfirm").value;
  const msg = $("passwordMessage");
  msg.textContent = "";
  msg.classList.remove("error");

  if (!currentPassword || !newPassword || !confirmPassword) {
    msg.textContent = "현재/새 비밀번호를 모두 입력해주세요.";
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

  try {
    await api("/auth/change-password", {
      method: "POST",
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    });
    $("currentPassword").value = "";
    $("newPassword").value = "";
    $("newPasswordConfirm").value = "";
    rep.using_initial_password = false;
    rep.must_change_password = false;
    saveRep(rep);
    $("passwordHint").classList.add("hidden");
    msg.textContent = "비밀번호가 변경되었습니다.";
  } catch (err) {
    msg.textContent = err.message || "비밀번호 변경에 실패했습니다.";
    msg.classList.add("error");
  }
}

// 비밀번호 재설정: 고유ID(SWING ID) + 등록된 전화번호가 맞으면 바로 새 비밀번호를 정한다.
function toggleResetBox() {
  const box = $("resetBox");
  const willShow = box.classList.contains("hidden");
  box.classList.toggle("hidden", !willShow);
  $("forgotBtn").textContent = willShow ? "닫기" : "비밀번호를 잊으셨나요?";
  if (willShow) {
    $("resetCode").value = $("loginCode").value.trim();
    $("resetMessage").textContent = "";
    $("resetMessage").classList.remove("error");
  }
}

async function handleResetPassword() {
  const msg = $("resetMessage");
  const employeeCode = $("resetCode").value.trim();
  const phone = $("resetPhone").value.trim();
  const newPassword = $("resetPassword").value;
  const confirmPassword = $("resetPasswordConfirm").value;
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

  const btn = $("resetBtn");
  btn.disabled = true;
  msg.textContent = "확인 중...";
  try {
    const res = await fetch(appUrl("/api/auth/reset-password"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ employee_code: employeeCode, phone, new_password: newPassword }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || `재설정 실패 (${res.status})`);
    ["resetPhone", "resetPassword", "resetPasswordConfirm"].forEach((id) => {
      $(id).value = "";
    });
    $("loginCode").value = employeeCode;
    $("loginPassword").value = "";
    toggleResetBox();
    const loginError = $("loginError");
    loginError.textContent = "비밀번호를 바꿨습니다. 새 비밀번호로 로그인해주세요.";
    loginError.classList.remove("hidden");
  } catch (err) {
    msg.textContent = err.message || "재설정에 실패했습니다.";
    msg.classList.add("error");
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// 이벤트 바인딩 / 초기화
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  $("loginBtn").addEventListener("click", handleLogin);
  $("loginPassword").addEventListener("keydown", (e) => {
    if (e.key === "Enter") handleLogin();
  });
  $("loginCode").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("loginPassword").focus();
  });
  $("forgotBtn").addEventListener("click", toggleResetBox);
  $("resetBtn").addEventListener("click", handleResetPassword);
  $("resetPasswordConfirm").addEventListener("keydown", (e) => {
    if (e.key === "Enter") handleResetPassword();
  });
  $("logoutBtn").addEventListener("click", handleLogout);
  $("refreshBtn").addEventListener("click", loadTreasures);
  $("changePasswordBtn").addEventListener("click", handleChangePassword);
  $("visitDoneBtn").addEventListener("click", () => {
    showScreen("map");
    loadTreasures();
  });
  $("visitCancelBtn").addEventListener("click", () => {
    if (visitState) clearInterval(visitState.timerId);
    visitState = null;
    showScreen("map");
  });

  document.querySelectorAll("[data-nav]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.getAttribute("data-nav");
      showScreen(target);
      if (target === "map") loadTreasures();
      if (target === "rewards") loadRewardsScreen();
    });
  });

  // 인증 방식이 바뀔 때 1회만 재로그인 유도 (v3: 로그인 토큰 도입)
  const AUTH_VERSION = "v3-token";
  if (localStorage.getItem("rs_auth_version") !== AUTH_VERSION) {
    clearRep();
    setRepToken("");
    localStorage.setItem("rs_auth_version", AUTH_VERSION);
  }

  rep = loadStoredRep();
  if (rep && !getRepToken()) {
    // 토큰 없이 저장된 예전 로그인 정보는 쓸 수 없다.
    clearRep();
    rep = null;
  }
  if (rep) {
    enterApp();
  } else {
    showScreen("login");
  }
});
