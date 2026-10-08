const state = {
  user: null,
  stores: [],
  productsGrouped: null,
  productsAll: [],
  shipments: [],
  adminShipmentSummary: { total: 0 },
  storeShipments: [],
  storeShipmentSummary: { total: 0 },
  storeTodaySummary: { total: 0 },
  returnOrders: [],
  storeReturnOrders: [],
  statuses: ["待处理", "已发货", "已签收", "异常", "已取消"],
  returnStatuses: ["待查询", "运输中", "已签收", "异常", "已取消"],
  submitItems: [{ category: "", barcode: "", quantity: 1 }],
  submitDraft: { store_id: "", recipient_name: "", phone: "", address: "", store_order_no: "", remark: "" },
  returnItems: [{ category: "", barcode: "", quantity: 1 }],
  returnDraft: { store_id: "", tracking_no: "", sender_phone: "", remark: "" },
  adminFilters: { store_id: "", status: "待处理", date_from: "", date_to: "", q: "" },
  storeFilters: { status: "", date_from: "", date_to: "", q: "" },
  adminShipmentPage: 1,
  storeShipmentPage: 1,
  adminShipmentPagination: { page: 1, page_size: 50, total: 0, total_pages: 1 },
  storeShipmentPagination: { page: 1, page_size: 50, total: 0, total_pages: 1 },
  adminBoardLoaded: false,
  storeBoardLoaded: false,
  adminReturnFilters: { store_id: "", status: "", date_from: "", date_to: "", q: "" },
  storeReturnFilters: { status: "", date_from: "", date_to: "", q: "" },
  productFilters: { category: "", q: "" },
  editingShipmentId: null,
  editingShipmentRemarkId: null,
  editingShipmentShippingId: null,
  shipmentEditItems: [],
  shippingSettings: null,
  shippingConfig: null,
  batchPreview: null,
  batchFilters: { store_id: "", status: "待处理", date_from: "", date_to: "", q: "" },
  batchSelectedIds: [],
  batchPrintOpen: false,
  batchPrintSelectedIds: [],
  batchPrintError: "",
  activeShippingBatch: null,
  shippingBatchPollTimer: null,
  shippingBatchPollError: "",
  taskAlerts: { counts: { total: 0 }, items: [] },
  taskAlertsLoadError: "",
  taskAlertsOpen: false,
  taskAlertsCategory: "全部",
  taskAlertsPollTimer: null,
  returnPages: { admin: 1, store: 1 },
  returnPagination: { admin: {}, store: {} },
  returnSummary: { admin: {}, store: {} },
  returnTodaySummary: { admin: {}, store: {} },
  trackingTasks: new Map(),
  trackingTasksOverflow: false,
  trackingPollTimer: null,
  batchPreviewPage: 1,
  batchSelectAll: false,
  batchCompanyOverrides: {},
  batchKnownTypes: {},
  batchKnownCompanies: {},
  batchBulkCompany: "",
  batchProfileId: null,
  adminRowDrafts: new Map(),
  batchProgressPage: 1,
  batchProgressFailedOnly: false,
};

const EXPRESS_COMPANIES = ["圆通", "京东", "顺丰", "中通"];
const DEFAULT_EXPRESS_COMPANY = "圆通";
const CATEGORY_COLOR_COUNT = 10;
const SHIPMENT_PAGE_SIZE = 50;
const pendingGetRequests = new Map();
let activeDataLoads = 0;
let pageEpoch = 0;
let viewRevision = 0;
let renderSequence = 0;
let currentRoute = `${location.pathname}${location.search}`;
const loadVersions = new Map();
const activeReadControllers = new Set();
const busyOperations = new WeakMap();
const activeConfirmations = new Set();

class StaleViewError extends Error {}

function beginLoad(key) {
  const version = (loadVersions.get(key) || 0) + 1;
  loadVersions.set(key, version);
  const epoch = pageEpoch;
  const user = state.user?.id;
  return () => {
    if (epoch !== pageEpoch || user !== state.user?.id || loadVersions.get(key) !== version) throw new StaleViewError();
  };
}

function beginView() {
  const revision = ++viewRevision;
  const epoch = pageEpoch;
  return () => {
    if (revision !== viewRevision || epoch !== pageEpoch) throw new StaleViewError();
  };
}

function invalidatePage({ clearIdentity = false } = {}) {
  if (typeof stopFulfillmentReport === "function") stopFulfillmentReport(clearIdentity);
  pageEpoch += 1;
  viewRevision += 1;
  activeConfirmations.forEach(cancel => cancel());
  activeReadControllers.forEach(controller => controller.abort());
  activeReadControllers.clear();
  pendingGetRequests.clear();
  stopTaskAlertPoll();
  clearTimeout(state.shippingBatchPollTimer);
  clearTimeout(state.trackingPollTimer);
  state.shippingBatchPollTimer = state.trackingPollTimer = null;
  state.adminRowDrafts.clear();
  if (clearIdentity) {
    state.stores = []; state.productsGrouped = null; state.productsAll = [];
    state.shipments = []; state.storeShipments = []; state.returnOrders = []; state.storeReturnOrders = [];
    state.adminBoardLoaded = state.storeBoardLoaded = false;
    state.activeShippingBatch = null; state.trackingTasks.clear(); state.trackingTasksOverflow = false;
    state.taskAlerts = { counts: { total: 0 }, items: [] };
    state.submitDraft = {}; state.returnDraft = {};
    state.submitItems = [{ category: "", barcode: "", quantity: 1 }];
    state.returnItems = [{ category: "", barcode: "", quantity: 1 }];
    clearShipmentSelections();
    if (typeof specialDraft !== "undefined") specialDraft = null;
  }
}

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

function trackingDetailParts(row) {
  const traceLines = trackingTraceLines(row);
  return [
    ...traceLines.map((line) => `<div>${escapeHtml(line)}</div>`),
    row.tracking_last_checked_at ? `<div>查询：${escapeHtml(formatDate(row.tracking_last_checked_at))}</div>` : "",
    row.tracking_signed_at ? `<div>签收：${escapeHtml(formatDate(row.tracking_signed_at))}</div>` : "",
    row.shipped_at ? `<div>发货：${escapeHtml(formatDate(row.shipped_at))}</div>` : "",
    row.tracking_error ? `<div class="tracking-error">错误：${escapeHtml(row.tracking_error)}</div>` : "",
    row.shipping_note ? `<div>备注：${escapeHtml(row.shipping_note)}</div>` : "",
  ].filter(Boolean);
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
  const pagination = scope === "admin" ? state.adminShipmentPagination : state.storeShipmentPagination;
  const total = Number(pagination?.total || 0);
  const totalPages = Math.max(1, Number(pagination?.total_pages || 1));
  const page = Math.min(Math.max(1, Number(pagination?.page || shipmentPageState(scope))), totalPages);
  setShipmentPage(scope, page);
  const start = (page - 1) * SHIPMENT_PAGE_SIZE;
  const end = Math.min(start + rows.length, total);
  return {
    rows,
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

function toast(message, options = {}) {
  const type = options.type || "info";
  const duration = Number(options.duration ?? (type === "error" ? 12000 : 3200));
  const existing = document.querySelector(".toast");
  if (existing) existing.remove();
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.setAttribute("role", type === "error" ? "alert" : "status");
  node.setAttribute("aria-live", type === "error" ? "assertive" : "polite");
  node.setAttribute("aria-atomic", "true");
  const text = document.createElement("span");
  text.textContent = String(message || "操作未完成，请重试。");
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "关闭提示");
  close.textContent = "×";
  close.addEventListener("click", () => node.remove());
  node.append(text, close);
  document.body.appendChild(node);
  if (duration > 0) setTimeout(() => node.remove(), duration);
}

function errorToast(error, fallback = "操作失败，请稍后重试。") {
  if (error instanceof StaleViewError) return;
  const message = String(error?.message || error || fallback).trim() || fallback;
  toast(`操作未完成：${message}`, { type: "error" });
}

async function withButtonBusy(button, busyLabel, operation) {
  if (!button) return operation();
  if (busyOperations.has(button)) return busyOperations.get(button);
  const originalLabel = button.textContent;
  const wasDisabled = button.disabled;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = busyLabel;
  const running = Promise.resolve().then(operation);
  busyOperations.set(button, running);
  try {
    return await running;
  } finally {
    busyOperations.delete(button);
    if (button.isConnected) {
      button.disabled = wasDisabled;
      button.removeAttribute("aria-busy");
      button.textContent = originalLabel;
    }
  }
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

function bindTrackingCopyButtons(root = document) {
  root.querySelectorAll("[data-copy-tracking]").forEach((node) => {
    if (node.dataset.copyBound) return;
    node.dataset.copyBound = "1";
    node.addEventListener("click", async (event) => {
      const row = event.currentTarget.closest("[data-shipment]");
      const explicitTrackingNo = event.currentTarget.dataset.copyTracking || "";
      try {
        await copyText(explicitTrackingNo || row?.querySelector("[data-tracking]")?.value || "");
      } catch (error) {
        errorToast(error, "复制失败。");
      }
    });
  });
}

function bindTrackingDetails(root = document) {
  root.querySelectorAll("[data-tracking-details]").forEach((details) => {
    if (details.dataset.detailsBound) return;
    details.dataset.detailsBound = "1";
    details.addEventListener("toggle", async () => {
      if (!details.open || details.dataset.trackingLoaded === "1" || details.dataset.trackingLoading === "1") {
        return;
      }
      const shipmentId = Number(details.dataset.trackingDetails);
      const target = details.querySelector("[data-tracking-detail-lines]");
      if (!Number.isInteger(shipmentId) || shipmentId < 1 || !target) return;
      details.dataset.trackingLoading = "1";
      try {
        const data = await api(details.dataset.trackingKind === "return" ? `/api/returns/${shipmentId}/tracking` : `/api/shipments?id=${shipmentId}&include_tracking_raw=1`);
        if (!details.isConnected) return;
        const row = data.return_order || data.tracking || (data.shipments || [])[0];
        if (!row) throw new Error("发货单不存在。");
        const parts = trackingDetailParts(row);
        target.innerHTML = parts.length ? parts.join("") : `<div class="muted mini">暂无详细物流轨迹</div>`;
        details.dataset.trackingLoaded = "1";
      } catch (error) {
        if (error instanceof StaleViewError || !details.isConnected) return;
        target.innerHTML = `<div class="tracking-error">${escapeHtml(error.message || "物流详情加载失败。")}</div>`;
      } finally {
        delete details.dataset.trackingLoading;
      }
    });
  });
}

function bindShipmentPagination() {
  const changePage = async (scope, page) => {
    setShipmentPage(scope, page);
    try {
      if (scope === "admin") {
        await loadShipments({ loadSummary: false });
      } else {
        await loadStoreShipments({ loadSummary: false });
      }
      await render({ refreshData: false });
      document.querySelector(".date-shipment-group, .store-shipments-table")?.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      errorToast(error, "这一页暂时无法读取，请稍后重试。");
    }
  };
  document.querySelectorAll("[data-shipment-page]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const scope = event.currentTarget.dataset.shipmentPage;
      await changePage(scope, event.currentTarget.dataset.page);
    });
  });
  document.querySelectorAll("[data-shipment-page-select]").forEach((node) => {
    node.addEventListener("change", async (event) => {
      const scope = event.currentTarget.dataset.shipmentPageSelect;
      await changePage(scope, event.currentTarget.value);
    });
  });
}

async function api(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const key = `${pageEpoch}:${path}`;
  if (method === "GET" && pendingGetRequests.has(key)) {
    return pendingGetRequests.get(key);
  }
  const request = apiRequest(path, options);
  if (method !== "GET") return request;
  pendingGetRequests.set(key, request);
  try {
    return await request;
  } finally {
    if (pendingGetRequests.get(key) === request) pendingGetRequests.delete(key);
  }
}

class ApiError extends Error {
  constructor(message, status = 0, details = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details || {};
  }
}

async function apiRequest(path, options = {}) {
  const epoch = pageEpoch;
  const method = String(options.method || "GET").toUpperCase();
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const controller = new AbortController();
  if (method === "GET") activeReadControllers.add(controller);
  // PDF printing uses its existing dedicated fetch. Uploads and provider-backed
  // label operations retain a longer budget; ordinary reads/writes are bounded.
  const longOperation = options.body instanceof FormData || /shipping-settings|\/labels?\//.test(path);
  const timer = setTimeout(() => controller.abort(), options.timeoutMs || (longOperation ? 180000 : 30000));
  let response;
  let data;
  try {
    response = await fetch(path, { ...options, headers, signal: controller.signal });
    const contentType = response.headers.get("content-type") || "";
    data = contentType.includes("application/json") ? await response.json() : await response.text();
  } catch (error) {
    if (epoch !== pageEpoch) throw new StaleViewError();
    if (method !== "GET") throw new ApiError("暂未确认提交结果。请保留当前内容，先核对结果，不要另建一笔重复提交。", 0, { uncertain: true });
    throw new ApiError(error.name === "AbortError" ? "读取超时，请检查网络后重试。" : "暂时无法连接，请检查网络。", 0);
  } finally {
    clearTimeout(timer);
    activeReadControllers.delete(controller);
  }
  if (epoch !== pageEpoch) throw new StaleViewError();
  if (!response.ok) {
    if (response.status === 401 && location.pathname !== "/login") {
      invalidatePage({ clearIdentity: true });
      state.user = null;
      navigate("/login");
    }
    const message = typeof data === "object" && data ? data.error : data;
    throw new ApiError(message || "请求失败", response.status, data?.details || {});
  }
  if (method !== "GET" && /\/api\/(shipments|admin\/shipping-batches)/.test(path)) notifyFulfillmentReport();
  return data;
}

function navigate(path) {
  invalidatePage();
  history.pushState({}, "", path);
  currentRoute = `${location.pathname}${location.search}`;
  render();
}

async function createWithConfirmation(kind, payload) {
  const storageKey = `scentpool_submission:${state.user.id}:${kind}`;
  const key = payload.submission_key || sessionStorage.getItem(storageKey) || crypto.randomUUID();
  sessionStorage.setItem(storageKey, key);
  const body = { ...payload, submission_key: key };
  try {
    const data = await api(kind === "return" ? "/api/returns" : "/api/shipments", { method: "POST", body: JSON.stringify(body) });
    sessionStorage.removeItem(storageKey);
    return data;
  } catch (error) {
    if (error instanceof StaleViewError) throw error;
    if (error.status && error.status < 500) {
      // Keep conflicting keys: generating a new key after 409 could create a
      // second real shipment when an earlier response was lost.
      if (error.status !== 409) sessionStorage.removeItem(storageKey);
      throw error;
    }
    toast("暂未收到提交结果，正在核对原提交，请不要重复点击。", { duration: 0 });
    try {
      const params = new URLSearchParams({ kind, submission_key: key });
      if (state.user.role === "admin") params.set("store_id", String(payload.store_id || ""));
      const result = await api(`/api/submissions/status?${params}`);
      if (result.found && result.deleted) throw new ApiError("原提交记录已被删除。内容和原提交编号已保留，请先联系总部核对，不要再次寄送。", 409, { deleted: true });
      if (result.found && (result.shipment || result.return_order)) { sessionStorage.removeItem(storageKey); return result; }
    } catch (checkError) { if (checkError instanceof StaleViewError || checkError.details?.deleted) throw checkError; }
    throw new ApiError("暂未确认保存结果，填写内容已保留。请保持原内容再次提交核对，系统会沿用同一次编号，避免重复创建；不要开始另一张同内容新单。", 0, { uncertain: true });
  }
}

function renderSubmissionRecovery(kind) {
  return `<div class="submission-recovery"><button class="btn secondary small" type="button" data-check-submission="${kind}">核对上次提交</button><span class="mini muted" data-submission-result role="status">网络中断时先核对，不要另建重复订单。</span></div>`;
}

function bindSubmissionRecovery() {
  document.querySelectorAll("[data-check-submission]").forEach(button => button.addEventListener("click", async () => {
    const kind = button.dataset.checkSubmission, keyName = `scentpool_submission:${state.user.id}:${kind}`;
    const key = sessionStorage.getItem(keyName), target = button.parentElement.querySelector("[data-submission-result]");
    if (!key) { target.textContent = "没有尚待核对的提交。已成功的订单可在看板查看。"; return; }
    try {
      const params = new URLSearchParams({kind, submission_key: key});
      if (state.user.role === "admin") params.set("store_id", String(button.closest("form").querySelector('[name="store_id"]')?.value || ""));
      const result = await withButtonBusy(button, "核对中…", () => api(`/api/submissions/status?${params}`));
      if (!result.found) { target.textContent = "暂未找到已提交结果。请保留原内容再次提交核对，继续使用原编号；不要另建同内容订单。"; return; }
      target.textContent = result.deleted ? "原提交记录已被删除，请联系总部核对。" : `上次已经保存${result.shipment?.business_id ? `：${result.shipment.business_id}` : ""}，无需重复提交。`;
      if (confirm(`${target.textContent}\n确认已经核对原订单，且接下来确实要开始另一张不同的新单？`)) {
        sessionStorage.removeItem(keyName);
        target.textContent += " 已允许开始另一张新单，请检查并修改本次内容后再提交。";
      }
    } catch (error) { errorToast(error); }
  }));
}

