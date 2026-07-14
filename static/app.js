const state = {
  user: null,
  stores: [],
  productsGrouped: null,
  productsAll: [],
  shipments: [],
  adminShipmentSummary: [],
  storeShipments: [],
  returnOrders: [],
  storeReturnOrders: [],
  statuses: ["待处理", "已发货", "已签收", "异常", "已取消"],
  returnStatuses: ["待查询", "运输中", "已签收", "异常", "已取消"],
  submitItems: [{ category: "", barcode: "", quantity: 1 }],
  submitDraft: { store_id: "", recipient_name: "", phone: "", address: "", store_order_no: "", remark: "" },
  returnItems: [{ category: "", barcode: "", quantity: 1 }],
  returnDraft: { store_id: "", express_company: "圆通", tracking_no: "", sender_phone: "", remark: "" },
  adminFilters: { store_id: "", status: "待处理", date_from: "", date_to: "", q: "" },
  storeFilters: { status: "", date_from: "", date_to: "", q: "" },
  adminShipmentPage: 1,
  storeShipmentPage: 1,
  adminReturnFilters: { store_id: "", status: "", date_from: "", date_to: "", q: "" },
  storeReturnFilters: { status: "", date_from: "", date_to: "", q: "" },
  productFilters: { category: "", q: "" },
  editingShipmentId: null,
  editingShipmentShippingId: null,
  shipmentEditItems: [],
  shippingSettings: null,
  shippingConfig: null,
  batchPreview: null,
  batchFilters: { store_id: "", status: "待处理", date_from: "", date_to: "", q: "" },
  batchSelectedIds: [],
  activeShippingBatch: null,
  shippingBatchPollTimer: null,
};

const EXPRESS_COMPANIES = ["圆通", "京东", "顺丰"];
const DEFAULT_EXPRESS_COMPANY = "圆通";
const CATEGORY_COLOR_COUNT = 10;
const SHIPMENT_PAGE_SIZE = 50;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function statusClass(status) {
  if (status === "待处理" || status === "待查询") return "pending";
  if (status === "已发货" || status === "运输中") return "shipped";
  if (status === "已签收") return "signed";
  if (status === "异常") return "exception";
  if (status === "已取消") return "cancelled";
  return "";
}

function trackingClass(status) {
  if (status === "已签收") return "signed";
  if (status === "查询失败" || status === "问题件" || status === "退签" || status === "退回" || status === "拒签") return "exception";
  if (status === "待查询" || status === "等待揽收" || status === "无轨迹") return "pending";
  if (status === "已揽收" || status === "运输中" || status === "转寄" || status === "转投" || status === "派件中" || status === "清关") return "shipped";
  return "";
}

function formatDate(value) {
  if (!value) return "";
  const raw = String(value);
  const normalized = raw.includes("T") ? raw : raw.replace(" ", "T");
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) {
    return raw.replace("T", " ").replace(/\+\d\d:\d\d$/, "");
  }
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  return `${year}-${month}-${day} ${hour}:${minute}`;
}

function localDate(offsetDays = 0) {
  const date = new Date();
  date.setDate(date.getDate() + offsetDays);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function datePart(value) {
  return formatDate(value).slice(0, 10) || "未分日期";
}

function compactDate(value) {
  return datePart(value).replaceAll("-", "");
}

function shipmentStoreCode(row) {
  const storeId = Number(row.store_id);
  if (Number.isFinite(storeId) && storeId > 0) {
    return `S${String(storeId).padStart(2, "0")}`;
  }
  return "S00";
}

function shipmentBusinessId(row) {
  return row.business_id || `${compactDate(row.created_at)}-${shipmentStoreCode(row)}-${row.store_order_no || row.id}`;
}

function bookingEditable(row) {
  return ["未下单", "下单失败", "已取消", ""].includes(String(row.booking_status || ""));
}

function bookingStatusClass(status) {
  if (["下单失败"].includes(status)) return "exception";
  if (["已出单"].includes(status)) return "shipped";
  if (["已取消"].includes(status)) return "cancelled";
  return "pending";
}

function renderBookingStatus(row) {
  const status = String(row.booking_status || "未下单");
  if (status === "未下单") return "";
  return `
    <div class="booking-status-block">
      <span class="status ${bookingStatusClass(status)}">${escapeHtml(status === "排队中" || status === "提交中" ? "面单处理中" : status)}</span>
      ${row.label_print_status ? `<span class="status ${row.label_print_status === "打印成功" ? "signed" : row.label_print_status === "打印失败" ? "exception" : "pending"}">${escapeHtml(row.label_print_status)}</span>` : ""}
      ${row.booking_error ? `<div class="tracking-error">${escapeHtml(row.booking_error)}</div>` : ""}
      ${row.label_print_error ? `<div class="tracking-error">${escapeHtml(row.label_print_error)}</div>` : ""}
    </div>
  `;
}

function cleanTrackingEvent(value) {
  let text = String(value || "").trim();
  if (!text) return "";
  text = text.replace(/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s*/, "").trim();
  text = text.replace(/[，,；;。]?(如有疑问|如遇|如需|若有疑问|请联系快递员|或致电专属客服|感谢使用)[\s\S]*$/, "").trim();
  const arrived = text.match(/(?:您的)?快件已到(?:达)?[^，,。；;\n]*/);
  if (arrived) return arrived[0].replace(/[，,。；;]+$/, "").trim();
  const firstSentence = text.split(/[。；;\n]/).find(Boolean) || text;
  return firstSentence.replace(/[，,。；;]+$/, "").trim();
}

function trackingTraceLines(row) {
  const raw = String(row.tracking_raw || "").trim();
  if (raw) {
    try {
      const data = JSON.parse(raw);
      const traces = Array.isArray(data.data) ? data.data : [];
      const lines = traces
        .map((trace) => {
          const time = trace.ftime || trace.time || "";
          const context = trace.context || trace.Context || "";
          return `${time} ${context}`.trim();
        })
        .filter(Boolean);
      if (lines.length) return lines;
    } catch (error) {
      // Keep the UI resilient if a provider returns non-standard raw data.
    }
  }
  return row.tracking_last_event ? [row.tracking_last_event] : [];
}

function shipmentStatusCounts(rows) {
  return rows.reduce(
    (acc, row) => {
      acc.total += 1;
      acc[row.status] = (acc[row.status] || 0) + 1;
      return acc;
    },
    { total: 0 }
  );
}

function shipmentPageState(scope) {
  return scope === "admin" ? state.adminShipmentPage : state.storeShipmentPage;
}

function setShipmentPage(scope, page) {
  const nextPage = Math.max(1, Number(page) || 1);
  if (scope === "admin") {
    state.adminShipmentPage = nextPage;
  } else {
    state.storeShipmentPage = nextPage;
  }
}

function paginatedShipments(rows, scope) {
  const total = rows.length;
  const totalPages = Math.max(1, Math.ceil(total / SHIPMENT_PAGE_SIZE));
  const page = Math.min(Math.max(1, shipmentPageState(scope)), totalPages);
  setShipmentPage(scope, page);
  const start = (page - 1) * SHIPMENT_PAGE_SIZE;
  const end = Math.min(start + SHIPMENT_PAGE_SIZE, total);
  return {
    rows: rows.slice(start, end),
    total,
    totalPages,
    page,
    start,
    end,
  };
}

function renderShipmentPagination(scope, pageData) {
  if (!pageData.total) return "";
  const pageOptions = Array.from({ length: pageData.totalPages }, (_, index) => {
    const page = index + 1;
    return `<option value="${page}" ${page === pageData.page ? "selected" : ""}>第 ${page} 页</option>`;
  }).join("");
  return `
    <div class="pagination-bar">
      <div class="muted mini">显示 ${pageData.start + 1}-${pageData.end} / 共 ${pageData.total} 单，每页 ${SHIPMENT_PAGE_SIZE} 单</div>
      ${
        pageData.totalPages > 1
          ? `
            <div class="pagination-controls">
              <button class="btn secondary small" data-shipment-page="${scope}" data-page="${pageData.page - 1}" type="button" ${pageData.page <= 1 ? "disabled" : ""}>上一页</button>
              <select class="select pagination-select" data-shipment-page-select="${scope}">
                ${pageOptions}
              </select>
              <button class="btn secondary small" data-shipment-page="${scope}" data-page="${pageData.page + 1}" type="button" ${pageData.page >= pageData.totalPages ? "disabled" : ""}>下一页</button>
            </div>
          `
          : ""
      }
    </div>
  `;
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

async function copyText(value) {
  const text = String(value || "").trim();
  if (!text) {
    toast("没有可复制的快递单号。");
    return;
  }
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
  } else {
    const input = document.createElement("textarea");
    input.value = text;
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.focus();
    input.select();
    document.execCommand("copy");
    input.remove();
  }
  toast("已复制快递单号。");
}

function bindTrackingCopyButtons() {
  document.querySelectorAll("[data-copy-tracking]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const row = event.currentTarget.closest("[data-shipment]");
      const explicitTrackingNo = event.currentTarget.dataset.copyTracking || "";
      try {
        await copyText(explicitTrackingNo || row?.querySelector("[data-tracking]")?.value || "");
      } catch (error) {
        toast(error.message || "复制失败。");
      }
    });
  });
}

