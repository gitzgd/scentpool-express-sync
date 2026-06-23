const state = {
  user: null,
  stores: [],
  productsGrouped: null,
  productsAll: [],
  shipments: [],
  storeShipments: [],
  statuses: ["待处理", "已发货", "异常", "已取消"],
  submitItems: [{ category: "", barcode: "", quantity: 1 }],
  submitDraft: { store_id: "", recipient_name: "", phone: "", address: "", store_order_no: "", remark: "" },
  adminFilters: { store_id: "", status: "", date_from: "", date_to: "", q: "" },
  storeFilters: { status: "", date_from: "", date_to: "", q: "" },
  productFilters: { category: "", q: "" },
};

const EXPRESS_COMPANIES = ["圆通", "京东", "顺丰"];
const DEFAULT_EXPRESS_COMPANY = "圆通";
const CATEGORY_COLOR_COUNT = 10;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function statusClass(status) {
  if (status === "待处理") return "pending";
  if (status === "已发货") return "shipped";
  if (status === "异常") return "exception";
  if (status === "已取消") return "cancelled";
  return "";
}

function formatDate(value) {
  if (!value) return "";
  return String(value).replace("T", " ").replace(/\+\d\d:\d\d$/, "");
}

function localDate(offsetDays = 0) {
  const date = new Date();
  date.setDate(date.getDate() + offsetDays);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function expressCompanyOptions(selected = "") {
  const current = selected || DEFAULT_EXPRESS_COMPANY;
  return EXPRESS_COMPANIES.map(
    (company) => `<option value="${company}" ${company === current ? "selected" : ""}>${company}</option>`
  ).join("");
}

function categoryColorClass(category) {
  let hash = 0;
  for (const char of String(category || "未分类")) {
    hash = (hash * 31 + char.charCodeAt(0)) % 9973;
  }
  return `cat-color-${(hash % CATEGORY_COLOR_COUNT) + 1}`;
}

function toast(message) {
  const existing = document.querySelector(".toast");
  if (existing) existing.remove();
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = message;
  document.body.appendChild(node);
  setTimeout(() => node.remove(), 3200);
}

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    if (response.status === 401 && location.pathname !== "/login") {
      state.user = null;
      navigate("/login");
    }
    throw new Error(data.error || "请求失败");
  }
  return data;
}

function navigate(path) {
  history.pushState({}, "", path);
  render();
}

function isActive(path) {
  return location.pathname === path ? "active" : "";
}

function roleName(user) {
  return user?.role === "admin" ? "总部" : "门店";
}

function shell(content) {
  if (!state.user && location.pathname === "/login") {
    return content;
  }
  const adminLinks =
    state.user?.role === "admin"
      ? `
        <a class="${isActive("/admin")}" href="/admin" data-route>发货后台</a>
        <a class="${isActive("/admin/stores")}" href="/admin/stores" data-route>门店</a>
        <a class="${isActive("/admin/products")}" href="/admin/products" data-route>商品</a>
      `
      : "";
  const storeLinks =
    state.user?.role === "staff"
      ? `<a class="${isActive("/shipments")}" href="/shipments" data-route>发货看板</a>`
      : "";
  const submitLink = `<a class="${isActive("/submit")}" href="/submit" data-route>新建发货</a>`;
  return `
    <div class="app-shell">
      <header class="topbar">
        <a class="brand" href="${state.user?.role === "admin" ? "/admin" : "/submit"}" data-route>
          <span class="mark">万</span>
          <span>万物香铺</span>
        </a>
        <nav class="nav">${submitLink}${storeLinks}${adminLinks}</nav>
        <div class="user-strip">
          <span>${escapeHtml(roleName(state.user))} · ${escapeHtml(state.user?.store_name || state.user?.username || "")}</span>
          <button class="btn ghost small" id="logoutBtn">退出</button>
        </div>
      </header>
      <main class="main">${content}</main>
    </div>
  `;
}

async function loadMe() {
  try {
    const data = await api("/api/me");
    state.user = data.user;
  } catch {
    state.user = null;
  }
}

async function ensureProductsGrouped() {
  if (!state.productsGrouped) {
    const data = await api("/api/products");
    state.productsGrouped = data.categories || {};
  }
}

async function loadStores(all = false) {
  const data = await api(`/api/stores${all ? "?all=1" : ""}`);
  state.stores = data.stores || [];
}