function isActive(path) {
  return location.pathname === path ? "active" : "";
}

function roleName(user) {
  return user?.role === "admin" ? "总部" : user?.store_kind === "team" ? "合作团队" : "门店";
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
        <a class="${isActive("/admin/stores")}" href="/admin/stores" data-route>门店与团队</a>
        <a class="${isActive("/admin/products")}" href="/admin/products" data-route>商品</a>
        <a class="${isActive("/admin/shipping")}" href="/admin/shipping" data-route>面单设置</a>
      `
      : "";
  const storeLinks =
    state.user?.role === "staff"
      ? `
        <a class="${isActive("/shipments")}" href="/shipments" data-route>发货看板</a>
        ${state.user.store_kind !== "team" ? `<a class="${isActive("/returns/new")}" href="/returns/new" data-route>新增退货</a>
        <a class="${isActive("/returns")}" href="/returns" data-route>退货看板</a>` : ""}
      `
      : "";
  const submitLink = `${state.user?.store_kind !== "team" ? `<a class="${isActive("/submit")}" href="/submit" data-route>普通发货</a>` : ""}<a class="${isActive("/special/new")}" href="/special/new" data-route>${state.user?.role === "admin" ? "售后与合作发货" : state.user?.store_kind === "team" ? "合作寄送" : "售后发货"}</a>`;
  return `
    <div class="app-shell">
      <header class="topbar">
        <a class="brand" href="${state.user?.role === "admin" ? "/admin" : "/submit"}" data-route>
          <span class="mark">万</span>
          <span>万物香铺</span>
        </a>
        <nav class="nav">${submitLink}${storeLinks}${adminLinks}<a class="${isActive("/reports/fulfillment")}" href="/reports/fulfillment" data-route>发货统计</a></nav>
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
  const epoch = pageEpoch;
  try {
    const data = await api("/api/me");
    if (epoch !== pageEpoch) return;
    state.user = data.user;
  } catch {
    if (epoch !== pageEpoch) return;
    state.user = null;
  }
}

async function ensureProductsGrouped() {
  if (!state.productsGrouped) {
    const current = beginLoad("productsGrouped");
    const data = await api("/api/products");
    current();
    state.productsGrouped = data.categories || {};
  }
}

async function loadStores(all = false) {
  const current = beginLoad("stores");
  const data = await api(`/api/stores${all ? "?all=1" : ""}`);
  current();
  state.stores = data.stores || [];
}

async function loadShipments({ loadSummary = true } = {}) {
  const current = beginLoad("shipments");
  const params = new URLSearchParams();
  Object.entries(state.adminFilters).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  params.set("page", String(state.adminShipmentPage));
  params.set("page_size", String(SHIPMENT_PAGE_SIZE));
  const summaryParams = new URLSearchParams(params);
  summaryParams.delete("status");
  summaryParams.delete("page");
  summaryParams.delete("page_size");
  const [data, summaryData] = await Promise.all([
    api(`/api/shipments?${params.toString()}`),
    loadSummary ? api(`/api/shipments/summary?${summaryParams.toString()}`) : Promise.resolve(null),
  ]);
  current();
  state.shipments = data.shipments || [];
  state.adminShipmentPagination = data.pagination || { page: 1, page_size: SHIPMENT_PAGE_SIZE, total: state.shipments.length, total_pages: 1 };
  state.adminShipmentPage = state.adminShipmentPagination.page || 1;
  state.statuses = data.statuses || state.statuses;
  if (summaryData) state.adminShipmentSummary = summaryData.counts || { total: 0 };
}

async function loadShippingSettings() {
  const current = beginLoad("shippingSettings");
  const data = await api("/api/admin/shipping-settings");
  current();
  state.shippingSettings = data.settings || {};
  state.shippingConfig = data.shipping || {};
}

async function loadShippingBatchPreview(filters, { reset = true } = {}) {
  const current = beginLoad("batchPreview");
  if (reset) {
    state.batchPreviewPage = 1; state.batchSelectAll = true; state.batchSelectedIds = [];
    state.batchCompanyOverrides = {}; state.batchKnownTypes = {}; state.batchKnownCompanies = {}; state.batchBulkCompany = "";
  }
  const data = await api("/api/admin/shipping-batches/preview", {
    method: "POST",
    body: JSON.stringify({ filters, page: state.batchPreviewPage, page_size: 50, profile_id: state.batchProfileId }),
  });
  current();
  state.batchPreview = data.preview || null;
  for (const row of state.batchPreview?.eligible || []) {
    state.batchKnownTypes[row.id] = row.shipment_type || "legacy";
    state.batchKnownCompanies[row.id] = row.express_company || DEFAULT_EXPRESS_COMPANY;
  }
}

function shippingBatchStorageKey() { return `scentpool_shipping_batch_id:${state.user?.id || "none"}`; }

async function loadTaskAlerts() {
  const current = beginLoad("taskAlerts");
  try {
    const data = await api("/api/admin/task-alerts");
    current();
    state.taskAlerts = data || { counts: { total: 0 }, items: [] };
    state.taskAlertsLoadError = "";
  } catch (error) {
    if (error instanceof StaleViewError) return;
    try { current(); } catch { return; }
    state.taskAlertsLoadError = error.message || "异常提醒暂时无法读取。";
  }
}

function stopTaskAlertPoll() {
  if (state.taskAlertsPollTimer) clearTimeout(state.taskAlertsPollTimer);
  state.taskAlertsPollTimer = null;
}

function scheduleTaskAlertPoll() {
  if (state.taskAlertsPollTimer) return;
  if (location.pathname !== "/admin" || state.user?.role !== "admin") return;
  const seconds = Math.max(15, Number(state.taskAlerts?.refresh?.alerts_seconds || 60));
  state.taskAlertsPollTimer = setTimeout(async () => {
    state.taskAlertsPollTimer = null;
    if (location.pathname !== "/admin" || state.user?.role !== "admin") return;
    if (document.visibilityState === "visible") {
      await loadTaskAlerts();
      updateTaskAlertUi();
    }
    scheduleTaskAlertPoll();
  }, seconds * 1000);
}

async function loadActiveShippingBatch() {
  const current = beginLoad("shippingBatch");
  const batchId = state.activeShippingBatch?.batch?.id || sessionStorage.getItem(shippingBatchStorageKey());
  if (!batchId) return;
  try {
    const data = await api(`/api/admin/shipping-batches/${batchId}?page=${state.batchProgressPage}&page_size=50${state.batchProgressFailedOnly ? "&status=" + encodeURIComponent("失败") : ""}`);
    current();
    const previousProgress = JSON.stringify(state.activeShippingBatch?.counts || {});
    state.activeShippingBatch = data;
    if (previousProgress !== JSON.stringify(data.counts || {})) notifyFulfillmentReport();
    state.shippingBatchPollError = "";
  } catch (error) {
    if (error instanceof StaleViewError) return;
    try { current(); } catch { return; }
    if (error.status === 404) {
      state.activeShippingBatch = null;
      state.shippingBatchPollError = "";
      sessionStorage.removeItem(shippingBatchStorageKey());
    } else {
      state.shippingBatchPollError = `批次进度暂时无法刷新：${error.message || "请检查网络后重试。"}`;
    }
  }
}

function scheduleShippingBatchPoll() {
  if (state.shippingBatchPollTimer) clearTimeout(state.shippingBatchPollTimer);
  const status = state.activeShippingBatch?.batch?.status;
  if (!status || !["排队中", "处理中"].includes(status) || location.pathname !== "/admin" || document.visibilityState === "hidden") return;
  const epoch = pageEpoch;
  state.shippingBatchPollTimer = setTimeout(async () => {
    state.shippingBatchPollTimer = null;
    try {
      const previousCounts = JSON.stringify(state.activeShippingBatch?.counts || {});
      const previousStatus = state.activeShippingBatch?.batch?.status || "";
      await loadActiveShippingBatch();
      if (epoch !== pageEpoch) return;
      const nextCounts = JSON.stringify(state.activeShippingBatch?.counts || {});
      const nextStatus = state.activeShippingBatch?.batch?.status || "";
      if (previousCounts !== nextCounts || previousStatus !== nextStatus) {
        const oldRows = state.shipments;
        await Promise.all([loadShipments(), loadTaskAlerts()]);
        if (epoch !== pageEpoch) return;
        updateShipmentRows(oldRows);
        updateTaskAlertUi();
      }
      updateShippingBatchUi();
      if (["排队中", "处理中"].includes(previousStatus) && !["排队中", "处理中"].includes(nextStatus)) {
        const failed = state.activeShippingBatch?.counts?.["失败"] || 0;
        if (failed) {
          errorToast(`电子面单批次已结束，其中 ${failed} 单失败。请打开页面顶部“异常提醒”查看。`);
        } else {
          toast("电子面单批次已全部完成。");
        }
      }
    } catch (error) {
      if (error instanceof StaleViewError || epoch !== pageEpoch) return;
      state.shippingBatchPollError = `批次进度暂时无法刷新：${error.message || "请检查网络后重试。"}`;
      updateShippingBatchUi();
    } finally {
      if (epoch === pageEpoch) scheduleShippingBatchPoll();
    }
  }, 2500);
}

function updateShippingBatchUi() {
  const host = document.getElementById("shippingBatchProgressHost");
  if (!host) return;
  const focusId = host.contains(document.activeElement) ? document.activeElement.id : "";
  host.innerHTML = renderShippingBatchProgress();
  bindAdmin(host);
  if (focusId) document.getElementById(focusId)?.focus({ preventScroll: true });
}

function updateBatchPreviewUi() {
  const host = document.getElementById("shippingBatchPreviewHost");
  if (host) { host.innerHTML = renderShippingBatchPreview(); bindAdmin(host); }
}

function updateShipmentRows(previousRows) {
  const summary = document.getElementById("adminShipmentSummary");
  if (summary) summary.innerHTML = renderAdminShipmentSummary();
  const previous = new Map(previousRows.map(row => [Number(row.id), row]));
  const latestIds = new Set(state.shipments.map(row => Number(row.id)));
  const batchItems = new Map((state.activeShippingBatch?.items || []).map(item => [Number(item.shipment_id), item]));
  const leftFilter = previousRows.filter(row => !latestIds.has(Number(row.id)));
  const updates = [...state.shipments];
  for (const old of leftFilter) {
    const item = batchItems.get(Number(old.id));
    if (item?.status === "成功" && item.tracking_no) {
      updates.push({ ...old, status: "已发货", booking_status: item.booking_status, tracking_no: item.tracking_no, express_company: item.express_company, _live_partial: true });
    } else {
      const node = document.querySelector(`tr[data-shipment="${Number(old.id)}"]`);
      if (!node) continue;
      let notice = node.querySelector("[data-live-conflict]");
      if (!notice) { notice = document.createElement("div"); notice.dataset.liveConflict = "1"; notice.className = "notice live-update-notice"; node.querySelector(".status-cell")?.append(notice); }
      notice.textContent = "此单已不符合当前筛选，以下是保留的旧内容。请完成其他编辑后点击筛选核对，不能保存旧内容。";
      node.querySelectorAll("[data-save-shipment], [data-save-admin-remark], [data-edit-shipment-items], [data-delete-shipment]").forEach(button => { button.disabled = true; });
    }
  }
  for (const row of updates) {
    if (JSON.stringify(previous.get(Number(row.id))) === JSON.stringify(row)) continue;
    const node = document.querySelector(`tr[data-shipment="${Number(row.id)}"]`);
    if (!node) continue;
    const editing = node.dataset.dirty === "1" || node.contains(document.activeElement) || node.querySelector("details[open]") ||
      [state.editingShipmentId, state.editingShipmentRemarkId, state.editingShipmentShippingId].includes(row.id);
    if (editing) {
      let notice = node.querySelector("[data-live-conflict]");
      if (!notice) {
        notice = document.createElement("div"); notice.dataset.liveConflict = "1";
        notice.className = "notice live-update-notice"; notice.setAttribute("role", "status");
        node.querySelector(".status-cell")?.append(notice);
      }
      notice.textContent = bookingEditable(row) ? "后台状态已更新；你的输入已保留，保存时将再次核对。" : "此单已进入面单或发货流程。输入已保留供核对，请刷新后查看；不能再保存旧内容。";
      if (!bookingEditable(row)) node.querySelectorAll("[data-save-shipment], [data-save-admin-remark], [data-save-edit-items], [data-edit-shipment-items], [data-delete-shipment]").forEach(button => { button.disabled = true; });
      continue;
    }
    const template = document.createElement("template");
    template.innerHTML = renderShipmentTable([row]);
    const replacement = template.content.querySelector("tr[data-shipment]");
    if (!replacement) continue;
    node.replaceWith(replacement);
    bindAdmin(replacement); bindTrackingCopyButtons(replacement); bindTrackingDetails(replacement); bindSpecialControls(replacement);
  }
  // Membership/order is changed only on an explicit filter/page refresh. This
  // keeps an in-progress edit and current selection from disappearing mid-use.
  if (previousRows.map(row => row.id).join() !== state.shipments.map(row => row.id).join()) {
    const notice = document.getElementById("shipmentListUpdateNotice");
    if (notice) { notice.hidden = false; notice.textContent = "部分订单状态已变化，当前行保留便于核对；完成编辑后点击“筛选”更新列表。"; }
  }
}

const ROW_DRAFT_FIELDS = ["data-status", "data-company", "data-tracking", "data-note", "data-admin-remark"];

function captureAdminRowDrafts() {
  if (location.pathname !== "/admin") return;
  for (const node of document.querySelectorAll('tr[data-shipment][data-dirty="1"]')) {
    const fields = {};
    for (const key of ROW_DRAFT_FIELDS) {
      const input = node.querySelector(`[${key}]`);
      if (input) fields[key] = input.value;
    }
    state.adminRowDrafts.set(Number(node.dataset.shipment), fields);
  }
  // Drafts never leave memory or grow beyond the currently displayed page.
  const visible = new Set([...document.querySelectorAll("tr[data-shipment]")].map(node => Number(node.dataset.shipment)));
  for (const id of state.adminRowDrafts.keys()) if (!visible.has(id)) state.adminRowDrafts.delete(id);
}

function restoreAdminRowDrafts() {
  for (const [id, fields] of state.adminRowDrafts) {
    const row = document.querySelector(`tr[data-shipment="${id}"]`);
    if (!row) { state.adminRowDrafts.delete(id); continue; }
    row.dataset.dirty = "1";
    for (const [key, value] of Object.entries(fields)) {
      const input = row.querySelector(`[${key}]`);
      if (input) input.value = value;
    }
    const data = state.shipments.find(item => Number(item.id) === id);
    if (data && !bookingEditable(data)) {
      const notice = document.createElement("div"); notice.className = "notice live-update-notice";
      notice.textContent = "后台已锁定此单，原输入保留供核对，不能保存旧内容。";
      // The read-only editor no longer exposes inputs; retain draft as text for
      // the employee to review rather than silently discarding their work.
      const draft = document.createElement("details"); const summary = document.createElement("summary");
      summary.textContent = "查看未保存内容"; draft.append(summary);
      const text = document.createElement("p"); text.textContent = Object.values(fields).join(" · "); draft.append(text); notice.append(draft);
      row.querySelector(".status-cell")?.append(notice);
      row.querySelectorAll("[data-save-shipment], [data-save-admin-remark]").forEach(button => { button.disabled = true; });
    }
  }
}

function clearAdminRowDraft(id) {
  state.adminRowDrafts.delete(Number(id));
  const row = document.querySelector(`tr[data-shipment="${Number(id)}"]`);
  if (row) delete row.dataset.dirty;
}

function trackingTaskStorageKey() { return `scentpool_tracking_tasks:${state.user?.id || "none"}`; }
function trackingTaskPending(task) { return ["queued", "running", "retry_wait", "排队中", "处理中", "等待重试"].includes(task.status); }

function trimTrackingTasks() {
  while (state.trackingTasks.size > 10) {
    const ended = [...state.trackingTasks.entries()].find(([, task]) => !trackingTaskPending(task));
    state.trackingTasks.delete(ended ? ended[0] : state.trackingTasks.keys().next().value);
    state.trackingTasksOverflow = true;
  }
}

function acceptTrackingTask(data) {
  if (data?.task?.id) {
    state.trackingTasks.set(String(data.task.id), data.task);
    trimTrackingTasks();
    sessionStorage.setItem(trackingTaskStorageKey(), JSON.stringify([...state.trackingTasks.keys()].slice(-10)));
    updateTrackingTaskUi(); scheduleTrackingPoll();
  }
  toast(data?.message || "物流查询已加入后台队列，无需重复点击，可继续其他操作。");
}

