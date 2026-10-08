/* Aggregate-only reporting: no customer data and no writes to orders. */
const fulfillmentReport = {owner: null, filters: null, data: null, error: "", timer: null,
  sequence: 0, running: false, queued: false, view: "daily", dataView: "daily", dailyPage: 1};
const REPORT_CHANNELS = {banna: "版纳", kunming: "昆明"};
let reportBroadcast = null;
try { if (typeof BroadcastChannel !== "undefined") reportBroadcast = new BroadcastChannel("fulfillment-report-refresh"); } catch {}

function reportDates(preset, now = new Date()) {
  const text = new Intl.DateTimeFormat("sv-SE", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"}).format(now);
  const end = new Date(`${text}T00:00:00Z`), start = new Date(end);
  if (preset === "yesterday") { start.setUTCDate(start.getUTCDate()-1); end.setUTCDate(end.getUTCDate()-1); }
  if (preset === "week") start.setUTCDate(start.getUTCDate()-((start.getUTCDay()+6)%7));
  if (preset === "month") start.setUTCDate(1);
  if (preset === "last_month") { start.setUTCDate(1); start.setUTCMonth(start.getUTCMonth()-1); end.setUTCDate(0); }
  return {date_from: start.toISOString().slice(0,10), date_to: end.toISOString().slice(0,10)};
}

function stopFulfillmentReport(clear = false) {
  clearTimeout(fulfillmentReport.timer); fulfillmentReport.timer = null;
  fulfillmentReport.sequence++; fulfillmentReport.queued = false;
  if (clear) { fulfillmentReport.data = null; fulfillmentReport.filters = null; fulfillmentReport.owner = null; }
}

function reportActive() { return location.pathname === "/reports/fulfillment" && !!state.user && !document.hidden; }
function notifyFulfillmentReport(broadcast = true) {
  if (broadcast) reportBroadcast?.postMessage("refresh");
  if (reportActive()) refreshFulfillmentReport();
}
if (reportBroadcast) reportBroadcast.onmessage = event => { if (event.data === "refresh") notifyFulfillmentReport(false); };

function reportParams() {
  return new URLSearchParams({...fulfillmentReport.filters, view: fulfillmentReport.view === "items_daily" ? "daily_products" : "summary"});
}

async function refreshFulfillmentReport() {
  const r = fulfillmentReport;
  clearTimeout(r.timer);
  if (!reportActive()) return;
  if (r.running) { r.queued = true; return; }
  const seq = r.sequence, owner = state.user.id, view = r.view;
  r.running = true; r.queued = false;
  const status = document.getElementById("reportStatus");
  if (status) status.textContent = r.data ? "正在更新，暂显示上次结果…" : "正在读取统计…";
  try {
    const data = await api(`/api/reports/fulfillment-items?${reportParams()}`);
    if (seq !== r.sequence || owner !== state.user?.id || !reportActive()) return;
    r.data = data; r.dataView = view; r.error = ""; r.filters.page = data.pagination.page;
    const select = document.querySelector('#reportFilters [name="store_id"]');
    if (select) {
      const value = r.filters.store_id;
      select.innerHTML = '<option value="">全部门店与团队</option>' + data.stores.map(s => `<option value="${s.id}">${escapeHtml(s.name)}${s.kind === "team" ? " · 合作团队" : ""}</option>`).join("");
      select.value = value;
    }
    updateFulfillmentReport();
  } catch (error) {
    if (seq !== r.sequence || owner !== state.user?.id || error instanceof StaleViewError || !reportActive()) return;
    r.error = `刷新失败：${error.message}。${r.data ? "当前显示上次成功结果，不是最新数据。" : "暂时无法取得数据，不能认定为零。"}`;
    updateFulfillmentReport();
  } finally {
    r.running = false;
    if (reportActive()) {
      if (r.queued) { r.queued = false; refreshFulfillmentReport(); }
      else r.timer = setTimeout(refreshFulfillmentReport,15000);
    }
  }
}

function reportTable(headers, rows) {
  if (!rows.length) return '<p class="empty">当前筛选范围暂无有效发货商品记录。</p>';
  return `<div class="report-table-wrap"><table class="report-table"><thead><tr>${headers.map(h=>`<th>${escapeHtml(h)}</th>`).join("")}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map((cell,i)=>`<td data-label="${escapeHtml(headers[i])}">${escapeHtml(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function updateFulfillmentReport() {
  const r=fulfillmentReport, data=r.data, target=document.getElementById("reportResults");
  if (!target) return;
  const status=document.getElementById("reportStatus");
  status.textContent = r.error || (data ? `最后更新：${new Date(data.generated_at).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai"})} · 页面可见时每 15 秒自动更新` : "等待读取统计");
  status.className = r.error ? "report-status report-error" : "report-status";
  if (!data) { target.innerHTML = '<p class="empty">统计尚未加载成功，请点击刷新重试。</p>'; return; }
  const s=data.summary, q=data.quality;
  const cards=[["有效发货订单",s.orders,"单"],["商品种类",s.product_kinds,"种"],["商品数量",s.product_quantity,"件"],["临时物料",s.material_quantity,"件"],["其中等待揽收",s.waiting_orders,"单"]];
  let content=`<div class="report-cards">${cards.map(([label,value,unit])=>`<div class="report-card"><span>${label}</span><strong>${value}<small>${unit}</small></strong></div>`).join("")}</div>`;
  content+=`<p class="report-scope">当前结果：${escapeHtml(data.filters.date_from)} 至 ${escapeHtml(data.filters.date_to)} · ${escapeHtml(REPORT_CHANNELS[data.filters.channel] || "全部渠道（版纳＋昆明）")} · ${escapeHtml(data.stores.find(s=>String(s.id)===String(data.filters.store_id))?.name || "权限范围内全部门店与团队")}${data.filters.q ? ` · 商品搜索：${escapeHtml(data.filters.q)}` : ""}</p>`;
  if (q.unknown_channel || q.invalid_time || q.undated_all_time || q.estimated_time || q.unclassified_time) content+=`<details class="report-quality"><summary>统计说明与待核实：渠道待核实 ${q.unknown_channel} 单，时间冲突 ${q.invalid_time} 单</summary><p>渠道待核实数量来自所选日期及归属范围，无法分配给版纳或昆明，未计入两渠道合计。时间冲突未计入。</p><p>另有 ${q.undated_all_time} 单无法归属日期（这是全部日期范围内的提示，不代表属于当前期间）。已计入的估算时间 ${q.estimated_time} 单、时间精度未分类 ${q.unclassified_time} 单。未自动修复历史数据。</p></details>`;
  let page,pages,total;
  if (r.dataView === "daily") {
    const daily=[...data.daily].reverse(); total=daily.length; pages=Math.max(1,Math.ceil(total/50));
    r.dailyPage=Math.min(r.dailyPage,pages); page=r.dailyPage;
    content+=reportTable(["日期","版纳订单","昆明订单","合计订单","版纳商品数量","昆明商品数量","商品合计","版纳物料","昆明物料"],daily.slice((page-1)*50,page*50).map(d=>[d.date,d.banna_orders,d.kunming_orders,d.orders,d.banna_product_quantity,d.kunming_product_quantity,d.product_quantity,d.banna_material_quantity,d.kunming_material_quantity]));
  } else {
    ({page,total,total_pages:pages}=data.pagination);
    const daily=r.dataView === "items_daily";
    const headers=[...(daily?["日期"]:[]),"渠道","门店／团队","条码","商品／物料","品类／规格","数量","涉及订单数"];
    const values=row=>[...(daily?[row.date]:[]),REPORT_CHANNELS[row.channel],row.store_name+(row.store_kind==="team"?" · 合作团队":""),row.item_kind==="material"?"临时物料":row.product_barcode,row.product_name+(row.name_changed?"（历史名称有变化）":""),row.item_kind==="material"?row.material_spec:row.product_category,row.quantity,row.orders];
    content+='<h3>正式商品</h3>'+reportTable(headers,data.rows.filter(x=>x.item_kind==="product").map(values));
    content+='<h3>临时物料</h3>'+reportTable(headers,data.rows.filter(x=>x.item_kind==="material").map(values));
    content+='<p class="muted mini">商品行的涉及订单数不能相加作为总订单数；商品与临时物料合计分页。</p>';
  }
  content+=`<div class="report-pagination"><button class="btn small" data-report-page="${page-1}" ${page<=1?"disabled":""}>上一页</button><span>第 ${page} / ${pages} 页 · 共 ${total} 行 · 每页最多 50 行</span><button class="btn small" data-report-page="${page+1}" ${page>=pages?"disabled":""}>下一页</button></div>`;
  const top=window.scrollY;
  target.innerHTML=content;
  target.querySelectorAll("[data-report-page]").forEach(button=>button.onclick=()=>{
    if (r.dataView!==r.view) return; // Old results retained after a failed view change are read-only.
    if (r.view==="daily") { r.dailyPage=Number(button.dataset.reportPage); updateFulfillmentReport(); }
    else { r.filters.page=Number(button.dataset.reportPage); r.sequence++; refreshFulfillmentReport(); }
  });
  window.scrollTo({top,behavior:"instant"});
}

async function downloadFulfillmentReport(button) {
  const epoch=pageEpoch, owner=state.user.id;
  const params=reportParams(); params.delete("page"); params.delete("page_size");
  const error=document.getElementById("reportExportStatus"); error.textContent="正在生成当前筛选范围的完整表格…";
  await withButtonBusy(button,"导出中…",async()=>{
    const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),190000);
    try {
      const response=await fetch(`/api/reports/fulfillment-items.xlsx?${params}`,{signal:controller.signal});
      if (!response.ok) { const body=await response.json(); throw new Error(body.error || "导出失败"); }
      const blob=await response.blob();
      if (epoch!==pageEpoch || owner!==state.user?.id) return;
      const url=URL.createObjectURL(blob), link=document.createElement("a");
      link.href=url; link.download=`发货统计-${params.get("channel")}-${params.get("date_from")}-${params.get("date_to")}.xlsx`;
      link.click(); setTimeout(()=>URL.revokeObjectURL(url),60000);
      error.textContent="表格已生成，已交给浏览器下载；之后的取消或重发请重新导出。";
    } catch (exc) {
      if (epoch===pageEpoch) error.textContent=`导出失败：${exc.name==="AbortError"?"生成超时，请缩小范围后重试":exc.message}。订单未被修改。`;
    } finally { clearTimeout(timer); }
  });
}

async function renderFulfillmentReport() {
  const r=fulfillmentReport;
  if (r.owner!==state.user.id || !r.filters) {
    r.owner=state.user.id; r.data=null; r.error=""; r.view="daily"; r.dailyPage=1;
    r.filters={...reportDates("today"),channel:"all",store_id:state.user.role==="staff"?String(state.user.store_id):"",shipment_type:"",q:"",page:1,page_size:50};
  }
  r.sequence++;
  const f=r.filters, option=(value,label,selected)=>`<option value="${escapeHtml(value)}" ${value===selected?"selected":""}>${escapeHtml(label)}</option>`;
  document.getElementById("app").innerHTML=shell(`<section class="report-page"><div class="page-head"><div><h1>发货统计</h1><p>版纳与昆明 · 按门店、商品核对有效发货量</p></div><div class="report-actions"><button class="btn" id="reportRefresh">刷新统计</button><button class="btn primary" id="reportExport">导出 Excel</button></div></div>
    <div class="panel panel-pad"><p class="report-policy">统计的是系统登记发货量，包含等待揽收；不是仓库库存余额。面单取消成功会移除原统计，重新出单按新日期和新渠道计入。</p>
    <div class="report-presets">${[["today","今日"],["yesterday","昨日"],["week","本周"],["month","本月"],["last_month","上个月"]].map(([key,label])=>`<button class="btn small" data-report-preset="${key}">${label}</button>`).join("")}</div>
    <form id="reportFilters" class="report-filters"><label>发货渠道<select name="channel">${[["all","全部渠道"],["banna","版纳"],["kunming","昆明"]].map(([k,v])=>option(k,v,f.channel)).join("")}</select></label>
    ${state.user.role==="admin"?'<label>门店／团队<select name="store_id"><option value="">全部门店与团队</option></select></label>':`<label>订单归属<input disabled value="${escapeHtml(state.user.store_name)}"></label>`}
    <label>开始日期<input required type="date" name="date_from" value="${f.date_from}"></label><label>结束日期<input required type="date" name="date_to" value="${f.date_to}"></label>
    <label>发货类型<select name="shipment_type">${option("","全部类型",f.shipment_type)}${Object.entries(SHIPMENT_TYPES).map(([key,[name]])=>option(key,name,f.shipment_type)).join("")}</select></label>
    <label>商品搜索<input name="q" maxlength="100" value="${escapeHtml(f.q)}" placeholder="商品名称 / 条码"></label><button class="btn primary" type="submit">筛选</button></form>
    <div class="report-tabs" role="tablist" aria-label="统计视图">${[["daily","每日渠道概览"],["items","门店商品汇总"],["items_daily","每日商品明细"]].map(([key,label])=>`<button class="btn" role="tab" aria-selected="${r.view===key}" data-report-view="${key}">${label}</button>`).join("")}</div>
    <p id="reportStatus" class="report-status" role="status"></p><p id="reportExportStatus" class="report-status" role="status"></p><div id="reportResults"></div></div></section>`);
  bindCommon();
  const form=document.getElementById("reportFilters");
  if (form.elements.store_id && r.data) {
    form.elements.store_id.innerHTML = '<option value="">全部门店与团队</option>' + r.data.stores.map(s=>option(String(s.id),s.name,f.store_id)).join("");
  }
  form.onsubmit=event=>{
    event.preventDefault(); const values=Object.fromEntries(new FormData(form));
    r.filters={...r.filters,...values,page:1}; r.dailyPage=1; r.sequence++;
    refreshFulfillmentReport();
  };
  document.querySelectorAll("[data-report-preset]").forEach(button=>button.onclick=()=>{
    const dates=reportDates(button.dataset.reportPreset);
    form.elements.date_from.value=dates.date_from; form.elements.date_to.value=dates.date_to; form.requestSubmit();
  });
  document.querySelectorAll("[data-report-view]").forEach(button=>button.onclick=()=>{
    r.view=button.dataset.reportView; r.filters.page=1; r.dailyPage=1; r.sequence++;
    document.querySelectorAll("[data-report-view]").forEach(b=>b.setAttribute("aria-selected",String(b===button)));
    refreshFulfillmentReport();
  });
  document.getElementById("reportRefresh").onclick=()=>refreshFulfillmentReport();
  document.getElementById("reportExport").onclick=event=>downloadFulfillmentReport(event.currentTarget);
  if (r.data) updateFulfillmentReport();
  await refreshFulfillmentReport();
}

document.addEventListener("visibilitychange",()=>{
  if (document.hidden) { clearTimeout(fulfillmentReport.timer); fulfillmentReport.timer=null; }
  else if (reportActive()) refreshFulfillmentReport();
});
window.addEventListener("online",()=>{ if (reportActive()) refreshFulfillmentReport(); });
