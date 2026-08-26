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
let suppressMapMoveLoad = false;
let mapMoveTimer = null;
let mapLoadSeq = 0;

// ---------------------------------------------------------------------------
// 유틸
// ---------------------------------------------------------------------------
function $(id) {
  return document.getElementById(id);
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

async function api(path, options) {
  const res = await fetch(appUrl(`/api${path}`), {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${path} 실패: ${res.status} ${text}`);
  }
  return res.json();
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
  clearRep();
  rep = null;
  showScreen("login");
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

    L.circle([currentPosition.lat, currentPosition.lng], {
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
        <span class="tier-badge">${t.tier === "rare" ? "⭐ 레어" : "🏅 일반"}${t.award_points ? ` · ${t.award_points}P` : ""}</span>
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
      body: JSON.stringify({ rep_id: rep.id, store_id: store.id, device_id: deviceId }),
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

  $("visitProgressFill").parentElement.classList.add("hidden");
  $("visitProgressText").classList.add("hidden");
  $("visitCancelBtn").classList.add("hidden");
  $("visitResult").classList.remove("hidden");

  $("visitResultEmoji").textContent = approved ? "🎉" : pending ? "🕵️" : "😥";
  $("visitResultTitle").textContent = approved
    ? "보물을 획득했습니다!"
    : pending
      ? "관리자 검토 대기 중입니다"
      : "인증에 실패했습니다";

  $("visitResultPoints").textContent = point_ledger_entry ? `+${point_ledger_entry.points} 포인트` : "";
  $("visitResultTier").textContent = claimed_treasure
    ? claimed_treasure.tier === "rare"
      ? "⭐ 레어 보물"
      : "🏅 일반 보물"
    : "";
  $("visitResultScore").textContent = `신뢰도 점수: ${Math.round(evaluation.score)}`;
  $("visitResultReasons").textContent = evaluation.reasons.join(", ");
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
  const data = await api(`/stats/rankings?rep_id=${encodeURIComponent(rep.id)}`);
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
    row.innerHTML = `<span>${l.reason}</span><span class="ledger-points">+${l.points}P</span>`;
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
    const res = await fetch(appUrl("/api/auth/change-password"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rep_id: rep.id,
        current_password: currentPassword,
        new_password: newPassword,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.message || data.error || `변경 실패 (${res.status})`);
    }
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

  // 비밀번호 기능 도입 시 1회만 재로그인 유도
  const AUTH_VERSION = "v2-password";
  if (localStorage.getItem("rs_auth_version") !== AUTH_VERSION) {
    clearRep();
    localStorage.setItem("rs_auth_version", AUTH_VERSION);
  }

  rep = loadStoredRep();
  if (rep) {
    enterApp();
  } else {
    showScreen("login");
  }
});