function renderTrackingTasks() {
  trimTrackingTasks();
  const labels = { queued: "排队中", running: "查询中", retry_wait: "等待重试", completed: "已完成", failed: "有查询失败", cancelled: "已停止" };
  return (state.trackingTasksOverflow ? `<p class="mini muted">这里只保留最近 10 个查询任务，优先保留进行中的任务；其他任务仍在后台执行或已结束，结果和失败原因请在对应订单及异常提醒中核对。</p>` : "") + [...state.trackingTasks.values()].map(task => `<section class="notice tracking-task ${Number(task.failed) || task.load_error || task.failure_categories?.provider_unavailable ? "danger-notice" : ""}" role="status">
    <strong>物流查询：${escapeHtml(labels[task.status] || task.status || "正在核对")}</strong>
    <span>共 ${Number(task.total || 0)} 项 · 完成 ${Number(task.completed || 0)} · 失败 ${Number(task.failed || 0)} · 跳过 ${Number(task.skipped || 0)} · 等待 ${Number(task.remaining || 0)}</span>
    ${task.load_error ? `<span>${escapeHtml(task.load_error)} 后台任务可能仍在执行，请勿重复提交。</span>` : ""}
    ${task.message || task.service_error ? `<span>${escapeHtml(task.message || task.service_error)}</span>` : ""}
    ${task.failure_categories?.provider_unavailable ? `<span>物流服务暂时不可用，订单和已有物流已保留。${trackingTaskPending(task) ? "系统将按保护间隔重试，请勿反复点击。" : "自动尝试已结束，请联系总部核查异常提醒。"}</span>` : ""}
    <span class="mini">${trackingTaskPending(task) ? "关闭页面不会取消任务；系统按查询间隔处理，请勿重复点击。" : Number(task.failed) ? "失败项没有被隐藏；总部可在异常提醒或对应记录查看原因。" : "结果已更新。"}${task.updated_at ? ` 最近更新 ${escapeHtml(formatDate(task.updated_at))}` : ""}</span>
  </section>`).join("");
}

function updateTrackingTaskUi() {
  const host = document.getElementById("trackingTaskHost");
  if (host) host.innerHTML = renderTrackingTasks();
}

function scheduleTrackingPoll(immediate = false) {
  clearTimeout(state.trackingPollTimer); state.trackingPollTimer = null;
  if (!state.user || document.visibilityState === "hidden" || !document.getElementById("trackingTaskHost")) return;
  const tasks = [...state.trackingTasks.values()].filter(task => trackingTaskPending(task) || task.load_error);
  const visibleRows = location.pathname === "/admin" ? state.shipments : location.pathname === "/admin/returns" ? state.returnOrders : location.pathname === "/returns" ? state.storeReturnOrders : [];
  const hasQueuedRows = visibleRows.some(row => row.tracking_queued);
  if (!tasks.length && !hasQueuedRows) return;
  const epoch = pageEpoch;
  state.trackingPollTimer = setTimeout(async () => {
    state.trackingPollTimer = null;
    let changed = hasQueuedRows;
    for (const old of tasks) {
      try {
        const data = await api(`/api/tracking/tasks/${encodeURIComponent(old.id)}`);
        if (epoch !== pageEpoch) return;
        changed ||= JSON.stringify(old) !== JSON.stringify(data.task);
        state.trackingTasks.set(String(old.id), data.task);
      } catch (error) {
        if (epoch !== pageEpoch || error instanceof StaleViewError) return;
        if ([403, 404].includes(error.status)) state.trackingTasks.delete(String(old.id));
        else state.trackingTasks.set(String(old.id), { ...old, load_error: error.message });
      }
    }
    if (epoch !== pageEpoch) return;
    updateTrackingTaskUi();
    if (changed) {
      try {
        if (location.pathname === "/admin") {
          const oldRows = state.shipments;
          await loadShipments(); updateShipmentRows(oldRows);
        } else if (["/admin/returns", "/returns"].includes(location.pathname)) {
          await loadReturnOrders(location.pathname === "/admin/returns");
          if (epoch === pageEpoch) updateReturnRows(location.pathname === "/admin/returns");
        }
      } catch (error) { if (!(error instanceof StaleViewError)) errorToast(error); }
    }
    if (epoch === pageEpoch) scheduleTrackingPoll();
  }, immediate ? 0 : 5000);
}

function restoreTrackingTasks() {
  if (!state.user || state.trackingTasks.size) return;
  try {
    const ids = JSON.parse(sessionStorage.getItem(trackingTaskStorageKey()) || "[]");
    for (const id of ids.slice(-10)) if (/^[A-Za-z0-9_-]{16,80}$/.test(String(id))) state.trackingTasks.set(String(id), { id, status: "queued" });
  } catch { /* No recipient fields are persisted. Ignore a corrupt ID list. */ }
}

async function loadStoreShipments({ loadSummary = true } = {}) {
  const current = beginLoad("storeShipments");
  const params = new URLSearchParams();
  Object.entries(state.storeFilters).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  params.set("page", String(state.storeShipmentPage));
  params.set("page_size", String(SHIPMENT_PAGE_SIZE));
  const summaryParams = new URLSearchParams(params);
  summaryParams.delete("status");
  summaryParams.delete("page");
  summaryParams.delete("page_size");
  const [data, summaryData] = await Promise.all([
    api(`/api/shipments?${params.toString()}`),
    loadSummary ? api(`/api/shipments/summary?${summaryParams.toString()}`) : Promise.resolve(null),
  ]);
  current();
  state.storeShipments = data.shipments || [];
  state.storeShipmentPagination = data.pagination || { page: 1, page_size: SHIPMENT_PAGE_SIZE, total: state.storeShipments.length, total_pages: 1 };
  state.storeShipmentPage = state.storeShipmentPagination.page || 1;
  state.statuses = data.statuses || state.statuses;
  if (summaryData) state.storeShipmentSummary = summaryData.counts || { total: 0 };
}

async function loadStoreTodaySummary() {
  const current = beginLoad("storeTodaySummary");
  const today = localDate();
  const data = await api(`/api/shipments/summary?date_from=${today}&date_to=${today}`);
  current();
  state.storeTodaySummary = data.counts || { total: 0 };
}

async function loadReturnOrders(admin = false) {
  const scope = admin ? "admin" : "store";
  const current = beginLoad(`returns:${scope}`);
  const filters = admin ? state.adminReturnFilters : state.storeReturnFilters;
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  const summaryParams = new URLSearchParams(params);
  summaryParams.delete("status");
  params.set("page", String(state.returnPages[scope]));
  params.set("page_size", "50");
  const [data, summary] = await Promise.all([
    api(`/api/returns?${params}`), api(`/api/returns/summary?${summaryParams}`),
  ]);
  current();
  state.returnPagination[scope] = data.pagination || { page: 1, total: data.returns?.length || 0, total_pages: 1, page_size: 50 };
  state.returnPages[scope] = state.returnPagination[scope].page || 1;
  state.returnSummary[scope] = summary.counts || {};
  state.returnTodaySummary[scope] = { total: summary.today_count || 0, "已签收": summary.today_signed_count || 0 };
  if (admin) {
    state.returnOrders = data.returns || [];
  } else {
    state.storeReturnOrders = data.returns || [];
  }
  state.returnStatuses = data.statuses || state.returnStatuses;
}

async function loadProductsAll() {
  const current = beginLoad("productsAll");
  const data = await api("/api/products?all=1");
  current();
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
      navigate(state.user.role === "admin" ? "/admin" : state.user.store_kind === "team" ? "/special/new" : "/submit");
    } catch (error) {
      errorToast(error);
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
    item_kind: item.item_kind || "product",
    name: item.item_kind === "material" ? item.product_name.slice(0, -(String(item.material_spec || "").length + 2)) : "",
    material_spec: item.material_spec || "",
    category: item.product_category || "",
    barcode: item.product_barcode || "",
    quantity: item.quantity || 1,
  }));
}

