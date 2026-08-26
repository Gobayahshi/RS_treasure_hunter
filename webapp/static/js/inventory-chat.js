const TOKEN_KEY = "rs_inventory_token";

let chatMap = null;
let chatMarkers = null;
let chatMeMarker = null;
let pendingQuestion = "";
let areaMode = false;
let areaDrawing = false;
let areaStart = null;
let areaLast = null;
let areaRect = null;
let areaBounds = null;
let areaBoundOnce = false;
let inventoryUser = {
  username: "",
  role: "super",
  dealer_id: "",
  dealer_name: "",
  can_see_all: true,
};

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
  if (res.status === 401) {
    setToken("");
    showLoggedOut();
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message || "대리점 로그인이 필요합니다.");
  }
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    const text = data ? data.message || data.error || JSON.stringify(data) : await res.text();
    throw new Error(text || `API ${path} 실패: ${res.status}`);
  }
  return res.json();
}

function showLoggedOut() {
  $("screen-chat-login").classList.remove("hidden");
  $("chat-app").classList.add("hidden");
  $("chatNav").classList.add("hidden");
}

function showLoggedIn(user) {
  inventoryUser = user || inventoryUser;
  $("screen-chat-login").classList.add("hidden");
  $("chat-app").classList.remove("hidden");
  $("chatNav").classList.remove("hidden");
  const name = inventoryUser.dealer_name || inventoryUser.username || "";
  $("chatUser").textContent = name ? `${name}` : "";
  const adminLink = $("chatAdminLink");
  if (adminLink) adminLink.classList.toggle("hidden", !inventoryUser.can_see_all);
  document.querySelectorAll("[data-all-dealers]").forEach((el) => {
    el.classList.toggle("hidden", !inventoryUser.can_see_all);
  });
  ensureChatMap();
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
  return L.divIcon({
    className: "stock-marker",
    html: `<div class="${cls}" style="background:${stockPinColor(point)}">${qty}</div>`,
    iconSize: [32, 32],
    iconAnchor: [16, 16],
    popupAnchor: [0, -16],
  });
}

function formatKm(meters) {
  if (meters == null) return "";
  if (meters < 1000) return `${Math.round(meters)}m`;
  return `${(meters / 1000).toFixed(1)}km`;
}

function ensureChatMap() {
  if (chatMap) {
    setTimeout(() => chatMap.invalidateSize(), 80);
    return chatMap;
  }
  chatMap = L.map("chatMap").setView([37.5, 126.9], 10);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
  }).addTo(chatMap);
  chatMarkers = L.layerGroup().addTo(chatMap);
  bindAreaDraw(chatMap);
  setTimeout(() => chatMap.invalidateSize(), 120);
  return chatMap;
}

function setAreaMode(on) {
  areaMode = !!on;
  const btn = $("areaSelectBtn");
  const pane = document.querySelector(".inventory-map-pane");
  if (!btn || !chatMap) return;
  if (areaMode) {
    btn.classList.add("active");
    btn.textContent = "드래그해서 영역을 그리세요";
    pane.classList.add("is-drawing");
    chatMap.dragging.disable();
    chatMap.boxZoom.disable();
  } else {
    btn.classList.remove("active");
    btn.textContent = "영역 선택";
    pane.classList.remove("is-drawing");
    chatMap.dragging.enable();
    chatMap.boxZoom.enable();
  }
}