function bindShipmentPagination() {
  document.querySelectorAll("[data-shipment-page]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const scope = event.currentTarget.dataset.shipmentPage;
      setShipmentPage(scope, event.currentTarget.dataset.page);
      render();
    });
  });
  document.querySelectorAll("[data-shipment-page-select]").forEach((node) => {
    node.addEventListener("change", (event) => {
      const scope = event.currentTarget.dataset.shipmentPageSelect;
      setShipmentPage(scope, event.currentTarget.value);
      render();
    });
  });
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
        <a class="${isActive("/admin/returns")}" href="/admin/returns" data-route>退货看板</a>
        <a class="${isActive("/admin/stores")}" href="/admin/stores" data-route>门店</a>
        <a class="${isActive("/admin/products")}" href="/admin/products" data-route>商品</a>
        <a class="${isActive("/admin/shipping")}" href="/admin/shipping" data-route>面单设置</a>
      `
      : "";
  const storeLinks =
    state.user?.role === "staff"
      ? `
        <a class="${isActive("/shipments")}" href="/shipments" data-route>发货看板</a>
        <a class="${isActive("/returns/new")}" href="/returns/new" data-route>新增退货</a>
        <a class="${isActive("/returns")}" href="/returns" data-route>退货看板</a>
      `
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

  const summaryParams = new URLSearchParams(params);
  summaryParams.delete("status");
  const summaryData = await api(`/api/shipments?${summaryParams.toString()}`);
  state.adminShipmentSummary = summaryData.shipments || [];
}

async function loadShippingSettings() {
  const data = await api("/api/admin/shipping-settings");
  state.shippingSettings = data.settings || {};
  state.shippingConfig = data.shipping || {};
}

async function loadShippingBatchPreview(filters) {
  const data = await api("/api/admin/shipping-batches/preview", {
    method: "POST",
    body: JSON.stringify({ filters }),
  });
  state.batchPreview = data.preview || null;
  state.batchSelectedIds = (state.batchPreview?.eligible || []).map((row) => Number(row.id));
}

async function loadActiveShippingBatch() {
  const batchId = state.activeShippingBatch?.batch?.id || sessionStorage.getItem("scentpool_shipping_batch_id");
  if (!batchId) return;
  try {
    state.activeShippingBatch = await api(`/api/admin/shipping-batches/${batchId}`);
  } catch {
    state.activeShippingBatch = null;
    sessionStorage.removeItem("scentpool_shipping_batch_id");
  }
}

function scheduleShippingBatchPoll() {
  if (state.shippingBatchPollTimer) clearTimeout(state.shippingBatchPollTimer);
  const status = state.activeShippingBatch?.batch?.status;
  if (!status || !["排队中", "处理中"].includes(status) || location.pathname !== "/admin") return;
  state.shippingBatchPollTimer = setTimeout(() => render(), 2500);
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

async function loadReturnOrders(admin = false) {
  const filters = admin ? state.adminReturnFilters : state.storeReturnFilters;
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  const data = await api(`/api/returns?${params.toString()}`);
  if (admin) {
    state.returnOrders = data.returns || [];
  } else {
    state.storeReturnOrders = data.returns || [];
  }
  state.returnStatuses = data.statuses || state.returnStatuses;
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
  return validItems(state.submitItems);
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

function itemsFromProductSnapshots(items) {
  return (items || []).map((item) => ({
    category: item.product_category || "",
    barcode: item.product_barcode || "",
    quantity: item.quantity || 1,
  }));
}

function validItems(items) {
  return items
    .filter((item) => item.barcode && Number(item.quantity) > 0)
    .map((item) => ({ barcode: item.barcode, quantity: Number(item.quantity) }));
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
            <span class="muted mini">同一门店同一天的订单号不能重复；次日可重新从 1001 开始。</span>
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

function renderTrackingDetailBlock(row, options = {}) {
  const showCopy = Boolean(options.showCopy);
  const company = row.express_company || DEFAULT_EXPRESS_COMPANY;
  const summary = cleanTrackingEvent(row.tracking_last_event) || row.tracking_status || (row.tracking_error ? "物流查询失败" : "物流待查询");
  const traceLines = trackingTraceLines(row);
  const detailParts = [
    ...traceLines.map((line) => `<div>${escapeHtml(line)}</div>`),
    row.tracking_last_checked_at ? `<div>查询：${escapeHtml(formatDate(row.tracking_last_checked_at))}</div>` : "",
    row.tracking_signed_at ? `<div>签收：${escapeHtml(formatDate(row.tracking_signed_at))}</div>` : "",
    row.shipped_at ? `<div>发货：${escapeHtml(formatDate(row.shipped_at))}</div>` : "",
    row.tracking_error ? `<div class="tracking-error">错误：${escapeHtml(row.tracking_error)}</div>` : "",
    row.shipping_note ? `<div>备注：${escapeHtml(row.shipping_note)}</div>` : "",
  ].filter(Boolean);
  const statusHtml = row.tracking_status
    ? `<span class="status ${trackingClass(row.tracking_status)}">${escapeHtml(row.tracking_status)}</span>`
    : `<span class="muted mini">物流待查询</span>`;
  return `
    <div class="tracking-panel">
      <div class="tracking-summary-row">
        ${statusHtml}
        <strong>${escapeHtml(summary)}</strong>
      </div>
      <div class="tracking-number-row">
        <span>${escapeHtml(company)} ${escapeHtml(row.tracking_no)}</span>
        ${showCopy ? `<button class="btn secondary small" data-copy-tracking="${escapeHtml(row.tracking_no)}" type="button">复制</button>` : ""}
      </div>
      ${
        detailParts.length
          ? `<details class="tracking-details"><summary>显示详细物流信息</summary><div class="tracking-detail-lines">${detailParts.join("")}</div></details>`
          : ""
      }
    </div>
  `;
}

function renderTrackingInfo(row, options = {}) {
  if (row.tracking_no) {
    return renderTrackingDetailBlock(row, options);
  }
  if (!bookingEditable(row)) {
    return renderBookingStatus(row);
  }
  if (row.booking_status === "下单失败") {
    return renderBookingStatus(row);
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
  await ensureProductsGrouped();
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
  const pageData = paginatedShipments(state.storeShipments, "store");
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
      ${renderStoreBoardTable(pageData.rows)}
      ${renderShipmentPagination("store", pageData)}
    </section>
  `;
  document.getElementById("app").innerHTML = shell(content);
  bindCommon();
  bindStoreBoard();
}