function validItems(items) {
  return items
    .filter((item) => item.item_kind === "material" || item.barcode)
    .map((item) => item.item_kind === "material" ? {item_kind: "material", name: item.name, material_spec: item.material_spec, quantity: Number(item.quantity)} : ({ barcode: item.barcode, quantity: Number(item.quantity) }));
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
  const currentView = beginView();
  await ensureProductsGrouped();
  if (state.user.role === "admin" && state.stores.length === 0) await loadStores();
  const storeField =
    state.user.role === "admin"
      ? `
        <div class="field">
          <label for="store_id">门店</label>
          <select class="select" id="store_id" name="store_id" required>
            <option value="">选择门店</option>
            ${state.stores.filter(store => store.kind !== "team").map((store) => `<option value="${store.id}" ${String(store.id) === String(state.submitDraft.store_id) ? "selected" : ""}>${escapeHtml(store.name)}</option>`).join("")}
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
          ${renderSubmissionRecovery("shipment")}
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
  currentView();
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
  const isReturnTracking = Boolean(row.express_company_source);
  const company = row.express_company || (isReturnTracking ? "快递公司待识别" : DEFAULT_EXPRESS_COMPANY);
  const companyLabel = row.express_company_source === "kuaidi100"
    ? `快递100识别（仅供参考）：${company}`
    : company;
  const summary = cleanTrackingEvent(row.tracking_last_event)
    || (row.tracking_status === "查询失败" && row.tracking_error ? row.tracking_error : row.tracking_status)
    || (row.tracking_error ? "物流查询失败" : "物流待查询");
  const detailParts = trackingDetailParts(row);
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
        <span>${escapeHtml(companyLabel)} ${escapeHtml(row.tracking_no)}</span>
        ${showCopy ? `<button class="btn secondary small" data-copy-tracking="${escapeHtml(row.tracking_no)}" type="button">复制</button>` : ""}
      </div>
      <div class="muted mini">${row.tracking_last_checked_at ? `上次查询 ${escapeHtml(formatDate(row.tracking_last_checked_at))}` : "尚未完成查询"}${row.tracking_queued ? " · 已排队，后台更新中" : ""}</div>
      ${
        row.tracking_no
          ? `<details class="tracking-details" data-tracking-kind="${options.kind || "shipment"}" data-tracking-details="${row.id}" data-tracking-loaded="${row.tracking_raw ? "1" : "0"}"><summary>显示详细物流信息</summary><div class="tracking-detail-lines" data-tracking-detail-lines>${detailParts.join("") || "展开后读取物流详情"}</div></details>`
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
    captureSubmitDraft();
    const submitButton = event.currentTarget.querySelector('button[type="submit"]');
    if (busyOperations.has(submitButton)) return;
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
      await withButtonBusy(submitButton, "正在保存…", () => createWithConfirmation("shipment", payload));
      state.submitItems = [{ category: "", barcode: "", quantity: 1 }];
      state.submitDraft = { store_id: "", recipient_name: "", phone: "", address: "", store_order_no: "", remark: "" };
      toast("已同步到总部。");
      render();
    } catch (error) {
      errorToast(error);
    }
  });
}

async function renderStoreBoard({ refreshData = true } = {}) {
  const currentView = beginView();
  if (refreshData) {
    const loads = [loadStoreShipments(), loadStoreTodaySummary()];
    if (!state.storeBoardLoaded) loads.push(ensureProductsGrouped());
    await Promise.all(loads);
    state.storeBoardLoaded = true;
  }
  const today = localDate();
  const yesterday = localDate(-1);
  const todayCounts = state.storeTodaySummary || { total: 0 };
  const counts = state.storeShipmentSummary || { total: 0 };
  const pageData = paginatedShipments(state.storeShipments, "store");
  const content = `
    ${pageHead(
      "发货看板",
      "查看本归属单位的发货记录、处理状态和快递单号。",
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
      ${classificationFilters("store")}
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
  currentView();
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
                  <td>${shipmentContext(row)}<strong>${escapeHtml(row.store_order_no)}</strong></td>
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
                  <td>${renderStoreShipmentRemark(row)}</td>
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

function renderStoreShipmentRemark(row) {
  const editable = row.status === "待处理" && bookingEditable(row);
  if (editable && state.editingShipmentRemarkId === row.id) {
    return `
      <div class="store-remark-editor">
        <textarea class="table-input" data-store-remark maxlength="500" rows="3" aria-label="订单备注">${escapeHtml(row.remark || "")}</textarea>
        <div class="inline-actions">
          <button class="btn primary small" data-save-store-remark="${row.id}" type="button">保存备注</button>
          <button class="btn ghost small" data-cancel-store-remark type="button">取消</button>
        </div>
      </div>
    `;
  }
  return `
    ${row.remark ? `<div>${escapeHtml(row.remark)}</div>` : `<span class="muted">无</span>`}
    ${row.shipping_note ? `<div class="muted mini">总部：${escapeHtml(row.shipping_note)}</div>` : ""}
    ${
      editable
        ? `<div class="inline-actions store-order-actions">
            <button class="btn secondary small" data-edit-store-remark="${row.id}" type="button">修改备注</button>
            <button class="btn danger small" data-delete-store-shipment="${row.id}" data-order-no="${escapeHtml(row.store_order_no)}" type="button">删除整单</button>
          </div>`
        : ""
    }
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
              ${item.item_kind === "material" ? `<div class="field"><label>临时物料名称</label><input class="input" data-edit-material-name="${index}" maxlength="100" value="${escapeHtml(item.name || "")}"></div><div class="field"><label>规格</label><input class="input" data-edit-material-spec="${index}" maxlength="100" value="${escapeHtml(item.material_spec || "")}"></div>` : `
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
              `}
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
        ${["resend", "exchange", "influencer", "sample"].includes(row.shipment_type) ? `<button class="btn secondary small" data-add-edit-material type="button">添加临时物料</button>` : ""}
        <button class="btn primary small" data-save-edit-items="${row.id}" type="button">保存商品</button>
        <button class="btn ghost small" data-cancel-edit-items type="button">取消</button>
      </div>
    </div>
  `;
}

function bindShipmentItemEditor(sourceRows, root = document) {
  root.querySelectorAll("[data-edit-material-name], [data-edit-material-spec]").forEach(node => node.addEventListener("input", () => {
    const isName = node.hasAttribute("data-edit-material-name");
    state.shipmentEditItems[Number(isName ? node.dataset.editMaterialName : node.dataset.editMaterialSpec)][isName ? "name" : "material_spec"] = node.value;
  }));
  root.querySelector("[data-add-edit-material]")?.addEventListener("click", () => { state.shipmentEditItems.push({item_kind: "material", name: "", material_spec: "", quantity: 1}); render({refreshData: false}); });
  root.querySelectorAll("[data-edit-shipment-items]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const id = Number(event.currentTarget.dataset.editShipmentItems);
      const row = sourceRows.find((item) => item.id === id);
      state.editingShipmentId = id;
      state.shipmentEditItems = itemsFromProductSnapshots(row?.items || []);
      render({ refreshData: false });
    });
  });
  root.querySelectorAll("[data-edit-item-category]").forEach((node) => {
    node.addEventListener("change", (event) => {
      const index = Number(event.currentTarget.dataset.editItemCategory);
      state.shipmentEditItems[index].category = event.currentTarget.value;
      state.shipmentEditItems[index].barcode = "";
      render({ refreshData: false });
    });
  });
  root.querySelectorAll("[data-edit-item-product]").forEach((node) => {
    node.addEventListener("change", (event) => {
      const index = Number(event.currentTarget.dataset.editItemProduct);
      state.shipmentEditItems[index].barcode = event.currentTarget.value;
      render({ refreshData: false });
    });
  });
  root.querySelectorAll("[data-edit-item-quantity]").forEach((node) => {
    node.addEventListener("input", (event) => {
      const index = Number(event.currentTarget.dataset.editItemQuantity);
      state.shipmentEditItems[index].quantity = Math.max(1, Number(event.currentTarget.value || 1));
    });
  });
  const addEditItem = root.querySelector("[data-add-edit-item]");
  if (addEditItem) {
    addEditItem.addEventListener("click", () => {
      state.shipmentEditItems.push({ category: "", barcode: "", quantity: 1 });
      render({ refreshData: false });
    });
  }
  root.querySelectorAll("[data-remove-edit-item]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const index = Number(event.currentTarget.dataset.removeEditItem);
      state.shipmentEditItems.splice(index, 1);
      if (!state.shipmentEditItems.length) state.shipmentEditItems.push({ category: "", barcode: "", quantity: 1 });
      render({ refreshData: false });
    });
  });
  const cancelEditItems = root.querySelector("[data-cancel-edit-items]");
  if (cancelEditItems) {
    cancelEditItems.addEventListener("click", () => {
      state.editingShipmentId = null;
      state.shipmentEditItems = [];
      render({ refreshData: false });
    });
  }
  root.querySelectorAll("[data-save-edit-items]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.saveEditItems;
      try {
        await withButtonBusy(event.currentTarget, "保存中…", () => api(`/api/shipments/${id}/items`, {
          method: "PATCH",
          body: JSON.stringify({ items: validItems(state.shipmentEditItems) }),
        }));
        state.editingShipmentId = null;
        state.shipmentEditItems = [];
        toast("商品明细已更新。");
        render();
      } catch (error) {
        errorToast(error);
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
      ...state.storeFilters,
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
  document.querySelectorAll("[data-edit-store-remark]").forEach((node) => {
    node.addEventListener("click", (event) => {
      state.editingShipmentRemarkId = Number(event.currentTarget.dataset.editStoreRemark);
      render({ refreshData: false });
    });
  });
  document.querySelectorAll("[data-cancel-store-remark]").forEach((node) => {
    node.addEventListener("click", () => {
      state.editingShipmentRemarkId = null;
      render({ refreshData: false });
    });
  });
  document.querySelectorAll("[data-save-store-remark]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.saveStoreRemark;
      const remark = event.currentTarget.closest(".store-remark-editor")?.querySelector("[data-store-remark]")?.value || "";
      try {
        await withButtonBusy(event.currentTarget, "保存中…", () =>
          api(`/api/shipments/${id}/remark`, {
            method: "PATCH",
            body: JSON.stringify({ remark }),
          })
        );
        state.editingShipmentRemarkId = null;
        toast("订单备注已更新。");
        render();
      } catch (error) {
        errorToast(error);
      }
    });
  });
  document.querySelectorAll("[data-delete-store-shipment]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.deleteStoreShipment;
      const orderNo = event.currentTarget.dataset.orderNo || id;
      if (!confirm(`确认删除未发货订单 ${orderNo}？商品明细和整张订单都会删除，且无法恢复。`)) return;
      try {
        await withButtonBusy(event.currentTarget, "删除中…", () =>
          api(`/api/shipments/${id}`, { method: "DELETE" })
        );
        state.editingShipmentRemarkId = null;
        toast("未发货订单已删除。");
        render();
      } catch (error) {
        errorToast(error);
      }
    });
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
  const currentView = beginView();
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
            <div class="field full">
              <label>门店</label>
              <span class="store-badge">${escapeHtml(state.user.store_name || "当前门店")}</span>
            </div>
            <div class="field full">
              <label>快递公司</label>
              <div class="notice" style="margin-top: 0;">无需选择。提交后由快递100根据单号自动识别，并在退货看板显示识别结果（仅供参考）；识别或查询失败时会显示明确的红色原因和处理提示。</div>
            </div>
            <div class="field">
              <label for="returnTrackingNo">退货快递单号</label>
              <input class="input" id="returnTrackingNo" name="tracking_no" value="${escapeHtml(state.returnDraft.tracking_no)}" required />
            </div>
            <div class="field">
              <label for="returnSenderPhone">联系电话（顺丰查询建议填写）</label>
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
          ${renderSubmissionRecovery("return")}
        </form>
      </section>
      <aside class="panel panel-pad">
        <div class="section-title"><h2>退货明细</h2></div>
        ${renderReturnSummary()}
      </aside>
    </div>
  `;
  currentView();
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
    captureReturnDraft();
    const submitButton = event.currentTarget.querySelector('button[type="submit"]');
    if (busyOperations.has(submitButton)) return;
    const form = new FormData(event.currentTarget);
    const payload = {
      tracking_no: form.get("tracking_no"),
      sender_phone: form.get("sender_phone"),
      remark: form.get("remark"),
      items: validReturnItems(),
    };
    try {
      const data = await withButtonBusy(submitButton, "正在保存…", () =>
        createWithConfirmation("return", payload)
      );
      if (data.task) acceptTrackingTask(data);
      state.returnItems = [{ category: "", barcode: "", quantity: 1 }];
      state.returnDraft = { store_id: "", tracking_no: "", sender_phone: "", remark: "" };
      navigate("/returns");
      if (data.return_order?.tracking_status === "查询失败") {
        toast("退货已保存，但快递公司识别或物流查询没有成功。请查看退货看板中的红色失败原因，核对单号后再重试。", { type: "error", duration: 9000 });
      } else if (data.return_order?.express_company) {
        toast(`退货已提交，快递100识别为“${data.return_order.express_company}”。`);
      } else {
        toast("退货已保存，快递公司识别和物流查询正在后台处理，可继续操作。");
      }
    } catch (error) {
      errorToast(error);
    }
  });
}

async function renderReturnBoard(admin = false) {
  const currentView = beginView();
  await Promise.all([loadReturnOrders(admin), admin ? loadStores() : Promise.resolve()]);
  const filters = admin ? state.adminReturnFilters : state.storeReturnFilters;
  const rows = admin ? state.returnOrders : state.storeReturnOrders;
  const today = localDate();
  const yesterday = localDate(-1);
  const todayCounts = state.returnTodaySummary[admin ? "admin" : "store"];
  const counts = state.returnSummary[admin ? "admin" : "store"];
  const extra = `
    <div class="actions">
      ${admin ? `<button class="btn secondary" id="syncReturnTracking" type="button">同步退货物流</button>` : `<a class="btn primary" href="/returns/new" data-route>新增退货</a>`}
      <span class="count-pill" data-return-count="today">今日 ${todayCounts.total} 单</span>
      <span class="count-pill" data-return-count="todaySigned">今日签收 ${todayCounts["已签收"] || 0}</span>
      <span class="count-pill" data-return-count="total">共 ${counts.total} 单</span>
      <span class="count-pill" data-return-count="transit">运输中 ${counts["运输中"] || 0}</span>
      <span class="count-pill" data-return-count="signed">已签收 ${counts["已签收"] || 0}</span>
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
    <div id="trackingTaskHost">${renderTrackingTasks()}</div>
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
      <div id="returnRowsHost">${renderReturnTable(rows, admin)}</div>
      <div id="returnPaginationHost">${renderReturnPagination(admin)}</div>
    </section>
  `;
  currentView();
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
                  <td class="items-cell">${renderItemLines(row.items)}<div class="actions"><a class="btn secondary small" href="/special/new?source_return=${row.id}" data-route>发起售后发货</a><button class="text-link" data-return-aftersales="${row.id}" type="button">查看关联寄出</button></div></td>
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
  return renderTrackingDetailBlock(row, { kind: "return" });
}

function renderReturnPagination(admin) {
  const pagination = state.returnPagination[admin ? "admin" : "store"];
  const page = Number(pagination.page || 1), pages = Number(pagination.total_pages || pagination.pages || 1);
  return `<div class="shipment-pagination"><span>共 ${Number(pagination.total || 0)} 单 · 第 ${page} / ${pages} 页 · 每页 50 单</span><div class="actions"><button class="btn secondary small" data-return-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button><button class="btn secondary small" data-return-page="${page + 1}" ${page >= pages ? "disabled" : ""}>下一页</button></div></div>`;
}

function updateReturnRows(admin) {
  const host = document.getElementById("returnRowsHost");
  if (!host) return;
  const opened = [...host.querySelectorAll("details[open]")].map(node => node.dataset.trackingDetails);
  host.innerHTML = renderReturnTable(admin ? state.returnOrders : state.storeReturnOrders, admin);
  for (const id of opened) host.querySelector(`[data-tracking-details="${Number(id)}"]`)?.setAttribute("open", "");
  bindTrackingDetails(host); bindSpecialControls(host); bindReturnRefresh(host);
  const counts = state.returnSummary[admin ? "admin" : "store"], today = state.returnTodaySummary[admin ? "admin" : "store"];
  const labels = {today:`今日 ${today.total || 0} 单`, todaySigned:`今日签收 ${today["已签收"] || 0}`, total:`共 ${counts.total || 0} 单`, transit:`运输中 ${counts["运输中"] || 0}`, signed:`已签收 ${counts["已签收"] || 0}`};
  document.querySelectorAll("[data-return-count]").forEach(node => { node.textContent = labels[node.dataset.returnCount]; });
  const pagination = document.getElementById("returnPaginationHost");
  if (pagination) { pagination.innerHTML = renderReturnPagination(admin); bindReturnPagination(admin); }
}

function bindReturnPagination(admin) {
  document.querySelectorAll("[data-return-page]").forEach(node => node.addEventListener("click", () => {
    state.returnPages[admin ? "admin" : "store"] = Number(node.dataset.returnPage); render();
  }));
}

function bindReturnBoard(admin = false) {
  const filters = admin ? state.adminReturnFilters : state.storeReturnFilters;
  bindReturnPagination(admin);
  document.querySelectorAll("[data-return-preset]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const preset = event.currentTarget.dataset.returnPreset;
      const targetDate = preset === "yesterday" ? localDate(-1) : localDate();
      const next = { ...filters, date_from: targetDate, date_to: targetDate };
      state.returnPages[admin ? "admin" : "store"] = 1;
      if (admin) state.adminReturnFilters = next;
      else state.storeReturnFilters = next;
      render();
    });
  });
  document.getElementById("applyReturnFilters").addEventListener("click", () => {
    state.returnPages[admin ? "admin" : "store"] = 1;
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
    state.returnPages[admin ? "admin" : "store"] = 1;
    const empty = { store_id: "", status: "", date_from: "", date_to: "", q: "" };
    if (admin) state.adminReturnFilters = empty;
    else state.storeReturnFilters = empty;
    render();
  });
  const syncReturnTracking = document.getElementById("syncReturnTracking");
  if (syncReturnTracking) {
    syncReturnTracking.addEventListener("click", async (event) => {
      try {
        const data = await withButtonBusy(event.currentTarget, "同步中…", () =>
          api("/api/admin/return-tracking/sync", {
            method: "POST",
            body: JSON.stringify({ limit: 50 }),
          })
        );
        acceptTrackingTask(data);
      } catch (error) {
        errorToast(error, "退货物流同步失败。");
      }
    });
  }
  bindReturnRefresh();
}

function bindReturnRefresh(root = document) {
  root.querySelectorAll("[data-refresh-return]").forEach(node => {
    node.addEventListener("click", async event => {
      try {
        const data = await withButtonBusy(event.currentTarget, "正在排队…", () =>
          api(`/api/returns/${node.dataset.refreshReturn}/tracking/refresh`, { method: "POST", body: "{}" }));
        acceptTrackingTask(data);
      } catch (error) { errorToast(error); }
    });
  });
}

function renderShippingBatchPreview() {
  const preview = state.batchPreview;
  if (!preview) return "";
  const eligible = preview.eligible || [];
  const eligibleIds = new Set(eligible.map((row) => Number(row.id)));
  const selectedIds = new Set(state.batchSelectAll ? [...eligibleIds] : state.batchSelectedIds.map(Number));
  const selectedCount = state.batchSelectAll ? Number(preview.eligible_count ?? eligible.length) : selectedIds.size;
  const pagination = preview.pagination || {};
  const page = Number(pagination.page || state.batchPreviewPage), pages = Number(pagination.total_pages || pagination.pages || 1);
  const config = state.shippingConfig || {};
  const configReady = Boolean(config.enabled && config.configured);
  const missingConfig = Array.isArray(config.missing) ? config.missing : [];
  return `
    <section class="panel panel-pad shipping-batch-panel">
      <div class="section-title">
        <div><h2>选择需要打单的订单</h2><div class="muted mini">筛选匹配 ${preview.matched || 0} 单，可打单 ${preview.eligible_count ?? eligible.length} 单，已选择 <span id="batchSelectedCount">${selectedCount}</span> 单</div><div id="batchSelectionMode" class="notice">${state.batchSelectAll ? "已选择整个筛选范围，包含其他预览页。" : "仅提交手动勾选的订单；翻页保留已选项。"}</div></div>
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
      ${preview.profile_error ? `<div class="notice danger-notice" role="alert">${escapeHtml(preview.profile_error)}</div>` : ""}
      ${!config.enabled ? `<div class="notice danger-notice">电子面单服务开关未开启：请在 Render 将 <strong>SCENTPOOL_KUAIDI100_LABEL_ENABLED</strong> 设置为 <strong>1</strong>。</div>` : ""}
      ${missingConfig.length ? `<div class="notice danger-notice">Render 还缺少：<strong>${missingConfig.map(escapeHtml).join("、")}</strong>。补齐并重新部署后即可正式提交。</div>` : ""}
      <div class="batch-controls label-batch-controls">
        <div class="field fulfillment-choice">
          <label for="batchProfile">本批次从哪里发货</label>
          <select class="select" id="batchProfile">
            ${!state.shippingSettings?.default_profile_id ? `<option value="" ${!preview.profile ? "selected" : ""}>原总部配置（尚未切换）</option>` : ""}
            ${(state.shippingSettings?.fulfillment_profiles || []).map(p => `<option value="${escapeHtml(p.id)}" ${preview.profile?.id === p.id ? "selected" : ""}>${escapeHtml(p.name)} · ${escapeHtml(p.express_company)}${p.id === state.shippingSettings?.default_profile_id ? "（默认）" : ""}</option>`).join("")}
          </select>
        </div>
        ${!preview.profile ? `
        <div class="field">
          <label>已选订单统一改为</label>
          <select class="select" id="batchBulkCompany"><option value="" ${state.batchBulkCompany ? "" : "selected"}>保持各单原快递</option>${EXPRESS_COMPANIES.map(company => `<option value="${company}" ${company === state.batchBulkCompany ? "selected" : ""}>${company}</option>`).join("")}</select>
        </div>` : ""}
        <div class="inline-actions batch-selection-actions">
          <button class="btn secondary small" id="selectAllBatchOrders" type="button">全选筛选结果</button>
          <button class="btn ghost small" id="clearBatchOrders" type="button">取消全选</button>
        </div>
        <button class="btn primary" id="createShippingBatch" data-ready="${preview.settings_ready && preview.label_ready && configReady ? "1" : "0"}" type="button" ${selectedCount && preview.settings_ready && preview.label_ready && configReady ? "" : "disabled"}>确认提交 ${selectedCount} 单</button>
      </div>
      ${preview.profile ? `<div class="notice fulfillment-summary"><strong>${escapeHtml(preview.profile.name)} · ${escapeHtml(preview.profile.express_company)}</strong><br>授权网点：${escapeHtml(preview.profile.tbNet)}<br>寄件：${escapeHtml(preview.profile.sender_name)} · ${escapeHtml(preview.profile.sender_mobile)}<br>${escapeHtml(preview.profile.sender_address)}<br><span class="muted">本批次统一使用此方案；提交后锁定地址、网点与授权。已有面单下载或复打不会换方案。</span></div>` : ""}
      <div class="notice" id="batchTypeCounts">${Object.entries(preview.type_counts || {}).filter(([, count]) => count).map(([key, count]) => `${SHIPMENT_TYPES[key]?.[0] || key} ${count} 单`).join(" · ")}（预览总量，确认时按实际勾选复核）</div>
      <div class="notice">提交后将立即获取快递单号并生成电子面单，不再创建上门取件预约。</div>
      <div class="batch-order-list">
        ${eligible.map((row) => `
          <div class="batch-order-row" data-batch-shipment="${row.id}">
            <input class="batch-order-checkbox" type="checkbox" data-batch-select value="${row.id}" aria-label="选择订单 ${escapeHtml(row.business_id)}" ${selectedIds.has(Number(row.id)) ? "checked" : ""} />
            <div>${shipmentTypeBadge(row)}<strong>${escapeHtml(row.business_id)}</strong><div class="muted mini">${escapeHtml(row.store_name_snapshot)} · ${escapeHtml(row.recipient_name)} · ${escapeHtml(row.address)}</div>${row.return_unsigned_warning ? `<p class="notice">退货尚未签收，请总部核对后决定发货</p>` : ""}</div>
            ${preview.profile ? `<span class="status shipped">${escapeHtml(preview.profile.express_company)}</span>` : `<select class="select" data-batch-company>${expressCompanyOptions(state.batchCompanyOverrides[row.id] || state.batchBulkCompany || row.express_company)}</select>`}
          </div>
        `).join("") || `<div class="empty">当前筛选没有可下单订单</div>`}
      </div>
      <div class="shipment-pagination"><span>预览明细第 ${page} / ${pages} 页，每类每页最多 50 单</span><div class="actions"><button class="btn secondary small" data-batch-preview-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button><button class="btn secondary small" data-batch-preview-page="${page + 1}" ${page >= pages ? "disabled" : ""}>下一页</button></div></div>
      ${(preview.excluded || []).length ? `<details class="tracking-details"><summary>被排除共 ${preview.excluded_count ?? preview.excluded.length} 单（本次显示 ${preview.excluded.length} 单）</summary><div class="tracking-detail-lines">${preview.excluded.map((row) => `<div>${escapeHtml(row.business_id)}：${escapeHtml(row.reason)}</div>`).join("")}</div></details>` : ""}
    </section>
  `;
}

function taskAlertAdvice(type, message = "") {
  if (type === "面单下单失败") {
    const normalized = String(message || "");
    if (/行政区|省市区|地址.*解析|地址.*格式/.test(normalized)) {
      return "请核对收件地址的省、市、区和详细地址是否完整，并使用标准行政区名称；修改后再重试失败订单。";
    }
    if (/停发|暂停服务|超区|服务范围|不派送|无法配送/.test(normalized)) {
      return "当前快递可能无法配送该区域。请先确认承运范围，必要时改用其他快递公司，再重试失败订单。";
    }
    if (/余额|欠费|单量|数量不足/.test(normalized)) {
      return "请先在电子面单设置中检查网点余额或联系网点充值，确认恢复后再重试失败订单。";
    }
    if (/授权|网点|账号|账户|配置/.test(normalized)) {
      return "请进入电子面单设置检查授权账号和网点配置，确认有效后再重试失败订单。";
    }
    if (/网络|超时|服务暂时|繁忙|自动重试/.test(normalized)) {
      return "系统已经尝试自动恢复。请稍后只重试失败订单；若仍失败，把业务ID告诉管理员。";
    }
    return "请检查收件信息、授权网点和面单余额，再重新提交失败订单；仍失败时把业务ID告诉管理员。";
  }
  if (type === "面单等待过久") {
    return "系统会自动恢复卡住的任务；刷新后仍未变化时请联系管理员，不要重复创建新批次。";
  }
  if (type === "面单打印失败") {
    return "请检查打印机或浏览器弹窗；PDF批量打印失败时减少单次数量后重试。";
  }
  if (type === "退货物流查询失败" && /识别|单号|快递公司/.test(String(message || ""))) {
    return "请先核对退货看板显示的快递单号。单号录入错误或提示接口权限、不支持该快递公司时，请联系管理员处理；单号无误时等待30分钟后再查询。";
  }
  if (type === "物流查询失败" || type === "退货物流查询失败") {
    return "这不会删除快递单号。请稍后再次查询，或等待系统下一轮自动同步。";
  }
  if (type === "物流服务异常") {
    if (/此前|逐单/.test(String(message || ""))) {
      return "历史平台故障已合并显示。系统会按每个单号原定节奏重新查询；某一单查询成功后，会自动从受影响数量中移除。";
    }
    return "这是快递100接口层故障，不需要逐个修改订单。系统已停止本轮批量查询，会在下一轮先探测服务是否恢复。";
  }
  return "请刷新后重试；问题持续存在时联系管理员并提供业务ID。";
}

function taskAlertTotal() {
  return Number(state.taskAlerts?.counts?.total || 0);
}

function renderTaskAlertTrigger() {
  const total = taskAlertTotal();
  const failed = Boolean(state.taskAlertsLoadError);
  const label = failed ? "异常状态未更新" : total ? "异常提醒" : "任务正常";
  return `
    <button class="btn task-alert-trigger ${failed || total ? "has-alerts" : "is-clear"}" id="openTaskAlerts" type="button" aria-haspopup="dialog">
      <span>${label}</span>
      <span class="task-alert-badge" aria-label="${failed ? "状态读取失败" : `${total} 条待处理异常`}">${failed ? "!" : total}</span>
    </button>
  `;
}

function formatTaskInterval(minutes) {
  const value = Number(minutes || 0);
  if (!value) return "按系统配置";
  if (value % 60 === 0) return `${value / 60} 小时`;
  return `${value} 分钟`;
}

function renderTaskAlertDialog() {
  if (!state.taskAlertsOpen) return "";
  const data = state.taskAlerts || { counts: { total: 0 }, items: [] };
  const items = Array.isArray(data.items) ? data.items : [];
  const total = taskAlertTotal();
  const affectedTotal = Number(data.affected_total || total);
  const categories = ["全部", "电子面单", "物流查询", "打印"];
  const selected = categories.includes(state.taskAlertsCategory) ? state.taskAlertsCategory : "全部";
  const categoryItems = Array.isArray(data.category_items?.[selected]) ? data.category_items[selected] : [];
  const visibleItems = selected === "全部" ? items : categoryItems;
  const refresh = data.refresh || {};
  return `
    <div class="task-alert-backdrop" id="taskAlertDialogBackdrop">
      <section class="task-alert-dialog" id="taskAlertDialog" role="dialog" aria-modal="true" aria-labelledby="taskAlertDialogTitle" tabindex="-1">
        <div class="task-alert-dialog-head">
          <div>
            <h2 id="taskAlertDialogTitle">异常提醒</h2>
            <div class="muted mini">当前 ${total} 条提醒${affectedTotal > total ? `，涉及 ${affectedTotal} 项任务` : ""}；恢复后自动消失。</div>
          </div>
          <button class="task-alert-close" id="closeTaskAlerts" type="button" aria-label="关闭异常提醒">×</button>
        </div>
        <div class="task-alert-refresh-note">
          提醒每 ${Number(refresh.alerts_seconds || 60)} 秒自动刷新；自动物流的最短查询间隔：发货 ${formatTaskInterval(refresh.shipment_tracking_minutes || 360)}，退货 ${formatTaskInterval(refresh.return_tracking_minutes || 720)}。实际更新时间受排队和快递服务影响。
        </div>
        <div class="task-alert-categories" role="tablist" aria-label="异常分类">
          ${categories.map((category) => {
            const count = category === "全部" ? total : Number(data.category_counts?.[category] || 0);
            return `<button class="task-alert-category ${selected === category ? "active" : ""}" data-task-alert-category="${category}" type="button" aria-pressed="${selected === category}">${category}<span>${count}</span></button>`;
          }).join("")}
        </div>
        <div class="task-alert-dialog-body" aria-live="polite">
          ${state.taskAlertsLoadError ? `<div class="notice danger-notice"><strong>异常状态暂时无法更新。</strong><br>${escapeHtml(state.taskAlertsLoadError)}<br>请检查网络；状态恢复前不要重复提交同一批任务。</div>` : ""}
          ${!state.taskAlertsLoadError && !visibleItems.length ? `<div class="task-ok-message">${selected === "全部" ? "当前没有需要处理的任务异常。" : `当前没有“${escapeHtml(selected)}”类异常。`}</div>` : ""}
          ${visibleItems.length ? `
            <div class="task-alert-list">
              ${visibleItems.map((item) => `
                <article class="task-alert-item ${item.system_scope ? "system-scope" : ""}">
                  <div class="task-alert-heading">
                    <div><strong>${escapeHtml(item.type)}</strong> · <span translate="no">${escapeHtml(item.business_id)}</span>${Number(item.affected_count || 0) > 1 ? ` · 影响 ${Number(item.affected_count)} 项` : ""}</div>
                    <span class="status exception">${escapeHtml(item.status)}</span>
                  </div>
                  <div class="task-alert-tags">
                    ${!item.system_scope && !item.type.startsWith("退货") ? shipmentTypeBadge(item) : ""}
                    <span>${escapeHtml(item.category || "其他")}</span>
                    <span>${escapeHtml(item.reason || "其他问题")}</span>
                    <span class="${item.auto_retry ? "auto" : "manual"}">${item.auto_retry ? "系统继续检查" : "需要人工处理"}</span>
                  </div>
                  <div class="muted mini">${escapeHtml(item.store_name)} · ${escapeHtml(formatDate(item.updated_at))}</div>
                  <div class="task-alert-message"><strong>失败原因：</strong>${escapeHtml(item.message || "任务没有成功完成。")}</div>
                  <div class="task-alert-advice"><strong>怎么处理：</strong>${escapeHtml(taskAlertAdvice(item.type, item.message))}</div>
                  ${item.system_scope
                    ? `<div class="task-alert-system-note">${item.incident_active
                        ? "无需逐单处理，也不要重复点击同步；服务恢复后提醒会自动更新。"
                        : "无需批量修改订单；每个单号重新查询成功后，受影响数量会自动减少。"}</div>`
                    : `<div class="inline-actions">
                        ${item.type === "面单下单失败" && item.batch_id ? `<button class="btn primary small" data-retry-alert-batch="${Number(item.batch_id)}" type="button">重新提交失败订单</button>` : ""}
                        ${item.type === "退货物流查询失败"
                          ? `<button class="btn secondary small" data-locate-return-alert="${escapeHtml(item.business_id)}" type="button">在退货看板中查看</button>`
                          : `<button class="btn secondary small" data-locate-task-alert="${escapeHtml(item.business_id)}" type="button">在订单列表中查看</button>`}
                        <button class="btn secondary small" data-resolve-task-alert="${escapeHtml(item.alert_key)}" data-alert-fingerprint="${escapeHtml(item.fingerprint)}" type="button">标记人工已处理</button>
                      </div>`}
                </article>
              `).join("")}
            </div>
            ${total > items.length ? `<div class="notice">本次先显示最近 ${items.length} 项；处理后列表会自动补充。</div>` : ""}
          ` : ""}
        </div>
      </section>
    </div>
  `;
}

function updateTaskAlertUi({ focusDialog = false, restoreTriggerFocus = false } = {}) {
  const trigger = document.getElementById("openTaskAlerts");
  const triggerHadFocus = trigger === document.activeElement;
  const dialogHadFocus = document.getElementById("taskAlertDialog")?.contains(document.activeElement);
  if (trigger) trigger.outerHTML = renderTaskAlertTrigger();
  const host = document.getElementById("taskAlertDialogHost");
  if (host) host.innerHTML = renderTaskAlertDialog();
  document.body.classList.toggle("task-alert-dialog-open", state.taskAlertsOpen);
  bindTaskAlertControls();
  if (focusDialog || dialogHadFocus) document.getElementById("taskAlertDialog")?.focus();
  if (restoreTriggerFocus || (triggerHadFocus && !state.taskAlertsOpen)) document.getElementById("openTaskAlerts")?.focus();
}

function bindTaskAlertControls() {
  document.getElementById("openTaskAlerts")?.addEventListener("click", () => {
    state.taskAlertsOpen = true;
    updateTaskAlertUi({ focusDialog: true });
  });
  document.getElementById("closeTaskAlerts")?.addEventListener("click", () => {
    state.taskAlertsOpen = false;
    updateTaskAlertUi({ restoreTriggerFocus: true });
  });
  const backdrop = document.getElementById("taskAlertDialogBackdrop");
  backdrop?.addEventListener("click", (event) => {
    if (event.target !== event.currentTarget) return;
    state.taskAlertsOpen = false;
    updateTaskAlertUi({ restoreTriggerFocus: true });
  });
  document.getElementById("taskAlertDialog")?.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      state.taskAlertsOpen = false;
      updateTaskAlertUi({ restoreTriggerFocus: true });
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      event.currentTarget.querySelectorAll("button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled])")
    );
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === event.currentTarget)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  document.querySelectorAll("[data-task-alert-category]").forEach((node) => {
    node.addEventListener("click", (event) => {
      state.taskAlertsCategory = event.currentTarget.dataset.taskAlertCategory || "全部";
      updateTaskAlertUi({ focusDialog: true });
    });
  });
  document.querySelectorAll("[data-locate-return-alert]").forEach((node) => {
    node.addEventListener("click", (event) => {
      state.taskAlertsOpen = false;
      state.adminReturnFilters = {
        store_id: "",
        status: "",
        date_from: "",
        date_to: "",
        q: event.currentTarget.dataset.locateReturnAlert || "",
      };
      navigate("/admin/returns");
    });
  });
  document.querySelectorAll("[data-locate-task-alert]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      state.taskAlertsOpen = false;
      state.adminFilters = {
        store_id: "",
        status: "",
        date_from: "",
        date_to: "",
        q: event.currentTarget.dataset.locateTaskAlert || "",
      };
      state.adminShipmentPage = 1;
      await render();
      document.querySelector(".shipments-table")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
  document.querySelectorAll("[data-retry-alert-batch]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const batchId = Number(event.currentTarget.dataset.retryAlertBatch);
      if (!batchId || !confirm("确认重新提交这个批次中的失败订单？系统只会重试失败项，不会重复处理成功订单。")) return;
      try {
        state.activeShippingBatch = await withButtonBusy(event.currentTarget, "重新排队中…", () =>
          api(`/api/admin/shipping-batches/${batchId}/retry`, { method: "POST", body: JSON.stringify({}) })
        );
        sessionStorage.setItem(shippingBatchStorageKey(), String(batchId));
        await Promise.all([loadShipments(), loadTaskAlerts()]);
        toast("失败订单已重新加入队列，页面会持续显示处理结果。");
        render({ refreshData: false });
      } catch (error) {
        errorToast(error, "失败订单重新提交失败。");
      }
    });
  });
  document.querySelectorAll("[data-resolve-task-alert]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const alertKey = event.currentTarget.dataset.resolveTaskAlert || "";
      const fingerprint = event.currentTarget.dataset.alertFingerprint || "";
      if (!confirm("确认这条异常已经在线下或第三方平台处理完成？这只会隐藏当前这一次提醒，不会修改订单或快递状态；如果系统之后再次检测到失败，会重新提醒。")) return;
      try {
        state.taskAlerts = await withButtonBusy(event.currentTarget, "确认中…", () =>
          api("/api/admin/task-alerts/resolve", {
            method: "POST",
            body: JSON.stringify({ alert_key: alertKey, fingerprint }),
          })
        );
        state.taskAlertsLoadError = "";
        updateTaskAlertUi({ focusDialog: true });
        toast("已标记为人工处理；如果问题再次出现，系统会重新提醒。");
      } catch (error) {
        errorToast(error, "无法确认这条异常，请刷新后再试。");
        await loadTaskAlerts();
        updateTaskAlertUi({ focusDialog: true });
      }
    });
  });
}

function batchPrintableShipments() {
  return state.shipments.filter(
    (row) => row.label_print_status === "待打印" && String(row.label_url || "").trim()
  );
}

function renderBatchPrintPanel() {
  if (!state.batchPrintOpen) return "";
  const eligible = batchPrintableShipments();
  const eligibleIds = new Set(eligible.map((row) => Number(row.id)));
  const selectedIds = new Set(
    state.batchPrintSelectedIds.filter((id) => eligibleIds.has(Number(id))).map(Number)
  );
  return `
    <section class="panel panel-pad shipping-batch-panel">
      <div class="section-title">
        <div><h2>批量打印面单</h2><div class="muted mini">当前页待打印 ${eligible.length} 单，已选择 <span id="batchPrintSelectedCount">${selectedIds.size}</span> 单；切换页面后可继续分批打印</div></div>
        <button class="btn ghost small" id="closeBatchPrint" type="button">关闭</button>
      </div>
      <div class="inline-actions batch-print-actions">
        <button class="btn secondary small" id="selectAllBatchPrint" type="button">全选本页</button>
        <button class="btn ghost small" id="clearBatchPrint" type="button">取消全选</button>
        <button class="btn primary" id="mergeBatchPrint" type="button" ${selectedIds.size ? "" : "disabled"}>合并并打印 ${selectedIds.size} 单</button>
      </div>
      ${state.batchPrintError ? `<div class="notice danger-notice"><strong>批量打印没有完成。</strong><br>${escapeHtml(state.batchPrintError)}<br>请减少勾选数量、检查网络或浏览器弹窗后重试；失败时订单不会被标记为已打印。</div>` : ""}
      <div class="notice task-progress" id="batchPrintStatus" role="status" aria-live="polite" hidden></div>
      <div class="batch-order-list">
        ${eligible.map((row) => `
          <div class="batch-order-row">
            <input class="batch-order-checkbox" type="checkbox" data-batch-print-select value="${row.id}" aria-label="选择面单 ${escapeHtml(row.business_id)}" ${selectedIds.has(Number(row.id)) ? "checked" : ""} />
            <div><strong>${escapeHtml(row.business_id)}</strong><div class="muted mini">${escapeHtml(row.store_name_snapshot)} · ${escapeHtml(row.recipient_name)}</div></div>
            <span class="muted mini">${escapeHtml(row.tracking_no)}</span>
          </div>
        `).join("") || `<div class="empty">当前页没有待打印面单</div>`}
      </div>
    </section>
  `;
}

function renderShippingBatchProgress() {
  const data = state.activeShippingBatch;
  if (!data?.batch) return "";
  const batch = data.batch;
  const counts = data.counts || {};
  const failed = counts["失败"] || 0;
  const page = Number(data.pagination?.page || state.batchProgressPage), pages = Number(data.pagination?.total_pages || data.pagination?.pages || 1);
  return `
    <section class="panel panel-pad shipping-batch-panel">
      <div class="section-title">
        <div><h2>电子面单批次 #${batch.id}</h2><div class="muted mini">${escapeHtml(batch.fulfillment_name || "历史批次")} · 后台按顺序取号并生成面单</div></div>
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
        ${failed ? `<button class="btn secondary small" id="showBatchFailures" type="button">${state.batchProgressFailedOnly ? "查看全部进度" : `查看全部 ${failed} 项失败`}</button>` : ""}
        ${failed ? `<button class="btn secondary small" id="retryShippingBatch" type="button">仅重试失败订单</button>` : ""}
        <button class="btn ghost small" id="closeShippingBatch" type="button">收起批次</button>
      </div>
      ${state.shippingBatchPollError ? `<div class="notice danger-notice"><strong>批次进度刷新失败。</strong><br>${escapeHtml(state.shippingBatchPollError)}<br>后台任务不一定停止，请检查网络后刷新页面，不要重复提交同一批订单。</div>` : ""}
      ${failed ? `<div class="notice danger-notice"><strong>本批次有 ${failed} 单没有取得快递单号。</strong><br>请阅读下方原因，检查信息后点击“仅重试失败订单”；不要为同一订单重新创建另一批次。</div>` : ""}
      ${failed ? `<div class="batch-errors">${(data.items || []).filter((item) => item.status === "失败").map((item) => `<div class="batch-error-item"><strong>${escapeHtml(item.business_id)}</strong><div>失败原因：${escapeHtml(item.error || "电子面单没有成功生成。")}</div><div>怎么处理：${escapeHtml(taskAlertAdvice("面单下单失败", item.error))}</div></div>`).join("")}</div>` : ""}
      ${failed && !(data.items || []).some(item => item.status === "失败") ? `<p class="notice">本页没有失败明细，请点“查看全部 ${failed} 项失败”，不会因分页隐藏失败记录。</p>` : ""}
      ${pages > 1 ? `<div class="shipment-pagination"><span>进度明细第 ${page} / ${pages} 页</span><div class="actions"><button class="btn secondary small" data-batch-progress-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button><button class="btn secondary small" data-batch-progress-page="${page + 1}" ${page >= pages ? "disabled" : ""}>下一页</button></div></div>` : ""}
    </section>
  `;
}

function renderAdminShipmentSummary() {
  const counts = state.adminShipmentSummary || {};
  return `<span class="count-pill">当前范围 ${counts.total || 0} 单</span>${["待处理","已发货","已签收","异常"].map(status => `<span class="count-pill">${status} ${counts[status] || 0}</span>`).join("")}`;
}

async function renderAdmin({ refreshData = true } = {}) {
  const currentView = beginView();
  captureAdminRowDrafts();
  if (refreshData) {
    const loads = [loadShipments()];
    if (!state.adminBoardLoaded) {
      loads.push(ensureProductsGrouped(), loadStores(), loadShippingSettings(), loadActiveShippingBatch(), loadTaskAlerts());
    }
    await Promise.all(loads);
    state.adminBoardLoaded = true;
  }
  const today = localDate();
  const yesterday = localDate(-1);
  const exportParams = new URLSearchParams();
  Object.entries(state.adminFilters).forEach(([key, value]) => {
    if (value) exportParams.set(key, value);
  });
  const counts = state.adminShipmentSummary || { total: 0 };
  const pageData = paginatedShipments(state.shipments, "admin");
  const content = `
    ${pageHead(
      "发货后台",
      "总部统一处理门店提交的发货需求。",
      `<div class="actions">
        ${renderTaskAlertTrigger()}
        <button class="btn primary" id="previewShippingBatch" type="button">批量打单</button>
        <button class="btn secondary" id="openBatchPrint" type="button" ${batchPrintableShipments().length ? "" : "disabled"}>批量打印面单</button>
        <button class="btn secondary" id="syncTracking" type="button">同步物流</button>
        <a class="btn primary" href="/api/export/shipments.xlsx?${exportParams.toString()}">导出 XLSX</a>
        <a class="btn secondary" href="/api/export/shipments.csv?${exportParams.toString()}">导出 CSV</a>
        <a class="btn secondary" href="/api/admin/backup.db">备份数据库</a>
      </div>`
    )}
    <div id="taskAlertDialogHost">${renderTaskAlertDialog()}</div>
    ${classificationFilters("admin")}
    ${renderBatchPrintPanel()}
    <div id="shippingBatchPreviewHost">${renderShippingBatchPreview()}</div>
    <div id="shippingBatchProgressHost">${renderShippingBatchProgress()}</div>
    <div id="trackingTaskHost">${renderTrackingTasks()}</div>
    <div id="shipmentListUpdateNotice" class="notice" role="status" hidden></div>
    <section class="panel panel-pad">
      <div class="status-overview" id="adminShipmentSummary">${renderAdminShipmentSummary()}</div>
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
  currentView();
  document.getElementById("app").innerHTML = shell(content);
  restoreAdminRowDrafts();
  bindCommon();
  bindAdmin();
  scheduleShippingBatchPoll();
  scheduleTaskAlertPoll();
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
  return state.editingShipmentShippingId === row.id || state.adminRowDrafts.get(Number(row.id))?.["data-tracking"] !== undefined || !String(row.tracking_no || "").trim();
}

