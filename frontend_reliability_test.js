/* Synthetic, network-free tests against the actual browser functions. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function harness() {
  const timers = new Map(), storage = new Map(), listeners = new Map(), messages = [];
  let serial = 0;
  const emptyClassList = { add() {}, remove() {}, toggle() {} };
  const element = () => ({ dataset: {}, style: {}, classList: emptyClassList, isConnected: true,
    setAttribute() {}, removeAttribute() {}, append() {}, remove() {}, querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {} });
  const document = { visibilityState: "visible", activeElement: null, body: element(), documentElement: element(),
    querySelector: () => null, querySelectorAll: () => [], getElementById: () => null, createElement: element,
    addEventListener: (name, callback) => listeners.set(name, callback) };
  const context = vm.createContext({ document, location: { pathname: "/admin", search: "" },
    history: { pushState() {}, replaceState() {} }, window: { addEventListener() {} }, navigator: {},
    sessionStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) },
    crypto: { randomUUID: () => "00000000-0000-4000-8000-000000000001" },
    AbortController, URLSearchParams, FormData, URL, console,
    setTimeout: (fn, ms) => { const id = ++serial; timers.set(id, { fn, ms }); return id; }, clearTimeout: id => timers.delete(id),
    fetch: async () => { throw Error("Unexpected network call"); }, confirm: () => true, messages });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "static/special.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "static/reports.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "static/app.js"), "utf8").replace(/\nrender\(\);\s*$/, "\n"), context);
  vm.runInContext('state.user = {id: 1, role: "admin"}; toast = (message) => messages.push(message);', context);
  const run = code => vm.runInContext(code, context);
  return { context, run, document, timers, storage, listeners, messages, element };
}

function response(data, status = 200) { return { ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => data }; }
async function flush() { await Promise.resolve(); await Promise.resolve(); }

async function main() {
  {
    const h = harness(), handlers = {}, button = h.element();
    button.addEventListener = (name, fn) => {handlers[name] = fn;};
    h.document.getElementById = id => id === "previewShippingBatch" ? button : null;
    h.context.fetch = async (_url, options) => {
      assert.equal(JSON.parse(options.body).profile_id, "");
      return response({preview:{eligible:[],profile:null,settings_ready:false,profile_error:"请手动选择"}});
    };
    h.run('state.batchProfileId="kunming_sf"; state.shippingSettings={default_profile_id:"kunming",fulfillment_profiles:[{id:"kunming",name:"昆明",express_company:"中通"},{id:"kunming_sf",name:"昆明顺丰",express_company:"顺丰",third_template_url:""}]}; updateBatchPreviewUi=()=>{}; updateShippingBatchUi=()=>{}; scheduleShippingBatchPoll=()=>{}; bindAdmin()');
    await handlers.click({currentTarget:button});
    assert.equal(h.run('state.batchProfileId'), "");
    const html = h.run('renderShippingBatchPreview()');
    assert.match(html, /value="" selected>请选择发货方案/);
    assert.match(html, /顺丰（待配置模板）/);
    assert.doesNotMatch(html, /（默认）|原总部配置（尚未切换）|id="batchBulkCompany"/);
    const settings = h.run('renderFulfillmentSettings(state.shippingSettings)');
    assert.match(settings, /data-profile-form="kunming_sf"/);
    assert.doesNotMatch(settings, /name="make_default"/);
  }
  {
    const h = harness(), pending = [];
    h.context.fetch = (url) => new Promise(resolve => pending.push({ url, resolve }));
    h.run('state.adminFilters = {q:"old"}');
    const first = h.run('loadShipments()').catch(error => error.constructor.name);
    h.run('state.adminFilters = {q:"new"}');
    const second = h.run('loadShipments()');
    for (const request of pending.filter(item => item.url.includes("q=new"))) request.resolve(response(request.url.includes("summary") ? { counts: {total: 2} } : { shipments: [{id: 2}], pagination: {page: 1} }));
    await second;
    for (const request of pending.filter(item => item.url.includes("q=old"))) request.resolve(response(request.url.includes("summary") ? { counts: {total: 1} } : { shipments: [{id: 1}], pagination: {page: 1} }));
    assert.equal(await first, "StaleViewError");
    assert.equal(h.run('state.shipments[0].id'), 2);
    assert.equal(h.run('state.adminShipmentSummary.total'), 2);
  }
  {
    const h = harness(); let release;
    h.context.fetch = () => new Promise(resolve => { release = resolve; });
    const pending = h.run('loadShipments({loadSummary:false})').catch(error => error.constructor.name);
    h.run('invalidatePage({clearIdentity:true}); state.user={id:2,role:"staff"}');
    release(response({ shipments: [{id: 999}] }));
    assert.equal(await pending, "StaleViewError");
    assert.equal(h.run('state.shipments.length'), 0);
    assert.equal(h.run('state.user.id'), 2);
  }
  {
    const h = harness(); let calls = 0, finish;
    const button = h.element(); button.textContent = "保存"; button.disabled = false;
    h.context.button = button;
    h.context.operation = () => { calls++; return new Promise(resolve => { finish = resolve; }); };
    const first = h.run('withButtonBusy(button,"保存中…",operation)');
    const second = h.run('withButtonBusy(button,"保存中…",operation)');
    assert.equal(button.disabled, true); await flush(); assert.equal(calls, 1);
    finish({ saved: true }); await Promise.all([first, second]); assert.equal(button.disabled, false);
  }
  {
    const h = harness(), requests = [];
    h.context.fetch = async (url, options) => { requests.push([url, options.method]); if (options.method === "POST") throw new TypeError("response lost"); return response({ found: true, shipment: {id: 3, business_id: "SYNTHETIC"} }); };
    const result = await h.run('createWithConfirmation("shipment",{store_id:2,store_order_no:"SYNTHETIC"})');
    assert.equal(result.shipment.id, 3);
    assert.equal(requests.filter(item => item[1] === "POST").length, 1);
    assert.match(requests[1][0], /submissions\/status\?kind=shipment/);
    assert.equal(h.storage.has("scentpool_submission:1:shipment"), false);
    h.context.fetch = async (url, options) => { if (options.method === "POST") throw new TypeError("lost"); return response({found:true, deleted:true}); };
    await assert.rejects(h.run('createWithConfirmation("shipment",{store_id:2})'), /原提交记录已被删除/);
    assert.equal(h.storage.has("scentpool_submission:1:shipment"), true);
  }
  {
    const h = harness();
    h.run('state.activeShippingBatch={batch:{id:1,status:"处理中"},counts:{"成功":1}}; loadActiveShippingBatch=async()=>{}; updateShippingBatchUi=()=>{}; render=()=>{throw Error("whole-page redraw")};');
    h.document.visibilityState = "hidden"; h.run('scheduleShippingBatchPoll()'); assert.equal(h.timers.size, 0);
    h.document.visibilityState = "visible"; h.run('scheduleShippingBatchPoll()');
    const timer = [...h.timers.entries()][0]; h.timers.delete(timer[0]); await timer[1].fn();
    assert.equal([...h.timers.values()].some(item => item.ms === 2500), true);
    h.run('invalidatePage()'); assert.equal(h.timers.size, 0);
  }
  {
    const h = harness();
    h.run('scheduleTaskAlertPoll()'); const id = h.run('state.taskAlertsPollTimer');
    h.run('scheduleTaskAlertPoll()'); assert.equal(h.run('state.taskAlertsPollTimer'), id);
    h.storage.set("scentpool_tracking_tasks:1", JSON.stringify(["abc_DEF-01234567890123456", "../invalid"]));
    h.run('restoreTrackingTasks()'); assert.equal(h.run('state.trackingTasks.size'), 1);
    h.run('state.trackingTasks.set("partial",{id:"partial",status:"completed",total:10,completed:8,failed:2,remaining:0})');
    assert.match(h.run('renderTrackingTasks()'), /失败 2/);
    assert.match(h.run('renderTrackingTasks()'), /失败项没有被隐藏/);
    h.run('for(let n=0;n<25;n++) state.trackingTasks.set("extra"+n,{id:"extra"+n,status:n<12?"completed":"queued",total:1,remaining:1});');
    const bounded = h.run('renderTrackingTasks()');
    assert.equal(h.run('state.trackingTasks.size'),10);
    assert.equal((bounded.match(/<section /g)||[]).length,10);
    assert.match(bounded,/其他任务仍在后台执行或已结束/);
  }
  {
    const h = harness();
    const fields = { "data-tracking": {value:"UNSAVED-SYNTHETIC"}, "data-note": {value:"未保存包装要求"} };
    const row = { dataset:{shipment:"6",dirty:"1"}, querySelector: selector => fields[selector.slice(1,-1)] || null };
    h.document.querySelectorAll = () => [row]; h.document.querySelector = () => row;
    h.run('captureAdminRowDrafts()'); fields["data-note"].value = "server";
    h.run('restoreAdminRowDrafts()'); assert.equal(fields["data-note"].value,"未保存包装要求");
    h.run('clearAdminRowDraft(6)'); assert.equal(h.run('state.adminRowDrafts.size'),0);
  }
  {
    const h = harness(), summary = {innerHTML:""};
    h.document.getElementById = id => id === "adminShipmentSummary" ? summary : null;
    h.run('state.adminShipmentSummary={total:81,"待处理":74,"已发货":7};updateShipmentRows([])');
    assert.match(summary.innerHTML,/待处理 74/); assert.match(summary.innerHTML,/已发货 7/);
  }
  {
    const h = harness();
    h.run('state.batchPreview={matched:150,eligible_count:120,eligible:[{id:1,express_company:"顺丰",shipment_type:"resend"}],excluded:[],pagination:{page:1,total_pages:3},settings_ready:true,label_ready:true};state.batchSelectAll=true;state.shippingConfig={enabled:true,configured:true}');
    const html = h.run('renderShippingBatchPreview()');
    assert.match(html,/确认提交 120 单/); assert.match(html,/整个筛选范围/); assert.match(html,/第 1 \/ 3 页/);
    h.run('state.activeShippingBatch={batch:{id:1,total_count:150,status:"部分完成"},counts:{"失败":2},items:[],pagination:{page:1,total_pages:3}}');
    assert.match(h.run('renderShippingBatchProgress()'),/查看全部 2 项失败/);
  }
  {
    const h = harness();
    h.run('state.batchPreview={type_counts:{sale:8,resend:2},company_counts:{"顺丰":8,"圆通":2}};state.batchSelectAll=true;state.batchKnownCompanies={1:"顺丰",2:"圆通"};state.batchKnownTypes={1:"sale",2:"resend"}');
    let summary = h.run('shippingBatchConfirmationSummary([{id:1,express_company:"京东"}],10)');
    assert.match(summary.companySummary,/顺丰 7 单/); assert.match(summary.companySummary,/京东 1 单/);
    h.run('state.batchBulkCompany="圆通"');
    summary = h.run('shippingBatchConfirmationSummary([{id:1,express_company:"京东"}],10)');
    assert.match(summary.companySummary,/圆通 9 单/); assert.doesNotMatch(summary.companySummary,/顺丰/);
    h.run('state.batchSelectAll=false;state.batchBulkCompany=""');
    summary = h.run('shippingBatchConfirmationSummary([{id:1},{id:2}],2)');
    assert.match(summary.companySummary,/顺丰 1 单/); assert.match(summary.companySummary,/圆通 1 单/);
  }
  {
    const h = harness(), handlers = {};
    const button = h.element(); button.dataset.saveShipment = "7"; button.textContent="保存";
    button.addEventListener = (name, callback) => { handlers[name] = callback; };
    const fields = {"[data-status]":{value:"待处理"},"[data-company]":{value:"顺丰"},"[data-tracking]":{value:""},"[data-note]":{value:"合成备注"}};
    const row = {matches: selector => selector === '[data-shipment="7"]', querySelector: selector => fields[selector] || null, querySelectorAll: selector => selector === '[data-save-shipment]' ? [button] : []};
    h.context.row=row; h.run('render=()=>{}; clearAdminRowDraft=()=>{}');
    let body;
    h.context.fetch = async (_url, options) => {body=JSON.parse(options.body); return response({message:"已保存。"});};
    h.run('bindAdmin(row)'); await handlers.click({currentTarget:button});
    assert.equal(body.express_company,"顺丰"); assert.equal(body.shipping_note,"合成备注");
    assert.equal(h.messages.at(-1),"已保存。");
    h.document.querySelectorAll = () => {throw Error("row update must not rebind all item editors")};
    h.run('bindShipmentItemEditor([],row)');
  }
  {
    const h = harness(), handlers = {};
    const cancel={focus(){},addEventListener:(name,fn)=>{handlers.cancel=fn}}, accept={addEventListener:(name,fn)=>{handlers.accept=fn}};
    const overlay={remove(){this.removed=true},querySelector:selector=>selector.includes("cancel")?cancel:accept,addEventListener(){}};
    h.document.createElement=()=>overlay; h.document.body.appendChild=()=>{};
    const decision=h.run('confirmShippingBatch({total:2,scope:"已勾选",typeSummary:"普通 2",companySummary:"顺丰 2"})');
    assert.match(overlay.innerHTML,/role="dialog"/); assert.match(overlay.innerHTML,/顺丰 2/);
    h.run('invalidatePage()'); assert.equal(await decision,false); assert.equal(overlay.removed,true);
  }
  {
    const h = harness(), handlers = {}, select = h.element(), submit = h.element();
    select.value = "banna";
    select.addEventListener = (name, fn) => { handlers[name] = fn; };
    h.document.getElementById = id => id === "batchProfile" ? select : id === "createShippingBatch" ? submit : null;
    let resolve;
    h.context.fetch = (_url, options) => {
      assert.equal(JSON.parse(options.body).profile_id, "banna");
      return new Promise(done => { resolve = done; });
    };
    h.document.querySelectorAll = selector => selector.includes("#shippingBatchPreviewHost") ? [select, submit] : [];
    h.run('state.batchProfileId="kunming"; state.batchSelectAll=true; state.batchSelectedIds=[1]; state.batchCompanyOverrides={1:"中通"}; updateBatchPreviewUi=()=>{}; bindAdmin()');
    const change = handlers.change({currentTarget:select});
    assert.equal(select.disabled,true); assert.equal(submit.disabled,true);
    resolve(response({preview:{eligible:[],profile:{id:"banna"}}}));
    await change;
    assert.equal(h.run('state.batchSelectAll'),true);
    assert.equal(h.run('state.batchSelectedIds.length'),1);
    assert.equal(h.run('Object.keys(state.batchCompanyOverrides).length'),0);
    assert.equal(h.run('state.batchProfileId'),"banna");
    assert.equal(h.run('state.batchPreviewLoading'),false);
  }
  {
    const h = harness(), requests = [];
    h.run('state.batchProfileId=""; state.batchSelectAll=false; state.batchSelectedIds=[1,52]; state.batchPreviewPage=2; state.batchKnownTypes={1:"sale",52:"resend"}; state.batchKnownCompanies={1:"圆通",52:"圆通"}; state.batchFilters={q:"合成",store_id:"2"};');
    for (const [profile, company] of [["kunming","中通"],["kunming_sf","顺丰"],["banna","圆通"],["",null]]) {
      h.context.fetch = async (_url, options) => {
        const body = JSON.parse(options.body); requests.push(body);
        assert.equal(body.page,2); assert.equal(body.profile_id,profile);
        return response({preview:{eligible_count:60,eligible:[{id:52,shipment_type:"resend",express_company:company}],pagination:{page:2,total_pages:2},profile:company?{id:profile,express_company:company}:null,preview_fingerprint:profile+"-new",settings_ready:!!company}});
      };
      await h.run(`loadShippingBatchPreview(state.batchFilters,{reset:false,profileId:${JSON.stringify(profile)}})`);
      assert.equal(h.run('JSON.stringify(state.batchSelectedIds)'),"[1,52]");
      assert.equal(h.run('state.batchSelectAll'),false);
      assert.equal(h.run('state.batchPreviewPage'),2);
      assert.equal(h.run('state.batchKnownTypes[1]'),"sale");
      assert.equal(h.run('state.batchPreview.preview_fingerprint'),profile+"-new");
      if (company) assert.equal(h.run('shippingBatchConfirmationSummary([{id:1},{id:52}],2).companySummary'),`${company} 2 单`);
      assert.match(h.run('renderShippingBatchPreview()'),/确认提交 2 单/);
    }
    assert.equal(requests.every(item=>item.filters.q==="合成" && item.filters.store_id==="2"),true);
    h.run('state.batchSelectedIds=[]');
    await h.run('loadShippingBatchPreview(state.batchFilters,{reset:false,profileId:""})');
    assert.equal(h.run('state.batchSelectedIds.length'),0); // Never silently reselect a cleared batch.
  }
  {
    const h = harness(), handlers = {}, select = h.element(), submit = h.element();
    select.value="kunming_sf";
    select.addEventListener=(_name,fn)=>{handlers.change=fn;};
    submit.addEventListener=(_name,fn)=>{handlers.submit=fn;};
    h.document.getElementById=id=>id==="batchProfile"?select:id==="createShippingBatch"?submit:null;
    h.document.querySelectorAll=selector=>selector.includes("#shippingBatchPreviewHost")?[select,submit]:[];
    h.run('state.batchProfileId="kunming";state.batchFilters={q:"old"};state.batchSelectAll=false;state.batchSelectedIds=[1,52];state.batchPreviewPage=2;state.batchPreview={eligible:[],profile:{id:"kunming"},settings_ready:true,preview_fingerprint:"old"};updateBatchPreviewUi=()=>{};bindAdmin()');
    let finish;
    h.context.fetch=()=>new Promise(resolve=>{finish=resolve;});
    const change=handlers.change({currentTarget:select});
    await handlers.submit({currentTarget:submit});
    assert.match(h.messages.at(-1),/正在核对/);
    finish(response({error:"合成网络失败"},503)); await change;
    assert.equal(h.run('state.batchProfileId'),"kunming");
    assert.equal(h.run('state.batchPreview.preview_fingerprint'),"old");
    assert.equal(h.run('JSON.stringify(state.batchSelectedIds)'),"[1,52]");
    assert.equal(h.run('state.batchPreviewPage'),2);
    assert.match(h.messages.at(-1),/原方案和订单选择已保留/);
    // Failed filter reads also retain the old scope and choices atomically.
    h.context.fetch=async()=>response({error:"合成读取失败"},503);
    await assert.rejects(h.run('loadShippingBatchPreview({q:"new"})'));
    assert.equal(h.run('state.batchFilters.q'),"old");
    assert.equal(h.run('JSON.stringify(state.batchSelectedIds)'),"[1,52]");
    h.context.fetch=async()=>response({preview:{eligible:[],eligible_count:0,profile:null}});
    await h.run('loadShippingBatchPreview({q:"new"})');
    assert.equal(h.run('state.batchFilters.q'),"new");
    assert.equal(h.run('state.batchSelectedIds.length'),0); // A real scope change still resets selection.
  }
  {
    const h=harness(), pending=[];
    h.context.fetch=(_url,options)=>new Promise(resolve=>pending.push({body:JSON.parse(options.body),resolve}));
    h.run('state.batchSelectAll=false;state.batchSelectedIds=[1,52];state.batchProfileId="kunming";state.batchPreview={profile:{id:"kunming"}}');
    const old=h.run('loadShippingBatchPreview({}, {reset:false,profileId:"banna"})').catch(error=>error.constructor.name);
    const latest=h.run('loadShippingBatchPreview({}, {reset:false,profileId:"kunming_sf"})');
    pending[1].resolve(response({preview:{eligible:[],profile:{id:"kunming_sf",express_company:"顺丰"},settings_ready:false,profile_error:"缺模板",preview_fingerprint:"new"}}));
    await latest;
    pending[0].resolve(response({error:"迟到失败"},503));
    assert.equal(await old,"StaleViewError");
    assert.equal(h.run('state.batchProfileId'),"kunming_sf");
    assert.equal(h.run('state.batchPreview.profile_error'),"缺模板");
    assert.equal(h.run('JSON.stringify(state.batchSelectedIds)'),"[1,52]");
    assert.match(h.run('renderShippingBatchPreview()'),/disabled>确认提交 2 单/);
    const late=h.run('loadShippingBatchPreview({}, {reset:false,profileId:"banna"})').catch(error=>error.constructor.name);
    h.run('invalidatePage({clearIdentity:true});state.user={id:2,role:"staff"}');
    pending[2].resolve(response({preview:{eligible:[{id:999}],profile:{id:"banna"}}}));
    assert.equal(await late,"StaleViewError");
    assert.equal(h.run('state.batchPreview'),null);
  }
  {
    const h=harness(), handlers={}, close=h.element(); let finish;
    close.addEventListener=(_name,fn)=>{handlers.close=fn;};
    h.document.getElementById=id=>id==="closeBatchPreview"?close:null;
    h.context.fetch=()=>new Promise(resolve=>{finish=resolve;});
    h.run('updateBatchPreviewUi=()=>{};updateShippingBatchUi=()=>{};scheduleShippingBatchPoll=()=>{};bindAdmin()');
    const late=h.run('loadShippingBatchPreview({}, {reset:false,profileId:"banna"})').catch(error=>error.constructor.name);
    handlers.close();
    finish(response({preview:{eligible:[{id:1}],profile:{id:"banna"}}}));
    assert.equal(await late,"StaleViewError");
    assert.equal(h.run('state.batchPreview'),null);
    assert.equal(h.run('state.batchPreviewLoading'),false);
  }
  {
    const h=harness(), handlers={}, submit=h.element(), posted=[];
    submit.addEventListener=(_name,fn)=>{handlers.submit=fn;};
    h.document.getElementById=id=>id==="createShippingBatch"?submit:null;
    h.run('state.batchProfileId="banna";state.batchSelectAll=false;state.batchSelectedIds=[1,52];state.batchCompanyOverrides={1:"圆通"};state.batchBulkCompany="圆通";state.batchKnownTypes={1:"sale",52:"resend"};updateBatchPreviewUi=()=>{};updateShippingBatchUi=()=>{};scheduleShippingBatchPoll=()=>{};confirmShippingBatch=async()=>true;bindAdmin()');
    h.context.fetch=async(url,options)=>{
      if (url.endsWith("/preview")) return response({preview:{eligible:[{id:52,shipment_type:"resend",express_company:"中通"}],profile:{id:"kunming",express_company:"中通"},settings_ready:true,preview_fingerprint:"kunming-new"}});
      posted.push(JSON.parse(options.body)); return response({batch:{id:3}});
    };
    await h.run('loadShippingBatchPreview({}, {reset:false,profileId:"kunming"})');
    await handlers.submit({currentTarget:submit});
    assert.deepEqual(posted,[{filters:{},shipments:[{id:1},{id:52}],selection_mode:"selected",preview_fingerprint:"kunming-new",express_company:"",profile_id:"kunming"}]);
    // Confirmation opened on an old preview must not create an old-profile task.
    await h.run('loadShippingBatchPreview({}, {reset:false,profileId:"kunming"})');
    h.run('confirmShippingBatch=async()=>{state.batchPreview={...state.batchPreview};return true;}');
    await handlers.submit({currentTarget:submit});
    assert.equal(posted.length,1);
    assert.match(h.messages.at(-1),/预览或发货方案已变化/);
  }
  console.log("frontend reliability tests passed: latest response, identity isolation, double click, lost/deleted submission, hidden/navigation lifecycle, local progress, independent alerts, opaque task recovery, partial failures, drafts, full-scope pagination, preserved fulfillment selection and submission payload");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