function renderStoreBoardTable(shipments) {
  if (!shipments.length) return `<div class="empty">没有符合条件的发货单</div>`;
  return `
    <div class="table-wrap store-shipments-table">
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
	                    ${renderShipmentItemsWithEditButton(row)}
	                  </td>
	                  <td><span class="status ${statusClass(row.status)}">${escapeHtml(row.status)}</span></td>
	                  <td>${renderTrackingInfo(row, { showCopy: true })}</td>
                  <td>
                    ${row.remark ? `<div>${escapeHtml(row.remark)}</div>` : `<span class="muted">无</span>`}
                    ${row.shipping_note ? `<div class="muted mini">总部：${escapeHtml(row.shipping_note)}</div>` : ""}
	                  </td>
	                </tr>
	                ${renderShipmentEditRow(row, 7)}
	              `
	            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderShipmentItemsWithEditButton(row) {
  return `
    ${row.items ? renderItemLines(row.items) : `<span class="muted">无商品</span>`}
    ${
      row.status === "待处理" && bookingEditable(row)
        ? `<button class="btn secondary small" data-edit-shipment-items="${row.id}" type="button" style="margin-top: 8px;">编辑商品</button>`
        : ""
    }
  `;
}

function renderShipmentEditRow(row, colspan) {
  if (state.editingShipmentId !== row.id || row.status !== "待处理") return "";
  return `
    <tr class="shipment-edit-row">
      <td colspan="${colspan}">
        ${renderShipmentItemEditor(row)}
      </td>
    </tr>
  `;
}

function renderShipmentItemEditor(row) {
  const items = state.shipmentEditItems.length ? state.shipmentEditItems : itemsFromProductSnapshots(row.items);
  state.shipmentEditItems = items.length ? items : [{ category: "", barcode: "", quantity: 1 }];
  return `
    <div class="shipment-item-editor">
      <div class="editor-head">
        <div>
          <strong>编辑商品明细</strong>
          <div class="muted mini">${escapeHtml(row.store_order_no || `#${row.id}`)}</div>
        </div>
        <span class="status pending">待处理</span>
      </div>
      ${state.shipmentEditItems
        .map(
          (item, index) => `
            <div class="edit-product-row" data-edit-item-row="${index}">
              <div class="field">
                <label>分类</label>
                <select class="select" data-edit-item-category="${index}" aria-label="货品分类">
                  ${categoryOptions(item.category)}
                </select>
              </div>
              <div class="field">
                <label>商品</label>
                <select class="select" data-edit-item-product="${index}" aria-label="货品名称">
                  ${productOptions(item.category, item.barcode)}
                </select>
              </div>
              <div class="field">
                <label>数量</label>
                <input class="input" type="number" min="1" step="1" value="${escapeHtml(item.quantity || 1)}" data-edit-item-quantity="${index}" aria-label="数量" />
              </div>
              <button class="btn danger small" type="button" data-remove-edit-item="${index}">删</button>
            </div>
          `
        )
        .join("")}
      <div class="inline-actions">
        <button class="btn secondary small" data-add-edit-item type="button">添加</button>
        <button class="btn primary small" data-save-edit-items="${row.id}" type="button">保存商品</button>
        <button class="btn ghost small" data-cancel-edit-items type="button">取消</button>
      </div>
    </div>
  `;
}

function bindShipmentItemEditor(sourceRows) {
  document.querySelectorAll("[data-edit-shipment-items]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const id = Number(event.currentTarget.dataset.editShipmentItems);
      const row = sourceRows.find((item) => item.id === id);
      state.editingShipmentId = id;
      state.shipmentEditItems = itemsFromProductSnapshots(row?.items || []);
      render();
    });
  });
  document.querySelectorAll("[data-edit-item-category]").forEach((node) => {
    node.addEventListener("change", (event) => {
      const index = Number(event.currentTarget.dataset.editItemCategory);
      state.shipmentEditItems[index].category = event.currentTarget.value;
      state.shipmentEditItems[index].barcode = "";
      render();
    });
  });
  document.querySelectorAll("[data-edit-item-product]").forEach((node) => {
    node.addEventListener("change", (event) => {
      const index = Number(event.currentTarget.dataset.editItemProduct);
      state.shipmentEditItems[index].barcode = event.currentTarget.value;
      render();
    });
  });
  document.querySelectorAll("[data-edit-item-quantity]").forEach((node) => {
    node.addEventListener("input", (event) => {
      const index = Number(event.currentTarget.dataset.editItemQuantity);
      state.shipmentEditItems[index].quantity = Math.max(1, Number(event.currentTarget.value || 1));
    });
  });
  const addEditItem = document.querySelector("[data-add-edit-item]");
  if (addEditItem) {
    addEditItem.addEventListener("click", () => {
      state.shipmentEditItems.push({ category: "", barcode: "", quantity: 1 });
      render();
    });
  }
  document.querySelectorAll("[data-remove-edit-item]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const index = Number(event.currentTarget.dataset.removeEditItem);
      state.shipmentEditItems.splice(index, 1);
      if (!state.shipmentEditItems.length) state.shipmentEditItems.push({ category: "", barcode: "", quantity: 1 });
      render();
    });
  });
  const cancelEditItems = document.querySelector("[data-cancel-edit-items]");
  if (cancelEditItems) {
    cancelEditItems.addEventListener("click", () => {
      state.editingShipmentId = null;
      state.shipmentEditItems = [];
      render();
    });
  }
  document.querySelectorAll("[data-save-edit-items]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.saveEditItems;
      try {
        await api(`/api/shipments/${id}/items`, {
          method: "PATCH",
          body: JSON.stringify({ items: validItems(state.shipmentEditItems) }),
        });
        state.editingShipmentId = null;
        state.shipmentEditItems = [];
        toast("商品明细已更新。");
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  });
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
      state.storeShipmentPage = 1;
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
    state.storeShipmentPage = 1;
    render();
  });
  document.getElementById("resetStoreFilters").addEventListener("click", () => {
    state.storeFilters = { status: "", date_from: "", date_to: "", q: "" };
    state.storeShipmentPage = 1;
    render();
  });
  bindShipmentItemEditor(state.storeShipments);
}

function validReturnItems() {
  return validItems(state.returnItems);
}

function captureReturnDraft() {
  const form = document.getElementById("returnForm");
  if (!form) return;
  const data = new FormData(form);
  state.returnDraft = {
    store_id: data.get("store_id") || "",
    express_company: data.get("express_company") || DEFAULT_EXPRESS_COMPANY,
    tracking_no: data.get("tracking_no") || "",
    sender_phone: data.get("sender_phone") || "",
    remark: data.get("remark") || "",
  };
}

function renderReturnSummary() {
  const items = validReturnItems();
  if (!items.length) return `<p class="muted">还没有选择退货商品。</p>`;
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

async function renderReturnNew() {
  await ensureProductsGrouped();
  const itemRows = state.returnItems
    .map(
      (item, index) => `
        <div class="item-row" data-return-item-row="${index}">
          <select class="select" data-return-item-category="${index}" aria-label="退货商品分类">
            ${categoryOptions(item.category)}
          </select>
          <select class="select" data-return-item-product="${index}" aria-label="退货商品名称">
            ${productOptions(item.category, item.barcode)}
          </select>
          <input class="input" type="number" min="1" step="1" value="${escapeHtml(item.quantity || 1)}" data-return-item-quantity="${index}" aria-label="数量" />
          <button class="btn danger small" type="button" data-remove-return-item="${index}">删</button>
        </div>
      `
    )
    .join("");
  const content = `
    ${pageHead("新增退货", "门店登记退货快递单号和退货商品，总部可在退货看板查看物流进度。")}
    <div class="grid-2">
      <section class="panel panel-pad">
        <form id="returnForm">
          <div class="form-grid">
            <div class="field">
              <label>门店</label>
              <span class="store-badge">${escapeHtml(state.user.store_name || "当前门店")}</span>
            </div>
            <div class="field">
              <label for="returnCompany">快递公司</label>
              <select class="select" id="returnCompany" name="express_company">
                ${expressCompanyOptions(state.returnDraft.express_company)}
              </select>
            </div>
            <div class="field">
              <label for="returnTrackingNo">退货快递单号</label>
              <input class="input" id="returnTrackingNo" name="tracking_no" value="${escapeHtml(state.returnDraft.tracking_no)}" required />
            </div>
            <div class="field">
              <label for="returnSenderPhone">联系电话/顺丰尾号</label>
              <input class="input" id="returnSenderPhone" name="sender_phone" inputmode="tel" value="${escapeHtml(state.returnDraft.sender_phone)}" />
            </div>
            <div class="field full">
              <label for="returnRemark">备注</label>
              <textarea class="textarea" id="returnRemark" name="remark">${escapeHtml(state.returnDraft.remark)}</textarea>
            </div>
          </div>
          <div class="section-title" style="margin-top: 22px;">
            <h2>退货商品</h2>
            <button class="btn secondary small" id="addReturnItemBtn" type="button">添加</button>
          </div>
          <div class="item-stack" id="returnItemsBox">${itemRows}</div>
          <div class="split-actions">
            <span class="muted mini">同一门店内退货快递单号不能重复。</span>
            <button class="btn primary" type="submit">提交退货</button>
          </div>
        </form>
      </section>
      <aside class="panel panel-pad">
        <div class="section-title"><h2>退货明细</h2></div>
        ${renderReturnSummary()}
      </aside>
    </div>
  `;
  document.getElementById("app").innerHTML = shell(content);
  bindCommon();
  bindReturnNew();
}

function bindReturnNew() {
  document.querySelectorAll("[data-return-item-category]").forEach((node) => {
    node.addEventListener("change", (event) => {
      const index = Number(event.currentTarget.dataset.returnItemCategory);
      captureReturnDraft();
      state.returnItems[index].category = event.currentTarget.value;
      state.returnItems[index].barcode = "";
      render();
    });
  });
  document.querySelectorAll("[data-return-item-product]").forEach((node) => {
    node.addEventListener("change", (event) => {
      const index = Number(event.currentTarget.dataset.returnItemProduct);
      captureReturnDraft();
      state.returnItems[index].barcode = event.currentTarget.value;
      render();
    });
  });
  document.querySelectorAll("[data-return-item-quantity]").forEach((node) => {
    node.addEventListener("input", (event) => {
      const index = Number(event.currentTarget.dataset.returnItemQuantity);
      state.returnItems[index].quantity = Math.max(1, Number(event.currentTarget.value || 1));
      const aside = document.querySelector("aside.panel");
      if (aside) {
        aside.innerHTML = `<div class="section-title"><h2>退货明细</h2></div>${renderReturnSummary()}`;
      }
    });
  });
  document.querySelectorAll("[data-remove-return-item]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const index = Number(event.currentTarget.dataset.removeReturnItem);
      captureReturnDraft();
      state.returnItems.splice(index, 1);
      if (!state.returnItems.length) state.returnItems.push({ category: "", barcode: "", quantity: 1 });
      render();
    });
  });
  document.getElementById("addReturnItemBtn").addEventListener("click", () => {
    captureReturnDraft();
    state.returnItems.push({ category: "", barcode: "", quantity: 1 });
    render();
  });
  document.getElementById("returnForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const payload = {
      express_company: form.get("express_company"),
      tracking_no: form.get("tracking_no"),
      sender_phone: form.get("sender_phone"),
      remark: form.get("remark"),
      items: validReturnItems(),
    };
    try {
      await api("/api/returns", { method: "POST", body: JSON.stringify(payload) });
      state.returnItems = [{ category: "", barcode: "", quantity: 1 }];
      state.returnDraft = { store_id: "", express_company: DEFAULT_EXPRESS_COMPANY, tracking_no: "", sender_phone: "", remark: "" };
      toast("退货已提交。");
      navigate("/returns");
    } catch (error) {
      toast(error.message);
    }
  });
}

async function renderReturnBoard(admin = false) {
  if (admin) await loadStores();
  await loadReturnOrders(admin);
  const filters = admin ? state.adminReturnFilters : state.storeReturnFilters;
  const rows = admin ? state.returnOrders : state.storeReturnOrders;
  const today = localDate();
  const yesterday = localDate(-1);
  const todayData = await api(`/api/returns?date_from=${today}&date_to=${today}`).catch(() => ({ returns: [] }));
  const todayCounts = (todayData.returns || []).reduce(
    (acc, row) => {
      acc.total += 1;
      acc[row.status] = (acc[row.status] || 0) + 1;
      return acc;
    },
    { total: 0 }
  );
  const counts = rows.reduce(
    (acc, row) => {
      acc.total += 1;
      acc[row.status] = (acc[row.status] || 0) + 1;
      return acc;
    },
    { total: 0 }
  );
  const extra = `
    <div class="actions">
      ${admin ? `<button class="btn secondary" id="syncReturnTracking" type="button">同步退货物流</button>` : `<a class="btn primary" href="/returns/new" data-route>新增退货</a>`}
      <span class="count-pill">今日 ${todayCounts.total} 单</span>
      <span class="count-pill">今日签收 ${todayCounts["已签收"] || 0}</span>
      <span class="count-pill">共 ${counts.total} 单</span>
      <span class="count-pill">运输中 ${counts["运输中"] || 0}</span>
      <span class="count-pill">已签收 ${counts["已签收"] || 0}</span>
    </div>
  `;
  const storeFilter = admin
    ? `
      <div class="field">
        <label>门店</label>
        <select class="select" id="returnFilterStore">
          <option value="">全部</option>
          ${state.stores
            .map((store) => `<option value="${store.id}" ${String(store.id) === String(filters.store_id) ? "selected" : ""}>${escapeHtml(store.name)}</option>`)
            .join("")}
        </select>
      </div>
    `
    : "";
  const content = `
    ${pageHead(admin ? "退货看板" : "退货看板", admin ? "总部查看所有门店退货和签收进度。" : "查看本门店退货快递进度。", extra)}
    <section class="panel panel-pad">
      <div class="filters ${admin ? "admin-return-filters" : "store-return-filters"}">
        <div class="quick-filters">
          <button class="btn secondary small ${filters.date_from === today && filters.date_to === today ? "active" : ""}" data-return-preset="today" type="button">今日</button>
          <button class="btn secondary small ${filters.date_from === yesterday && filters.date_to === yesterday ? "active" : ""}" data-return-preset="yesterday" type="button">昨日</button>
        </div>
        ${storeFilter}
        <div class="field">
          <label>状态</label>
          <select class="select" id="returnFilterStatus">
            <option value="">全部</option>
            ${state.returnStatuses.map((status) => `<option value="${status}" ${status === filters.status ? "selected" : ""}>${status}</option>`).join("")}
          </select>
        </div>
        <div class="field">
          <label>开始日期</label>
          <input class="input" type="date" id="returnFilterFrom" value="${escapeHtml(filters.date_from)}" />
        </div>
        <div class="field">
          <label>结束日期</label>
          <input class="input" type="date" id="returnFilterTo" value="${escapeHtml(filters.date_to)}" />
        </div>
        <div class="field">
          <label>搜索</label>
          <input class="input" id="returnFilterQ" value="${escapeHtml(filters.q)}" placeholder="快递单号 / 电话 / 备注" />
        </div>
        <button class="btn primary" id="applyReturnFilters" type="button">筛选</button>
        <button class="btn secondary" id="resetReturnFilters" type="button">清空</button>
      </div>
      ${renderReturnTable(rows, admin)}
    </section>
  `;
  document.getElementById("app").innerHTML = shell(content);
  bindCommon();
  bindReturnBoard(admin);
}

function renderReturnTable(rows, admin = false) {
  if (!rows.length) return `<div class="empty">没有符合条件的退货单</div>`;
  return `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>提交时间</th>${admin ? "<th>门店</th>" : ""}<th>快递</th><th>退货商品</th><th>状态</th><th>备注</th>${admin ? "<th>操作</th>" : ""}
          </tr>
        </thead>
        <tbody>
          ${rows
            .map(
              (row) => `
                <tr class="shipment-row ${statusClass(row.status)}">
                  <td>${row.id}</td>
                  <td>${escapeHtml(formatDate(row.created_at))}</td>
                  ${admin ? `<td>${escapeHtml(row.store_name_snapshot)}</td>` : ""}
                  <td>${renderReturnTrackingInfo(row)}</td>
                  <td class="items-cell">${renderItemLines(row.items)}</td>
                  <td><span class="status ${statusClass(row.status)}">${escapeHtml(row.status)}</span></td>
                  <td>
                    ${row.sender_phone ? `<div class="muted mini">电话：${escapeHtml(row.sender_phone)}</div>` : ""}
                    ${row.remark ? `<div>${escapeHtml(row.remark)}</div>` : `<span class="muted">无</span>`}
                  </td>
                  ${admin ? `<td>${row.status !== "已签收" ? `<button class="btn secondary small" data-refresh-return="${row.id}" type="button">查物流</button>` : `<span class="muted mini">已签收</span>`}</td>` : ""}
                </tr>
              `
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderReturnTrackingInfo(row) {
  return renderTrackingDetailBlock(row);
}

function bindReturnBoard(admin = false) {
  const filters = admin ? state.adminReturnFilters : state.storeReturnFilters;
  document.querySelectorAll("[data-return-preset]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const preset = event.currentTarget.dataset.returnPreset;
      const targetDate = preset === "yesterday" ? localDate(-1) : localDate();
      const next = { ...filters, date_from: targetDate, date_to: targetDate };
      if (admin) state.adminReturnFilters = next;
      else state.storeReturnFilters = next;
      render();
    });
  });
  document.getElementById("applyReturnFilters").addEventListener("click", () => {
    const next = {
      store_id: admin ? document.getElementById("returnFilterStore").value : "",
      status: document.getElementById("returnFilterStatus").value,
      date_from: document.getElementById("returnFilterFrom").value,
      date_to: document.getElementById("returnFilterTo").value,
      q: document.getElementById("returnFilterQ").value.trim(),
    };
    if (admin) state.adminReturnFilters = next;
    else state.storeReturnFilters = next;
    render();
  });
  document.getElementById("resetReturnFilters").addEventListener("click", () => {
    const empty = { store_id: "", status: "", date_from: "", date_to: "", q: "" };
    if (admin) state.adminReturnFilters = empty;
    else state.storeReturnFilters = empty;
    render();
  });
  const syncReturnTracking = document.getElementById("syncReturnTracking");
  if (syncReturnTracking) {
    syncReturnTracking.addEventListener("click", async () => {
      try {
        const data = await api("/api/admin/return-tracking/sync", {
          method: "POST",
          body: JSON.stringify({ limit: 50 }),
        });
        const result = data.result || {};
        toast(`已同步 ${result.checked || 0} 单，签收 ${result.signed || 0} 单。`);
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  }
  document.querySelectorAll("[data-refresh-return]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.refreshReturn;
      try {
        await api(`/api/returns/${id}/tracking/refresh`, { method: "POST", body: JSON.stringify({}) });
        toast("退货物流已刷新。");
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  });
}

function renderShippingBatchPreview() {
  const preview = state.batchPreview;
  if (!preview) return "";
  const eligible = preview.eligible || [];
  const eligibleIds = new Set(eligible.map((row) => Number(row.id)));
  const selectedIds = new Set(state.batchSelectedIds.filter((id) => eligibleIds.has(Number(id))).map(Number));
  const config = state.shippingConfig || {};
  const configReady = Boolean(config.enabled && config.configured);
  const missingConfig = Array.isArray(config.missing) ? config.missing : [];
  return `
    <section class="panel panel-pad shipping-batch-panel">
      <div class="section-title">
        <div><h2>选择需要打单的订单</h2><div class="muted mini">筛选匹配 ${preview.matched || 0} 单，可打单 ${eligible.length} 单，已选择 <span id="batchSelectedCount">${selectedIds.size}</span> 单</div></div>
        <button class="btn ghost small" id="closeBatchPreview" type="button">关闭</button>
      </div>
      <div class="batch-filter-grid">
        <div class="field">
          <label for="batchFilterStore">门店</label>
          <select class="select" id="batchFilterStore">
            <option value="">全部门店</option>
            ${state.stores.map((store) => `<option value="${store.id}" ${String(store.id) === String(state.batchFilters.store_id) ? "selected" : ""}>${escapeHtml(store.name)}</option>`).join("")}
          </select>
        </div>
        <div class="field"><label for="batchFilterFrom">开始日期</label><input class="input" id="batchFilterFrom" type="date" value="${escapeHtml(state.batchFilters.date_from)}" /></div>
        <div class="field"><label for="batchFilterTo">结束日期</label><input class="input" id="batchFilterTo" type="date" value="${escapeHtml(state.batchFilters.date_to)}" /></div>
        <div class="field"><label for="batchFilterQ">搜索订单</label><input class="input" id="batchFilterQ" value="${escapeHtml(state.batchFilters.q)}" placeholder="业务ID / 门店订单号 / 收件人" /></div>
        <button class="btn primary" id="applyBatchFilters" type="button">筛选</button>
        <button class="btn secondary" id="resetBatchFilters" type="button">清空</button>
      </div>
      ${!preview.settings_ready ? `<div class="notice danger-notice">总部寄件信息未完成，请先进入“面单设置”。</div>` : ""}
      ${!preview.label_ready ? `<div class="notice danger-notice">菜鸟电子面单账号尚未授权。</div>` : ""}
      ${!config.enabled ? `<div class="notice danger-notice">电子面单服务开关未开启：请在 Render 将 <strong>SCENTPOOL_KUAIDI100_LABEL_ENABLED</strong> 设置为 <strong>1</strong>。</div>` : ""}
      ${missingConfig.length ? `<div class="notice danger-notice">Render 还缺少：<strong>${missingConfig.map(escapeHtml).join("、")}</strong>。补齐并重新部署后即可正式提交。</div>` : ""}
      <div class="batch-controls label-batch-controls">
        <div class="field">
          <label>已选订单统一改为</label>
          <select class="select" id="batchBulkCompany">${expressCompanyOptions(DEFAULT_EXPRESS_COMPANY)}</select>
        </div>
        <div class="inline-actions batch-selection-actions">
          <button class="btn secondary small" id="selectAllBatchOrders" type="button">全选筛选结果</button>
          <button class="btn ghost small" id="clearBatchOrders" type="button">取消全选</button>
        </div>
        <button class="btn primary" id="createShippingBatch" data-ready="${preview.settings_ready && preview.label_ready && configReady ? "1" : "0"}" type="button" ${selectedIds.size && preview.settings_ready && preview.label_ready && configReady ? "" : "disabled"}>确认提交 ${selectedIds.size} 单</button>
      </div>
      <div class="notice">提交后将立即获取快递单号并生成电子面单，不再创建上门取件预约。</div>
      <div class="batch-order-list">
        ${eligible.map((row) => `
          <div class="batch-order-row" data-batch-shipment="${row.id}">
            <input class="batch-order-checkbox" type="checkbox" data-batch-select value="${row.id}" aria-label="选择订单 ${escapeHtml(row.business_id)}" ${selectedIds.has(Number(row.id)) ? "checked" : ""} />
            <div><strong>${escapeHtml(row.business_id)}</strong><div class="muted mini">${escapeHtml(row.store_name_snapshot)} · ${escapeHtml(row.recipient_name)} · ${escapeHtml(row.address)}</div></div>
            <select class="select" data-batch-company>${expressCompanyOptions(row.express_company)}</select>
          </div>
        `).join("") || `<div class="empty">当前筛选没有可下单订单</div>`}
      </div>
      ${(preview.excluded || []).length ? `<details class="tracking-details"><summary>查看被排除的 ${(preview.excluded || []).length} 单</summary><div class="tracking-detail-lines">${preview.excluded.map((row) => `<div>${escapeHtml(row.business_id)}：${escapeHtml(row.reason)}</div>`).join("")}</div></details>` : ""}
    </section>
  `;
}

function renderShippingBatchProgress() {
  const data = state.activeShippingBatch;
  if (!data?.batch) return "";
  const batch = data.batch;
  const counts = data.counts || {};
  const failed = counts["失败"] || 0;
  return `
    <section class="panel panel-pad shipping-batch-panel">
      <div class="section-title">
        <div><h2>电子面单批次 #${batch.id}</h2><div class="muted mini">后台按顺序取号并生成面单</div></div>
        <span class="status ${failed ? "exception" : batch.status === "已完成" ? "shipped" : "pending"}">${escapeHtml(batch.status)}</span>
      </div>
      <div class="status-overview">
        <span class="count-pill">总数 ${batch.total_count || 0}</span>
        <span class="count-pill">排队 ${counts["排队中"] || 0}</span>
        <span class="count-pill">提交中 ${counts["提交中"] || 0}</span>
        <span class="count-pill">成功 ${counts["成功"] || 0}</span>
        <span class="count-pill">失败 ${failed}</span>
      </div>
      <div class="inline-actions">
        ${failed ? `<button class="btn secondary small" id="retryShippingBatch" type="button">仅重试失败订单</button>` : ""}
        <button class="btn ghost small" id="closeShippingBatch" type="button">收起批次</button>
      </div>
      ${failed ? `<div class="batch-errors">${(data.items || []).filter((item) => item.status === "失败").map((item) => `<div><strong>${escapeHtml(item.business_id)}</strong> ${escapeHtml(item.error || "下单失败")}</div>`).join("")}</div>` : ""}
    </section>
  `;
}

async function renderAdmin() {
  await ensureProductsGrouped();
  await loadStores();
  await loadShipments();
  await loadShippingSettings();
  await loadActiveShippingBatch();
  const today = localDate();
  const yesterday = localDate(-1);
  const exportParams = new URLSearchParams();
  Object.entries(state.adminFilters).forEach(([key, value]) => {
    if (value) exportParams.set(key, value);
  });
  const counts = shipmentStatusCounts(state.adminShipmentSummary);
  const pageData = paginatedShipments(state.shipments, "admin");
  const content = `
    ${pageHead(
      "发货后台",
      "总部统一处理门店提交的发货需求。",
      `<div class="actions">
        <button class="btn primary" id="previewShippingBatch" type="button">批量打单</button>
        <button class="btn secondary" id="syncTracking" type="button">同步物流</button>
        <a class="btn secondary" href="/api/export/cainiao.xlsx?${exportParams.toString()}">菜鸟打印数据</a>
        <a class="btn primary" href="/api/export/shipments.xlsx?${exportParams.toString()}">导出 XLSX</a>
        <a class="btn secondary" href="/api/export/shipments.csv?${exportParams.toString()}">导出 CSV</a>
        <a class="btn secondary" href="/api/admin/backup.db">备份数据库</a>
      </div>`
    )}
    ${renderShippingBatchPreview()}
    ${renderShippingBatchProgress()}
    <section class="panel panel-pad">
      <div class="status-overview">
        <span class="count-pill">当前范围 ${counts.total} 单</span>
        <span class="count-pill">待处理 ${counts["待处理"] || 0}</span>
        <span class="count-pill">已发货 ${counts["已发货"] || 0}</span>
        <span class="count-pill">已签收 ${counts["已签收"] || 0}</span>
        <span class="count-pill">异常 ${counts["异常"] || 0}</span>
      </div>
      <div class="filters admin-filters">
        <div class="quick-filters">
          <button class="btn secondary small ${state.adminFilters.date_from === today && state.adminFilters.date_to === today ? "active" : ""}" data-admin-preset="today" type="button">今日</button>
          <button class="btn secondary small ${state.adminFilters.date_from === yesterday && state.adminFilters.date_to === yesterday ? "active" : ""}" data-admin-preset="yesterday" type="button">昨日</button>
        </div>
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
          <input class="input" id="filterQ" value="${escapeHtml(state.adminFilters.q)}" placeholder="业务ID / 订单号 / 姓名 / 电话 / 单号" />
        </div>
        <button class="btn primary" id="applyFilters" type="button">筛选</button>
        <button class="btn secondary" id="resetFilters" type="button">清空</button>
      </div>
      ${renderShipmentBoard(pageData.rows, pageData.start)}
      ${renderShipmentPagination("admin", pageData)}
    </section>
  `;
  document.getElementById("app").innerHTML = shell(content);
  bindCommon();
  bindAdmin();
  scheduleShippingBatchPoll();
}

function renderShipmentBoard(shipments) {
  if (!shipments.length) return `<div class="empty">没有符合条件的发货单</div>`;
  const groups = [];
  shipments.forEach((row) => {
    const day = row.order_date || datePart(row.created_at);
    let group = groups.find((item) => item.day === day);
    if (!group) {
      group = { day, rows: [] };
      groups.push(group);
    }
    group.rows.push(row);
  });
  return groups
    .map(
      (group) => `
        <div class="date-shipment-group">
          <div class="date-group-title">
            <h3>${escapeHtml(group.day)}</h3>
            <span class="count-pill">${group.rows.length} 单</span>
          </div>
          ${renderShipmentTable(group.rows)}
        </div>
      `
    )
    .join("");
}

function shipmentShippingEditing(row) {
  if (!bookingEditable(row)) return false;
  return state.editingShipmentShippingId === row.id || !String(row.tracking_no || "").trim();
}

function renderAdminShipmentStatusCell(row) {
  if (!shipmentShippingEditing(row)) {
    return `<span class="status ${statusClass(row.status)}">${escapeHtml(row.status)}</span>${renderBookingStatus(row)}`;
  }
  return `
    <select class="table-input" data-status>
      ${state.statuses.map((status) => `<option value="${status}" ${status === row.status ? "selected" : ""}>${status}</option>`).join("")}
    </select>
  `;
}

function renderAdminShipmentShippingCell(row) {
  if (!shipmentShippingEditing(row)) {
    if (!row.tracking_no) return `<span class="muted">快递平台正在分配单号</span>`;
    return renderTrackingDetailBlock(row, { showCopy: true });
  }
  return `
    <div class="shipping-editor">
      <label>
        <span>快递公司</span>
        <select class="table-input" data-company>
          ${expressCompanyOptions(row.express_company)}
        </select>
      </label>
      <label>
        <span>快递单号</span>
        <div class="tracking-input-row">
          <input class="table-input" data-tracking value="${escapeHtml(row.tracking_no)}" placeholder="快递单号" />
          <button class="btn secondary small" data-copy-tracking type="button" style="${row.tracking_no ? "" : "display: none;"}">复制</button>
        </div>
      </label>
      <label>
        <span>发货备注</span>
        <input class="table-input" data-note value="${escapeHtml(row.shipping_note)}" placeholder="可选" />
      </label>
    </div>
  `;
}

function renderShipmentActions(row) {
  const editing = shipmentShippingEditing(row);
  const shippedAt = row.shipped_at ? `<div class="muted mini action-time">${escapeHtml(formatDate(row.shipped_at))}</div>` : "";
  if (!bookingEditable(row)) {
    const canCancel = row.booking_task_id && row.tracking_no && row.status !== "已签收";
    return `
      <div class="shipment-actions">
        ${row.label_url ? `<a class="btn secondary small" href="${escapeHtml(row.label_url)}" target="_blank" rel="noopener">查看面单</a>` : ""}
        ${row.label_url && row.label_print_status !== "打印成功" ? `<button class="btn secondary small" data-label-printed="${row.id}" type="button">标记已打印</button>` : ""}
        ${row.label_print_type === "CLOUD" && row.booking_task_id ? `<button class="btn secondary small" data-reprint-label="${row.id}" type="button">复打面单</button>` : ""}
        ${canCancel ? `<button class="btn danger small" data-cancel-label="${row.id}" type="button">取消面单</button>` : `<span class="muted mini">面单处理中</span>`}
        ${row.tracking_no ? `<button class="btn secondary small" data-refresh-tracking="${row.id}" type="button">查物流</button>` : ""}
        ${shippedAt}
      </div>
    `;
  }
  if (editing) {
    return `
      <div class="shipment-actions">
        <button class="btn primary small" data-save-shipment="${row.id}" type="button">保存</button>
        ${row.tracking_no ? `<button class="btn ghost small" data-cancel-shipping="${row.id}" type="button">取消</button>` : ""}
        <button class="btn danger small" data-delete-shipment="${row.id}" data-order-no="${escapeHtml(row.store_order_no)}" type="button">删除</button>
        ${shippedAt}
      </div>
    `;
  }
  return `
    <div class="shipment-actions">
      <button class="btn secondary small" data-edit-shipping="${row.id}" type="button">编辑</button>
      ${row.tracking_no ? `<button class="btn secondary small" data-refresh-tracking="${row.id}" type="button">查物流</button>` : ""}
      <button class="btn danger small" data-delete-shipment="${row.id}" data-order-no="${escapeHtml(row.store_order_no)}" type="button">删除</button>
      ${shippedAt}
    </div>
  `;
}

function renderShipmentTable(shipments) {
  if (!shipments.length) return `<div class="empty">没有符合条件的发货单</div>`;
  return `
    <div class="table-wrap shipments-table">
      <table>
        <colgroup>
          <col class="col-seq" />
          <col class="col-business" />
          <col class="col-created" />
          <col class="col-store" />
          <col class="col-order" />
          <col class="col-recipient" />
          <col class="col-items" />
          <col class="col-status" />
          <col class="col-shipping" />
          <col class="col-actions" />
        </colgroup>
        <thead>
          <tr>
            <th>序号</th><th>业务ID</th><th>提交</th><th>门店</th><th>订单</th><th>收件信息</th><th>商品</th><th>状态</th><th>快递</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          ${shipments
            .map(
              (row) => `
                <tr class="shipment-row ${statusClass(row.status)}" data-shipment="${row.id}">
                  <td>${row.id}</td>
                  <td class="business-cell">
                    <strong class="business-id">${escapeHtml(shipmentBusinessId(row))}</strong>
                  </td>
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
	                    ${renderShipmentItemsWithEditButton(row)}
	                  </td>
                  <td class="status-cell">
                    ${renderAdminShipmentStatusCell(row)}
                  </td>
                  <td class="shipping-cell">
                    ${renderAdminShipmentShippingCell(row)}
                  </td>
                  <td class="actions-cell">
                    ${renderShipmentActions(row)}
	                  </td>
	                </tr>
	                ${renderShipmentEditRow(row, 10)}
	              `
	            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function bindAdmin() {
  const previewButton = document.getElementById("previewShippingBatch");
  if (previewButton) {
    previewButton.addEventListener("click", async () => {
      try {
        state.batchFilters = {
          store_id: state.adminFilters.store_id,
          status: "待处理",
          date_from: state.adminFilters.date_from,
          date_to: state.adminFilters.date_to,
          q: state.adminFilters.q,
        };
        await loadShippingBatchPreview(state.batchFilters);
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  }
  document.getElementById("closeBatchPreview")?.addEventListener("click", () => {
    state.batchPreview = null;
    state.batchSelectedIds = [];
    render();
  });
  const updateBatchSelection = () => {
    const selected = Array.from(document.querySelectorAll("[data-batch-select]:checked")).map((node) => Number(node.value));
    state.batchSelectedIds = selected;
    const count = document.getElementById("batchSelectedCount");
    if (count) count.textContent = String(selected.length);
    const submit = document.getElementById("createShippingBatch");
    if (submit) {
      submit.textContent = `确认提交 ${selected.length} 单`;
      submit.disabled = !selected.length || submit.dataset.ready !== "1";
    }
  };
  document.querySelectorAll("[data-batch-select]").forEach((node) => node.addEventListener("change", updateBatchSelection));
  document.getElementById("selectAllBatchOrders")?.addEventListener("click", () => {
    document.querySelectorAll("[data-batch-select]").forEach((node) => { node.checked = true; });
    updateBatchSelection();
  });
  document.getElementById("clearBatchOrders")?.addEventListener("click", () => {
    document.querySelectorAll("[data-batch-select]").forEach((node) => { node.checked = false; });
    updateBatchSelection();
  });
  document.getElementById("applyBatchFilters")?.addEventListener("click", async () => {
    state.batchFilters = {
      store_id: document.getElementById("batchFilterStore").value,
      status: "待处理",
      date_from: document.getElementById("batchFilterFrom").value,
      date_to: document.getElementById("batchFilterTo").value,
      q: document.getElementById("batchFilterQ").value.trim(),
    };
    try {
      await loadShippingBatchPreview(state.batchFilters);
      render();
    } catch (error) {
      toast(error.message);
    }
  });
  document.getElementById("resetBatchFilters")?.addEventListener("click", async () => {
    state.batchFilters = { store_id: "", status: "待处理", date_from: "", date_to: "", q: "" };
    try {
      await loadShippingBatchPreview(state.batchFilters);
      render();
    } catch (error) {
      toast(error.message);
    }
  });
  document.getElementById("batchBulkCompany")?.addEventListener("change", (event) => {
    document.querySelectorAll("[data-batch-shipment]").forEach((row) => {
      if (row.querySelector("[data-batch-select]")?.checked) row.querySelector("[data-batch-company]").value = event.currentTarget.value;
    });
  });
  document.getElementById("createShippingBatch")?.addEventListener("click", async () => {
    const shipments = Array.from(document.querySelectorAll("[data-batch-shipment]"))
      .filter((row) => row.querySelector("[data-batch-select]")?.checked)
      .map((row) => ({
        id: Number(row.dataset.batchShipment),
        express_company: row.querySelector("[data-batch-company]").value,
      }));
    if (!shipments.length) {
      toast("请至少选择一个需要打单的订单。");
      return;
    }
    if (!confirm(`确认向快递100提交 ${shipments.length} 张电子面单？成功后将立即取得快递单号。`)) return;
    try {
      const data = await api("/api/admin/shipping-batches", {
        method: "POST",
        body: JSON.stringify({
          filters: state.batchFilters,
          shipments,
        }),
      });
      state.batchPreview = null;
      state.activeShippingBatch = data;
      sessionStorage.setItem("scentpool_shipping_batch_id", String(data.batch.id));
      toast("电子面单任务已创建。即使关闭页面，后台也会继续处理。");
      render();
    } catch (error) {
      toast(error.message);
    }
  });
  document.getElementById("retryShippingBatch")?.addEventListener("click", async () => {
    const batchId = state.activeShippingBatch?.batch?.id;
    if (!batchId) return;
    try {
      state.activeShippingBatch = await api(`/api/admin/shipping-batches/${batchId}/retry`, { method: "POST", body: JSON.stringify({}) });
      toast("失败订单已重新加入队列。");
      render();
    } catch (error) {
      toast(error.message);
    }
  });
  document.getElementById("closeShippingBatch")?.addEventListener("click", () => {
    state.activeShippingBatch = null;
    sessionStorage.removeItem("scentpool_shipping_batch_id");
    render();
  });
  document.querySelectorAll("[data-admin-preset]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const preset = event.currentTarget.dataset.adminPreset;
      const targetDate = preset === "yesterday" ? localDate(-1) : localDate();
      state.adminFilters = {
        ...state.adminFilters,
        date_from: targetDate,
        date_to: targetDate,
      };
      state.adminShipmentPage = 1;
      render();
    });
  });
  document.getElementById("applyFilters").addEventListener("click", () => {
    state.adminFilters = {
      store_id: document.getElementById("filterStore").value,
      status: document.getElementById("filterStatus").value,
      date_from: document.getElementById("filterFrom").value,
      date_to: document.getElementById("filterTo").value,
      q: document.getElementById("filterQ").value.trim(),
    };
    state.adminShipmentPage = 1;
    render();
  });
  document.getElementById("resetFilters").addEventListener("click", () => {
    state.adminFilters = { store_id: "", status: "", date_from: "", date_to: "", q: "" };
    state.adminShipmentPage = 1;
    render();
  });
  document.getElementById("syncTracking").addEventListener("click", async () => {
    try {
      const data = await api("/api/admin/tracking/sync", {
        method: "POST",
        body: JSON.stringify({ force: true, limit: 0 }),
      });
      const result = data.result || {};
      const skipped = result.skipped_recent || 0;
      const failed = result.errors || 0;
      toast(`已同步 ${result.checked || 0} 单，签收 ${result.signed || 0} 单${failed ? `，失败 ${failed} 单` : ""}${skipped ? `；另有 ${skipped} 单在 30 分钟保护期内` : ""}。`);
      render();
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelectorAll("[data-edit-shipping]").forEach((node) => {
    node.addEventListener("click", (event) => {
      state.editingShipmentShippingId = Number(event.currentTarget.dataset.editShipping);
      render();
    });
  });
  document.querySelectorAll("[data-cancel-shipping]").forEach((node) => {
    node.addEventListener("click", () => {
      state.editingShipmentShippingId = null;
      render();
    });
  });
  document.querySelectorAll("[data-refresh-tracking]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.refreshTracking;
      try {
        await api(`/api/shipments/${id}/tracking/refresh`, { method: "POST", body: JSON.stringify({}) });
        toast("物流已刷新。");
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  });
  document.querySelectorAll("[data-cancel-label]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.cancelLabel;
      if (!confirm("确认取消并回收这张电子面单？只有快递公司确认成功后订单才会解锁。")) return;
      try {
        await api(`/api/shipments/${id}/label/cancel`, { method: "POST", body: JSON.stringify({}) });
        toast("电子面单已取消。");
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  });
  document.querySelectorAll("[data-reprint-label]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.reprintLabel;
      try {
        await api(`/api/shipments/${id}/label/reprint`, { method: "POST", body: JSON.stringify({}) });
        toast("复打任务已发送到快递100云打印机。");
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  });
  document.querySelectorAll("[data-label-printed]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.labelPrinted;
      try {
        await api(`/api/shipments/${id}/label/printed`, { method: "POST", body: JSON.stringify({}) });
        toast("面单已标记为打印成功。");
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  });
  document.querySelectorAll("[data-tracking]").forEach((node) => {
    node.addEventListener("input", (event) => {
      const row = event.currentTarget.closest("[data-shipment]");
      const button = row?.querySelector("[data-copy-tracking]");
      if (button) button.style.display = event.currentTarget.value.trim() ? "" : "none";
    });
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
        state.editingShipmentShippingId = null;
        toast("已保存。");
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  });
  document.querySelectorAll("[data-delete-shipment]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.deleteShipment;
      const orderNo = event.currentTarget.dataset.orderNo || id;
      if (!confirm(`确认删除发货记录 ${orderNo}？删除后不可恢复。`)) return;
      try {
        await api(`/api/shipments/${id}`, { method: "DELETE" });
        toast("发货记录已删除。");
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  });
  bindShipmentItemEditor(state.shipments);
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

async function renderShippingSettings() {
  await loadShippingSettings();
  const settings = state.shippingSettings || {};
  const config = state.shippingConfig || {};
  const carrierSettings = settings.carrier_settings || {};
  const branchOptions = settings.branch_options || [];
  const branchSelect = (company) => {
    const current = carrierSettings[company]?.tbNet || "";
    const options = branchOptions.filter((item) => item.company === company);
    return `<select class="select" data-carrier-branch="${company}">
      <option value="">${options.length ? "选择授权网点" : "授权后刷新网点"}</option>
      ${options.map((item) => `<option value="${escapeHtml(item.tbNet)}" ${item.tbNet === current ? "selected" : ""}>${escapeHtml(item.branchName || item.tbNet)} · 余 ${item.quantity}</option>`).join("")}
    </select>`;
  };
  const content = `
    ${pageHead("电子面单设置", "在系统内完成菜鸟授权、快递取号、面单生成与打印。")}
    <form id="shippingSettingsForm">
    <div class="grid-2 shipping-settings-grid">
      <section class="panel panel-pad">
        <div class="section-title"><h2>总部寄件信息</h2></div>
        <div class="form-grid">
          <div class="field">
            <label for="senderName">寄件人姓名</label>
            <input class="input" id="senderName" name="sender_name" value="${escapeHtml(settings.sender_name || "")}" required />
          </div>
          <div class="field">
            <label for="senderMobile">联系电话</label>
            <input class="input" id="senderMobile" name="sender_mobile" value="${escapeHtml(settings.sender_mobile || "")}" required />
          </div>
          <div class="field full">
            <label for="senderCompany">寄件公司</label>
            <input class="input" id="senderCompany" name="sender_company" value="${escapeHtml(settings.sender_company || "万物香铺")}" />
          </div>
          <div class="field full">
            <label for="senderAddress">完整寄件地址</label>
            <textarea class="textarea" id="senderAddress" name="sender_address" required>${escapeHtml(settings.sender_address || "")}</textarea>
          </div>
          <div class="field">
            <label for="defaultCompany">默认快递公司</label>
            <select class="select" id="defaultCompany" name="default_company">${expressCompanyOptions(settings.default_company || DEFAULT_EXPRESS_COMPANY)}</select>
          </div>
          <div class="field">
            <label for="cargoName">物品名称</label>
            <input class="input" id="cargoName" name="cargo_name" value="${escapeHtml(settings.cargo_name || "香氛商品")}" required />
          </div>
          <div class="field">
            <label for="payType">付款方式</label>
            <select class="select" id="payType" name="pay_type"><option value="MONTHLY" ${settings.pay_type === "MONTHLY" ? "selected" : ""}>月结</option><option value="SHIPPER" ${settings.pay_type === "SHIPPER" ? "selected" : ""}>寄方付</option></select>
          </div>
        </div>
      </section>
      <section class="panel panel-pad">
        <div class="section-title"><h2>菜鸟电子面单授权</h2></div>
        <ul class="summary-list">
          <li><span>功能开关</span><span class="status ${config.enabled ? "shipped" : "pending"}">${config.enabled ? "已开启" : "未开启"}</span></li>
          <li><span>企业 KEY</span><span class="status ${config.key_configured ? "shipped" : "exception"}">${config.key_configured ? "已配置" : "未配置"}</span></li>
          <li><span>LABEL SECRET</span><span class="status ${config.secret_configured ? "shipped" : "exception"}">${config.secret_configured ? "已配置" : "未配置"}</span></li>
          <li><span>公网回调地址</span><span class="status ${config.public_base_url_configured ? "shipped" : "exception"}">${config.public_base_url_configured ? "已配置" : "未配置"}</span></li>
          <li><span>菜鸟账号</span><span class="status ${settings.partner_authorized ? "signed" : "pending"}">${settings.partner_authorized ? "已授权" : "未授权"}</span></li>
          ${config.missing?.length ? `<li><span>Render 缺少</span><strong>${config.missing.map(escapeHtml).join("、")}</strong></li>` : ""}
          ${settings.partner_authorized ? `<li><span>partnerId</span><strong>${escapeHtml(settings.partner_id_masked || "已保存")}</strong></li>` : ""}
        </ul>
        <div class="inline-actions">
          <button class="btn primary" id="authorizeCainiao" type="button">${settings.partner_authorized ? "重新授权菜鸟" : "授权菜鸟账号"}</button>
          <button class="btn secondary" id="refreshLabelBranches" type="button" ${settings.partner_authorized ? "" : "disabled"}>刷新网点与面单余额</button>
        </div>
        <div class="notice">企业 KEY 与 LABEL SECRET 只放在 Render Environment；菜鸟授权凭证由回调写入数据库，页面只显示脱敏结果。</div>
      </section>
    </div>
    <section class="panel panel-pad label-carrier-settings">
      <div class="section-title"><div><h2>快递公司与授权网点</h2><div class="muted mini">每家公司选择菜鸟授权网点和默认产品类型。</div></div></div>
      <div class="carrier-setting-grid">
        ${EXPRESS_COMPANIES.map((company) => `
          <div class="carrier-setting-row">
            <strong>${company}</strong>
            ${branchSelect(company)}
            <input class="input" data-carrier-exp="${company}" value="${escapeHtml(carrierSettings[company]?.expType || (company === "顺丰" ? "顺丰标快" : "标准快递"))}" aria-label="${company}产品类型" />
          </div>
        `).join("")}
      </div>
    </section>
    <section class="panel panel-pad label-print-settings">
      <div class="section-title"><div><h2>面单打印</h2><div class="muted mini">菜鸟授权默认返回 PDF；快递100云打印需要云打印设备码。</div></div></div>
      <div class="form-grid">
        <div class="field"><label for="printMode">打印方式</label><select class="select" id="printMode" name="print_mode"><option value="PDF" ${settings.print_mode !== "CLOUD" ? "selected" : ""}>菜鸟 PDF 面单</option><option value="CLOUD" ${settings.print_mode === "CLOUD" ? "selected" : ""}>快递100云打印</option></select></div>
        <div class="field"><label for="printerSiid">云打印设备码 siid</label><input class="input" id="printerSiid" name="printer_siid" value="${escapeHtml(settings.printer_siid || "")}" /></div>
        <div class="field"><label for="templateId">网点面单模板 tempId</label><input class="input" id="templateId" name="template_id" value="${escapeHtml(settings.template_id || "")}" /></div>
        <div class="field"><label>纸张尺寸（毫米）</label><div class="tracking-input-row"><input class="input" name="paper_width" value="${escapeHtml(settings.paper_width || "100")}" aria-label="纸张宽度" /><input class="input" name="paper_height" value="${escapeHtml(settings.paper_height || "180")}" aria-label="纸张高度" /></div></div>
        <label class="check-row"><input type="checkbox" name="need_desensitization" ${settings.need_desensitization ? "checked" : ""} /> 电话号码脱敏</label>
        <label class="check-row"><input type="checkbox" name="need_logo" ${settings.need_logo ? "checked" : ""} /> 面单显示 Logo</label>
      </div>
      <div class="inline-actions settings-save-actions"><button class="btn primary" type="submit">保存电子面单设置</button></div>
    </section>
    </form>
  `;
  document.getElementById("app").innerHTML = shell(content);
  bindCommon();
  document.getElementById("shippingSettingsForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const carrier_settings = {};
    EXPRESS_COMPANIES.forEach((company) => {
      carrier_settings[company] = {
        tbNet: document.querySelector(`[data-carrier-branch="${company}"]`)?.value || "",
        expType: document.querySelector(`[data-carrier-exp="${company}"]`)?.value.trim() || (company === "顺丰" ? "顺丰标快" : "标准快递"),
      };
    });
    try {
      const data = await api("/api/admin/shipping-settings", {
        method: "PUT",
        body: JSON.stringify({
          ...Object.fromEntries(form.entries()),
          need_desensitization: form.has("need_desensitization"),
          need_logo: form.has("need_logo"),
          carrier_settings,
        }),
      });
      state.shippingSettings = data.settings;
      state.shippingConfig = data.shipping;
      toast("电子面单设置已保存。");
      render();
    } catch (error) {
      toast(error.message);
    }
  });
  document.getElementById("authorizeCainiao")?.addEventListener("click", async () => {
    try {
      const data = await api("/api/admin/label-auth/cainiao", { method: "POST", body: JSON.stringify({}) });
      const authorization = data.authorization || {};
      if (authorization.authorized) {
        toast("菜鸟账号已授权。");
        render();
      } else if (authorization.url) {
        window.location.href = authorization.url;
      }
    } catch (error) {
      toast(error.message);
    }
  });
  document.getElementById("refreshLabelBranches")?.addEventListener("click", async () => {
    try {
      const data = await api("/api/admin/label-branches/refresh", { method: "POST", body: JSON.stringify({}) });
      state.shippingSettings = data.settings;
      toast("授权网点和面单余额已刷新。");
      render();
    } catch (error) {
      toast(error.message);
    }
  });
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
    ${pageHead("商品", "维护点菜单商品，可单个新增，也可用 Excel 批量刷新。", `<span class="count-pill">${state.productsAll.length} 个商品</span>`)}
    <section class="panel panel-pad">
      <div class="section-title"><h2>新增 / 更新商品</h2></div>
      <form id="productForm" class="product-form-grid">
        <div class="field">
          <label for="newProductBarcode">条码</label>
          <input class="input" id="newProductBarcode" name="barcode" required placeholder="唯一商品 ID" />
        </div>
        <div class="field">
          <label for="newProductCategory">分类</label>
          <input class="input" id="newProductCategory" name="category" list="productCategoryList" required placeholder="例如 线香" />
          <datalist id="productCategoryList">
            ${categoriesAll.map((cat) => `<option value="${escapeHtml(cat)}"></option>`).join("")}
          </datalist>
        </div>
        <div class="field">
          <label for="newProductName">名称</label>
          <input class="input" id="newProductName" name="name" required placeholder="商品名称" />
        </div>
        <div class="field">
          <label for="newProductSpec">规格</label>
          <input class="input" id="newProductSpec" name="spec" placeholder="可选" />
        </div>
        <div class="field">
          <label for="newProductPrice">售价</label>
          <input class="input" id="newProductPrice" name="price" inputmode="decimal" placeholder="0.00" />
        </div>
        <div class="field">
          <label for="newProductStatus">状态</label>
          <select class="select" id="newProductStatus" name="status">
            <option value="启用">启用</option>
            <option value="停用">停用</option>
          </select>
        </div>
        <button class="btn primary product-submit" type="submit">保存商品</button>
      </form>
    </section>
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
        <thead><tr><th>分类</th><th>货品名称</th><th>条码</th><th>售价</th><th>状态</th><th>更新时间</th><th>操作</th></tr></thead>
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
                  <td><button class="btn danger small" data-delete-product="${escapeHtml(product.barcode)}" data-product-name="${escapeHtml(product.name)}" type="button">删除</button></td>
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
  document.getElementById("productForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const data = await api("/api/products", {
        method: "POST",
        body: JSON.stringify({
          barcode: form.get("barcode"),
          category: form.get("category"),
          name: form.get("name"),
          spec: form.get("spec"),
          price: form.get("price"),
          status: form.get("status"),
        }),
      });
      state.productsAll = data.products || [];
      state.productsGrouped = null;
      event.currentTarget.reset();
      toast("商品已保存。");
      render();
    } catch (error) {
      toast(error.message);
    }
  });
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
  document.querySelectorAll("[data-delete-product]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const barcode = event.currentTarget.dataset.deleteProduct;
      const name = event.currentTarget.dataset.productName || barcode;
      if (!confirm(`确认删除商品「${name}」？删除后门店点菜单将不再显示该商品。`)) return;
      try {
        const data = await api(`/api/products/${encodeURIComponent(barcode)}`, { method: "DELETE" });
        state.productsAll = data.products || [];
        state.productsGrouped = null;
        toast("商品已删除。");
        render();
      } catch (error) {
        toast(error.message);
      }
    });
  });
}

function bindCommon() {
  bindTrackingCopyButtons();
  bindShipmentPagination();
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
    } else if (location.pathname === "/returns/new" && state.user.role === "staff") {
      await renderReturnNew();
    } else if (location.pathname === "/returns" && state.user.role === "staff") {
      await renderReturnBoard(false);
    } else if (location.pathname === "/admin" && state.user.role === "admin") {
      await renderAdmin();
    } else if (location.pathname === "/admin/returns" && state.user.role === "admin") {
      await renderReturnBoard(true);
    } else if (location.pathname === "/admin/stores" && state.user.role === "admin") {
      await renderStores();
    } else if (location.pathname === "/admin/products" && state.user.role === "admin") {
      await renderProducts();
    } else if (location.pathname === "/admin/shipping" && state.user.role === "admin") {
      await renderShippingSettings();
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