function renderAdminShipmentStatusCell(row) {
  if (!shipmentShippingEditing(row)) {
    return `<span class="status ${statusClass(row.status)}">${escapeHtml(row.status)}</span>${renderBookingStatus(row)}`;
  }
  return `
    <select class="table-input" data-status>
      ${state.statuses.map((status) => `<option value="${status}" ${status === row.status ? "selected" : ""}>${status}</option>`).join("")}
    </select>
    ${renderBookingStatus(row)}
  `;
}

function renderAdminShipmentShippingCell(row) {
  const origin = row.fulfillment_name ? `<div class="mini"><strong>${escapeHtml(row.fulfillment_name)}</strong></div>` : "";
  if (!shipmentShippingEditing(row)) {
    if (!row.tracking_no) return `${origin}<span class="muted">快递平台正在分配单号</span>`;
    return origin + renderTrackingDetailBlock(row, { showCopy: true });
  }
  return `
    <div class="shipping-editor">
      ${origin}
      ${row.booking_status === "下单失败" ? `<div class="inline-failure"><strong>电子面单未成功。</strong><div>${escapeHtml(row.booking_error || "没有取得快递单号。")}</div><div>${escapeHtml(taskAlertAdvice("面单下单失败", row.booking_error))}</div></div>` : ""}
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

function renderAdminShipmentOrderCell(row) {
  const editable = row.status === "待处理" && bookingEditable(row);
  if (editable && (state.editingShipmentRemarkId === row.id || state.adminRowDrafts.get(Number(row.id))?.["data-admin-remark"] !== undefined)) {
    return `
      <div class="admin-order-cell">
        ${shipmentContext(row)}
        <strong class="order-number" translate="no">${escapeHtml(row.store_order_no)}</strong>
        <div class="store-remark-editor">
          <textarea class="table-input" data-admin-remark maxlength="500" rows="3" aria-label="订单备注">${escapeHtml(row.remark || "")}</textarea>
          <div class="inline-actions">
            <button class="btn primary small" data-save-admin-remark="${row.id}" type="button">保存备注</button>
            <button class="btn ghost small" data-cancel-admin-remark type="button">取消</button>
          </div>
        </div>
      </div>
    `;
  }
  return `
    <div class="admin-order-cell">
      ${shipmentContext(row)}
      <strong class="order-number" translate="no">${escapeHtml(row.store_order_no)}</strong>
      ${row.remark ? `<div class="muted mini order-remark">${escapeHtml(row.remark)}</div>` : `<div class="muted mini">无备注</div>`}
      ${editable ? `<button class="btn secondary small" data-edit-admin-remark="${row.id}" type="button">修改备注</button>` : ""}
    </div>
  `;
}

function renderShipmentActions(row) {
  if (row._live_partial) return `<span class="muted mini">已取得快递单号。完成编辑后点击“筛选”，查看完整面单操作。</span>`;
  const editing = shipmentShippingEditing(row);
  const shippedAt = row.shipped_at ? `<div class="muted mini action-time">${escapeHtml(formatDate(row.shipped_at))}</div>` : "";
  if (!bookingEditable(row)) {
    const canCancel = row.booking_task_id && row.tracking_no && row.status !== "已签收";
    return `
      <div class="shipment-actions">
        ${row.label_url ? `<a class="btn secondary small" href="${escapeHtml(row.label_url)}" target="_blank" rel="noopener">查看面单</a>` : ""}
        ${row.label_url && row.label_print_status !== "打印成功" ? `<button class="btn secondary small" data-label-printed="${row.id}" type="button">标记已打印</button>` : ""}
        ${row.label_print_type === "CLOUD" && row.booking_task_id ? `<button class="btn secondary small" data-reprint-label="${row.id}" type="button">复打面单</button>` : ""}
        ${canCancel ? `<button class="btn danger small" data-cancel-label="${row.id}" type="button">取消并回收面单</button>` : `<span class="muted mini">面单处理中</span>`}
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
        ${row.status === "待处理" ? `<button class="btn danger small" data-delete-shipment="${row.id}" data-order-no="${escapeHtml(row.store_order_no)}" type="button">删除整单</button>` : ""}
        ${shippedAt}
      </div>
    `;
  }
  return `
    <div class="shipment-actions">
      <button class="btn secondary small" data-edit-shipping="${row.id}" type="button">编辑</button>
      ${row.tracking_no ? `<button class="btn secondary small" data-refresh-tracking="${row.id}" type="button">查物流</button>` : ""}
      ${row.status === "待处理" ? `<button class="btn danger small" data-delete-shipment="${row.id}" data-order-no="${escapeHtml(row.store_order_no)}" type="button">删除整单</button>` : ""}
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
                  <td class="sequence-cell">${row.id}</td>
                  <td class="business-cell">
                    <strong class="business-id" translate="no">${escapeHtml(shipmentBusinessId(row))}</strong>
                  </td>
                  <td class="created-cell">${escapeHtml(formatDate(row.created_at))}</td>
                  <td class="store-cell">${escapeHtml(row.store_name_snapshot)}</td>
                  <td>${renderAdminShipmentOrderCell(row)}</td>
                  <td class="recipient-cell">
                    <strong class="recipient-name">${escapeHtml(row.recipient_name)}</strong>
                    <span class="muted recipient-phone" translate="no">${escapeHtml(row.phone)}</span>
                    <span class="recipient-address">${escapeHtml(row.address)}</span>
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

function shippingBatchConfirmationSummary(shipments, total) {
  const typeCounts = state.batchSelectAll ? { ...state.batchPreview.type_counts } : {};
  const companyCounts = state.batchSelectAll
    ? (state.batchBulkCompany ? { [state.batchBulkCompany]: total } : { ...state.batchPreview.company_counts })
    : {};
  for (const choice of shipments) {
    const company = choice.express_company || state.batchKnownCompanies[choice.id] || DEFAULT_EXPRESS_COMPANY;
    if (state.batchSelectAll) {
      const original = state.batchBulkCompany || state.batchKnownCompanies[choice.id] || DEFAULT_EXPRESS_COMPANY;
      companyCounts[original] = (companyCounts[original] || 0) - 1;
    } else {
      const type = state.batchKnownTypes[choice.id] || "legacy";
      typeCounts[type] = (typeCounts[type] || 0) + 1;
    }
    companyCounts[company] = (companyCounts[company] || 0) + 1;
  }
  return {
    total,
    scope: state.batchSelectAll ? "整个筛选范围（包含其他预览页）" : "仅手动勾选的订单（包含跨页勾选）",
    typeSummary: Object.entries(typeCounts).filter(([, count]) => count > 0).map(([type, count]) => `${SHIPMENT_TYPES[type]?.[0] || "历史未分类"} ${count} 单`).join("、"),
    companySummary: Object.entries(companyCounts).filter(([, count]) => count > 0).map(([company, count]) => `${company} ${count} 单`).join("、"),
    profile: state.batchPreview.profile || null,
  };
}

function confirmShippingBatch(summary) {
  return new Promise(resolve => {
    const previousFocus = document.activeElement;
    const overlay = document.createElement("div");
    overlay.className = "batch-confirm-backdrop";
    overlay.innerHTML = `<section class="batch-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="batchConfirmTitle" aria-describedby="batchConfirmDescription" tabindex="-1">
      <h2 id="batchConfirmTitle">核对本批次电子面单</h2>
      <div id="batchConfirmDescription"><p>本次将提交 <strong>${Number(summary.total)} 单</strong>，快递公司接单成功后将生成快递单号。</p>
      <dl><dt>提交范围</dt><dd>${escapeHtml(summary.scope)}</dd><dt>发货类别</dt><dd>${escapeHtml(summary.typeSummary)}</dd><dt>快递公司</dt><dd>${escapeHtml(summary.companySummary)}</dd></dl>
      ${summary.profile ? `<dl><dt>发货方案</dt><dd>${escapeHtml(summary.profile.name)}</dd><dt>授权网点</dt><dd>${escapeHtml(summary.profile.tbNet)}</dd><dt>寄件信息</dt><dd>${escapeHtml(summary.profile.sender_name)} · ${escapeHtml(summary.profile.sender_mobile)}<br>${escapeHtml(summary.profile.sender_address)}</dd></dl>` : ""}
      <p class="muted">请再次核对范围和快递公司。返回修改不会创建任务。</p></div>
      <div class="batch-confirm-actions"><button type="button" class="btn secondary" data-batch-confirm-cancel>返回修改</button><button type="button" class="btn primary" data-batch-confirm-accept>确认创建 ${Number(summary.total)} 单任务</button></div>
    </section>`;
    const cancel = () => finish(false);
    const finish = accepted => {
      activeConfirmations.delete(cancel);
      overlay.remove();
      if (previousFocus?.isConnected) previousFocus.focus();
      resolve(accepted);
    };
    activeConfirmations.add(cancel);
    overlay.querySelector("[data-batch-confirm-cancel]").addEventListener("click", cancel);
    overlay.querySelector("[data-batch-confirm-accept]").addEventListener("click", () => finish(true));
    overlay.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); cancel(); }
      if (event.key === "Tab") {
        const buttons = Array.from(overlay.querySelectorAll("button"));
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
    document.body.appendChild(overlay);
    overlay.querySelector("[data-batch-confirm-cancel]").focus();
  });
}