function clearArea() {
  areaDrawing = false;
  areaStart = null;
  areaBounds = null;
  if (areaRect && chatMap) {
    chatMap.removeLayer(areaRect);
    areaRect = null;
  }
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
    if (!areaMode) return;
    L.DomEvent.preventDefault(e.originalEvent);
    areaDrawing = true;
    areaStart = e.latlng;
    areaLast = e.latlng;
    if (areaRect) map.removeLayer(areaRect);
    areaRect = L.rectangle(L.latLngBounds(areaStart, areaStart), {
      color: "#7c3aed",
      weight: 2,
      fillColor: "#8b5cf6",
      fillOpacity: 0.12,
    }).addTo(map);
  });
  map.on("mousemove", (e) => {
    if (!areaDrawing || !areaRect || !areaStart) return;
    areaLast = e.latlng;
    areaRect.setBounds(L.latLngBounds(areaStart, e.latlng));
  });
  const finish = () => {
    if (!areaDrawing || !areaRect || !areaStart) return;
    areaDrawing = false;
    const end = areaLast || areaStart;
    areaRect.setBounds(L.latLngBounds(areaStart, end));
    const b = areaRect.getBounds();
    if (b.getSouth() === b.getNorth() || b.getWest() === b.getEast()) {
      setAreaMode(false);
      return;
    }
    areaBounds = {
      south: b.getSouth(),
      west: b.getWest(),
      north: b.getNorth(),
      east: b.getEast(),
    };
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
  if (chatMap) setTimeout(() => chatMap.invalidateSize(), 80);
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
  if (chatMap) setTimeout(() => chatMap.invalidateSize(), 80);
}

async function applyAreaBounds(bbox) {
  try {
    const params = new URLSearchParams({
      model: "SM-F971,SM-A175N,SM-S931N",
      south: String(bbox.south),
      west: String(bbox.west),
      north: String(bbox.north),
      east: String(bbox.east),
    });
    const data = await api(`/inventory/map?${params}`);
    renderChatMap(data);
    renderAreaTable(data);
  } catch (err) {
    addBot(String(err.message || err));
  }
}

function renderChatMap(data, origin) {
  const map = ensureChatMap();
  if (!map || !chatMarkers) return;
  chatMarkers.clearLayers();
  if (chatMeMarker) {
    map.removeLayer(chatMeMarker);
    chatMeMarker = null;
  }
  const points = (data && data.points) || [];
  const nearestCode = data && data.nearest && data.nearest.store_code;
  const bounds = [];
  let nearestMarker = null;
  for (const p of points) {
    if (p.lat == null || p.lng == null) continue;
    const isNearest = nearestCode && p.store_code === nearestCode;
    const marker = L.marker([p.lat, p.lng], { icon: stockIcon(p, isNearest) });
    const dist = p.distance_meters != null ? `<div class="distance">${formatKm(p.distance_meters)}</div>` : "";
    const hold = p.max_hold_days != null
      ? ` · 최장 ${p.max_hold_days}일${p.aged_qty ? ` · 30일+ ${p.aged_qty}대` : ""}`
      : "";
    const dealers = (p.dealers || []).map((d) => `${escHtml(d.dealer_name)} ${d.qty}대`).join(" · ");
    const models = (p.models || []).map((m) => `${escHtml(m.model)} ${m.qty}대`).join(" · ");
    const modelLine = models || `${escHtml((data && data.model) || "")} ${p.qty}대`;
    marker.bindPopup(
      `<div class="map-popup"><div class="store-name">${escHtml(p.name)} (${escHtml(p.store_code)})</div>
      <div class="muted small">${escHtml(p.address || "")}</div>
      <div class="distance">${p.qty}대${hold}</div>
      ${models ? `<div class="muted small">${modelLine}</div>` : ""}
      ${dealers ? `<div class="muted small">${dealers}</div>` : ""}${dist}</div>`
    );
    marker.addTo(chatMarkers);
    bounds.push([p.lat, p.lng]);
    if (isNearest) nearestMarker = marker;
  }
  if (origin) {
    chatMeMarker = L.circleMarker([origin.lat, origin.lng], {
      radius: 8,
      color: "#1d4ed8",
      weight: 2,
      fillColor: "#3b82f6",
      fillOpacity: 0.95,
    })
      .bindPopup("내 위치")
      .addTo(map);
    bounds.push([origin.lat, origin.lng]);
  }
  if (nearestMarker && data.nearest) {
    map.setView([data.nearest.lat, data.nearest.lng], 14);
    nearestMarker.openPopup();
  } else if (bounds.length === 1) {
    map.setView(bounds[0], 14);
  } else if (bounds.length > 1) {
    map.fitBounds(bounds, { padding: [28, 28], maxZoom: 13 });
  }
  setTimeout(() => map.invalidateSize(), 80);
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

async function handleInventoryUpload() {
  const input = $("inventoryFile");
  const status = $("inventoryUploadStatus");
  if (!input || !status) return;
  const file = input.files && input.files[0];
  if (!file) {
    status.textContent = "xlsx 파일을 선택해주세요.";
    return;
  }
  const form = new FormData();
  form.append("file", file);
  status.textContent = "올리는 중...";
  try {
    const data = await api("/inventory/excel", { method: "POST", body: form });
    const name = data.dealer_name || inventoryUser.dealer_name || "";
    status.textContent = `${name} ${Number(data.row_count || 0).toLocaleString("ko-KR")}행으로 교체했습니다.`;
    addBot(
      `${name} 재고를 올렸습니다. ${Number(data.row_count || 0).toLocaleString("ko-KR")}행이며, 이 대리점의 이전 재고는 지웠습니다.`
    );
    loadDefaultMap();
  } catch (e) {
    status.textContent = e.message || "업로드에 실패했습니다.";
  }
}

function pickDealer(username, btn) {
  $("chatUsername").value = username;
  document.querySelectorAll(".dealer-pick button").forEach((el) => el.classList.remove("active"));
  if (btn) btn.classList.add("active");
  $("chatPassword").focus();
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
    setToken(data.token);
    $("chatPassword").value = "";
    showLoggedIn(data);
    greet();
  } catch (e) {
    err.textContent = e.message || "로그인에 실패했습니다.";
    err.classList.remove("hidden");
  }
}

