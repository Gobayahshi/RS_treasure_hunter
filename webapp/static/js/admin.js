const TOKEN_KEY = "rs_admin_token";

let plantMap = null;
let plantMarker = null;
let geocodeTimer = null;
let inventoryMap = null;
let inventoryMarkers = null;
let inventoryMeMarker = null;
let inventoryRegion = "";
let inventoryDealer = "";
let lastInventoryData = null;
let adminCanEdit = false; // SKT 총괄만 true. SKT 직원은 조회 전용.
let allReps = [];
let activeAdminTab = "ops";

function $(id) {
  return document.getElementById(id);
}

function appUrl(path) {
  const base = (window.APP_BASE || "").replace(/\/$/, "");
  if (!path.startsWith("/")) path = `/${path}`;
  return `${base}${path}`;
}

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
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
  if (res.status === 401 && path !== "/admin/login") {
    setToken("");
    showLoggedOut();
    throw new Error((data && data.message) || "관리자 로그인이 필요합니다.");
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
  if (res.status === 204) return null;
  return data;
}

function hasCoords(s) {
  return Number(s.lat) !== 0 || Number(s.lng) !== 0;
}

function showLoggedOut() {
  $("screen-admin-login").classList.remove("hidden");
  $("admin-app").classList.add("hidden");
  $("adminNav").classList.add("hidden");
  if (geocodeTimer) {
    clearInterval(geocodeTimer);
    geocodeTimer = null;
  }
}

function showAdminTab(tab) {
  activeAdminTab = tab;
  document.querySelectorAll("#admin-app > section.screen[data-tab]").forEach((el) => {
    el.classList.toggle("tab-hidden", el.dataset.tab !== tab);
  });
  document.querySelectorAll("#adminTabs [data-tab-btn]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tabBtn === tab);
  });
  // 지도가 숨겨진 탭에서 그려지면 크기가 0으로 굳으므로, 그 탭으로 돌아올 때 다시 맞춘다.
  if (tab === "ops" && plantMap) setTimeout(() => plantMap.invalidateSize(), 80);
}

function showLoggedIn(user) {
  const info = user || {};
  // 예전 서버 응답에는 role 이 없다. 그때는 총괄로 본다.
  adminCanEdit = info.can_edit !== false;
  $("screen-admin-login").classList.add("hidden");
  $("admin-app").classList.remove("hidden");
  $("adminNav").classList.remove("hidden");
  const roleLabel = adminCanEdit ? "SKT 총괄" : "SKT 직원 · 조회 전용";
  $("adminUser").textContent = info.username ? `${info.username}님 (${roleLabel})` : "";
  document.querySelectorAll("[data-super-only]").forEach((el) => {
    el.classList.toggle("hidden", !adminCanEdit);
  });
  if ($("viewOnlyNotice")) $("viewOnlyNotice").classList.toggle("hidden", adminCanEdit);
  showAdminTab(activeAdminTab);
  if (adminCanEdit) ensurePlantMap();
  ensureInventoryMap();
}

async function handleAdminLogin() {
  const username = $("adminUsername").value.trim();
  const password = $("adminPassword").value;
  const err = $("adminLoginError");
  err.classList.add("hidden");
  if (!username || !password) {
    err.textContent = "아이디와 비밀번호를 입력해주세요.";
    err.classList.remove("hidden");
    return;
  }
  try {
    const data = await api("/admin/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    setToken(data.token);
    $("adminPassword").value = "";
    showLoggedIn(data);
    await reloadAll();
    startGeocodePolling();
  } catch (e) {
    err.textContent = e.message || "로그인에 실패했습니다.";
    err.classList.remove("hidden");
  }
}

async function handleAdminLogout() {
  try {
    await api("/admin/logout", { method: "POST" });
  } catch (_) {
    /* 토큰이 이미 만료돼도 화면은 나간다 */
  }
  setToken("");
  showLoggedOut();
}

function setPlantLatLng(lat, lng, pan = true) {
  $("plantLat").value = Number(lat).toFixed(6);
  $("plantLng").value = Number(lng).toFixed(6);
  const map = ensurePlantMap();
  const pos = [Number(lat), Number(lng)];
  if (plantMarker) {
    plantMarker.setLatLng(pos);
  } else {
    plantMarker = L.marker(pos).addTo(map);
  }
  if (pan) map.setView(pos, Math.max(map.getZoom(), 16));
}

function ensurePlantMap() {
  if (plantMap) {
    setTimeout(() => plantMap.invalidateSize(), 80);
    return plantMap;
  }
  const el = $("plantMap");
  if (!el) return null;
  plantMap = L.map("plantMap").setView([37.5665, 126.978], 13);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
  }).addTo(plantMap);
  plantMap.on("click", (e) => setPlantLatLng(e.latlng.lat, e.latlng.lng, false));
  setTimeout(() => plantMap.invalidateSize(), 80);
  return plantMap;
}

function stockPinColor(point) {
  const days = point && point.max_hold_days;
  if (days != null && days >= 30) return "#dc2626";
  if (days != null && days >= 15) return "#d97706";
  return "#2563eb";
}

function stockIcon(point, nearest = false) {
  const qty = typeof point === "number" ? point : (point && point.qty) || 0;
  const shared = point && point.shared;
  const cls = `stock-pin${nearest ? " nearest" : ""}${shared ? " shared" : ""}`;
  const code = escHtml((point && point.store_code) || "");
  const name = escHtml((point && point.name) || "");
  const caption = code || name ? `<div class="stock-caption"><span class="stock-code">${code}</span> ${name}</div>` : "";
  return L.divIcon({
    className: "stock-marker",
    html: `<div class="stock-marker-inner"><div class="${cls}" style="background:${stockPinColor(point)}">${qty}</div>${caption}</div>`,
    iconSize: [220, 36],
    iconAnchor: [16, 18],
    popupAnchor: [0, -18],
  });
}

