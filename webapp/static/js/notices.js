// 보물찾기(app.js)/재고Map(inventory-chat.js)과 같은 localStorage 키를 그대로 쓴다.
// 한 번 로그인하면 다른 화면도 다시 로그인하지 않아도 되게 하기 위해서다.
const REP_TOKEN_KEY = "rs_rep_token";
const ADMIN_TOKEN_KEY = "rs_admin_token";
let activeTokenKind = "";
let noticeUser = { can_see_all: false };

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
  const { admin, rep } = getStoredTokens();
  if (activeTokenKind === "rep") return rep;
  if (activeTokenKind === "admin") return admin;
  return admin || rep;
}

function setToken(token, kind) {
  if (!token) {
    localStorage.removeItem(ADMIN_TOKEN_KEY);
    localStorage.removeItem(REP_TOKEN_KEY);
    activeTokenKind = "";
    return;
  }
  if (kind === "rep") localStorage.setItem(REP_TOKEN_KEY, token);
  else localStorage.setItem(ADMIN_TOKEN_KEY, token);
  activeTokenKind = kind;
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
  if (msg.length > 180) return `${msg.slice(0, 180)}…`;
  return msg;
}

async function api(path, options = {}) {
  const opts = { ...options };
  const headers = { ...(opts.headers || {}) };
  const token = getToken();
  if (token) headers["X-Admin-Token"] = token;
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
    if (activeTokenKind === "rep") localStorage.removeItem(REP_TOKEN_KEY);
    else if (activeTokenKind === "admin") localStorage.removeItem(ADMIN_TOKEN_KEY);
    activeTokenKind = "";
    showLoggedOut();
    throw new Error((data && data.message) || "다시 로그인해주세요.");
  }
  if (res.status === 403 && data && data.error === "NO_DEALER") {
    const err = new Error(data.message);
    err.code = "NO_DEALER";
    throw err;
  }
  if (res.status === 403 && data && data.error === "PASSWORD_CHANGE_REQUIRED") {
    showPasswordChange();
    throw new Error(data.message);
  }
  if (!res.ok) {
    throw new Error((data && data.message) || raw || `요청 실패 (${res.status})`);
  }
  return data;
}

function showLoggedOut() {
  $("screen-notice-login").classList.remove("hidden");
  $("screen-notice-password").classList.add("hidden");
  $("notice-app").classList.add("hidden");
  $("noticeNav").classList.add("hidden");
}

function showPasswordChange() {
  $("screen-notice-login").classList.add("hidden");
  $("screen-notice-password").classList.remove("hidden");
  $("notice-app").classList.add("hidden");
  $("noticeNav").classList.add("hidden");
}

function noticeUserLabel(user) {
  if (!user) return "";
  if (user.role === "dealer") {
    const roleLabel = user.dealer_role === "manager" ? "관리자" : "직원";
    return `${user.dealer_name || ""} · ${user.name || user.username} (${roleLabel})`;
  }
  if (user.role === "staff") return `SKT 직원 · ${user.username}`;
  return `SKT 총괄 · ${user.username}`;
}

async function showLoggedIn(user) {
  noticeUser = user || noticeUser;
  $("screen-notice-password").classList.add("hidden");
  $("screen-notice-login").classList.add("hidden");
  $("notice-app").classList.remove("hidden");
  $("noticeNav").classList.remove("hidden");
  $("noticeUser").textContent = noticeUserLabel(noticeUser);
  const newBtn = $("newNoticeBtn");
  if (newBtn) newBtn.classList.toggle("hidden", !noticeUser.can_see_all);
  await loadNotices();
}

async function loadNotices() {
  const box = $("noticeList");
  box.innerHTML = '<p class="muted small">불러오는 중...</p>';
  try {
    const items = await api("/notices");
    if (!items.length) {
      box.innerHTML = '<p class="empty">등록된 공지가 없습니다.</p>';
      return;
    }
    box.innerHTML = "";
    for (const n of items) {
      const el = document.createElement("div");
      el.className = "item-card notice-card";
      const created = String(n.created_at || "").replace("T", " ").slice(0, 16);
      const canDelete = noticeUser.can_see_all;
      el.innerHTML = `
        <div class="between">
          <strong>${escHtml(n.title)}</strong>
          ${canDelete ? `<button type="button" class="link-btn" style="width:auto;flex:none" data-delete="${escHtml(n.id)}">삭제</button>` : ""}
        </div>
        <p class="notice-body">${escHtml(n.body)}</p>
        <p class="muted small">${escHtml(n.author_name || "")} · ${escHtml(created)}</p>
      `;
      box.appendChild(el);
    }
  } catch (err) {
    box.innerHTML = `<p class="error">${escHtml(friendlyError(err))}</p>`;
  }
}

