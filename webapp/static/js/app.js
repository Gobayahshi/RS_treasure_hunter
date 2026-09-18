// ---------------------------------------------------------------------------
// 상태 / 상수
// ---------------------------------------------------------------------------
const VISIT_RADIUS_METERS = 30;
const NEARBY_RADIUS_KM = 5;
const DWELL_SECONDS = 5; // confidence.py RULES_CONFIG.min_dwell_seconds 와 맞출 것
const SAMPLE_INTERVAL_MS = 1000;

let rep = null;
let currentPosition = null;
let visitState = null; // { sessionId, store, timerId, elapsedSeconds }
let treasureMap = null;
let mapMarkersLayer = null;
let meMarker = null;
let meAccuracyCircle = null;
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
      if (meMarker) meMarker.setLatLng([currentPosition.lat, currentPosition.lng]);
      if (meAccuracyCircle) meAccuracyCircle.setLatLng([currentPosition.lat, currentPosition.lng]);
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
  const dealer = rep.dealer_name ? ` · ${rep.dealer_name}` : "";
  $("repGreeting").textContent = `${rep.name}${dealer}`;
  if (rep.using_initial_password) {
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
function ensureMap() {
  if (treasureMap) {
    setTimeout(() => treasureMap.invalidateSize(), 50);
    return treasureMap;
  }

  treasureMap = L.map("treasureMap", {
    zoomControl: true,
    attributionControl: true,
  }).setView([37.5665, 126.978], 14);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(treasureMap);

  mapMarkersLayer = L.layerGroup().addTo(treasureMap);

  treasureMap.on("moveend", () => {
    if (suppressMapMoveLoad) return;
    if (mapMoveTimer) clearTimeout(mapMoveTimer);
    mapMoveTimer = setTimeout(() => {
      loadTreasuresAt(treasureMap.getCenter().lat, treasureMap.getCenter().lng, {
        fitBounds: false,
        useGpsDistance: true,
      });
    }, 350);
  });

  setTimeout(() => treasureMap.invalidateSize(), 80);
  return treasureMap;
}

function radiusKmForMapView(map) {
  const center = map.getCenter();
  const corner = map.getBounds().getNorthEast();
  const meters = haversineDistanceMeters(center.lat, center.lng, corner.lat, corner.lng);
  return Math.min(50, Math.max(0.8, meters / 1000));
}

function treasureIcon(tier, withinRadius) {
  const color = withinRadius ? "#16a34a" : tier === "rare" ? "#d97706" : "#2563eb";
  const label = tier === "rare" ? "★" : "●";
  return L.divIcon({
    className: "treasure-marker",
    html: `<div class="treasure-pin" style="background:${color}">${label}</div>`,
    iconSize: [28, 28],
    iconAnchor: [14, 14],
    popupAnchor: [0, -14],
  });
}

function renderTreasureMap(treasures, options = {}) {
  const { fitBounds = true } = options;
  const map = ensureMap();
  mapMarkersLayer.clearLayers();

  if (meMarker) {
    map.removeLayer(meMarker);
    meMarker = null;
  }
  meAccuracyCircle = null;

  if (currentPosition) {
    meMarker = L.circleMarker([currentPosition.lat, currentPosition.lng], {
      radius: 9,
      color: "#1d4ed8",
      weight: 2,
      fillColor: "#3b82f6",
      fillOpacity: 0.95,
    })
      .bindPopup("내 위치")
      .addTo(map);

    meAccuracyCircle = L.circle([currentPosition.lat, currentPosition.lng], {
      radius: VISIT_RADIUS_METERS,
      color: "#2563eb",
      weight: 1,
      fillColor: "#93c5fd",
      fillOpacity: 0.15,
    }).addTo(mapMarkersLayer);
  }

  const bounds = [];
  if (currentPosition) bounds.push([currentPosition.lat, currentPosition.lng]);

  for (const t of treasures) {
    const lat = t.store.lat;
    const lng = t.store.lng;
    if (lat == null || lng == null) continue;

    const withinRadius = t.distanceMeters <= VISIT_RADIUS_METERS;
    const marker = L.marker([lat, lng], { icon: treasureIcon(t.tier, withinRadius) });
    const tierLabel = t.tier === "rare" ? "⭐ 레어" : "🏅 일반";
    const pointsLabel = t.award_points ? ` · ${t.award_points}P` : "";
    const actionHtml = withinRadius
      ? `<button type="button" class="map-visit-btn" data-store-id="${t.store.id}">보물 캐러가기</button>`
      : `<p class="muted small">매장 근처(30m)로 이동하세요</p>`;

    marker.bindPopup(
      `<div class="map-popup">
        <strong>${tierLabel}${pointsLabel}</strong>
        <div class="store-name">${treasurePlaceName(t.store)}</div>
        ${treasurePlaceSub(t.store) ? `<div class="muted small">${treasurePlaceSub(t.store)}</div>` : ""}
        <div class="distance">내 위치에서 ${Math.round(t.distanceMeters)}m</div>
        ${actionHtml}
      </div>`
    );

    marker.on("popupopen", () => {
      const btn = document.querySelector(`.map-visit-btn[data-store-id="${t.store.id}"]`);
      if (btn) {
        btn.onclick = () => startVisit(t.store);
      }
    });

    marker.addTo(mapMarkersLayer);
    bounds.push([lat, lng]);
  }

  if (fitBounds && bounds.length > 0) {
    suppressMapMoveLoad = true;
    if (bounds.length === 1) {
      map.setView(bounds[0], 15);
    } else {
      map.fitBounds(bounds, { padding: [36, 36], maxZoom: 16 });
    }
    setTimeout(() => {
      suppressMapMoveLoad = false;
    }, 500);
  }

  setTimeout(() => map.invalidateSize(), 80);
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

    renderTreasureMap(items, { fitBounds });
    renderTreasureList(items, data.total_in_radius, radiusKm);
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

function renderTreasureList(treasures, totalInRadius, radiusKm = NEARBY_RADIUS_KM) {
  const container = $("treasureList");
  container.innerHTML = "";

  if (treasures.length === 0) {
    container.innerHTML = `<p class="empty">이 지도 범위(약 ${radiusKm.toFixed(1)}km) 안에 보물이 없습니다.</p>`;
    return;
  }

  if (totalInRadius && totalInRadius > treasures.length) {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = `지도 주변 ${totalInRadius}곳 중 가까운 ${treasures.length}곳 (거리는 내 위치 기준)`;
    container.appendChild(note);
  }

  for (const t of treasures) {
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
    saveRep(rep);
    $("passwordHint").classList.add("hidden");
    msg.textContent = "비밀번호가 변경되었습니다.";
  } catch (err) {
    msg.textContent = err.message || "비밀번호 변경에 실패했습니다.";
    msg.classList.add("error");
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
