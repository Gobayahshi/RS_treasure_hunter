const TOKEN_KEY = "rs_admin_token";

let plantMap = null;
let plantMarker = null;
let geocodeTimer = null;

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

async function api(path, options = {}) {
  const opts = { ...options };
  const headers = authHeaders(opts.headers || {});
  if (!(opts.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(appUrl(`/api${path}`), { ...opts, headers });
  if (res.status === 401 && path !== "/admin/login") {
    setToken("");
    showLoggedOut();
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message || "관리자 로그인이 필요합니다.");
  }
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    const text = data ? data.message || data.error || JSON.stringify(data) : await res.text();
    throw new Error(text || `API ${path} 실패: ${res.status}`);
  }
  if (res.status === 204) return null;
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return res.json();
  return res;
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

function showLoggedIn(username) {
  $("screen-admin-login").classList.add("hidden");
  $("admin-app").classList.remove("hidden");
  $("adminNav").classList.remove("hidden");
  $("adminUser").textContent = username ? `${username}님` : "";
  ensurePlantMap();
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
    showLoggedIn(data.username);
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
      <div class="store-name">${t.store_name || "관리자 보물"} ${claimed ? '<span class="muted small">획득됨</span>' : ""}</div>
      <div class="muted small">${(t.store_address || "").startsWith("ADMIN/") ? `${Number(t.lat).toFixed(5)}, ${Number(t.lng).toFixed(5)}` : t.store_address || ""}</div>
      <div class="muted small">lat ${Number(t.lat).toFixed(5)}, lng ${Number(t.lng).toFixed(5)} · ${t.award_points}P</div>
      ${
        claimed
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
    a.download = `RS_Treasure_stats_${new Date().toISOString().slice(0, 10)}.xlsx`;
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
    el.innerHTML = `<div class="store-name">${d.name}</div><div class="muted small">대리점ID: ${d.dealer_code}</div>`;
    container.appendChild(el);
  }
}

async function loadReps() {
  const reps = await api("/reps");
  const container = $("repList");
  container.innerHTML = "";
  if (reps.length === 0) {
    container.innerHTML = '<p class="empty">등록된 영업사원이 없습니다. 엑셀을 올려주세요.</p>';
    return;
  }
  for (const r of reps) {
    const el = document.createElement("div");
    el.className = "item-card";
    el.innerHTML = `
      <div class="store-name">${r.name}</div>
      <div class="muted small">고유ID: ${r.employee_code} · ${r.dealer_name || "소속대리점 없음"} (${r.dealer_code || "-"})</div>
    `;
    container.appendChild(el);
  }
}

async function loadStores() {
  const stores = await api("/stores");
  const container = $("storeList");
  container.innerHTML = "";
  if (stores.length === 0) {
    container.innerHTML = '<p class="empty">등록된 판매점이 없습니다.</p>';
    return;
  }
  for (const s of stores) {
    const el = document.createElement("div");
    el.className = "item-card";
    const coordText = hasCoords(s)
      ? `lat: ${s.lat}, lng: ${s.lng}`
      : "좌표 없음 — 주소 변환 필요";
    el.innerHTML = `
      <div class="store-name">${s.name}${s.store_code ? ` <span class="muted">(${s.store_code})</span>` : ""}</div>
      <div class="store-address">${[s.address, s.detail_address].filter(Boolean).join(" ")}</div>
      <div class="muted small">${s.dealer_name || "소속대리점 없음"} (${s.dealer_code || "-"}) · ${coordText}</div>
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
    el.innerHTML = `<span>${idx + 1}. ${r.name} (${r.employee_code})</span><span class="ledger-points">${r.total_points}P</span>`;
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
    lines.push(`${label}: 신규 ${s.created} / 수정 ${s.updated} / 건너뜀 ${s.skipped}${s.duplicate_codes ? ` / 중복코드 ${s.duplicate_codes}` : ""}`);
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
    showLoggedIn(me.username);
    await reloadAll();
    startGeocodePolling();
  } catch (_) {
    setToken("");
    showLoggedOut();
  }
}

document.addEventListener("DOMContentLoaded", () => {
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
  $("importBtn").addEventListener("click", handleImport);
  restoreSession();
});