function bindAdmin(root = document) {
  const dom = root === document ? document : {
    querySelectorAll: selector => root.querySelectorAll(selector),
    querySelector: selector => root.matches(selector) ? root : root.querySelector(selector),
    getElementById: id => root.querySelector(`#${id}`),
  };
  if (root === document) bindTaskAlertControls();
  dom.getElementById("openBatchPrint")?.addEventListener("click", () => {
    state.batchPrintSelectedIds = batchPrintableShipments().map((row) => Number(row.id));
    state.batchPrintOpen = true;
    render({ refreshData: false });
  });
  dom.getElementById("closeBatchPrint")?.addEventListener("click", () => {
    state.batchPrintOpen = false;
    state.batchPrintSelectedIds = [];
    state.batchPrintError = "";
    render({ refreshData: false });
  });
  const updateBatchPrintSelection = () => {
    const selected = Array.from(dom.querySelectorAll("[data-batch-print-select]:checked"))
      .map((node) => Number(node.value));
    state.batchPrintSelectedIds = selected;
    const count = dom.getElementById("batchPrintSelectedCount");
    if (count) count.textContent = String(selected.length);
    const submit = dom.getElementById("mergeBatchPrint");
    if (submit) {
      submit.textContent = `合并并打印 ${selected.length} 单`;
      submit.disabled = !selected.length;
    }
  };
  dom.querySelectorAll("[data-batch-print-select]").forEach((node) => {
    node.addEventListener("change", updateBatchPrintSelection);
  });
  dom.getElementById("selectAllBatchPrint")?.addEventListener("click", () => {
    dom.querySelectorAll("[data-batch-print-select]").forEach((node) => { node.checked = true; });
    updateBatchPrintSelection();
  });
  dom.getElementById("clearBatchPrint")?.addEventListener("click", () => {
    dom.querySelectorAll("[data-batch-print-select]").forEach((node) => { node.checked = false; });
    updateBatchPrintSelection();
  });
  dom.getElementById("mergeBatchPrint")?.addEventListener("click", async () => {
    const shipmentIds = Array.from(dom.querySelectorAll("[data-batch-print-select]:checked"))
      .map((node) => Number(node.value));
    if (!shipmentIds.length) {
      toast("请至少选择一张待打印面单。");
      return;
    }
    if (!confirm(`确认合并并打印 ${shipmentIds.length} 张面单？生成成功后这些订单将标记为已打印。`)) return;
    const printWindow = window.open("", "_blank");
    if (!printWindow) {
      toast("浏览器阻止了打印窗口，请允许本站打开弹窗后重试。");
      return;
    }
    printWindow.document.write("<!doctype html><meta charset='utf-8'><title>正在合并面单</title><p style='font-family:sans-serif;padding:32px;line-height:1.7'>正在合并面单，请稍候。<br>订单较多时可能需要 10–30 秒，请不要重复点击或关闭此窗口。</p>");
    printWindow.document.close();
    const button = dom.getElementById("mergeBatchPrint");
    const progress = dom.getElementById("batchPrintStatus");
    state.batchPrintError = "";
    if (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = `正在合并 ${shipmentIds.length} 单…`;
    }
    if (progress) {
      progress.hidden = false;
      progress.textContent = `正在合并 ${shipmentIds.length} 张面单，请勿重复提交。`;
    }
    const slowTimer = setTimeout(() => {
      if (progress?.isConnected) {
        progress.textContent = "任务仍在正常处理。较大批次可能需要更长时间，请继续等待；若最终失败，页面会明确显示原因。";
      }
    }, 8000);
    try {
      const response = await fetch("/api/admin/labels/batch-print", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ shipment_ids: shipmentIds }),
      });
      if (!response.ok) {
        const contentType = response.headers.get("content-type") || "";
        const errorData = contentType.includes("application/json") ? await response.json() : await response.text();
        throw new Error(errorData.error || errorData || "批量面单合并失败");
      }
      const pdfUrl = URL.createObjectURL(await response.blob());
      printWindow.location.replace(pdfUrl);
      setTimeout(() => {
        try {
          printWindow.focus();
          printWindow.print();
        } catch (_error) {
          // PDF remains open so the browser print button can still be used.
        }
      }, 1800);
      setTimeout(() => URL.revokeObjectURL(pdfUrl), 10 * 60 * 1000);
      state.batchPrintOpen = false;
      state.batchPrintSelectedIds = [];
      state.batchPrintError = "";
      toast(`已合并 ${shipmentIds.length} 张面单并标记为已打印。`);
      render();
    } catch (error) {
      printWindow.close();
      state.batchPrintError = error.message || "批量面单合并失败。";
      errorToast(error, "批量面单合并失败。");
      render({ refreshData: false });
    } finally {
      clearTimeout(slowTimer);
      if (button?.isConnected) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = `合并并打印 ${shipmentIds.length} 单`;
      }
    }
  });
  const previewButton = dom.getElementById("previewShippingBatch");
  if (previewButton) {
    previewButton.addEventListener("click", async (event) => {
      try {
        state.batchProfileId = null; // Each new preview starts from the configured default.
        state.batchFilters = {
          ...state.adminFilters,
          store_id: state.adminFilters.store_id,
          status: "待处理",
          date_from: state.adminFilters.date_from,
          date_to: state.adminFilters.date_to,
          q: state.adminFilters.q,
        };
        await withButtonBusy(event.currentTarget, "正在加载…", () => loadShippingBatchPreview(state.batchFilters));
        updateBatchPreviewUi(); updateShippingBatchUi(); scheduleShippingBatchPoll();
      } catch (error) {
        errorToast(error);
      }
    });
  }
  dom.getElementById("closeBatchPreview")?.addEventListener("click", () => {
    state.batchPreview = null;
    state.batchSelectedIds = [];
    updateBatchPreviewUi(); updateShippingBatchUi(); scheduleShippingBatchPoll();
  });
  const updateBatchSelection = () => {
    const selected = Array.from(dom.querySelectorAll("[data-batch-select]:checked")).map((node) => Number(node.value));
    const pageIds = new Set((state.batchPreview?.eligible || []).map(row => Number(row.id)));
    state.batchSelectedIds = [...state.batchSelectedIds.filter(id => !pageIds.has(id)), ...selected];
    const total = state.batchSelectAll ? Number(state.batchPreview?.eligible_count || 0) : state.batchSelectedIds.length;
    const count = dom.getElementById("batchSelectedCount");
    if (count) count.textContent = String(total);
    const mode = dom.getElementById("batchSelectionMode");
    if (mode) mode.textContent = state.batchSelectAll ? "已选择整个筛选范围，包含其他预览页。" : "仅提交手动勾选的订单；翻页保留已选项。";
    const submit = dom.getElementById("createShippingBatch");
    if (submit) {
      submit.textContent = `确认提交 ${total} 单`;
      submit.disabled = !total || submit.dataset.ready !== "1";
    }
  };
  dom.querySelectorAll("[data-batch-select]").forEach((node) => node.addEventListener("change", () => {
    if (state.batchSelectAll) {
      state.batchSelectAll = false; state.batchSelectedIds = [];
      toast("已改为手动勾选模式：仅当前页勾选项已选中，其他页未自动选择。");
    }
    updateBatchSelection();
  }));
  dom.getElementById("selectAllBatchOrders")?.addEventListener("click", () => {
    state.batchSelectAll = true;
    dom.querySelectorAll("[data-batch-select]").forEach((node) => { node.checked = true; });
    updateBatchSelection();
  });
  dom.getElementById("clearBatchOrders")?.addEventListener("click", () => {
    state.batchSelectAll = false; state.batchSelectedIds = [];
    dom.querySelectorAll("[data-batch-select]").forEach((node) => { node.checked = false; });
    updateBatchSelection();
  });
  dom.getElementById("applyBatchFilters")?.addEventListener("click", async () => {
    state.batchFilters = {
      ...state.batchFilters,
      store_id: dom.getElementById("batchFilterStore").value,
      status: "待处理",
      date_from: dom.getElementById("batchFilterFrom").value,
      date_to: dom.getElementById("batchFilterTo").value,
      q: dom.getElementById("batchFilterQ").value.trim(),
    };
    try {
      await loadShippingBatchPreview(state.batchFilters);
      updateBatchPreviewUi(); updateShippingBatchUi(); scheduleShippingBatchPoll();
    } catch (error) {
      errorToast(error);
    }
  });
  dom.getElementById("resetBatchFilters")?.addEventListener("click", async () => {
    state.batchFilters = { ...state.batchFilters, store_id: "", status: "待处理", date_from: "", date_to: "", q: "" };
    try {
      await loadShippingBatchPreview(state.batchFilters);
      updateBatchPreviewUi(); updateShippingBatchUi(); scheduleShippingBatchPoll();
    } catch (error) {
      errorToast(error);
    }
  });
  dom.getElementById("batchProfile")?.addEventListener("change", async (event) => {
    const select = event.currentTarget;
    const oldId = state.batchProfileId;
    state.batchProfileId = select.value;
    select.disabled = true;
    state.batchSelectAll = false; state.batchSelectedIds = [];
    dom.getElementById("createShippingBatch").disabled = true;
    try {
      await loadShippingBatchPreview(state.batchFilters);
      state.batchSelectAll = false; state.batchSelectedIds = [];
      updateBatchPreviewUi();
      toast("发货方案已切换，请重新勾选订单并核对地址。");
    } catch (error) {
      state.batchProfileId = oldId;
      state.batchPreview = null; updateBatchPreviewUi();
      errorToast(error, "方案切换失败，请重新打开批量打单。");
    }
  });
  dom.getElementById("batchBulkCompany")?.addEventListener("change", (event) => {
    state.batchBulkCompany = event.currentTarget.value;
    state.batchCompanyOverrides = {};
    dom.querySelectorAll("[data-batch-shipment]").forEach((row) => {
      if (row.querySelector("[data-batch-select]")?.checked) row.querySelector("[data-batch-company]").value = state.batchBulkCompany || state.batchPreview.eligible.find(item => Number(item.id) === Number(row.dataset.batchShipment))?.express_company || DEFAULT_EXPRESS_COMPANY;
    });
  });
  dom.querySelectorAll("[data-batch-company]").forEach(node => node.addEventListener("change", () => {
    state.batchCompanyOverrides[node.closest("[data-batch-shipment]").dataset.batchShipment] = node.value;
  }));
  dom.querySelectorAll("[data-batch-preview-page]").forEach(node => node.addEventListener("click", async () => {
    state.batchPreviewPage = Number(node.dataset.batchPreviewPage);
    try { await withButtonBusy(node, "读取中…", () => loadShippingBatchPreview(state.batchFilters, { reset: false })); updateBatchPreviewUi(); }
    catch (error) { errorToast(error); }
  }));
  dom.getElementById("createShippingBatch")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (busyOperations.has(button)) return;
    const shipments = (state.batchSelectAll ? Object.keys(state.batchCompanyOverrides).map(Number) : state.batchSelectedIds)
      .map(id => ({ id, ...(state.batchCompanyOverrides[id] || state.batchBulkCompany ? { express_company: state.batchCompanyOverrides[id] || state.batchBulkCompany } : {}) }));
    const total = state.batchSelectAll ? Number(state.batchPreview.eligible_count || 0) : shipments.length;
    if (!total) {
      toast("请至少选择一个需要打单的订单。");
      return;
    }
    const summary = shippingBatchConfirmationSummary(shipments, total);
    const payload = {
      filters: { ...state.batchFilters }, shipments,
      selection_mode: state.batchSelectAll ? "all_matching" : "selected",
      preview_fingerprint: state.batchPreview.preview_fingerprint || state.batchPreview.fingerprint,
      express_company: state.batchBulkCompany,
      profile_id: state.batchProfileId,
    };
    const epoch = pageEpoch;
    try {
      const data = await withButtonBusy(button, "等待核对…", async () => {
        if (!await confirmShippingBatch(summary) || epoch !== pageEpoch) return null;
        button.textContent = "正在创建任务…";
        return api("/api/admin/shipping-batches", { method: "POST", body: JSON.stringify(payload) });
      });
      if (!data) return;
      state.batchPreview = null;
      state.activeShippingBatch = data;
      sessionStorage.setItem(shippingBatchStorageKey(), String(data.batch.id));
      toast("电子面单任务已创建。即使关闭页面，后台也会继续处理。");
      updateBatchPreviewUi(); updateShippingBatchUi(); scheduleShippingBatchPoll();
    } catch (error) {
      errorToast(error, "电子面单任务创建失败。");
    }
  });
  dom.getElementById("retryShippingBatch")?.addEventListener("click", async (event) => {
    const batchId = state.activeShippingBatch?.batch?.id;
    if (!batchId) return;
    if (!confirm("确认只重新提交本批次中的失败订单？已经成功的订单不会重复下单。")) return;
    try {
      state.activeShippingBatch = await withButtonBusy(event.currentTarget, "重新排队中…", () =>
        api(`/api/admin/shipping-batches/${batchId}/retry`, { method: "POST", body: JSON.stringify({}) })
      );
      await loadTaskAlerts();
      toast("失败订单已重新加入队列，页面会持续显示处理结果。");
      updateBatchPreviewUi(); updateShippingBatchUi(); scheduleShippingBatchPoll();
    } catch (error) {
      errorToast(error, "失败订单重新提交失败。");
    }
  });
  dom.getElementById("showBatchFailures")?.addEventListener("click", async () => {
    state.batchProgressFailedOnly = !state.batchProgressFailedOnly; state.batchProgressPage = 1;
    await loadActiveShippingBatch(); updateShippingBatchUi();
  });
  dom.querySelectorAll("[data-batch-progress-page]").forEach(node => node.addEventListener("click", async () => {
    state.batchProgressPage = Number(node.dataset.batchProgressPage);
    await loadActiveShippingBatch(); updateShippingBatchUi();
  }));
  dom.getElementById("closeShippingBatch")?.addEventListener("click", () => {
    state.activeShippingBatch = null;
    sessionStorage.removeItem(shippingBatchStorageKey());
    updateBatchPreviewUi(); updateShippingBatchUi(); scheduleShippingBatchPoll();
  });
  dom.querySelectorAll("[data-admin-preset]").forEach((node) => {
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
  dom.getElementById("applyFilters")?.addEventListener("click", () => {
    clearShipmentSelections();
    state.adminFilters = {
      ...state.adminFilters,
      store_id: dom.getElementById("filterStore").value,
      status: dom.getElementById("filterStatus").value,
      date_from: dom.getElementById("filterFrom").value,
      date_to: dom.getElementById("filterTo").value,
      q: dom.getElementById("filterQ").value.trim(),
    };
    state.adminShipmentPage = 1;
    render();
  });
  dom.getElementById("resetFilters")?.addEventListener("click", () => {
    clearShipmentSelections();
    state.adminFilters = { store_id: "", status: "", date_from: "", date_to: "", q: "" };
    state.adminShipmentPage = 1;
    render();
  });
  dom.getElementById("syncTracking")?.addEventListener("click", async (event) => {
    try {
      const data = await withButtonBusy(event.currentTarget, "同步中…", () =>
        api("/api/admin/tracking/sync", {
          method: "POST",
          body: JSON.stringify({ force: true, limit: 0 }),
        })
      );
      acceptTrackingTask(data);
    } catch (error) {
      errorToast(error, "物流同步失败。");
    }
  });
  dom.querySelectorAll("[data-edit-shipping]").forEach((node) => {
    node.addEventListener("click", (event) => {
      state.editingShipmentShippingId = Number(event.currentTarget.dataset.editShipping);
      render({ refreshData: false });
    });
  });
  dom.querySelectorAll("[data-cancel-shipping]").forEach((node) => {
    node.addEventListener("click", () => {
      clearAdminRowDraft(node.closest("tr[data-shipment]")?.dataset.shipment);
      state.editingShipmentShippingId = null;
      render({ refreshData: false });
    });
  });
  dom.querySelectorAll("[data-refresh-tracking]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.refreshTracking;
      try {
        const data = await withButtonBusy(event.currentTarget, "正在排队…", () =>
          api(`/api/shipments/${id}/tracking/refresh`, { method: "POST", body: JSON.stringify({}) })
        );
        acceptTrackingTask(data);
      } catch (error) {
        errorToast(error);
      }
    });
  });
  dom.querySelectorAll("[data-cancel-label]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.cancelLabel;
      if (!confirm("确认取消并回收这张电子面单？快递100与快递公司确认成功后，订单会恢复为待处理，并可重新下单生成新面单。")) return;
      try {
        await withButtonBusy(event.currentTarget, "取消中…", () =>
          api(`/api/shipments/${id}/label/cancel`, {
            method: "POST",
            body: JSON.stringify({ reason: "订单信息需要修改" }),
          })
        );
        toast("面单已由快递100确认取消，可重新加入批量打单。");
        render();
      } catch (error) {
        errorToast(error);
      }
    });
  });
  dom.querySelectorAll("[data-reprint-label]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.reprintLabel;
      try {
        await api(`/api/shipments/${id}/label/reprint`, { method: "POST", body: JSON.stringify({}) });
        toast("复打任务已发送到快递100云打印机。");
        render();
      } catch (error) {
        errorToast(error);
      }
    });
  });
  dom.querySelectorAll("[data-label-printed]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.labelPrinted;
      try {
        await api(`/api/shipments/${id}/label/printed`, { method: "POST", body: JSON.stringify({}) });
        toast("面单已标记为打印成功。");
        render();
      } catch (error) {
        errorToast(error);
      }
    });
  });
  dom.querySelectorAll("[data-tracking]").forEach((node) => {
    node.addEventListener("input", (event) => {
      const row = event.currentTarget.closest("[data-shipment]");
      const button = row?.querySelector("[data-copy-tracking]");
      if (button) button.style.display = event.currentTarget.value.trim() ? "" : "none";
    });
  });
  dom.querySelectorAll("[data-save-shipment]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.saveShipment;
      const row = dom.querySelector(`[data-shipment="${id}"]`);
      const payload = {
        status: row.querySelector("[data-status]").value,
        express_company: row.querySelector("[data-company]").value,
        tracking_no: row.querySelector("[data-tracking]").value,
        shipping_note: row.querySelector("[data-note]").value,
      };
      try {
        const data = await withButtonBusy(event.currentTarget, "保存中…", () =>
          api(`/api/shipments/${id}`, { method: "PATCH", body: JSON.stringify(payload) })
        );
        if (data.task) acceptTrackingTask(data);
        clearAdminRowDraft(id);
        state.editingShipmentShippingId = null;
        toast(data.message || (data.task || data.tracking_queued ? "已保存，物流信息将由后台更新，可继续操作。" : "已保存。"));
        render();
      } catch (error) {
        errorToast(error);
      }
    });
  });
  dom.querySelectorAll("[data-edit-admin-remark]").forEach((node) => {
    node.addEventListener("click", (event) => {
      state.editingShipmentRemarkId = Number(event.currentTarget.dataset.editAdminRemark);
      render({ refreshData: false });
    });
  });
  dom.querySelectorAll("[data-cancel-admin-remark]").forEach((node) => {
    node.addEventListener("click", () => {
      clearAdminRowDraft(node.closest("tr[data-shipment]")?.dataset.shipment);
      state.editingShipmentRemarkId = null;
      render({ refreshData: false });
    });
  });
  dom.querySelectorAll("[data-save-admin-remark]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.saveAdminRemark;
      const remark = event.currentTarget.closest(".admin-order-cell")?.querySelector("[data-admin-remark]")?.value || "";
      try {
        await withButtonBusy(event.currentTarget, "保存中…", () =>
          api(`/api/shipments/${id}/remark`, {
            method: "PATCH",
            body: JSON.stringify({ remark }),
          })
        );
        clearAdminRowDraft(id);
        state.editingShipmentRemarkId = null;
        toast("订单备注已更新。");
        render();
      } catch (error) {
        errorToast(error);
      }
    });
  });
  dom.querySelectorAll("[data-delete-shipment]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      const id = event.currentTarget.dataset.deleteShipment;
      const orderNo = event.currentTarget.dataset.orderNo || id;
      if (!confirm(`确认删除未发货订单 ${orderNo}？商品明细和整张订单都会删除，且无法恢复。`)) return;
      try {
        await withButtonBusy(event.currentTarget, "删除中…", () =>
          api(`/api/shipments/${id}`, { method: "DELETE" })
        );
        state.editingShipmentRemarkId = null;
        toast("未发货订单已删除。");
        render();
      } catch (error) {
        errorToast(error);
      }
    });
  });
  bindShipmentItemEditor(state.shipments, root);
}