async function handleNoticeListClick(e) {
  const btn = e.target.closest("[data-delete]");
  if (!btn) return;
  const id = btn.getAttribute("data-delete");
  if (!confirm("이 공지를 삭제할까요? 되돌릴 수 없습니다.")) return;
  try {
    await api(`/notices/${encodeURIComponent(id)}`, { method: "DELETE" });
    await loadNotices();
  } catch (err) {
    alert(friendlyError(err));
  }
}

function openNoticeForm() {
  $("noticeTitleInput").value = "";
  $("noticeBodyInput").value = "";
  const msg = $("noticeFormMessage");
  if (msg) msg.textContent = "";
  $("noticeFormModal").classList.remove("hidden");
}

function closeNoticeForm() {
  $("noticeFormModal").classList.add("hidden");
}

async function handleNoticeSubmit() {
  const title = $("noticeTitleInput").value.trim();
  const body = $("noticeBodyInput").value.trim();
  const msg = $("noticeFormMessage");
  const btn = $("noticeSubmitBtn");
  if (!title || !body) {
    msg.textContent = "제목과 내용을 입력해주세요.";
    return;
  }
  if (btn) btn.disabled = true;
  try {
    await api("/notices", { method: "POST", body: JSON.stringify({ title, body }) });
    closeNoticeForm();
    await loadNotices();
  } catch (err) {
    msg.textContent = friendlyError(err);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleLogin() {
  const username = $("noticeUsername").value.trim();
  const password = $("noticePassword").value;
  const err = $("noticeLoginError");
  err.classList.add("hidden");
  if (!username || !password) {
    err.textContent = "아이디와 비밀번호를 입력해주세요.";
    err.classList.remove("hidden");
    return;
  }
  try {
    const data = await api("/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    setToken(data.token, data.role === "dealer" ? "rep" : "admin");
    $("noticePassword").value = "";
    if (data.must_change_password) {
      showPasswordChange();
      return;
    }
    await showLoggedIn(data);
  } catch (e) {
    err.textContent = friendlyError(e);
    err.classList.remove("hidden");
  }
}

async function handleChangePassword() {
  const msg = $("noticePasswordMessage");
  const current = $("noticeCurrentPassword").value;
  const next = $("noticeNewPassword").value;
  const confirmValue = $("noticeNewPasswordConfirm").value;
  if (!current || !next) {
    msg.textContent = "현재/새 비밀번호를 입력해주세요.";
    return;
  }
  if (next.length < 4) {
    msg.textContent = "새 비밀번호는 4자 이상이어야 합니다.";
    return;
  }
  if (next !== confirmValue) {
    msg.textContent = "새 비밀번호 확인이 일치하지 않습니다.";
    return;
  }
  try {
    const me = await api("/inventory/me");
    const path = me.role === "dealer" ? "/auth/change-password" : "/admin/change-password";
    await api(path, {
      method: "POST",
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
    ["noticeCurrentPassword", "noticeNewPassword", "noticeNewPasswordConfirm"].forEach((id) => {
      $(id).value = "";
    });
    const refreshed = await api("/inventory/me");
    await showLoggedIn(refreshed);
  } catch (e) {
    msg.textContent = friendlyError(e);
  }
}

function handleLogout() {
  api("/inventory/logout", { method: "POST" }).catch(() => {});
  setToken("");
  showLoggedOut();
}

async function restoreSession() {
  const { admin, rep } = getStoredTokens();
  if (!admin && !rep) {
    showLoggedOut();
    return;
  }
  for (const kind of ["admin", "rep"]) {
    const token = kind === "admin" ? admin : rep;
    if (!token) continue;
    activeTokenKind = kind;
    try {
      const me = await api("/inventory/me");
      if (me.must_change_password) {
        showPasswordChange();
      } else {
        await showLoggedIn(me);
      }
      return;
    } catch (_) {
      activeTokenKind = "";
    }
  }
  showLoggedOut();
}

document.addEventListener("DOMContentLoaded", () => {
  $("noticeLoginBtn").addEventListener("click", handleLogin);
  $("noticeUsername").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("noticePassword").focus();
  });
  $("noticePassword").addEventListener("keydown", (e) => {
    if (e.key === "Enter") handleLogin();
  });
  $("noticeChangePasswordBtn").addEventListener("click", handleChangePassword);
  $("noticeLogoutBtn").addEventListener("click", handleLogout);
  $("noticeList").addEventListener("click", handleNoticeListClick);
  const newBtn = $("newNoticeBtn");
  if (newBtn) newBtn.addEventListener("click", openNoticeForm);
  $("noticeFormCloseBtn").addEventListener("click", closeNoticeForm);
  $("noticeSubmitBtn").addEventListener("click", handleNoticeSubmit);
  const modal = $("noticeFormModal");
  if (modal) {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closeNoticeForm();
    });
  }
  restoreSession();
});
