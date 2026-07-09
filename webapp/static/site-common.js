/* site-common.js — общая логика для всех страниц сайта CarWash Cloud.
   Подключается на каждой странице (кроме site-login.html) первым скриптом.
   Отвечает за:
   - проверку входа (редирект на /static/site-login.html, если токена нет)
   - обёртку fetch с заголовком X-Site-Token и обработкой 401
   - рендер сайдбара (с подсветкой активного пункта)
   - выбор активного филиала (для владельца — переключаемый, для админа/мойщика — фиксированный)
*/

const CW = (() => {
  const API = ""; // сайт и API на одном хосте

  function getToken() { return localStorage.getItem("cw_token") || ""; }
  function getName() { return localStorage.getItem("cw_name") || ""; }
  function getRole() { return localStorage.getItem("cw_role") || ""; }
  function getLoginBranch() { return localStorage.getItem("cw_branch") || ""; }

  function getActiveBranch() {
    const role = getRole();
    if (role === "владелец") {
      return localStorage.getItem("cw_active_branch") || "";
    }
    return getLoginBranch();
  }

  function setActiveBranch(branch) {
    localStorage.setItem("cw_active_branch", branch);
  }

  function requireAuth() {
    if (!getToken()) {
      window.location.href = "/static/site-login.html";
      return false;
    }
    return true;
  }

  async function authFetch(path, opts = {}) {
    const headers = Object.assign({}, opts.headers || {}, {
      "X-Site-Token": getToken(),
    });
    if (opts.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(API + path, Object.assign({}, opts, { headers }));
    if (res.status === 401) {
      logout();
      throw new Error("Сессия истекла");
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok) {
      const msg = (data && data.detail) || `Ошибка запроса (${res.status})`;
      throw new Error(msg);
    }
    return data;
  }

  function logout() {
    localStorage.removeItem("cw_token");
    localStorage.removeItem("cw_name");
    localStorage.removeItem("cw_role");
    localStorage.removeItem("cw_branch");
    localStorage.removeItem("cw_active_branch");
    window.location.href = "/static/site-login.html";
  }

  const NAV = [
    { group: "Обзор", items: [
      { key: "dashboard", icon: "ti-layout-dashboard", label: "Дашборд", href: "/static/dashboard.html" },
      { key: "cars", icon: "ti-car", label: "Машины", href: "/static/cars.html" },
      { key: "cash", icon: "ti-cash", label: "Касса за смену", href: "/static/cash.html" },
    ]},
    { group: "Управление", items: [
      { key: "workers", icon: "ti-users", label: "Сотрудники", href: "/static/workers.html" },
      { key: "loyalty", icon: "ti-heart", label: "Лояльность", href: "/static/loyalty.html" },
      { key: "finance", icon: "ti-receipt", label: "Расходы и доходы", href: "/static/finance.html" },
      { key: "reports", icon: "ti-chart-bar", label: "Отчёты", href: "/static/reports.html" },
    ]},
    { group: "Система", items: [
      { key: "history", icon: "ti-history", label: "История изменений", href: "/static/history.html", adminOnly: true },
      { key: "branches", icon: "ti-building-store", label: "Филиалы", href: "/static/branches.html", ownerOnly: true },
      { key: "settings", icon: "ti-settings", label: "Настройки", href: "/static/settings.html" },
    ]},
  ];

  function initials(name) {
    const parts = (name || "").trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return "??";
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }

  function roleLabel(role) {
    return { "мойщик": "Мойщик", "админ": "Администратор", "владелец": "Владелец" }[role] || role;
  }

  /* Рендерит сайдбар в элемент с id="sidebarRoot".
     activeKey — ключ текущей страницы (см. NAV[].items[].key). */
  function renderSidebar(activeKey) {
    const root = document.getElementById("sidebarRoot");
    if (!root) return;
    const role = getRole();

    const navHtml = NAV.map(group => {
      const items = group.items.filter(it =>
        (!it.ownerOnly || role === "владелец") &&
        (!it.adminOnly || role === "админ" || role === "владелец")
      );
      if (!items.length) return "";
      const itemsHtml = items.map(it => `
        <div class="nav-item ${it.key === activeKey ? "active" : ""}" data-href="${it.href}">
          <i class="ti ${it.icon}"></i>${it.label}
        </div>`).join("");
      return `<div class="nav-group-label">${group.group}</div>${itemsHtml}`;
    }).join("");

    root.innerHTML = `
      <div class="brand">
        <div class="brand-icon"><i class="ti ti-droplet"></i></div>
        <div class="brand-name">CarWash Cloud</div>
      </div>

      <div class="branch-select" id="branchSelect" style="position:relative">
        <div><div class="bs-label">Филиал</div><div class="bs-value" id="bsValue">—</div></div>
        <i class="ti ti-chevron-down" id="bsChevron" style="${role === 'владелец' ? '' : 'display:none'}"></i>
        <select id="bsSelect" style="position:absolute;inset:0;opacity:0;${role === 'владелец' ? 'cursor:pointer' : 'pointer-events:none'}"></select>
      </div>

      ${navHtml}

      <div class="sidebar-foot">
        <div class="user-row">
          <div class="user-av">${initials(getName())}</div>
          <div>
            <div class="user-name">${getName() || "—"}</div>
            <div class="user-role">${roleLabel(role)}</div>
          </div>
        </div>
        <div class="nav-item" id="logoutBtn" style="margin-top:6px">
          <i class="ti ti-logout"></i>Выйти
        </div>
      </div>
    `;

    root.querySelectorAll(".nav-item[data-href]").forEach(el => {
      el.addEventListener("click", () => { window.location.href = el.dataset.href; });
    });
    document.getElementById("logoutBtn").addEventListener("click", logout);

    document.getElementById("bsValue").textContent = getActiveBranch() || "Выберите филиал";

    if (role === "владелец") {
      authFetch("/api/config").then(cfg => {
        const sel = document.getElementById("bsSelect");
        sel.innerHTML = cfg.branches.map(b => `<option value="${b}">${b}</option>`).join("");
        const current = getActiveBranch() || cfg.branches[0];
        sel.value = current;
        if (!getActiveBranch()) setActiveBranch(current);
        document.getElementById("bsValue").textContent = current;
        sel.addEventListener("change", () => {
          setActiveBranch(sel.value);
          window.location.reload();
        });
      }).catch(() => {});
    }
  }

  function money(n) {
    return (Math.round(n || 0)).toLocaleString("ru-RU") + " ₽";
  }

  function todayLabel() {
    return new Date().toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" });
  }

  return {
    getToken, getName, getRole, getLoginBranch,
    getActiveBranch, setActiveBranch,
    requireAuth, authFetch, logout,
    renderSidebar, initials, roleLabel, money, todayLabel,
  };
})();
