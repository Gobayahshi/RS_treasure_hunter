const TOKEN_KEY = "rs_inventory_token";

let chatMap = null;
let chatMarkers = null;
let chatMeMarker = null;
let lastChatMapData = null;
let lastChatOrigin = null;
let storeLabelsOn = null;
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
let lastCoords = null;
let hqDealerId = "";
let modelCatalog = [];
let mapProductShort = "";
let mapModelName = "";
let mapPinColor = "";
let mapAgedOnly = false;
let catalogPicked = false;

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
  const asOf = $("asOfBadge");
  if (asOf) {
    asOf.textContent = "";
    asOf.classList.add("hidden");
  }
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

function stockPinColor(point) {
  if (mapPinColor) return mapPinColor;
  const days = point && point.max_hold_days;
  if (days != null && days >= 30) return "#dc2626";
  if (days != null && days >= 15) return "#d97706";
  return "#2563eb";
}

function storeCaptionHtml(point) {
  const stores = (point && point.stores) || [point];
  const first = stores[0] || {};
  const code = escHtml((first.store_code || point.store_code || "").trim());
  const name = escHtml((first.name || point.name || "").trim());
  const extra = stores.length > 1 ? ` 외 ${stores.length - 1}곳` : "";
  return `<span class="stock-code">${code}</span> ${name}${extra}`;
}