async function renderStores() {
  const currentView = beginView();
  await loadStores(true);
  const content = `
    ${pageHead("门店与团队", "管理实体门店与合作团队。合作账号只能查看本团队寄送记录。")}
    <div class="grid-2">
      <section class="panel panel-pad">
        <div class="section-title"><h2>新增门店 / 合作团队</h2></div>
        <form id="storeForm" class="form-grid">
          <label class="field full">归属类型<select class="select" name="kind"><option value="store">实体门店</option><option value="team">合作团队</option></select></label>
          <div class="field full">
            <label for="storeName">门店或团队名称</label>
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
        <div class="section-title"><h2>实体门店数量</h2><span class="count-pill">${state.stores.filter(row => row.kind !== "team").length}</span></div>
        <p>合作团队：${state.stores.filter(row => row.kind === "team").length} 个（不计入实体门店）</p>
        <p class="muted">停用门店或团队会同步停用对应账号。</p>
      </section>
    </div>
    <section class="panel panel-pad" style="margin-top: 16px;">
      ${renderStoresTable()}
    </section>
  `;
  currentView();
  document.getElementById("app").innerHTML = shell(content);
  bindCommon();
  bindStores();
}

function renderFulfillmentSettings(settings) {
  const saved = settings.fulfillment_profiles || [];
  return `<section class="panel panel-pad fulfillment-settings"><div class="section-title"><div><h2>发货方案</h2><p class="muted">地址可随时编辑，仅影响新提交的批次。已排队、失败重试和已生成面单保留原信息。门店归属不变。</p></div></div>
    <div class="notice">先保存原版纳方案，再配置昆明中台并设为默认。菜鸟账号中的地址不会自动覆盖这里；修改地址后请同时核对网点承接范围。当前仅下载 PDF 后本地打印。</div>
    <div class="fulfillment-profile-grid">${[["banna", "版纳门店发货", "圆通"], ["kunming", "昆明中台发货", "中通"]].map(([id, name, company]) => {
      const existing = saved.find(p => p.id === id);
      const carrier = settings.carrier_settings?.[company] || {};
      const p = existing || {id, name, express_company: company,
        sender_name: id === "banna" ? settings.sender_name : "", sender_mobile: id === "banna" ? settings.sender_mobile : "",
        sender_address: id === "banna" ? settings.sender_address : "", sender_company: settings.sender_company || "",
        tbNet: carrier.tbNet || "", exp_type: carrier.expType || "标准快递", pay_type: settings.pay_type || "MONTHLY",
        third_template_url: carrier.thirdTemplateURL || "", third_custom_template_url: carrier.thirdCustomTemplateUrl || ""};
      const branches = (settings.branch_options || []).filter(b => b.company === company);
      return `<form class="fulfillment-profile-form" data-profile-form="${id}"><h3>${escapeHtml(name)} · ${company}${settings.default_profile_id === id ? "（默认）" : ""}</h3>
        <p class="muted mini" data-profile-saved>${existing ? "已保存" : "尚未启用；保存不会创建面单"}</p><div class="form-grid">
        <div class="field full"><label>方案名称<input class="input" name="name" maxlength="100" required value="${escapeHtml(p.name)}"></label></div>
        <div class="field"><label>寄件联系人<input class="input" name="sender_name" maxlength="100" required value="${escapeHtml(p.sender_name || "")}"></label></div>
        <div class="field"><label>联系电话<input class="input" name="sender_mobile" type="tel" maxlength="100" required value="${escapeHtml(p.sender_mobile || "")}"></label></div>
        <div class="field full"><label>寄件公司<input class="input" name="sender_company" maxlength="100" value="${escapeHtml(p.sender_company || "")}"></label></div>
        <div class="field full"><label>完整寄件地址<textarea class="textarea" name="sender_address" maxlength="500" required>${escapeHtml(p.sender_address || "")}</textarea></label></div>
        <div class="field full"><label>菜鸟授权网点<select class="select" name="tbNet" required><option value="">请先刷新授权网点</option>${p.tbNet && !branches.some(b => b.tbNet === p.tbNet) ? `<option value="${escapeHtml(p.tbNet)}" selected>${escapeHtml(p.tbNet)}（需重新核验）</option>` : ""}${branches.map(b => `<option value="${escapeHtml(b.tbNet)}" ${b.tbNet === p.tbNet ? "selected" : ""}>${escapeHtml(b.branchName || b.tbNet)} · ${escapeHtml(b.branchCode || "")} · 余 ${Number(b.quantity) || 0}</option>`).join("")}</select></label></div>
        <div class="field"><label>产品类型<input class="input" name="exp_type" maxlength="100" required value="${escapeHtml(p.exp_type)}"></label></div>
        <div class="field"><label>付款方式<select class="select" name="pay_type"><option value="MONTHLY" ${p.pay_type === "MONTHLY" ? "selected" : ""}>月结</option><option value="SHIPPER" ${p.pay_type === "SHIPPER" ? "selected" : ""}>寄方付</option></select></label></div>
        <div class="field full"><label>菜鸟基础模板（可留空使用平台默认）<input class="input" name="third_template_url" type="url" maxlength="500" value="${escapeHtml(p.third_template_url)}"></label></div>
        <div class="field full"><label>货品自定义区模板（选填）<input class="input" name="third_custom_template_url" type="url" maxlength="500" value="${escapeHtml(p.third_custom_template_url)}"></label></div>
        </div><p class="muted mini">中通不能沿用圆通专用模板。默认模板的商品明细展示需在正式启用前核对。</p>
        <label class="check-row"><input type="checkbox" name="make_default" ${settings.default_profile_id === id ? "checked" : ""}>设为新批次默认方案</label>
        <div class="inline-actions"><button class="btn primary" type="submit">保存${id === "kunming" ? "中台" : "版纳"}方案</button></div><p class="profile-save-result" role="status" aria-live="polite"></p>
      </form>`;
    }).join("")}</div></section>`;
}