async function handleLogout() {
  try {
    await api("/inventory/logout", { method: "POST" });
  } catch (_) {
    /* ignore */
  }
  setToken("");
  showLoggedOut();
}

function greet() {
  $("chatLog").innerHTML = "";
  const dealer = inventoryUser.dealer_name;
  if (dealer && !inventoryUser.can_see_all) {
    addBot(
      `${dealer} 재고만 보여 드립니다. 엑셀을 올리면 이전 재고는 지우고 이번 파일만 남습니다. 지도에서 위치를 보고 오른쪽에 물어보세요. 기종을 안 주시면 지도는 F971·A175N·S931N 기준입니다.`
    );
  } else {
    addBot(
      "재고 현황을 보고 답합니다. 어디에 몇 대인지, 오래 묵은 재고, 대리점 비교, 어디를 먼저 처리할지 물어보세요. 지도에서 「영역 선택」으로 사각형을 그리면 그 안의 기종별 대수도 계산합니다. 기종을 안 주시면 지도는 F971·A175N·S931N 기준입니다."
    );
  }
  loadDefaultMap();
}

async function loadDefaultMap() {
  try {
    const data = await api("/inventory/map?model=SM-F971,SM-A175N,SM-S931N");
    renderChatMap(data);
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
  const el = document.createElement("div");
  el.className = "chat-bubble bot thinking";
  el.id = "chatThinking";
  el.textContent = "찾는 중...";
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function removeThinking() {
  const el = $("chatThinking");
  if (el) el.remove();
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
      renderChatMap(data.map, coords || null);
      if (areaBounds || (data.map && data.map.bbox)) renderAreaTable(data.map);
    }
  } catch (err) {
    removeThinking();
    addBot(String(err.message || err));
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
  if (!getToken()) {
    showLoggedOut();
    return;
  }
  try {
    const me = await api("/inventory/me");
    showLoggedIn(me);
    greet();
  } catch (_) {
    setToken("");
    showLoggedOut();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("chatLoginBtn").addEventListener("click", handleLogin);
  $("pickYuwon").addEventListener("click", (e) => pickDealer("yuwon", e.currentTarget));
  $("pickFrisbee").addEventListener("click", (e) => pickDealer("frisbee", e.currentTarget));
  $("inventoryUploadBtn").addEventListener("click", handleInventoryUpload);
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
    if (areaMode) setAreaMode(false);
    else {
      ensureChatMap();
      setAreaMode(true);
    }
  });
  $("areaClearBtn").addEventListener("click", clearArea);
  $("chatMicBtn").addEventListener("click", () => {
    addBot("음성 질문/답변은 다음 단계에서 붙입니다. 지금은 글로 물어봐 주세요.");
  });
  restore();
});