async function loadShipments() {
  const params = new URLSearchParams();
  Object.entries(state.adminFilters).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  const data = await api(`/api/shipments?${params.toString()}`);
  state.shipments = data.shipments || [];
  state.statuses = data.statuses || state.statuses;
}

async function loadStoreShipments() {
  const params = new URLSearchParams();
  Object.entries(state.storeFilters).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  const data = await api(`/api/shipments?${params.toString()}`);
  state.storeShipments = data.shipments || [];
  state.statuses = data.statuses || state.statuses;
}

async function loadProductsAll() {
  const data = await api("/api/products?all=1");
  state.productsAll = data.products || [];
}

function categories() {
  return Object.keys(state.productsGrouped || {}).sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
}

function categoryOptions(selected = "") {
  return `<option value="">选择分类</option>${categories()
    .map((cat) => `<option value="${escapeHtml(cat)}" ${cat === selected ? "selected" : ""}>${escapeHtml(cat)}</option>`)
    .join("")}`;
}

function productOptions(category, selected = "") {
  const products = state.productsGrouped?.[category] || [];
  return `<option value="">选择货品</option>${products
    .map((product) => {
      const label = `${product.name}${product.price ? ` · ¥${product.price}` : ""} · ${product.barcode}`;
      return `<option value="${escapeHtml(product.barcode)}" ${product.barcode === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
    })
    .join("")}`;
}

function pageHead(title, subtitle, extra = "") {
  return `
    <div class="page-head">
      <div>
        <h1>${escapeHtml(title)}</h1>
        ${subtitle ? `<p>${escapeHtml(subtitle)}</p>` : ""}
      </div>
      ${extra}
    </div>
  `;
}

function renderLogin() {
  document.getElementById("app").innerHTML = `
    <div class="login-wrap">
      <section class="login-panel">
        <h1 class="login-title">万物香铺</h1>
        <p class="login-subtitle">快递同步工作台</p>
        <form id="loginForm" class="form-grid">
          <div class="field full">
            <label for="username">账号</label>
            <input class="input" id="username" name="username" autocomplete="username" required />
          </div>
          <div class="field full">
            <label for="password">密码</label>
            <input class="input" id="password" name="password" type="password" autocomplete="current-password" required />
          </div>
        <div class="field full">
          <button class="btn primary" type="submit">登录</button>
        </div>
      </form>
      </section>
    </div>
  `;
  document.getElementById("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const data = await api("/api/login", {
        method: "POST",
        body: JSON.stringify({
          username: form.get("username"),
          password: form.get("password"),
        }),
      });
      state.user = data.user;
      navigate(state.user.role === "admin" ? "/admin" : "/submit");
    } catch (error) {
      toast(error.message);
    }
  });
}

function validSubmitItems() {
  return state.submitItems
    .filter((item) => item.barcode && Number(item.quantity) > 0)
    .map((item) => ({ barcode: item.barcode, quantity: Number(item.quantity) }));
}

function captureSubmitDraft() {
  const form = document.getElementById("shipmentForm");
  if (!form) return;
  const data = new FormData(form);
  state.submitDraft = {
    store_id: data.get("store_id") || "",
    recipient_name: data.get("recipient_name") || "",
    phone: data.get("phone") || "",
    address: data.get("address") || "",
    store_order_no: data.get("store_order_no") || "",
    remark: data.get("remark") || "",
  };
}

function selectedProduct(barcode) {
  for (const list of Object.values(state.productsGrouped || {})) {
    const product = list.find((item) => item.barcode === barcode);
    if (product) return product;
  }
  return null;
}

function renderSubmitSummary() {
  const items = validSubmitItems();
  if (!items.length) return `<p class="muted">还没有选择货品。</p>`;
  return `
    <ul class="summary-list">
      ${items
        .map((item) => {
          const product = selectedProduct(item.barcode);
          return `
            <li>
              <span>
                <strong>${escapeHtml(product?.name || item.barcode)}</strong><br />
                <span class="mini">${renderCategoryChip(product?.category || "未分类")} <span class="muted">${escapeHtml(item.barcode)}</span></span>
              </span>
              <span class="count-pill">x${item.quantity}</span>
            </li>
          `;
        })
        .join("")}
    </ul>
  `;
}

async function renderSubmit() {
  await ensureProductsGrouped();
  if (state.user.role === "admin" && state.stores.length === 0) await loadStores();
  const storeField =
    state.user.role === "admin"
      ? `
        <div class="field">
          <label for="store_id">门店</label>
          <select class="select" id="store_id" name="store_id" required>
            <option value="">选择门店</option>
            ${state.stores.map((store) => `<option value="${store.id}" ${String(store.id) === String(state.submitDraft.store_id) ? "selected" : ""}>${escapeHtml(store.name)}</option>`).join("")}
          </select>
        </div>
      `
      : `
        <div class="field">
          <label>门店</label>
          <span class="store-badge">${escapeHtml(state.user.store_name || "当前门店")}</span>
        </div>
      `;

  const itemRows = state.submitItems
    .map(
      (item, index) => `
        <div class="item-row" data-item-row="${index}">
          <select class="select" data-item-category="${index}" aria-label="货品分类">
            ${categoryOptions(item.category)}
          </select>
          <select class="select" data-item-product="${index}" aria-label="货品名称">
            ${productOptions(item.category, item.barcode)}
          </select>
          <input class="input" type="number" min="1" step="1" value="${escapeHtml(item.quantity || 1)}" data-item-quantity="${index}" aria-label="数量" />
          <button class="btn danger small" type="button" data-remove-item="${index}">删</button>
        </div>
      `
    )
    .join("");

  const recent = await api("/api/shipments").catch(() => ({ shipments: [] }));
  const content = `
    ${pageHead("新建发货", "门店提交后，总部会在发货后台同步看到。", `<span class="count-pill">${categories().length} 个分类</span>`)}
    <div class="grid-2">
      <section class="panel panel-pad">
        <form id="shipmentForm">
          <div class="form-grid">
            ${storeField}
            <div class="field">
              <label for="recipient_name">姓名</label>
              <input class="input" id="recipient_name" name="recipient_name" value="${escapeHtml(state.submitDraft.recipient_name)}" required />
            </div>
            <div class="field">
              <label for="phone">联系电话</label>
              <input class="input" id="phone" name="phone" inputmode="tel" value="${escapeHtml(state.submitDraft.phone)}" required />
            </div>
            <div class="field">
              <label for="store_order_no">门店订单号</label>
              <input class="input" id="store_order_no" name="store_order_no" value="${escapeHtml(state.submitDraft.store_order_no)}" required />
            </div>
            <div class="field full">
              <label for="address">快递地址</label>
              <textarea class="textarea" id="address" name="address" required>${escapeHtml(state.submitDraft.address)}</textarea>
            </div>
            <div class="field full">
              <label for="remark">备注</label>
              <textarea class="textarea" id="remark" name="remark">${escapeHtml(state.submitDraft.remark)}</textarea>
            </div>
          </div>
          <div class="section-title" style="margin-top: 22px;">
            <h2>货品</h2>
            <button class="btn secondary small" id="addItemBtn" type="button">添加</button>
          </div>
          <div class="item-stack" id="itemsBox">${itemRows}</div>
          <div class="split-actions">
            <span class="muted mini">订单号在同一门店内不能重复。</span>
            <button class="btn primary" type="submit">提交总部</button>
          </div>
        </form>
      </section>
      <aside class="panel panel-pad">
        <div class="section-title"><h2>本次明细</h2></div>
        ${renderSubmitSummary()}
      </aside>
    </div>
    <section class="panel panel-pad" style="margin-top: 16px;">
      <div class="section-title"><h2>近期记录</h2><span class="count-pill">${recent.shipments.length}</span></div>
      ${renderMiniShipments(recent.shipments.slice(0, 6))}
    </section>
  `;
  document.getElementById("app").innerHTML = shell(content);
  bindCommon();
  bindSubmit();
}

function renderMiniShipments(shipments) {
  if (!shipments.length) return `<div class="empty">暂无记录</div>`;
  return `
    <div class="table-wrap">
      <table>
        <thead><tr><th>时间</th><th>订单号</th><th>收件人</th><th>商品</th><th>状态</th><th>快递信息</th></tr></thead>
        <tbody>
          ${shipments
            .map(
              (row) => `
                <tr>
                  <td>${escapeHtml(formatDate(row.created_at))}</td>
                  <td>${escapeHtml(row.store_order_no)}</td>
                  <td>${escapeHtml(row.recipient_name)}</td>
                  <td class="items-cell">${renderItemLines(row.items)}</td>
                  <td><span class="status ${statusClass(row.status)}">${escapeHtml(row.status)}</span></td>
                  <td>${renderTrackingInfo(row)}</td>
                </tr>
              `
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderTrackingInfo(row) {
  if (row.tracking_no) {
    return `
      <strong>${escapeHtml(row.express_company || DEFAULT_EXPRESS_COMPANY)}</strong><br />
      <span>${escapeHtml(row.tracking_no)}</span>
      ${row.shipped_at ? `<div class="muted mini">${escapeHtml(formatDate(row.shipped_at))}</div>` : ""}
      ${row.shipping_note ? `<div class="muted mini">${escapeHtml(row.shipping_note)}</div>` : ""}
    `;
  }
  if (row.status === "已发货") {
    return `<span class="muted">待总部填写单号</span>`;
  }
  return `<span class="muted">暂无单号</span>`;
}

function renderCategoryChip(category) {
  return `<span class="category-chip ${categoryColorClass(category)}">${escapeHtml(category || "未分类")}</span>`;
}

function renderItemLines(items) {
  if (!items || !items.length) return `<span class="muted">无商品</span>`;
  return items
    .map(
      (item) => `
        <div class="item-line">
          ${renderCategoryChip(item.product_category)}
          <span class="item-name">${escapeHtml(item.product_name)}</span>
          <span class="count-pill">x${item.quantity}</span>
        </div>
      `
    )
    .join("");
}

function bindSubmit() {
  document.querySelectorAll("[data-item-category]").forEach((node) => {
    node.addEventListener("change", (event) => {
      const index = Number(event.currentTarget.dataset.itemCategory);
      captureSubmitDraft();
      state.submitItems[index].category = event.currentTarget.value;
      state.submitItems[index].barcode = "";
      render();
    });
  });
  document.querySelectorAll("[data-item-product]").forEach((node) => {
    node.addEventListener("change", (event) => {
      const index = Number(event.currentTarget.dataset.itemProduct);
      captureSubmitDraft();
      state.submitItems[index].barcode = event.currentTarget.value;
      render();
    });
  });
  document.querySelectorAll("[data-item-quantity]").forEach((node) => {
    node.addEventListener("input", (event) => {
      const index = Number(event.currentTarget.dataset.itemQuantity);
      state.submitItems[index].quantity = Math.max(1, Number(event.currentTarget.value || 1));
      const aside = document.querySelector("aside.panel");
      if (aside) {
        aside.innerHTML = `<div class="section-title"><h2>本次明细</h2></div>${renderSubmitSummary()}`;
      }
    });
  });
  document.querySelectorAll("[data-remove-item]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const index = Number(event.currentTarget.dataset.removeItem);
      captureSubmitDraft();
      state.submitItems.splice(index, 1);
      if (!state.submitItems.length) state.submitItems.push({ category: "", barcode: "", quantity: 1 });
      render();
    });
  });
  document.getElementById("addItemBtn").addEventListener("click", () => {
    captureSubmitDraft();
    state.submitItems.push({ category: "", barcode: "", quantity: 1 });
    render();
  });
  document.getElementById("shipmentForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const payload = {
      store_id: form.get("store_id"),
      recipient_name: form.get("recipient_name"),
      phone: form.get("phone"),
      address: form.get("address"),
      store_order_no: form.get("store_order_no"),
      remark: form.get("remark"),
      items: validSubmitItems(),
    };
    try {
      await api("/api/shipments", { method: "POST", body: JSON.stringify(payload) });
      state.submitItems = [{ category: "", barcode: "", quantity: 1 }];
      state.submitDraft = { store_id: "", recipient_name: "", phone: "", address: "", store_order_no: "", remark: "" };
      toast("已同步到总部。");
      render();
    } catch (error) {
      toast(error.message);
    }
  });
}