function storeCaptionText(point) {
  const stores = (point && point.stores) || [point];
  const first = stores[0] || {};
  const code = (first.store_code || point.store_code || "").trim();
  const name = (first.name || point.name || "").trim();
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

function stockIcon(point, nearest = false, showLabel = true) {
  const qty = typeof point === "number" ? point : (point && point.qty) || 0;
  const shared = point && point.shared;
  const cls = `stock-pin${nearest ? " nearest" : ""}${shared ? " shared" : ""}`;
  const caption = showLabel && typeof point === "object" && point ? storeCaptionHtml(point) : "";
  return L.divIcon({
    className: "stock-marker",
    html: `<div class="stock-marker-inner"><div class="${cls}" style="background:${stockPinColor(point)}">${qty}</div>${caption ? `<div class="stock-caption">${caption}</div>` : ""}</div>`,
    iconSize: showLabel ? [220, 36] : [32, 32],
    iconAnchor: [16, showLabel ? 18 : 16],
    popupAnchor: [0, -18],
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
  chatMap.on("zoomend", () => {
    const show = chatMap.getZoom() >= 12;
    if (show !== storeLabelsOn && lastChatMapData) {
      renderChatMap(lastChatMapData, lastChatOrigin, true);
    }
  });
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
    const params = mapQueryParams();
    params.set("south", String(bbox.south));
    params.set("west", String(bbox.west));
    params.set("north", String(bbox.north));
    params.set("east", String(bbox.east));
    const data = await api(`/inventory/map?${params}`);
    applyMapMeta(data);
    renderChatMap(data);
    renderAreaTable(data);
  } catch (err) {
    addBot(String(err.message || err));
  }
}

function renderChatMap(data, origin, keepView = false) {
  const map = ensureChatMap();
  if (!map || !chatMarkers) return;
  lastChatMapData = data;
  lastChatOrigin = origin;
  chatMarkers.clearLayers();
  if (chatMeMarker) {
    map.removeLayer(chatMeMarker);
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
    const marker = L.marker([p.lat, p.lng], { icon: stockIcon(p, isNearest, showLabel) });
    const dist = p.distance_meters != null ? `<div class="distance">${formatKm(p.distance_meters)}</div>` : "";
    const hold = p.max_hold_days != null
      ? ` · 최장 ${p.max_hold_days}일${p.aged_qty ? ` · 30일+ ${p.aged_qty}대` : ""}`
      : "";
    const dealers = (p.dealers || []).map((d) => `${escHtml(d.dealer_name)} ${d.qty}대`).join(" · ");
    const models = (p.models || []).map((m) => `${escHtml(m.model)} ${m.qty}대`).join(" · ");
    const storeRows = (p.stores || [p]).map((s) => {
      const sHold = s.max_hold_days != null ? ` · 최장 ${s.max_hold_days}일` : "";
      return `<div class="store-line"><span class="store-code">${escHtml(s.store_code)}</span> ${escHtml(s.name)} <span class="muted">${s.qty}대${sHold}</span></div>`;
    }).join("");
    marker.bindPopup(
      `<div class="map-popup">
      <div class="store-list">${storeRows}</div>
      <div class="muted small">${escHtml(p.address || "")}</div>
      <div class="distance">${p.qty}대${hold}</div>
      ${models ? `<div class="muted small">${models}</div>` : ""}
      ${dealers ? `<div class="muted small">${dealers}</div>` : ""}${dist}</div>`
    );
    if (!showLabel) {
      marker.bindTooltip(storeCaptionText(p), { direction: "right", offset: [16, 0], opacity: 0.95 });
    }
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
  if (keepView) {
    setTimeout(() => map.invalidateSize(), 80);
    return;
  }
  if (origin && bounds.length) {
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
    if (nearestMarker) nearestMarker.openPopup();
  } else if (nearestMarker && data.nearest) {
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
    catalogPicked = false;
    await loadCatalog();
    if (inventoryUser.can_see_all) loadHqSummary();
    const mapData = await loadInventoryMap(lastCoords);
    addBot(describeMap(mapData, lastCoords));
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
    await showLoggedIn(data);
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
      `${dealer} 재고만 보여 드립니다. 엑셀을 올리면 이전 재고는 지우고 이번 파일만 남습니다. 위 드롭다운에서 대표상품명·모델명을 고르면 그 기종만 지도에 나옵니다. 채팅은 질문한 내용 그대로 답합니다.`
    );
  } else {
    addBot(
      "올린 대리점 재고를 함께 봅니다. 왼쪽에서 대리점을 고를 수 있고, 위 드롭다운에서 대표상품명·모델명을 고르면 그 기종만 지도에 나옵니다. 채팅은 질문한 내용 그대로 답합니다."
    );
  }
  requestMyLocation({ announce: true });
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
  if (mapProductShort) params.set("product_short", mapProductShort);
  if (mapModelName) params.set("model_name", mapModelName);
  if (!mapProductShort && !mapModelName) params.set("model", "ALL");
  if (mapAgedOnly) params.set("aged_only", "1");
  if (mapPinColor) params.set("pin_color", mapPinColor);
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
    modelCatalog = data.products || [];
    renderAsOf(data);
    fillProductSelect();
  } catch (_) {
    modelCatalog = [];
    fillProductSelect();
  }
}

function fillProductSelect() {
  const sel = $("productShortSelect");
  if (!sel) return;
  const prev = mapProductShort;
  sel.innerHTML = `<option value="">대표상품명 선택</option>`;
  for (const p of modelCatalog) {
    const opt = document.createElement("option");
    opt.value = p.product_short;
    opt.textContent = `${p.product_short} (${Number(p.qty || 0).toLocaleString("ko-KR")}대)`;
    sel.appendChild(opt);
  }
  if (!catalogPicked && !prev && modelCatalog[0]) {
    mapProductShort = modelCatalog[0].product_short;
    catalogPicked = true;
  } else if (prev && [...sel.options].some((o) => o.value === prev)) {
    mapProductShort = prev;
  } else if (prev && ![...sel.options].some((o) => o.value === prev)) {
    mapProductShort = "";
    mapModelName = "";
  }
  sel.value = mapProductShort;
  fillModelSelect();
}

function fillModelSelect() {
  const sel = $("modelNameSelect");
  if (!sel) return;
  const product = modelCatalog.find((p) => p.product_short === mapProductShort);
  sel.innerHTML = `<option value="">해당 기종 전체</option>`;
  sel.disabled = !product;
  if (!product) {
    mapModelName = "";
    return;
  }
  for (const m of product.models || []) {
    const opt = document.createElement("option");
    opt.value = m.model_name;
    opt.textContent = `${m.model_name} (${Number(m.qty || 0).toLocaleString("ko-KR")}대)`;
    sel.appendChild(opt);
  }
  if (mapModelName && [...sel.options].some((o) => o.value === mapModelName)) {
    sel.value = mapModelName;
  } else {
    mapModelName = "";
    sel.value = "";
  }
}

function resetMapStyle() {
  mapPinColor = "";
  mapAgedOnly = false;
}

function onProductShortChange() {
  mapProductShort = $("productShortSelect").value || "";
  mapModelName = "";
  catalogPicked = true;
  resetMapStyle();
  fillModelSelect();
  loadInventoryMap(lastCoords).then((data) => addBot(describeMap(data, lastCoords))).catch((err) => addBot(String(err.message || err)));
}

function onModelNameChange() {
  mapModelName = $("modelNameSelect").value || "";
  catalogPicked = true;
  resetMapStyle();
  loadInventoryMap(lastCoords).then((data) => addBot(describeMap(data, lastCoords))).catch((err) => addBot(String(err.message || err)));
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
    .then((data) => addBot(describeMap(data, lastCoords)))
    .catch((err) => addBot(String(err.message || err)));
}

function handleHqClick(ev) {
  const filterBtn = ev.target.closest("[data-hq-filter]");
  if (filterBtn) return;
  const btn = ev.target.closest("[data-dealer]");
  if (!btn) return;
  applyHqDealer(btn.getAttribute("data-dealer") || "");
}

function describeMap(data, coords) {
  const mapped = Number((data && data.mapped_qty) || 0);
  const unmapped = Number((data && data.unmapped_qty) || 0);
  const stores = ((data && data.points) || []).length;
  if (!mapped && !unmapped) {
    return "표시할 판매점 재고가 없습니다. 엑셀을 올려 주세요. 판매점(P코드) 재고만 지도에 나옵니다.";
  }
  if (!mapped && unmapped) {
    return `재고 ${unmapped.toLocaleString("ko-KR")}대가 있지만 판매점 좌표가 없어 지도에 찍지 못했습니다.`;
  }
  const nearest = data && data.nearest;
  if (coords && nearest) {
    return `지금 위치 기준 약 20km 안 ${stores}곳, ${mapped.toLocaleString("ko-KR")}대입니다. 가장 가까운 곳은 ${nearest.name} (${formatKm(nearest.distance_meters)}) · ${nearest.qty}대입니다.`;
  }
  return `지도에 판매점 ${stores}곳, ${mapped.toLocaleString("ko-KR")}대를 표시했습니다.`;
}

async function requestMyLocation(options = {}) {
  const announce = !!options.announce;
  const btn = $("myLocationBtn");
  if (btn) btn.classList.add("active");
  if (!navigator.geolocation) {
    if (announce) addBot("이 브라우저는 위치 정보를 지원하지 않아 전체 재고를 표시합니다.");
    await loadInventoryMap(null).then((data) => {
      if (announce) addBot(describeMap(data, null));
    }).catch((err) => addBot(String(err.message || err)));
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
          if (announce) {
            addBot("근처 20km 안에서는 표시할 재고가 없어, 같은 기종을 전국으로 보여 드립니다.");
            addBot(describeMap(nationwide, lastCoords));
          }
          return;
        }
        if (announce) addBot(describeMap(data, lastCoords));
      } catch (err) {
        addBot(String(err.message || err));
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
          addBot(describeMap(data, null));
        }
      } catch (e) {
        addBot(String(e.message || e));
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
    if (inventoryUser.can_see_all && hqDealerId) body.dealer_id = hqDealerId;
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
      mapAgedOnly = !!data.map.aged_only;
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
    await showLoggedIn(me);
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
  const pickJieun = $("pickJieun");
  if (pickJieun) pickJieun.addEventListener("click", (e) => pickDealer("jieun", e.currentTarget));
  const pickAdmin = $("pickAdmin");
  if (pickAdmin) pickAdmin.addEventListener("click", (e) => pickDealer("admin", e.currentTarget));
  $("inventoryUploadBtn").addEventListener("click", handleInventoryUpload);
  const productSel = $("productShortSelect");
  const modelSel = $("modelNameSelect");
  if (productSel) productSel.addEventListener("change", onProductShortChange);
  if (modelSel) modelSel.addEventListener("change", onModelNameChange);
  const hqPanel = $("hqPanel");
  if (hqPanel) hqPanel.addEventListener("click", handleHqClick);
  const hqSearch = $("hqSearch");
  if (hqSearch) hqSearch.addEventListener("input", () => loadHqSummary());
  const sktSel = $("sktDealerSelect");
  if (sktSel) {
    sktSel.addEventListener("change", () => applyHqDealer(sktSel.value || ""));
  }
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
  $("myLocationBtn").addEventListener("click", () => requestMyLocation({ announce: true }));
  $("chatMicBtn").addEventListener("click", () => {
    addBot("음성 질문/답변은 다음 단계에서 붙입니다. 지금은 글로 물어봐 주세요.");
  });
  restore();
});
