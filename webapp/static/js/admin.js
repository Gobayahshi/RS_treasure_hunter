function $(id) {
  return document.getElementById(id);
}

function appUrl(path) {
  const base = (window.APP_BASE || "").replace(/\/$/, "");
  if (!path.startsWith("/")) path = `/${path}`;
  return `${base}${path}`;
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

function hasCoords(s) {
  return Number(s.lat) !== 0 || Number(s.lng) !== 0;
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
  await Promise.all([loadDealers(), loadReps(), loadStores(), loadLeaderboard(), loadGeocodeStatus()]);
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
    msg.textContent = String(err);
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
    msg.textContent = String(err);
  }
}

async function handleSpawn() {
  const msg = $("spawnMessage");
  msg.textContent = "실행 중...";
  try {
    const result = await api("/treasures/spawn", { method: "POST" });
    msg.textContent = `${result.spawned}개 주소에 새 보물을 스폰했습니다. (같은 주소는 한 곳, 좌표 없는 매장은 제외)`;
  } catch (err) {
    msg.textContent = String(err);
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
    const res = await fetch(appUrl("/api/import/excel"), { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || res.statusText);
    }
    msg.textContent = "업로드가 완료되었습니다.";
    resultBox.textContent = formatImportSummary(data);
    resultBox.classList.remove("hidden");
    await reloadAll();
  } catch (err) {
    msg.textContent = String(err);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("addStoreBtn").addEventListener("click", handleAddStore);
  $("useMyLocationBtn").addEventListener("click", handleUseMyLocation);
  $("spawnBtn").addEventListener("click", handleSpawn);
  $("geocodeBtn").addEventListener("click", handleGeocode);
  $("refreshGeocodeStatusBtn").addEventListener("click", loadGeocodeStatus);
  $("importBtn").addEventListener("click", handleImport);
  reloadAll();
  window.setInterval(loadGeocodeStatus, 15000);
});
