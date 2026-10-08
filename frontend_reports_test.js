/* Actual report lifecycle functions, synthetic DOM/network and controllable promises. */
const assert=require('node:assert/strict'), fs=require('node:fs'),vm=require('node:vm');
const listeners={},timers=new Map(),requests=[];let serial=0;
const context=vm.createContext({console,URLSearchParams,Intl,Date,
  document:{hidden:false,addEventListener:(key,fn)=>listeners[key]=fn,getElementById:()=>null,querySelector:()=>null},
  window:{addEventListener:(key,fn)=>listeners[key]=fn},location:{pathname:'/reports/fulfillment'},
  state:{user:{id:1,role:'admin'}},StaleViewError:class extends Error{},
  setTimeout:(fn,ms)=>{timers.set(++serial,{fn,ms});return serial},clearTimeout:id=>timers.delete(id),
  api:url=>new Promise((resolve,reject)=>requests.push({url,resolve,reject}))});
vm.runInContext(fs.readFileSync('static/reports.js','utf8'),context);
const run=code=>vm.runInContext(code,context);
run("fulfillmentReport.filters={...reportDates('today'),channel:'all',page:1}; updateFulfillmentReport=()=>{};");
const data=n=>({summary:{orders:n},pagination:{page:1},stores:[]});
const flush=async()=>{await Promise.resolve();await Promise.resolve();await Promise.resolve()};
(async()=>{
  assert.equal(run("reportDates('today',new Date('2026-10-07T16:01:00Z')).date_from"),'2026-10-08');
  assert.equal(run("reportDates('week',new Date('2026-10-08T10:00:00Z')).date_from"),'2026-10-05');
  assert.equal(run("reportDates('last_month',new Date('2026-03-01T10:00:00Z')).date_to"),'2026-02-28');
  const first=run('refreshFulfillmentReport()');
  run("fulfillmentReport.sequence++;fulfillmentReport.filters.channel='banna';refreshFulfillmentReport();");
  assert.equal(requests.length,1,'do not overlap');requests[0].resolve(data(999));await first;await flush();
  assert.equal(run('fulfillmentReport.data'),null,'old response must not overwrite');
  assert.equal(requests.length,2);assert(requests[1].url.includes('channel=banna'));
  requests[1].resolve(data(2));await flush();assert.equal(run('fulfillmentReport.data.summary.orders'),2);
  assert([...timers.values()].some(t=>t.ms===15000));
  const failure=run('refreshFulfillmentReport()');requests[2].reject(Error('offline'));await failure;
  assert.equal(run('fulfillmentReport.data.summary.orders'),2);assert.match(run('fulfillmentReport.error'),/上次成功/);
  run("fulfillmentReport.view='items_daily';fulfillmentReport.sequence++");
  const viewFailure=run('refreshFulfillmentReport()');requests[3].reject(Error('timeout'));await viewFailure;
  assert.equal(run('fulfillmentReport.dataView'),'daily','failed new view retains old result shape');
  context.document.hidden=true;listeners.visibilitychange();assert.equal(timers.size,0);
  const count=requests.length;await run('refreshFulfillmentReport()');assert.equal(requests.length,count);
  context.document.hidden=false;listeners.visibilitychange();assert.equal(requests.length,count+1);
  run('stopFulfillmentReport(true);state.user=null');requests[count].resolve(data(888));await flush();
  assert.equal(run('fulfillmentReport.data'),null,'logout cannot leak cached aggregate');
  assert.equal(timers.size,0);
  console.log('frontend report lifecycle tests passed');
})().catch(e=>{console.error(e);process.exitCode=1});