async function renderStoreBoard() {
  await loadStoreShipments();
  const today = localDate();
  const yesterday = localDate(-1);
  const todayData = await api(`/api/shipments?date_from=${today}&date_to=${today}`);
  const todayCounts = (todayData.shipments || []).reduce(
    (acc, row) => {
      acc.total += 1;
      acc[row.status] = (acc[row.status] || 0) + 1;
      return acc;
    },
    { total: 0 }
  );
  const counts = state.storeShipments.reduce(
    (acc, row) => {
      acc.total += 1;
      acc[row.status] = (acc[row.status] || 0) + 1;
      return acc;
    },
    { total: 0 }
  );
  const content = `
    ${pageHead(
      "发货看板",
      "查看本门店所有发货记录、处理状态和快递单号。",
      `<div class="actions">
        <span class="count-pill">今日 ${todayCounts.total} 单</span>
        <span class="count-pill">今日待处理 ${todayCounts["待处理"] || 0}</span>
        <span class="count-pill">今日已发货 ${todayCounts["已发货"] || 0}</span>
        <span class="count-pill">共 ${counts.total} 单</span>
        <span class="count-pill">待处理 ${counts["待处理"] || 0}</span>
        <span class="count-pill">已发货 ${counts["已发货"] || 0}</span>
      </div>`
    )}
    <section class="panel panel-pad">
      <div class="filters store-filters">
        <div class="quick-filters">
          <button class="btn secondary small ${state.storeFilters.date_from === today && state.storeFilters.date_to === today ? "active" : ""}" data-store-preset="today" type="button">今日</button>
          <button class="btn secondary small ${state.storeFilters.date_from === yesterday && state.storeFilters.date_to === yesterday ? "active" : ""}" data-store-preset="yesterday" type="button">昨日</button>
        </div>
        <div class="field">
          <label>状态</label>
          <select class="select" id="storeFilterStatus">
            <option value="">全部</option>
            ${state.statuses.map((status) => `<option value="${status}" ${status === state.storeFilters.status ? "selected" : ""}>${status}</option>`).join("")}
          </select>
        </div>
        <div class="field">
          <label>开始日期</label>
          <input class="input" type="date" id="storeFilterFrom" value="${escapeHtml(state.storeFilters.date_from)}" />
        </div>
        <div class="field">
          <label>结束日期</label>
          <input class="input" type="date" id="storeFilterTo" value="${escapeHtml(state.storeFilters.date_to)}" />
        </div>
        <div class="field">
          <label>搜索</label>
          <input class="input" id="storeFilterQ" value="${escapeHtml(state.storeFilters.q)}" placeholder="订单号 / 姓名 / 电话 / 单号" />
        </div>
        <button class="btn primary" id="applyStoreFilters" type="button">筛选</button>
        <button class="btn secondary" id="resetStoreFilters" type="button">清空</button>
      </div>
      ${renderStoreBoardTable(state.storeShipments)}
    </section>
  `;
  document.getElementById("app").innerHTML = shell(content);
  bindCommon();
  bindStoreBoard();
}

