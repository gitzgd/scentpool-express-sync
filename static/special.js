/* Business context only. Booking, printing and tracking remain in app.js. */
const SHIPMENT_TYPES = {
  legacy: ["历史未分类", "历史", "legacy"], standard: ["门店订单", "普通", "ordinary"],
  resend: ["售后补发", "补发", "aftersales"], exchange: ["换货寄出", "换货", "aftersales"],
  influencer: ["博主合作", "博主", "cooperation"], sample: ["合作样品", "样品", "cooperation"],
};
const SHIPMENT_GROUPS = {"": "全部", ordinary: "普通发货", aftersales: "售后发货", cooperation: "合作寄送", legacy: "历史未分类"};
let specialDraft = null;
let specialDraftContext = "";

function shipmentTypeBadge(row) {
  const [label, , group] = SHIPMENT_TYPES[row.shipment_type] || SHIPMENT_TYPES.legacy;
  return `<span class="shipment-type type-${group}">${escapeHtml(label)}</span>`;
}

function shipmentContext(row, includeActions = true) {
  const special = ["resend", "exchange", "influencer", "sample"].includes(row.shipment_type);
  const internal = [row.cooperation_subject, row.internal_note].filter(Boolean).join(" · ");
  return `<div class="shipment-context">
    <div class="shipment-context-head">${shipmentTypeBadge(row)}<span class="mini">${escapeHtml(row.store_name_snapshot || "")} · ${escapeHtml(row.status || "")}</span></div>
    ${internal ? `<details class="purpose-details"><summary>${escapeHtml(internal.slice(0, 45))}${internal.length > 45 ? "… 展开" : ""}</summary><p>${escapeHtml(internal)}</p><span class="mini muted">内部说明，不打印在面单上</span></details>` : ""}
    ${row.original_shipment_id ? `<button class="text-link" data-shipment-reference="${row.original_shipment_id}" type="button">原单：${escapeHtml(row.original_business_id || row.original_shipment_id)}</button>` : ""}
    ${row.related_return_id ? `<button class="text-link" data-return-reference="${row.related_return_id}" type="button">关联退货 #${row.related_return_id}</button>` : ""}
    ${row.return_unsigned_warning ? `<div class="notice return-warning">退货尚未签收 · 总部核对后仍可发货</div>` : ""}
    ${row.aftersales_count ? `<button class="text-link" data-aftersales-for="${row.id}" type="button">查看关联售后（${row.aftersales_count}）</button>` : ""}
    ${includeActions && row.store_kind !== "team" ? `<a class="btn secondary small" href="/special/new?source_shipment=${row.id}" data-route>发起售后发货</a>` : ""}
    ${includeActions && special && row.status === "待处理" && bookingEditable(row) ? `<button class="text-link" data-edit-context="${row.id}" type="button">修改内部说明</button>` : ""}
  </div>`;
}

function classificationFilters(scope) {
  const filters = scope === "admin" ? state.adminFilters : state.storeFilters;
  const allowedGroup = key => state.user.role === "admin" || (state.user.store_kind === "team" ? ["", "cooperation"].includes(key) : key !== "cooperation");
  const groups = Object.entries(SHIPMENT_GROUPS).filter(([key]) => allowedGroup(key));
  return `<div class="classification-bar" aria-label="发货分类">
    <div class="classification-tabs">${groups.map(([key, text]) => `<button class="btn secondary small ${key === (filters.shipment_group || "") ? "active" : ""}" type="button" data-shipment-group="${key}" data-scope="${scope}" aria-pressed="${key === (filters.shipment_group || "")}">${text}</button>`).join("")}</div>
    <label>具体类型 <select class="select" data-shipment-subtype data-scope="${scope}"><option value="">全部类型</option>${Object.entries(SHIPMENT_TYPES).filter(([, value]) => (!filters.shipment_group || value[2] === filters.shipment_group) && allowedGroup(value[2])).map(([key, value]) => `<option value="${key}" ${key === filters.shipment_type ? "selected" : ""}>${value[0]}</option>`).join("")}</select></label>
    ${filters.id || filters.original_shipment_id || filters.related_return_id ? `<span class="notice">正在查看关联记录，点击“清空”恢复完整列表</span>` : ""}
  </div>`;
}

function clearShipmentSelections() {
  state.batchPreview = null; state.batchSelectedIds = []; state.batchPrintSelectedIds = [];
  state.batchPrintOpen = false; state.batchPrintError = "";
}

function bindSpecialControls() {
  document.querySelectorAll("[data-shipment-group], [data-shipment-subtype]").forEach(node => node.addEventListener(node.hasAttribute("data-shipment-group") ? "click" : "change", () => {
    const scope = node.dataset.scope;
    const filters = scope === "admin" ? state.adminFilters : state.storeFilters;
    if (node.hasAttribute("data-shipment-group")) { filters.shipment_group = node.dataset.shipmentGroup; filters.shipment_type = ""; }
    else filters.shipment_type = node.value;
    delete filters.id; delete filters.original_shipment_id; delete filters.related_return_id;
    clearShipmentSelections(); state.adminShipmentPage = 1; state.storeShipmentPage = 1; render();
  }));
  document.querySelectorAll("[data-shipment-reference], [data-aftersales-for], [data-return-aftersales]").forEach(node => node.addEventListener("click", () => {
    const key = node.hasAttribute("data-shipment-reference") ? "id" : node.hasAttribute("data-aftersales-for") ? "original_shipment_id" : "related_return_id";
    const value = node.dataset.shipmentReference || node.dataset.aftersalesFor || node.dataset.returnAftersales;
    const filters = { status: "", date_from: "", date_to: "", q: "", [key]: value };
    if (state.user.role === "admin") state.adminFilters = filters; else state.storeFilters = filters;
    clearShipmentSelections(); state.adminShipmentPage = 1; state.storeShipmentPage = 1;
    navigate(state.user.role === "admin" ? "/admin" : "/shipments");
  }));
  document.querySelectorAll("[data-return-reference]").forEach(node => node.addEventListener("click", () => {
    const filters = { id: node.dataset.returnReference, status: "", q: "", date_from: "", date_to: "" };
    if (state.user.role === "admin") state.adminReturnFilters = filters; else state.storeReturnFilters = filters;
    navigate(state.user.role === "admin" ? "/admin/returns" : "/returns");
  }));
  document.querySelectorAll("[data-edit-context]").forEach(node => node.addEventListener("click", () => {
    const row = [...(state.shipments || []), ...(state.storeShipments || [])].find(item => item.id === Number(node.dataset.editContext));
    if (!row) return;
    const dialog = document.createElement("dialog"); dialog.className = "special-context-dialog";
    dialog.innerHTML = `<form class="form-grid"><h2>修改内部说明</h2><p>不改变发货类型、归属或面单包装要求。</p>${["influencer", "sample"].includes(row.shipment_type) ? `<label class="field full">合作对象或项目<input class="input" name="cooperation_subject" required maxlength="1000" value="${escapeHtml(row.cooperation_subject)}"></label>` : ""}<label class="field full">内部原因 / 用途<textarea class="textarea" name="internal_note" required maxlength="1000">${escapeHtml(row.internal_note)}</textarea></label><p class="form-error" role="alert"></p><div class="actions"><button class="btn primary">保存</button><button class="btn secondary" type="button" data-close-context>取消</button></div></form>`;
    document.body.append(dialog); dialog.showModal();
    dialog.querySelector("[data-close-context]").onclick = () => dialog.close(); dialog.onclose = () => dialog.remove();
    dialog.querySelector("form").onsubmit = async event => {
      event.preventDefault(); const button = event.submitter;
      try { await withButtonBusy(button, "保存中…", () => api(`/api/shipments/${row.id}/context`, {method: "PATCH", body: JSON.stringify(Object.fromEntries(new FormData(event.target)))})); dialog.close(); toast("内部说明已保存。"); render(); }
      catch (error) { dialog.querySelector(".form-error").textContent = error.message; }
    };
  }));
}

function specialField(name, label, {required = true, area = false, limit = 1000, type = "text"} = {}) {
  const attributes = `class="${area ? "textarea" : "input"}" name="${name}" id="special-${name}" ${required ? "required" : ""} maxlength="${limit}"`;
  return `<label class="field ${area ? "full" : ""}" for="special-${name}">${label}${area ? `<textarea ${attributes}>${escapeHtml(specialDraft[name] || "")}</textarea>` : `<input ${attributes} type="${type}" value="${escapeHtml(specialDraft[name] || "")}">`}</label>`;
}

async function renderSpecialShipment() {
  await Promise.all([ensureProductsGrouped(), loadStores()]);
  const context = `${state.user.id}:${location.search}`;
  if (specialDraftContext !== context || !specialDraft) {
    specialDraftContext = context;
    // Only a random request key is persisted, never recipient information.
    const key = `scentpool_special_request:${context}`;
    const requestKey = sessionStorage.getItem(key) || crypto.randomUUID(); sessionStorage.setItem(key, requestKey);
    specialDraft = { shipment_type: state.user.store_kind === "team" ? "influencer" : "resend", store_id: state.user.store_id || "", items: [], submission_key: requestKey };
    const query = new URLSearchParams(location.search);
    for (const [param, apiPath, listKey] of [["source_shipment", "shipments", "shipments"], ["source_return", "returns", "returns"]]) {
      if (!query.has(param)) continue;
      const value = query.get(param);
      if (!/^[1-9][0-9]*$/.test(value)) throw new Error("关联记录编号无效。");
      const data = await api(`/api/${apiPath}?id=${value}`); const row = data[listKey]?.[0];
      if (!row) throw new Error("关联记录不存在或没有访问权限。");
      if (row.store_kind === "team") throw new Error("合作寄送不能作为门店售后原单。");
      specialDraft.store_id = row.store_id;
      if (param === "source_shipment") Object.assign(specialDraft, { original_shipment_id: row.id, recipient_name: row.recipient_name, phone: row.phone, address: row.address });
      else Object.assign(specialDraft, { related_return_id: row.id, phone: specialDraft.phone || row.sender_phone, shipment_type: "exchange" });
      specialDraft.prefilled = true;
    }
  }
  const cooperation = ["influencer", "sample"].includes(specialDraft.shipment_type);
  const types = state.user.role === "admin" ? ["resend", "exchange", "influencer", "sample"] : state.user.store_kind === "team" ? ["influencer", "sample"] : ["resend", "exchange"];
  const stores = state.stores.filter(row => row.kind === (cooperation ? "team" : "store"));
  const itemRows = specialDraft.items.map((item, index) => `<div class="special-item" data-special-item="${index}"><strong>${item.item_kind === "material" ? "临时物料" : "目录商品"} ${index + 1}</strong>${item.item_kind === "material" ? `<label>名称<input class="input" data-item-field="name" value="${escapeHtml(item.name || "")}" maxlength="100" required></label><label>规格<input class="input" data-item-field="material_spec" value="${escapeHtml(item.material_spec || "")}" maxlength="100" required></label>` : `<label>分类<select class="select" data-item-field="category" required>${categoryOptions(item.category || "")}</select></label><label>商品<select class="select" data-item-field="barcode" required>${productOptions(item.category || "", item.barcode || "")}</select></label>`}<label>数量<input class="input" type="number" min="1" max="999999" step="1" data-item-field="quantity" value="${escapeHtml(item.quantity)}" required></label><button class="btn danger small" data-remove-special="${index}" type="button">删除明细</button></div>`).join("");
  document.getElementById("app").innerHTML = shell(`${pageHead(cooperation ? "合作寄送" : "售后发货", "独立编号 · 总部统一处理 · 与原订单、退货分别保留记录")}
    <form id="specialShipmentForm" class="panel panel-pad special-form"><fieldset><div class="form-grid">
    <label class="field">发货类型<select class="select" name="shipment_type" required>${types.map(key => `<option value="${key}" ${key === specialDraft.shipment_type ? "selected" : ""}>${SHIPMENT_TYPES[key][0]}</option>`).join("")}</select></label>
    ${state.user.role === "admin" ? `<label class="field">归属门店 / 团队<select class="select" name="store_id" required><option value="">请选择</option>${stores.map(row => `<option value="${row.id}" ${String(row.id) === String(specialDraft.store_id) ? "selected" : ""}>${escapeHtml(row.name)}</option>`).join("")}</select></label>${!stores.length ? `<p class="notice full">请先在“门店与团队”创建合作团队及其账号。</p>` : ""}` : `<div class="field"><label>归属</label><strong>${escapeHtml(state.user.store_name)}</strong></div>`}
    ${cooperation ? specialField("cooperation_subject", "合作对象或项目（内部）") : ""}
    ${specialField("internal_note", cooperation ? "寄送用途（内部，不上面单）" : "售后原因（内部，不上面单）", {area: true})}
    ${!cooperation ? `<div class="full form-grid">${specialField("original_shipment_id", "原发货记录 ID（可不填）", {required: false, type: "number"})}${specialField("related_return_id", "退货记录 ID（可不填）", {required: false, type: "number"})}<p class="mini muted full">可从原订单或退货看板发起，自动关联。尚未签收的退货会提醒总部，不阻止提交。</p></div>` : ""}
    <h2 class="full">本次收件信息</h2>${specialDraft.prefilled ? `<p class="notice full">已预填能取得的收件信息，请逐项确认；退货记录通常只有联系电话。寄送明细请重新选择。</p>` : ""}
    ${specialField("recipient_name", "收件人", {limit: 100})}${specialField("phone", "联系电话", {limit: 80, type: "tel"})}${specialField("address", "详细地址", {area: true})}
    <h2 class="full">本次寄送明细</h2><p class="mini muted full">只填写本次要寄出的东西。临时物料不进入正式商品目录。</p><div id="specialItems" class="full">${itemRows || `<p class="empty">请添加目录商品或临时物料。</p>`}</div>
    <div class="actions full"><button class="btn secondary" id="addSpecialProduct" type="button">＋ 目录商品</button><button class="btn secondary" id="addSpecialMaterial" type="button">＋ 临时物料</button></div>
    ${specialField("remark", "面单包装要求（会打印给收件人，请勿填内部信息）", {area: true, required: false, limit: 500})}
    <p id="specialError" class="form-error full" role="alert"></p><div class="actions full"><button class="btn primary" type="submit">提交总部处理</button><button class="btn secondary" id="resetSpecialDraft" type="button">开始另一张新单</button><span class="muted mini">自动生成编号，无需填写门店订单号。</span></div></div></fieldset></form>`);
  bindCommon();
  const form = document.getElementById("specialShipmentForm");
  const capture = () => {
    Object.assign(specialDraft, Object.fromEntries(new FormData(form)));
    form.querySelectorAll("[data-special-item]").forEach(row => row.querySelectorAll("[data-item-field]").forEach(input => {specialDraft.items[Number(row.dataset.specialItem)][input.dataset.itemField] = input.value;}));
  };
  form.addEventListener("input", capture);
  document.getElementById("resetSpecialDraft").onclick = () => {
    if (!confirm("请先在发货看板确认上一次是否已提交，避免重复寄送。确定放弃当前草稿，开始另一张新单？")) return;
    sessionStorage.removeItem(`scentpool_special_request:${context}`); specialDraft = null; renderSpecialShipment();
  };
  form.querySelector('[name="shipment_type"]').onchange = () => {
    capture(); if (["influencer", "sample"].includes(specialDraft.shipment_type) !== cooperation) { specialDraft.store_id = state.user.store_id || ""; specialDraft.original_shipment_id = ""; specialDraft.related_return_id = ""; specialDraft.cooperation_subject = ""; }
    renderSpecialShipment();
  };
  form.querySelectorAll('[data-item-field="category"]').forEach(input => input.onchange = () => { capture(); specialDraft.items[Number(input.closest("[data-special-item]").dataset.specialItem)].barcode = ""; renderSpecialShipment(); });
  ["Product", "Material"].forEach(kind => document.getElementById(`addSpecial${kind}`).onclick = () => {capture(); specialDraft.items.push({item_kind: kind === "Material" ? "material" : "product", quantity: 1}); renderSpecialShipment();});
  form.querySelectorAll("[data-remove-special]").forEach(node => node.onclick = () => {capture(); specialDraft.items.splice(Number(node.dataset.removeSpecial), 1); renderSpecialShipment();});
  form.onsubmit = async event => {
    event.preventDefault(); capture();
    const payload = {...specialDraft, items: specialDraft.items.map(item => item.item_kind === "material" ? {item_kind: "material", name: item.name, material_spec: item.material_spec, quantity: Number(item.quantity)} : {barcode: item.barcode, quantity: Number(item.quantity)})};
    if (!payload.items.length) { document.getElementById("specialError").textContent = "请至少添加一项本次寄送明细。"; return; }
    const fieldset = form.querySelector("fieldset"); fieldset.disabled = true; event.submitter.textContent = "提交中，请勿重复操作…";
    try {
      const data = await api("/api/shipments", {method: "POST", body: JSON.stringify(payload)});
      sessionStorage.removeItem(`scentpool_special_request:${context}`); specialDraft = null;
      toast(`已提交总部：${data.shipment.business_id}`); navigate(state.user.role === "admin" ? "/admin" : "/shipments");
    } catch (error) {document.getElementById("specialError").textContent = error.status && error.status < 500 ? `提交未成功：${error.message} 内容已保留，请确认后重试。` : `未确认提交成功：${error.message} 网络超时时请保留原内容重试，系统不会重复创建同一次提交。`;}
    finally {fieldset.disabled = false; if (event.submitter.isConnected) event.submitter.textContent = "提交总部处理";}
  };
}