function bindFulfillmentSettings() {
  document.querySelectorAll("[data-profile-form]").forEach(form => form.addEventListener("submit", async event => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const result = form.querySelector(".profile-save-result");
    const values = new FormData(form), id = form.dataset.profileForm;
    const existing = state.shippingSettings.fulfillment_profiles?.find(p => p.id === id);
    const payload = {...Object.fromEntries(values.entries()), id, revision: existing?.revision || 0, make_default: values.has("make_default")};
    try {
      const response = await withButtonBusy(button, "保存中…", () => api("/api/admin/fulfillment-profiles", {method: "PUT", body: JSON.stringify(payload)}));
      state.shippingSettings = response.settings;
      document.querySelectorAll("[data-profile-form]").forEach(other => {
        other.querySelector('[name="make_default"]').checked = other.dataset.profileForm === response.settings.default_profile_id;
        const saved = response.settings.fulfillment_profiles.find(p => p.id === other.dataset.profileForm);
        if (saved) {
          other.querySelector("h3").textContent = `${saved.name} · ${saved.express_company}${saved.id === response.settings.default_profile_id ? "（默认）" : ""}`;
          other.querySelector("[data-profile-saved]").textContent = "已保存";
        }
      });
      result.textContent = "已保存。仅新提交的批次使用本次设置；原面单和已排队任务未改变。";
      // Do not rerender the other form: it may contain unsaved changes.
      toast("发货方案已保存；重新打开批量打单可使用。");
    } catch (error) { result.textContent = `保存失败：${error.message || "请稍后重试"}。填写内容已保留。`; errorToast(error); }
  }));
}

async function renderShippingSettings() {
  const currentView = beginView();
  await loadShippingSettings();
  const settings = state.shippingSettings || {};
  const config = state.shippingConfig || {};
  const carrierSettings = settings.carrier_settings || {};
  const branchOptions = settings.branch_options || [];
  const defaultTemplateUrls = {
    "圆通": "https://cloudprint.cainiao.com/template/standard/850338",
  };
  const defaultCustomTemplateUrls = {
    "圆通": "https://cloudprint.cainiao.com/template/customArea/77205369",
  };
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
    ${renderFulfillmentSettings(settings)}
    <form id="shippingSettingsForm">
    <div class="grid-2 shipping-settings-grid">
      <section class="panel panel-pad">
        <div class="section-title"><h2>原总部寄件配置</h2></div><p class="muted mini">启用发货方案后，批量打单使用上方方案地址。此处保留原配置及其他承运商设置，不覆盖已保存方案。</p>
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
            <div class="carrier-template-inputs">
              <input class="input" data-carrier-template="${company}" value="${escapeHtml(carrierSettings[company]?.thirdTemplateURL || defaultTemplateUrls[company] || "")}" placeholder="菜鸟一联单模板 URL" aria-label="${company}菜鸟一联单模板 URL" />
              <input class="input" data-carrier-custom-template="${company}" value="${escapeHtml(carrierSettings[company]?.thirdCustomTemplateUrl || defaultCustomTemplateUrls[company] || "")}" placeholder="菜鸟货物自定义区模板 URL" aria-label="${company}菜鸟货物自定义区模板 URL" />
            </div>
          </div>
        `).join("")}
      </div>
    </section>
    <section class="panel panel-pad label-print-settings">
      <div class="section-title"><div><h2>面单打印</h2><div class="muted mini">圆通已配置菜鸟一联单模板；菜鸟授权返回 PDF，快递100云打印需要云打印设备码。</div></div></div>
      <div class="form-grid">
        <div class="field"><label for="printMode">打印方式</label><select class="select" id="printMode" name="print_mode"><option value="PDF" ${settings.print_mode !== "CLOUD" ? "selected" : ""}>菜鸟 PDF 面单</option><option value="CLOUD" ${settings.print_mode === "CLOUD" ? "selected" : ""}>快递100云打印</option></select></div>
        <div class="field"><label for="printerSiid">云打印设备码 siid</label><input class="input" id="printerSiid" name="printer_siid" value="${escapeHtml(settings.printer_siid || "")}" /></div>
        <div class="field"><label>云打印纸张尺寸（毫米）</label><div class="tracking-input-row"><input class="input" name="paper_width" value="${escapeHtml(settings.paper_width || "100")}" aria-label="纸张宽度" /><input class="input" name="paper_height" value="${escapeHtml(settings.paper_height || "180")}" aria-label="纸张高度" /></div></div>
        <label class="check-row"><input type="checkbox" name="need_desensitization" ${settings.need_desensitization ? "checked" : ""} /> 电话号码脱敏</label>
        <label class="check-row"><input type="checkbox" name="need_logo" ${settings.need_logo ? "checked" : ""} /> 面单显示 Logo</label>
      </div>
      <div class="inline-actions settings-save-actions"><button class="btn primary" type="submit">保存电子面单设置</button></div>
    </section>
    </form>
  `;
  currentView();
  document.getElementById("app").innerHTML = shell(content);
  bindCommon();
  bindFulfillmentSettings();
  document.getElementById("shippingSettingsForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const carrier_settings = {};
    EXPRESS_COMPANIES.forEach((company) => {
      carrier_settings[company] = {
        tbNet: document.querySelector(`[data-carrier-branch="${company}"]`)?.value || "",
        expType: document.querySelector(`[data-carrier-exp="${company}"]`)?.value.trim() || (company === "顺丰" ? "顺丰标快" : "标准快递"),
        thirdTemplateURL: document.querySelector(`[data-carrier-template="${company}"]`)?.value.trim() || "",
        thirdCustomTemplateUrl: document.querySelector(`[data-carrier-custom-template="${company}"]`)?.value.trim() || "",
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
      errorToast(error);
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
      errorToast(error);
    }
  });
  document.getElementById("refreshLabelBranches")?.addEventListener("click", async () => {
    try {
      const data = await api("/api/admin/label-branches/refresh", { method: "POST", body: JSON.stringify({}) });
      state.shippingSettings = data.settings;
      toast("授权网点和面单余额已刷新。");
      render();
    } catch (error) {
      errorToast(error);
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
                  <td><strong>${escapeHtml(store.name)}</strong><br><span class="shipment-type">${store.kind === "team" ? "合作团队" : "实体门店"}</span></td>
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
          kind: form.get("kind"),
        }),
      });
      toast("门店 / 团队及其账号已创建。");
      render();
    } catch (error) {
      errorToast(error);
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
        errorToast(error);
      }
    });
  });
}

async function renderProducts({ refreshData = true } = {}) {
  const currentView = beginView();
  if (refreshData) await loadProductsAll();
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
  currentView();
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
      render({ refreshData: false });
    } catch (error) {
      errorToast(error);
    }
  });
  document.getElementById("applyProductFilters").addEventListener("click", () => {
    state.productFilters = {
      category: document.getElementById("productCategory").value,
      q: document.getElementById("productSearch").value.trim(),
    };
    render({ refreshData: false });
  });
  document.getElementById("resetProductFilters").addEventListener("click", () => {
    state.productFilters = { category: "", q: "" };
    render({ refreshData: false });
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
      render({ refreshData: false });
    } catch (error) {
      errorToast(error);
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
        render({ refreshData: false });
      } catch (error) {
        errorToast(error);
      }
    });
  });
}

function bindCommon() {
  bindSubmissionRecovery();
  restoreTrackingTasks(); updateTrackingTaskUi(); scheduleTrackingPoll();
  bindSpecialControls();
  bindTrackingCopyButtons();
  bindTrackingDetails();
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
      invalidatePage({ clearIdentity: true });
      await api("/api/logout", { method: "POST" }).catch(() => null);
      state.user = null;
      navigate("/login");
    });
  }
}

async function render({ refreshData = true } = {}) {
  const renderId = ++renderSequence;
  const route = `${location.pathname}${location.search}`;
  if (route !== currentRoute) { invalidatePage(); currentRoute = route; }
  const path = location.pathname;
  const epoch = pageEpoch;
  if (path !== "/admin") {
    stopTaskAlertPoll();
    state.taskAlertsOpen = false;
    state.adminBoardLoaded = false;
    document.body.classList.remove("task-alert-dialog-open");
  }
  if (path !== "/shipments") state.storeBoardLoaded = false;
  const showsLoading = refreshData && path !== "/login";
  if (showsLoading) {
    activeDataLoads += 1;
    document.documentElement.classList.add("app-loading");
  }
  if (!state.user && path !== "/login") {
    await loadMe();
  }
  if (epoch !== pageEpoch) {
    if (showsLoading) { activeDataLoads = Math.max(0, activeDataLoads - 1); if (!activeDataLoads) document.documentElement.classList.remove("app-loading"); }
    return;
  }
  if (!state.user && path !== "/login") {
    history.replaceState({}, "", "/login");
    renderLogin();
    if (showsLoading) {
      activeDataLoads = Math.max(0, activeDataLoads - 1);
      if (!activeDataLoads) document.documentElement.classList.remove("app-loading");
    }
    return;
  }
  if (state.user && (path === "/" || path === "/login")) {
    history.replaceState({}, "", state.user.role === "admin" ? "/admin" : state.user.store_kind === "team" ? "/special/new" : "/submit");
  }

  try {
    if (location.pathname === "/login") {
      renderLogin();
    } else if (location.pathname === "/reports/fulfillment") {
      await renderFulfillmentReport();
    } else if (location.pathname === "/special/new" || (location.pathname === "/submit" && state.user.store_kind === "team")) {
      await renderSpecialShipment();
    } else if (location.pathname === "/submit") {
      await renderSubmit();
    } else if (location.pathname === "/shipments" && state.user.role === "staff") {
      await renderStoreBoard({ refreshData });
    } else if (location.pathname === "/returns/new" && state.user.role === "staff" && state.user.store_kind !== "team") {
      await renderReturnNew();
    } else if (location.pathname === "/returns" && state.user.role === "staff" && state.user.store_kind !== "team") {
      await renderReturnBoard(false);
    } else if (location.pathname === "/admin" && state.user.role === "admin") {
      await renderAdmin({ refreshData });
    } else if (location.pathname === "/admin/returns" && state.user.role === "admin") {
      await renderReturnBoard(true);
    } else if (location.pathname === "/admin/stores" && state.user.role === "admin") {
      await renderStores();
    } else if (location.pathname === "/admin/products" && state.user.role === "admin") {
      await renderProducts({ refreshData });
    } else if (location.pathname === "/admin/shipping" && state.user.role === "admin") {
      await renderShippingSettings();
    } else {
      navigate(state.user.role === "admin" ? "/admin" : state.user.store_kind === "team" ? "/special/new" : "/submit");
    }
  } catch (error) {
    if (error instanceof StaleViewError || renderId !== renderSequence || epoch !== pageEpoch) return;
    document.getElementById("app").innerHTML = shell(`<section class="panel panel-pad"><div class="empty">${escapeHtml(error.message)}</div></section>`);
    bindCommon();
  } finally {
    if (showsLoading) {
      activeDataLoads = Math.max(0, activeDataLoads - 1);
      if (!activeDataLoads) document.documentElement.classList.remove("app-loading");
    }
  }
}

window.addEventListener("popstate", render);
document.addEventListener("input", event => {
  const row = event.target.closest?.("tr[data-shipment]");
  if (row) row.dataset.dirty = "1";
});
document.addEventListener("change", event => {
  const row = event.target.closest?.("tr[data-shipment]");
  if (row) row.dataset.dirty = "1";
});
document.addEventListener("visibilitychange", async () => {
  if (document.visibilityState === "hidden") {
    stopTaskAlertPoll(); clearTimeout(state.shippingBatchPollTimer); clearTimeout(state.trackingPollTimer);
    state.shippingBatchPollTimer = state.trackingPollTimer = null;
    return;
  }
  const epoch = pageEpoch;
  if (location.pathname === "/admin" && state.user?.role === "admin") {
    await Promise.all([loadTaskAlerts(), loadActiveShippingBatch()]);
    if (epoch !== pageEpoch) return;
    updateTaskAlertUi(); updateShippingBatchUi(); scheduleTaskAlertPoll(); scheduleShippingBatchPoll();
  }
  scheduleTrackingPoll(true);
});
window.addEventListener("pagehide", () => invalidatePage());
render();