function renderStoreBoardTable(shipments) {
  if (!shipments.length) return `<div class="empty">没有符合条件的发货单</div>`;
  return `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>提交时间</th><th>门店订单号</th><th>收件信息</th><th>商品明细</th><th>状态</th><th>快递信息</th><th>备注</th>
          </tr>
        </thead>
        <tbody>
          ${shipments
            .map(
              (row) => `
                <tr>
                  <td>${escapeHtml(formatDate(row.created_at))}</td>
                  <td><strong>${escapeHtml(row.store_order_no)}</strong></td>
                  <td>
                    <strong>${escapeHtml(row.recipient_name)}</strong><br />
                    <span class="muted">${escapeHtml(row.phone)}</span><br />
                    <span>${escapeHtml(row.address)}</span>
                  </td>
                  <td class="items-cell">
                    ${row.items
                      ? renderItemLines(row.items)
                      : `<span class="muted">无商品</span>`}
                  </td>
                  <td><span class="status ${statusClass(row.status)}">${escapeHtml(row.status)}</span></td>
                  <td>${renderTrackingInfo(row)}</td>
                  <td>
                    ${row.remark ? `<div>${escapeHtml(row.remark)}</div>` : `<span class="muted">无</span>`}
                    ${row.shipping_note ? `<div class="muted mini">总部：${escapeHtml(row.shipping_note)}</div>` : ""}
                  </td>
                </tr>
              `
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function bindStoreBoard() {
  document.querySelectorAll("[data-store-preset]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const preset = event.currentTarget.dataset.storePreset;
      const targetDate = preset === "yesterday" ? localDate(-1) : localDate();
      state.storeFilters = {
        ...state.storeFilters,
        date_from: targetDate,
        date_to: targetDate,
      };
      render();
    });
  });
  document.getElementById("applyStoreFilters").addEventListener("click", () => {
    state.storeFilters = {
      status: document.getElementById("storeFilterStatus").value,
      date_from: document.getElementById("storeFilterFrom").value,
      date_to: document.getElementById("storeFilterTo").value,
      q: document.getElementById("storeFilterQ").value.trim(),
    };
    render();
  });
  document.getElementById("resetStoreFilters").addEventListener("click", () => {
    state.storeFilters = { status: "", date_from: "", date_to: "", q: "" };
    render();
  });
}

async function renderAdmin() {
  await loadStores();
  await loadShipments();
  const exportParams = new URLSearchParams();
  Object.entries(state.adminFilters).forEach(([key, value]) => {
    if (value) exportParams.set(key, value);
  });
  const content = `
    ${pageHead(
      "发货后台",
      "总部统一处理门店提交的发货需求。",
      `<div class="actions">
        <a class="btn primary" href="/api/export/shipments.xlsx?${exportParams.toString()}">导出 XLSX</a>
        <a class="btn secondary" href="/api/export/shipments.csv?${exportParams.toString()}">导出 CSV</a>
        <a class="btn secondary" href="/api/admin/backup.db">备份数据库</a>
      </div>`
    )}
    <section class="panel panel-pad">
      <div class="filters">
        <div class="field">
          <label>门店</label>
          <select class="select" id="filterStore">
            <option value="">全部</option>
            ${state.stores
              .map((store) => `<option value="${store.id}" ${String(store.id) === String(state.adminFilters.store_id) ? "selected" : ""}>${escapeHtml(store.name)}</option>`)
              .join("")}
          </select>
        </div>
        <div class="field">
          <label>状态</label>
          <select class="select" id="filterStatus">
            <option value="">全部</option>
            ${state.statuses.map((status) => `<option value="${status}" ${status === state.adminFilters.status ? "selected" : ""}>${status}</option>`).join("")}
          </select>
        </div>
        <div class="field">
          <label>开始日期</label>
          <input class="input" type="date" id="filterFrom" value="${escapeHtml(state.adminFilters.date_from)}" />
        </div>
        <div class="field">
          <label>结束日期</label>
          <input class="input" type="date" id="filterTo" value="${escapeHtml(state.adminFilters.date_to)}" />
        </div>
        <div class="field">
          <label>搜索</label>
          <input class="input" id="filterQ" value="${escapeHtml(state.adminFilters.q)}" placeholder="订单号 / 姓名 / 电话 / 单号" />
        </div>
        <button class="btn primary" id="applyFilters" type="button">筛选</button>
        <button class="btn secondary" id="resetFilters" type="button">清空</button>
      </div>
      ${renderShipmentTable(state.shipments)}
    </section>
  `;
  document.getElementById("app").innerHTML = shell(content);
  bindCommon();
  bindAdmin();
}

function renderShipmentTable(shipments) {
  if (!shipments.length) return `<div class="empty">没有符合条件的发货单</div>`;
  return `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>提交</th><th>门店</th><th>订单</th><th>收件信息</th><th>商品</th><th>状态</th><th>快递</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          ${shipments
            .map(
              (row) => `
                <tr data-shipment="${row.id}">
                  <td>${row.id}</td>
                  <td>${escapeHtml(formatDate(row.created_at))}</td>
                  <td>${escapeHtml(row.store_name_snapshot)}</td>
                  <td>
                    <strong>${escapeHtml(row.store_order_no)}</strong>
                    ${row.remark ? `<div class="muted mini">${escapeHtml(row.remark)}</div>` : ""}
                  </td>
                  <td>
                    <strong>${escapeHtml(row.recipient_name)}</strong><br />
                    <span class="muted">${escapeHtml(row.phone)}</span><br />
                    <span>${escapeHtml(row.address)}</span>
                  </td>
                  <td class="items-cell">
                    ${renderItemLines(row.items)}
                  </td>
                  <td>
                    <select class="table-input" data-status>
                      ${state.statuses.map((status) => `<option value="${status}" ${status === row.status ? "selected" : ""}>${status}</option>`).join("")}
                    </select>
                  </td>
                  <td>
                    <select class="table-input" data-company>
                      ${expressCompanyOptions(row.express_company)}
                    </select><br />
                    <input class="table-input" data-tracking value="${escapeHtml(row.tracking_no)}" placeholder="快递单号" style="margin-top: 6px;" /><br />
                    <input class="table-input" data-note value="${escapeHtml(row.shipping_note)}" placeholder="发货备注" style="margin-top: 6px;" />
                  </td>
                  <td>
                    <button class="btn primary small" data-save-shipment="${row.id}" type="button">保存</button>
                    ${row.shipped_at ? `<div class="muted mini" style="margin-top: 8px;">${escapeHtml(formatDate(row.shipped_at))}</div>` : ""}
                  </td>
                </tr>
              `
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function bindAdmin() {
  document.getElementById("applyFilters").addEventListener("click", () => {
    state.adminFilters = {
      store_id: document.getElementById("filterStore").value,
      status: document.getElementById("filterStatus").value,
      date_from: document.getElementById("filterFrom").value,
      date_to: document.getElementById("filterTo").value,
      q: document.getElementById("filterQ").value.trim(),
    };
    render();
  });
  document.getElementById("resetFilters").addEventListener("click", () => {
    state.adminFilters = { store_id: "", status: "", date_from: "", date_to: "", q: "" };
    render();
  });
  document.querySelectorAll("[data-save-shipment]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.saveShipment;
      const row = document.querySelector(`[data-shipment="${id}"]`);
      const payload = {
        status: row.querySelector("[data-status]").value,
        express_company: row.querySelector("[data-company]").value,
        tracking_no: row.querySelector("[data-tracking]").value,
        shipping_note: row.querySelector("[data-note]").value,
      };
      try {
        await api(`/api/shipments/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
        toast("已保存。");
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  });
}

async function renderStores() {
  await loadStores(true);
  const content = `
    ${pageHead("门店", "维护门店和店员登录账号。")}
    <div class="grid-2">
      <section class="panel panel-pad">
        <div class="section-title"><h2>新增门店</h2></div>
        <form id="storeForm" class="form-grid">
          <div class="field full">
            <label for="storeName">门店名称</label>
            <input class="input" id="storeName" name="name" required />
          </div>
          <div class="field">
            <label for="storeUser">店员账号</label>
            <input class="input" id="storeUser" name="username" required />
          </div>
          <div class="field">
            <label for="storePassword">初始密码</label>
            <input class="input" id="storePassword" name="password" type="password" minlength="6" required />
          </div>
          <div class="field full">
            <button class="btn primary" type="submit">创建</button>
          </div>
        </form>
      </section>
      <section class="panel panel-pad">
        <div class="section-title"><h2>门店数量</h2><span class="count-pill">${state.stores.length}</span></div>
        <p class="muted">停用门店会同步停用对应店员账号。</p>
      </section>
    </div>
    <section class="panel panel-pad" style="margin-top: 16px;">
      ${renderStoresTable()}
    </section>
  `;
  document.getElementById("app").innerHTML = shell(content);
  bindCommon();
  bindStores();
}

function renderStoresTable() {
  if (!state.stores.length) return `<div class="empty">暂无门店</div>`;
  return `
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>门店</th><th>账号</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody>
          ${state.stores
            .map(
              (store) => `
                <tr>
                  <td>${store.id}</td>
                  <td><strong>${escapeHtml(store.name)}</strong></td>
                  <td>${escapeHtml(store.usernames || "")}</td>
                  <td><span class="status ${store.active ? "shipped" : "cancelled"}">${store.active ? "启用" : "停用"}</span></td>
                  <td>${escapeHtml(formatDate(store.created_at))}</td>
                  <td><button class="btn secondary small" data-toggle-store="${store.id}" data-active="${store.active ? "0" : "1"}">${store.active ? "停用" : "启用"}</button></td>
                </tr>
              `
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function bindStores() {
  document.getElementById("storeForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api("/api/stores", {
        method: "POST",
        body: JSON.stringify({
          name: form.get("name"),
          username: form.get("username"),
          password: form.get("password"),
        }),
      });
      toast("门店已创建。");
      render();
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelectorAll("[data-toggle-store]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.toggleStore;
      const active = event.currentTarget.dataset.active === "1";
      try {
        await api(`/api/stores/${id}`, { method: "PATCH", body: JSON.stringify({ active }) });
        toast("门店状态已更新。");
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  });
}

async function renderProducts() {
  await loadProductsAll();
  const categoriesAll = [...new Set(state.productsAll.map((product) => product.category))].sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
  const filtered = state.productsAll.filter((product) => {
    const q = state.productFilters.q.trim();
    const categoryOk = !state.productFilters.category || product.category === state.productFilters.category;
    const qOk = !q || `${product.name} ${product.barcode} ${product.category}`.includes(q);
    return categoryOk && qOk;
  });
  const content = `
    ${pageHead("商品", "上传 Excel 商品资料刷新点菜单。", `<span class="count-pill">${state.productsAll.length} 个商品</span>`)}
    <section class="panel panel-pad">
      <div class="product-toolbar">
        <div class="field product-upload-field">
          <label for="productFile">商品文件</label>
          <input class="input" id="productFile" type="file" accept=".xlsx" />
        </div>
        <div class="field">
          <label for="productCategory">分类</label>
          <select class="select" id="productCategory">
            <option value="">全部</option>
            ${categoriesAll.map((cat) => `<option value="${cat}" ${cat === state.productFilters.category ? "selected" : ""}>${escapeHtml(cat)}</option>`).join("")}
          </select>
        </div>
        <button class="btn primary" id="importProducts" type="button">刷新商品</button>
      </div>
      <div class="product-toolbar">
        <div class="field">
          <label for="productSearch">搜索</label>
          <input class="input" id="productSearch" value="${escapeHtml(state.productFilters.q)}" placeholder="名称 / 条码 / 分类" />
        </div>
        <button class="btn secondary" id="applyProductFilters" type="button">筛选</button>
        <button class="btn secondary" id="resetProductFilters" type="button">清空</button>
      </div>
      ${renderProductsTable(filtered)}
    </section>
  `;
  document.getElementById("app").innerHTML = shell(content);
  bindCommon();
  bindProducts();
}

function renderProductsTable(products) {
  if (!products.length) return `<div class="empty">没有符合条件的商品</div>`;
  return `
    <div class="table-wrap">
      <table>
        <thead><tr><th>分类</th><th>货品名称</th><th>条码</th><th>售价</th><th>状态</th><th>更新时间</th></tr></thead>
        <tbody>
          ${products
            .map(
              (product) => `
                <tr>
                  <td>${renderCategoryChip(product.category)}</td>
                  <td><strong>${escapeHtml(product.name)}</strong></td>
                  <td>${escapeHtml(product.barcode)}</td>
                  <td>¥${escapeHtml(product.price)}</td>
                  <td><span class="status ${product.status === "启用" ? "shipped" : "cancelled"}">${escapeHtml(product.status)}</span></td>
                  <td>${escapeHtml(formatDate(product.updated_at))}</td>
                </tr>
              `
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function bindProducts() {
  document.getElementById("applyProductFilters").addEventListener("click", () => {
    state.productFilters = {
      category: document.getElementById("productCategory").value,
      q: document.getElementById("productSearch").value.trim(),
    };
    render();
  });
  document.getElementById("resetProductFilters").addEventListener("click", () => {
    state.productFilters = { category: "", q: "" };
    render();
  });
  document.getElementById("importProducts").addEventListener("click", async () => {
    const file = document.getElementById("productFile").files[0];
    if (!file) {
      toast("请选择 .xlsx 商品文件。");
      return;
    }
    try {
      const form = new FormData();
      form.append("product_file", file);
      const data = await api("/api/products/import", {
        method: "POST",
        body: form,
      });
      state.productsAll = data.products || [];
      state.productsGrouped = null;
      toast(`已刷新 ${data.result.imported} 个商品。`);
      render();
    } catch (error) {
      toast(error.message);
    }
  });
}

function bindCommon() {
  document.querySelectorAll("[data-route]").forEach((node) => {
    node.addEventListener("click", (event) => {
      event.preventDefault();
      navigate(event.currentTarget.getAttribute("href"));
    });
  });
  const logout = document.getElementById("logoutBtn");
  if (logout) {
    logout.addEventListener("click", async () => {
      await api("/api/logout", { method: "POST" }).catch(() => null);
      state.user = null;
      navigate("/login");
    });
  }
}

async function render() {
  const path = location.pathname;
  if (!state.user && path !== "/login") {
    await loadMe();
  }
  if (!state.user && path !== "/login") {
    history.replaceState({}, "", "/login");
    renderLogin();
    return;
  }
  if (state.user && (path === "/" || path === "/login")) {
    history.replaceState({}, "", state.user.role === "admin" ? "/admin" : "/submit");
  }

  try {
    if (location.pathname === "/login") {
      renderLogin();
    } else if (location.pathname === "/submit") {
      await renderSubmit();
    } else if (location.pathname === "/shipments" && state.user.role === "staff") {
      await renderStoreBoard();
    } else if (location.pathname === "/admin" && state.user.role === "admin") {
      await renderAdmin();
    } else if (location.pathname === "/admin/stores" && state.user.role === "admin") {
      await renderStores();
    } else if (location.pathname === "/admin/products" && state.user.role === "admin") {
      await renderProducts();
    } else {
      navigate(state.user.role === "admin" ? "/admin" : "/submit");
    }
  } catch (error) {
    document.getElementById("app").innerHTML = shell(`<section class="panel panel-pad"><div class="empty">${escapeHtml(error.message)}</div></section>`);
    bindCommon();
  }
}

window.addEventListener("popstate", render);
render();