function offsetOverlaps(points) {
  const groups = new Map();
  for (const p of points) {
    const key = `${Number(p.lat).toFixed(5)},${Number(p.lng).toFixed(5)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  }
  const out = [];
  for (const group of groups.values()) {
    group.forEach((p, i) => {
      const copy = { ...p };
      if (group.length > 1) {
        const angle = (2 * Math.PI * i) / group.length;
        const dist = 0.00022;
        copy.lat = Number(p.lat) + dist * Math.cos(angle);
        copy.lng = Number(p.lng) + dist * Math.sin(angle);
      }
      out.push(copy);
    });
  }
  return out;
}

function ensureInventoryMap() {
  if (inventoryMap) {
    setTimeout(() => inventoryMap.invalidateSize(), 80);
    return inventoryMap;
  }
  const el = $("inventoryMap");
  if (!el) return null;
  inventoryMap = L.map("inventoryMap").setView([37.5, 126.9], 10);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
  }).addTo(inventoryMap);
  inventoryMarkers = L.layerGroup().addTo(inventoryMap);
  setTimeout(() => inventoryMap.invalidateSize(), 80);
  return inventoryMap;
}

function formatKm(meters) {
  if (meters == null) return "";
  if (meters < 1000) return `${Math.round(meters)}m`;
  return `${(meters / 1000).toFixed(1)}km`;
}

function renderInventoryMap(data) {
  const map = ensureInventoryMap();
  if (!map || !inventoryMarkers) return;
  inventoryMarkers.clearLayers();
  if (inventoryMeMarker) {
    map.removeLayer(inventoryMeMarker);
    inventoryMeMarker = null;
  }
  const nearestCode = data.nearest && data.nearest.store_code;
  const points = offsetOverlaps(data.points || []);
  const bounds = [];
  let nearestMarker = null;
  for (const p of points) {
    const isNearest = nearestCode && p.store_code === nearestCode;
    const marker = L.marker([p.lat, p.lng], { icon: stockIcon(p, isNearest) });
    const hold = p.max_hold_days != null
      ? ` · 최장 ${p.max_hold_days}일${p.aged_qty ? ` · 30일+ ${p.aged_qty}대` : ""}`
      : "";
    const dist = p.distance_meters != null ? `<div class="distance">내 위치에서 ${formatKm(p.distance_meters)}</div>` : "";
    const addr = [p.address, p.detail_address].filter(Boolean).join(" ");
    const dealers = (p.dealers || [])
      .map((d) => `${escHtml(d.dealer_name)} ${d.qty}대`)
      .join(" · ");
    marker.bindPopup(
      `<div class="map-popup">
        <div class="store-name"><span class="store-code">${escHtml(p.store_code)}</span> ${escHtml(p.name)}</div>
        <div class="muted small">${escHtml(addr || "주소 없음")}</div>
        <div class="distance">${escHtml(data.model)} ${p.qty}대${hold}</div>
        ${dealers ? `<div class="muted small">${dealers}</div>` : ""}
        ${dist}
      </div>`
    );
    marker.addTo(inventoryMarkers);
    bounds.push([p.lat, p.lng]);
    if (isNearest) nearestMarker = marker;
  }
  if (data.origin) {
    inventoryMeMarker = L.circleMarker([data.origin.lat, data.origin.lng], {
      radius: 8,
      color: "#1d4ed8",
      weight: 2,
      fillColor: "#3b82f6",
      fillOpacity: 0.95,
    })
      .bindPopup("내 위치")
      .addTo(map);
    bounds.push([data.origin.lat, data.origin.lng]);
  }
  if (nearestMarker) {
    map.setView([data.nearest.lat, data.nearest.lng], 14);
    nearestMarker.openPopup();
  } else if (bounds.length === 1) {
    map.setView(bounds[0], 14);
  } else if (bounds.length > 1) {
    map.fitBounds(bounds, { padding: [28, 28], maxZoom: 13 });
  }
  setTimeout(() => map.invalidateSize(), 80);
}

function inventoryQueryParams(extra) {
  const model = ($("inventoryModel").value || "SM-F971").trim();
  const params = new URLSearchParams({ model });
  if (inventoryRegion) params.set("region", inventoryRegion);
  if (inventoryDealer) params.set("dealer_id", inventoryDealer);
  if (extra) {
    Object.entries(extra).forEach(([k, v]) => {
      if (v != null && v !== "") params.set(k, String(v));
    });
  }
  return params;
}

function setInventoryQueryResult(data) {
  const box = $("inventoryQueryResult");
  if (!box) return;
  const parts = [];
  if (data.regions && data.regions.length) {
    const seoul = data.regions.find((r) => r.region === "서울");
    if (seoul) parts.push(`서울 ${seoul.qty}대 / ${seoul.stores}곳`);
    if (inventoryRegion) {
      parts.push(`선택 지역(${inventoryRegion}) ${data.mapped_qty}대 / ${data.points.length}곳`);
    }
  }
  if (data.nearest) {
    parts.push(
      `가장 가까운 곳: ${data.nearest.name} (${data.nearest.store_code}) ${formatKm(data.nearest.distance_meters)} · ${data.nearest.qty}대`
    );
  }
  box.textContent = parts.join(" · ");
}

async function loadInventoryMap(extra) {
  if (!$("inventoryMap")) return;
  const summary = $("inventorySummary");
  const unmappedBox = $("inventoryUnmapped");
  try {
    const params = inventoryQueryParams(extra);
    const data = await api(`/inventory/map?${params.toString()}`);
    if (extra && extra.lat != null) {
      data.origin = { lat: Number(extra.lat), lng: Number(extra.lng) };
    }
    lastInventoryData = data;
    const asOf = data.as_of_date ? ` · 기준일 ${data.as_of_date}` : "";
    if (!data.total_qty && !(data.regions || []).length) {
      summary.textContent = "올린 재고현황이 없습니다. 위에서 엑셀을 올려주세요.";
      unmappedBox.innerHTML = "";
      setInventoryQueryResult(data);
      renderInventoryMap({ points: [], model: data.model });
      return;
    }
    const regionLabel = inventoryRegion ? `${inventoryRegion} ` : "판매점 ";
    const dealerBit = (data.dealer_totals || []).map((d) => `${d.dealer_name} ${d.qty}대`).join(" · ");
    const sharedBit = data.shared_store_count ? ` · 공유 판매점 ${data.shared_store_count}곳` : "";
    summary.textContent = `${data.model} ${regionLabel}${data.mapped_qty}대 / ${data.points.length}곳${asOf}` +
      (dealerBit ? ` · ${dealerBit}` : "") +
      sharedBit +
      (data.unmapped_qty ? ` · 좌표 없음 ${data.unmapped_qty}대 / ${data.unmapped.length}곳` : "");
    setInventoryQueryResult(data);
    renderInventoryDealerChips(data);
    renderInventoryMap(data);
    unmappedBox.innerHTML = "";
    for (const u of data.unmapped || []) {
      const el = document.createElement("div");
      el.className = "item-card";
      el.innerHTML = `<div class="store-name">${escHtml(u.name)} <span class="muted">(${escHtml(u.store_code)})</span></div>
        <div class="muted small">주신 주소 리스트(주소.xlsx)에 이 P코드가 없어 지도에 못 올렸습니다 · ${escHtml(data.model)} ${u.qty}대</div>`;
      unmappedBox.appendChild(el);
    }
  } catch (err) {
    summary.textContent = String(err.message || err);
  }
}

function handleInventoryRegionClick(ev) {
  const btn = ev.target.closest("[data-region]");
  if (!btn) return;
  inventoryRegion = btn.getAttribute("data-region") || "";
  document.querySelectorAll("#inventoryRegionChips .chip").forEach((el) => {
    el.classList.toggle("active", el === btn);
  });
  loadInventoryMap();
}

function renderInventoryDealerChips(data) {
  const box = $("inventoryDealerChips");
  if (!box) return;
  const totals = data.dealer_totals || [];
  box.innerHTML = "";
  if (!totals.length) return;
  const all = document.createElement("button");
  all.type = "button";
  all.className = `chip${inventoryDealer ? "" : " active"}`;
  all.setAttribute("data-dealer", "");
  all.textContent = "대리점 전체";
  box.appendChild(all);
  for (const d of totals) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `chip${inventoryDealer === d.dealer_id ? " active" : ""}`;
    btn.setAttribute("data-dealer", d.dealer_id || "");
    btn.textContent = `${d.dealer_name} ${d.qty}`;
    box.appendChild(btn);
  }
}

function handleInventoryDealerClick(ev) {
  const btn = ev.target.closest("[data-dealer]");
  if (!btn) return;
  inventoryDealer = btn.getAttribute("data-dealer") || "";
  document.querySelectorAll("#inventoryDealerChips .chip").forEach((el) => {
    el.classList.toggle("active", el === btn);
  });
  loadInventoryMap();
}

function handleInventoryNearest() {
  const msg = $("inventoryQueryResult");
  if (!navigator.geolocation) {
    msg.textContent = "이 브라우저는 위치 정보를 지원하지 않습니다.";
    return;
  }
  msg.textContent = "현재 위치를 확인하는 중...";
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      loadInventoryMap({
        lat: pos.coords.latitude,
        lng: pos.coords.longitude,
      });
    },
    (err) => {
      msg.textContent = `위치 조회 실패: ${err.message}`;
    },
    { enableHighAccuracy: true, timeout: 12000 }
  );
}

async function handleInventoryImport() {
  const input = $("inventoryFile");
  const msg = $("inventoryImportMessage");
  if (!input.files.length) {
    msg.textContent = "재고현황 xlsx 파일을 선택해주세요.";
    return;
  }
  const form = new FormData();
  form.append("file", input.files[0]);
  msg.textContent = "재고 엑셀을 읽는 중...";
  try {
    const data = await api("/inventory/excel", { method: "POST", body: form });
    const partner = (data.by_holder_type && data.by_holder_type.partner) || 0;
    msg.textContent = `업로드 완료: ${data.dealer_name || ""} ${data.row_count}행 (판매점 ${partner}대, 기준일 ${data.as_of_date || "-"}). 지도는 재고 화면에서 확인하세요.`;
    if ($("inventoryMap")) await loadInventoryMap();
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

function handlePlantUseMyLocation() {
  const msg = $("plantMessage");
  if (!navigator.geolocation) {
    msg.textContent = "이 브라우저는 위치 정보를 지원하지 않습니다.";
    return;
  }
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      setPlantLatLng(pos.coords.latitude, pos.coords.longitude);
      msg.textContent = "현재 위치를 지정했습니다.";
    },
    (err) => {
      msg.textContent = `위치 조회 실패: ${err.message}`;
    }
  );
}

async function handlePlantTreasure() {
  const msg = $("plantMessage");
  const name = $("plantName").value.trim();
  const lat = parseFloat($("plantLat").value);
  const lng = parseFloat($("plantLng").value);
  const points = parseInt($("plantPoints").value, 10);
  if (Number.isNaN(lat) || Number.isNaN(lng)) {
    msg.textContent = "지도에서 위치를 지정하거나 위도/경도를 입력해주세요.";
    return;
  }
  if (!Number.isFinite(points) || points < 1) {
    msg.textContent = "포인트는 1 이상이어야 합니다.";
    return;
  }
  msg.textContent = "심는 중...";
  try {
    await api("/admin/treasures/plant", {
      method: "POST",
      body: JSON.stringify({ name, lat, lng, points }),
    });
    msg.textContent = `${points}P 보물을 심었습니다. 영업사원 지도에 바로 보입니다.`;
    $("plantName").value = "";
    await loadPlanted();
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

async function loadPlanted() {
  const container = $("plantedList");
  const items = await api("/admin/treasures");
  container.innerHTML = "";
  if (!items.length) {
    container.innerHTML = '<p class="empty">아직 관리자가 심은 보물이 없습니다.</p>';
    return;
  }
  for (const t of items) {
    const claimed = Boolean(t.claimed_at);
    const el = document.createElement("div");
    el.className = "item-card";
    el.innerHTML = `
      <div class="store-name">${escHtml(t.store_name || "관리자 보물")} ${claimed ? '<span class="muted small">획득됨</span>' : ""}</div>
      <div class="muted small">${(t.store_address || "").startsWith("ADMIN/") ? `${Number(t.lat).toFixed(5)}, ${Number(t.lng).toFixed(5)}` : escHtml(t.store_address || "")}</div>
      <div class="muted small">lat ${Number(t.lat).toFixed(5)}, lng ${Number(t.lng).toFixed(5)} · ${t.award_points}P</div>
      ${
        claimed || !adminCanEdit
          ? ""
          : `<div class="row planted-actions">
              <input type="number" min="1" value="${t.award_points}" data-points />
              <button class="btn-secondary compact" data-save>포인트 저장</button>
              <button class="btn-secondary compact" data-remove>회수</button>
            </div>`
      }
    `;
    const saveBtn = el.querySelector("[data-save]");
    const removeBtn = el.querySelector("[data-remove]");
    if (saveBtn) {
      saveBtn.addEventListener("click", async () => {
        const next = parseInt(el.querySelector("[data-points]").value, 10);
        try {
          await api(`/admin/treasures/${t.id}`, {
            method: "PATCH",
            body: JSON.stringify({ points: next }),
          });
          await loadPlanted();
        } catch (err) {
          $("plantMessage").textContent = String(err.message || err);
        }
      });
    }
    if (removeBtn) {
      removeBtn.addEventListener("click", async () => {
        if (!confirm("이 보물을 회수할까요? 지도에서 사라집니다.")) return;
        try {
          await api(`/admin/treasures/${t.id}`, { method: "DELETE" });
          await loadPlanted();
        } catch (err) {
          $("plantMessage").textContent = String(err.message || err);
        }
      });
    }
    container.appendChild(el);
  }
}

function formatVisitTime(iso) {
  // 서버는 UTC로 저장한다. 한국 시간으로 보여준다.
  if (!iso) return "";
  const date = new Date(`${iso}${/[zZ+]/.test(iso) ? "" : "Z"}`);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("ko-KR", { timeZone: "Asia/Seoul", hour12: false });
}

async function loadReviewQueue() {
  const container = $("reviewList");
  if (!container) return;
  const data = await api("/admin/visit-sessions?status=pending_review");
  const items = data.items || [];
  container.innerHTML = "";
  if (!items.length) {
    container.innerHTML = '<p class="empty">검토 대기 중인 방문이 없습니다.</p>';
    return;
  }
  for (const s of items) {
    const el = document.createElement("div");
    el.className = "item-card";
    const reasons = (s.reasons || []).map((r) => escHtml(r.label)).join(", ") || "사유 없음";
    const distance =
      s.first_sample_distance_m === null || s.first_sample_distance_m === undefined
        ? "거리 정보 없음"
        : `매장에서 ${s.first_sample_distance_m}m`;
    el.innerHTML = `
      <div class="store-name">${escHtml(s.store_name || "")} <span class="muted small">${escHtml(s.address || "")}</span></div>
      <div class="muted small">
        ${escHtml(s.rep_name || "")} (${escHtml(s.employee_code || "")}) · ${escHtml(s.dealer_name || "소속 없음")}
      </div>
      <div class="muted small">
        ${escHtml(formatVisitTime(s.ended_at || s.started_at))} · 점수 ${escHtml(s.confidence_score ?? "-")}점 ·
        ${escHtml(distance)} · 위치 ${escHtml(s.sample_count ?? 0)}건
      </div>
      <div class="muted small">사유: ${reasons}</div>
      ${
        adminCanEdit
          ? `<div class="row planted-actions">
               <button class="btn-secondary compact" data-approve>승인 (포인트 지급)</button>
               <button class="btn-secondary compact" data-reject>반려</button>
             </div>`
          : ""
      }
    `;
    const decide = async (decision) => {
      const label = decision === "approve" ? "승인" : "반려";
      if (!confirm(`이 방문을 ${label}할까요?`)) return;
      try {
        const res = await api(`/admin/visit-sessions/${s.id}/review`, {
          method: "POST",
          body: JSON.stringify({ decision }),
        });
        const points = res.point_ledger_entry ? `${res.point_ledger_entry.points}P 지급` : "포인트 지급 없음";
        const detail = decision === "approve" ? res.note || points : "포인트 없음";
        $("reviewMessage").textContent = `${label} 완료 · ${detail}`;
        await Promise.all([loadReviewQueue(), loadLeaderboard()]);
      } catch (err) {
        $("reviewMessage").textContent = String(err.message || err);
      }
    };
    const approveBtn = el.querySelector("[data-approve]");
    const rejectBtn = el.querySelector("[data-reject]");
    if (approveBtn) approveBtn.addEventListener("click", () => decide("approve"));
    if (rejectBtn) rejectBtn.addEventListener("click", () => decide("reject"));
    container.appendChild(el);
  }
}

async function loadSettings() {
  const data = await api("/admin/settings");
  $("pointsNormal").value = data.points_normal;
  $("pointsRare").value = data.points_rare;
}

async function handleSavePoints() {
  const msg = $("pointsMessage");
  const points_normal = parseInt($("pointsNormal").value, 10);
  const points_rare = parseInt($("pointsRare").value, 10);
  try {
    await api("/admin/settings", {
      method: "POST",
      body: JSON.stringify({ points_normal, points_rare }),
    });
    msg.textContent = "기본 포인트를 저장했습니다.";
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

async function handleStatsDownload() {
  const msg = $("statsMessage");
  msg.textContent = "만드는 중...";
  try {
    const res = await fetch(appUrl("/api/admin/stats.xlsx"), { headers: authHeaders() });
    if (res.status === 401) {
      setToken("");
      showLoggedOut();
      throw new Error("관리자 로그인이 필요합니다.");
    }
    if (!res.ok) throw new Error("통계 파일을 만들지 못했습니다.");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    // toISOString()은 UTC라 한국 시간 오전에는 어제 날짜가 된다. 로컬 날짜를 쓴다.
    const now = new Date();
    const stamp = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(
      now.getDate()
    ).padStart(2, "0")}`;
    a.download = `RS_Treasure_stats_${stamp}.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    msg.textContent = "다운로드했습니다.";
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

async function handleTemplateDownload() {
  const msg = $("importMessage");
  try {
    const res = await fetch(appUrl("/api/import/template"), { headers: authHeaders() });
    if (!res.ok) throw new Error("양식을 받지 못했습니다.");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "RS_Treasure_master_template.xlsx";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

async function handleChangeAdminPassword() {
  const msg = $("adminPasswordMessage");
  const currentPassword = $("adminCurrentPassword").value;
  const newPassword = $("adminNewPassword").value;
  const confirmPassword = $("adminNewPasswordConfirm").value;
  if (!currentPassword || !newPassword) {
    msg.textContent = "현재 비밀번호와 새 비밀번호를 입력해주세요.";
    return;
  }
  if (newPassword !== confirmPassword) {
    msg.textContent = "새 비밀번호가 서로 다릅니다.";
    return;
  }
  try {
    const data = await api("/admin/change-password", {
      method: "POST",
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    });
    $("adminCurrentPassword").value = "";
    $("adminNewPassword").value = "";
    $("adminNewPasswordConfirm").value = "";
    msg.textContent = data.message || "변경되었습니다.";
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

async function loadDealers() {
  const dealers = await api("/dealers");
  const container = $("dealerList");
  container.innerHTML = "";
  if (dealers.length === 0) {
    container.innerHTML = '<p class="empty">등록된 대리점이 없습니다. 엑셀을 올려주세요.</p>';
    return;
  }
  for (const d of dealers) {
    const el = document.createElement("div");
    el.className = "item-card";
    el.innerHTML = `<div class="store-name">${escHtml(d.name)}</div><div class="muted small">대리점ID: ${escHtml(d.dealer_code)}</div>`;
    container.appendChild(el);
  }
}

async function loadReps() {
  allReps = await api("/reps");
  renderReps();
}

function renderReps() {
  const container = $("repList");
  container.innerHTML = "";
  if (allReps.length === 0) {
    container.innerHTML = '<p class="empty">등록된 영업사원이 없습니다. 엑셀을 올려주세요.</p>';
    return;
  }
  const q = String(($("repSearch") || {}).value || "")
    .replace(/\s+/g, "")
    .toLowerCase();
  const rows = allReps.filter((r) => {
    if (!q) return true;
    return `${r.name}${r.employee_code}${r.dealer_name || ""}${r.dealer_code || ""}`
      .replace(/\s+/g, "")
      .toLowerCase()
      .includes(q);
  });
  if (!rows.length) {
    container.innerHTML = '<p class="empty">검색 결과가 없습니다.</p>';
    return;
  }
  for (const r of rows) {
    const el = document.createElement("div");
    el.className = "item-card";
    const hasDealer = Boolean(r.dealer_id);
    const role = r.dealer_role === "manager" ? "manager" : "staff";
    el.innerHTML = `
      <div class="between" style="margin-top:0">
        <div>
          <div class="store-name">${escHtml(r.name)}</div>
          <div class="muted small">고유ID: ${escHtml(r.employee_code)} · ${escHtml(r.dealer_name || "소속대리점 없음")} (${escHtml(r.dealer_code || "-")})</div>
          <div class="muted small">
            ${
              r.has_phone
                ? `전화번호 ${escHtml(r.phone_masked)}`
                : "전화번호 미등록 · 본인 비밀번호 재설정 불가"
            }${r.must_change_password ? " · 초기 비밀번호 사용 중" : ""}${
              r.password_reset_at ? ` · 재설정 ${escHtml(formatVisitTime(r.password_reset_at))}` : ""
            }
          </div>
        </div>
        ${
          hasDealer
            ? `<select data-dealer-role aria-label="대리점 관리자/직원 구분">
                 <option value="staff"${role === "staff" ? " selected" : ""}>대리점 직원</option>
                 <option value="manager"${role === "manager" ? " selected" : ""}>대리점 관리자</option>
               </select>`
            : '<span class="muted small">재고 화면 사용 불가</span>'
        }
      </div>
    `;
    const select = el.querySelector("[data-dealer-role]");
    if (select) {
      select.addEventListener("change", async () => {
        const next = select.value;
        select.disabled = true;
        try {
          const updated = await api(`/reps/${r.id}/dealer-role`, {
            method: "PATCH",
            body: JSON.stringify({ dealer_role: next }),
          });
          r.dealer_role = updated.dealer_role;
          $("repMessage").textContent = `${r.name}님을 ${next === "manager" ? "대리점 관리자" : "대리점 직원"}로 바꿨습니다.`;
        } catch (err) {
          select.value = role;
          $("repMessage").textContent = String(err.message || err);
        } finally {
          select.disabled = false;
        }
      });
    }
    container.appendChild(el);
  }
}

async function handleCreateTestAccounts() {
  const msg = $("testAccountMessage");
  msg.textContent = "만드는 중...";
  try {
    const data = await api("/admin/test-accounts", { method: "POST" });
    msg.textContent = `대리점 ${data.dealer_count}곳 · 새로 만든 계정 ${data.created}개 (이미 있던 계정 ${data.already}개). ${data.note}`;
    await loadReps();
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

async function handleDeleteTestAccounts() {
  const msg = $("testAccountMessage");
  if (!confirm("테스트 계정을 모두 지웁니다. 그 계정의 방문·포인트 기록도 함께 사라집니다. 계속할까요?")) return;
  msg.textContent = "지우는 중...";
  try {
    const data = await api("/admin/test-accounts", { method: "DELETE" });
    msg.textContent = `테스트 계정 ${data.removed}개를 삭제했습니다.`;
    await loadReps();
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

async function loadAccounts() {
  const container = $("accountList");
  if (!container || !adminCanEdit) return;
  const accounts = await api("/admin/accounts");
  container.innerHTML = "";
  for (const a of accounts) {
    const el = document.createElement("div");
    el.className = "item-card";
    const isStaff = a.role === "staff";
    el.innerHTML = `
      <div class="between" style="margin-top:0">
        <div>
          <div class="store-name">${escHtml(a.username)}</div>
          <div class="muted small">${isStaff ? "SKT 직원 · 조회 전용" : "SKT 총괄"}</div>
        </div>
        ${isStaff ? '<button class="btn-secondary compact" data-remove>삭제</button>' : ""}
      </div>
    `;
    const removeBtn = el.querySelector("[data-remove]");
    if (removeBtn) {
      removeBtn.addEventListener("click", async () => {
        if (!confirm(`SKT 직원 계정 '${a.username}'을 삭제할까요? 바로 로그아웃됩니다.`)) return;
        try {
          await api(`/admin/accounts/${a.id}`, { method: "DELETE" });
          $("accountMessage").textContent = `'${a.username}' 계정을 삭제했습니다.`;
          await loadAccounts();
        } catch (err) {
          $("accountMessage").textContent = String(err.message || err);
        }
      });
    }
    container.appendChild(el);
  }
}

async function handleCreateAccount() {
  const username = $("accountUsername").value.trim();
  const password = $("accountPassword").value;
  const msg = $("accountMessage");
  if (!username || !password) {
    msg.textContent = "아이디와 초기 비밀번호를 입력해주세요.";
    return;
  }
  try {
    await api("/admin/accounts", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    $("accountUsername").value = "";
    $("accountPassword").value = "";
    msg.textContent = `'${username}' SKT 직원 계정을 만들었습니다. 초기 비밀번호를 본인에게 전달하세요.`;
    await loadAccounts();
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

let storeSearchTimer = null;

function handleStoreSearchInput() {
  if (storeSearchTimer) clearTimeout(storeSearchTimer);
  storeSearchTimer = setTimeout(() => loadStores($("storeSearch").value.trim()), 300);
}

async function loadStores(q = "") {
  const container = $("storeList");
  const data = await api(`/stores?limit=50${q ? `&q=${encodeURIComponent(q)}` : ""}`);
  container.innerHTML = "";
  const summary = document.createElement("p");
  summary.className = "muted small";
  summary.textContent = q
    ? `"${q}" 검색 결과 ${data.matched}곳 중 ${data.items.length}곳 표시 (전체 판매점 ${data.total}곳)`
    : `전체 ${data.total}곳 중 최근 ${data.items.length}곳. 검색어를 입력하면 전체에서 찾습니다.`;
  container.appendChild(summary);
  if (data.items.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = q ? "검색 결과가 없습니다." : "등록된 판매점이 없습니다.";
    container.appendChild(empty);
    return;
  }
  for (const s of data.items) {
    const el = document.createElement("div");
    el.className = "item-card";
    const coordText = hasCoords(s)
      ? `lat: ${s.lat}, lng: ${s.lng}`
      : "좌표 없음 — 주소 변환 필요";
    el.innerHTML = `
      <div class="store-name">${escHtml(s.name)}${s.store_code ? ` <span class="muted">(${escHtml(s.store_code)})</span>` : ""}</div>
      <div class="store-address">${escHtml([s.address, s.detail_address].filter(Boolean).join(" "))}</div>
      <div class="muted small">${escHtml(s.dealer_name || "소속대리점 없음")} (${escHtml(s.dealer_code || "-")}) · ${coordText}</div>
    `;
    container.appendChild(el);
  }
}

async function loadGeocodeStatus() {
  const data = await api("/stores/geocode/status");
  $("geocodeProgressFill").style.width = `${data.percent}%`;

  const log = data.log || {};
  let summary = `전체 ${data.total_stores}개 중 좌표 완료 ${data.geocoded_stores}개, 남음 ${data.missing_stores}개 (${data.percent}%)`;
  if (data.is_complete) {
    summary += " · 완료";
  } else if (log.last_progress) {
    summary += ` · 최근 진행 ${log.last_progress.attempted}/${log.last_progress.total} (성공 ${log.last_progress.filled}, 실패 ${log.last_progress.failed})`;
  }
  $("geocodeStatusSummary").textContent = summary;

  const lines = [];
  if (log.last_done) {
    lines.push(`마지막 완료: ${log.last_done.provider} / 성공 ${log.last_done.filled} / 실패 ${log.last_done.failed} / 시도 ${log.last_done.attempted}`);
  }
  if (log.last_lines && log.last_lines.length) {
    lines.push(...log.last_lines);
  } else {
    lines.push("아직 진행 로그가 없습니다.");
  }
  $("geocodeStatusLog").textContent = lines.join("\n");
}

async function loadLeaderboard() {
  const rows = await api("/points");
  const container = $("leaderboard");
  container.innerHTML = "";
  if (rows.length === 0) {
    container.innerHTML = '<p class="empty">등록된 사원이 없습니다.</p>';
    return;
  }
  rows.forEach((r, idx) => {
    const el = document.createElement("div");
    el.className = "rank-row";
    el.innerHTML = `<span>${idx + 1}. ${escHtml(r.name)} (${escHtml(r.employee_code)})</span><span class="ledger-points">${r.total_points}P</span>`;
    container.appendChild(el);
  });
}

async function reloadAll() {
  await Promise.all([
    loadDealers(),
    loadReps(),
    loadStores(),
    loadLeaderboard(),
    loadGeocodeStatus(),
    loadSettings(),
    loadPlanted(),
    loadReviewQueue(),
    loadAccounts(),
    $("inventoryMap") ? loadInventoryMap() : Promise.resolve(),
  ]);
}

async function handleAddStore() {
  const name = $("storeName").value.trim();
  const storeCode = $("storeCode").value.trim();
  const address = $("storeAddress").value.trim();
  const detailAddress = $("storeDetailAddress").value.trim();
  const dealerCode = $("storeDealerCode").value.trim();
  const lat = parseFloat($("storeLat").value);
  const lng = parseFloat($("storeLng").value);
  const msg = $("storeMessage");

  if (!storeCode || !name || !address || Number.isNaN(lat) || Number.isNaN(lng)) {
    msg.textContent = "판매점코드, 판매점명, 기본주소, 위도, 경도를 입력해주세요.";
    return;
  }

  try {
    await api("/stores", {
      method: "POST",
      body: JSON.stringify({
        store_code: storeCode,
        name,
        address,
        detail_address: detailAddress || undefined,
        lat,
        lng,
        dealer_code: dealerCode || undefined,
      }),
    });
    msg.textContent = "등록되었습니다.";
    $("storeCode").value = "";
    $("storeName").value = "";
    $("storeAddress").value = "";
    $("storeDetailAddress").value = "";
    $("storeDealerCode").value = "";
    $("storeLat").value = "";
    $("storeLng").value = "";
    loadStores();
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

function handleUseMyLocation() {
  const msg = $("storeMessage");
  if (!navigator.geolocation) {
    msg.textContent = "이 브라우저는 위치 정보를 지원하지 않습니다.";
    return;
  }
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      $("storeLat").value = pos.coords.latitude.toFixed(6);
      $("storeLng").value = pos.coords.longitude.toFixed(6);
      msg.textContent = "현재 위치를 채웠습니다.";
    },
    (err) => {
      msg.textContent = `위치 조회 실패: ${err.message}`;
    }
  );
}

async function handleGeocode() {
  const msg = $("spawnMessage");
  msg.textContent = "주소로 좌표를 변환하는 중입니다. 매장이 많으면 오래 걸릴 수 있습니다.";
  try {
    const result = await api("/stores/geocode", { method: "POST" });
    const failHint = result.failed_count ? ` / 실패 ${result.failed_count}건` : "";
    msg.textContent = `좌표 변환 완료: ${result.filled}/${result.attempted}건 (${result.provider})${failHint}`;
    await Promise.all([loadStores(), loadGeocodeStatus()]);
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

async function handleSpawn() {
  const msg = $("spawnMessage");
  msg.textContent = "실행 중...";
  try {
    const result = await api("/treasures/spawn", { method: "POST" });
    msg.textContent = `${result.spawned}개 주소에 새 보물을 스폰했습니다. (같은 주소는 한 곳, 좌표 없는 매장은 제외)`;
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

function formatImportSummary(summary) {
  const lines = [];
  for (const key of ["dealers", "reps", "stores"]) {
    const label = { dealers: "대리점", reps: "영업사원", stores: "판매점" }[key];
    const s = summary[key];
    lines.push(
      `${label}: 신규 ${s.created} / 수정 ${s.updated} / 건너뜀 ${s.skipped}` +
        `${s.removed ? ` / 삭제 ${s.removed}` : ""}${s.duplicate_codes ? ` / 중복코드 ${s.duplicate_codes}` : ""}`
    );
    (s.errors || []).slice(0, 20).forEach((e) => lines.push(`  - ${e}`));
    if ((s.errors || []).length > 20) lines.push(`  - ...외 ${s.errors.length - 20}건`);
  }
  if (summary.unknown_sheets && summary.unknown_sheets.length) {
    lines.push(`분류 못 한 시트: ${summary.unknown_sheets.join(", ")}`);
  }
  if (summary.geocode) {
    const g = summary.geocode;
    if (g.note) lines.push(g.note);
    if (g.provider && g.provider !== "background") {
      lines.push(`좌표 변환(${g.provider}): ${g.filled}/${g.attempted}건 성공`);
    }
    (g.failed || []).slice(0, 10).forEach((e) => lines.push(`  - 실패: ${e}`));
    if (g.failed_count > 10) lines.push(`  - ...외 ${g.failed_count - 10}건 실패`);
  }
  return lines.join("\n");
}

async function handleImport() {
  const input = $("excelFiles");
  const msg = $("importMessage");
  const resultBox = $("importResult");
  if (!input.files.length) {
    msg.textContent = "xlsx 파일을 선택해주세요.";
    return;
  }

  const form = new FormData();
  for (const file of input.files) {
    form.append("files", file);
  }

  const removeMissing = $("removeMissingReps");
  if (removeMissing && removeMissing.checked) {
    // 되돌릴 수 없는 삭제라 한 번 더 확인한다.
    if (!confirm("이 파일에 없는 영업사원을 삭제합니다. 그 사람들의 방문·포인트 기록도 함께 사라지고 되돌릴 수 없습니다. 계속할까요?")) {
      return;
    }
    form.append("remove_missing_reps", "1");
  }

  msg.textContent = "올리는 중... 주소로 좌표를 변환하므로 매장이 많으면 시간이 걸릴 수 있습니다.";
  resultBox.classList.add("hidden");
  try {
    const data = await api("/import/excel", { method: "POST", body: form });
    msg.textContent = "업로드가 완료되었습니다.";
    resultBox.textContent = formatImportSummary(data);
    resultBox.classList.remove("hidden");
    await reloadAll();
  } catch (err) {
    msg.textContent = String(err.message || err);
  }
}

function startGeocodePolling() {
  if (geocodeTimer) clearInterval(geocodeTimer);
  geocodeTimer = window.setInterval(() => {
    loadGeocodeStatus().catch(() => {});
  }, 15000);
}

async function restoreSession() {
  if (!getToken()) {
    showLoggedOut();
    return;
  }
  try {
    const me = await api("/admin/me");
    showLoggedIn(me);
    await reloadAll();
    startGeocodePolling();
  } catch (_) {
    setToken("");
    showLoggedOut();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("#adminTabs [data-tab-btn]").forEach((btn) => {
    btn.addEventListener("click", () => showAdminTab(btn.dataset.tabBtn));
  });
  $("adminLoginBtn").addEventListener("click", handleAdminLogin);
  $("adminPassword").addEventListener("keydown", (e) => {
    if (e.key === "Enter") handleAdminLogin();
  });
  $("adminUsername").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("adminPassword").focus();
  });
  $("adminLogoutBtn").addEventListener("click", handleAdminLogout);
  $("plantTreasureBtn").addEventListener("click", handlePlantTreasure);
  $("plantUseMyLocationBtn").addEventListener("click", handlePlantUseMyLocation);
  $("savePointsBtn").addEventListener("click", handleSavePoints);
  $("statsDownloadBtn").addEventListener("click", handleStatsDownload);
  $("templateBtn").addEventListener("click", handleTemplateDownload);
  $("adminChangePasswordBtn").addEventListener("click", handleChangeAdminPassword);
  $("addStoreBtn").addEventListener("click", handleAddStore);
  $("useMyLocationBtn").addEventListener("click", handleUseMyLocation);
  $("spawnBtn").addEventListener("click", handleSpawn);
  $("geocodeBtn").addEventListener("click", handleGeocode);
  $("refreshGeocodeStatusBtn").addEventListener("click", loadGeocodeStatus);
  if ($("refreshReviewBtn")) $("refreshReviewBtn").addEventListener("click", loadReviewQueue);
  if ($("refreshAccountsBtn")) $("refreshAccountsBtn").addEventListener("click", loadAccounts);
  if ($("createAccountBtn")) $("createAccountBtn").addEventListener("click", handleCreateAccount);
  if ($("repSearch")) $("repSearch").addEventListener("input", renderReps);
  if ($("createTestAccountsBtn")) $("createTestAccountsBtn").addEventListener("click", handleCreateTestAccounts);
  if ($("deleteTestAccountsBtn")) $("deleteTestAccountsBtn").addEventListener("click", handleDeleteTestAccounts);
  if ($("storeSearch")) $("storeSearch").addEventListener("input", handleStoreSearchInput);
  $("importBtn").addEventListener("click", handleImport);
  $("inventoryImportBtn").addEventListener("click", handleInventoryImport);
  if ($("inventoryMapBtn")) $("inventoryMapBtn").addEventListener("click", () => loadInventoryMap());
  if ($("inventoryNearestBtn")) $("inventoryNearestBtn").addEventListener("click", handleInventoryNearest);
  if ($("inventoryRegionChips")) $("inventoryRegionChips").addEventListener("click", handleInventoryRegionClick);
  if ($("inventoryDealerChips")) $("inventoryDealerChips").addEventListener("click", handleInventoryDealerClick);
  if ($("inventoryModel")) {
    $("inventoryModel").addEventListener("keydown", (e) => {
      if (e.key === "Enter") loadInventoryMap();
    });
  }
  restoreSession();
});
