"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const parameters = new URLSearchParams(location.search);
  const id = parameters.get("inventory_task") || "";
  let task = null, busy = false;
  function notice(message, error = false) {
    $("notice").textContent = message;
    $("notice").className = error ? "error" : "";
  }
  function loading(value) {
    busy = value;
    $("refreshTask").disabled = value;
    $("cancelTask").disabled = value || !task?.can_cancel;
    $("loginForm").querySelectorAll("input,button").forEach(node => { node.disabled = value; });
  }
  function invalidate(error) {
    task = null;
    if (error.status === 401 || error.status === 403) {
      $("taskPanel").hidden = true;
      $("taskDetails").replaceChildren(); $("taskItems").replaceChildren();
      $("loginPanel").hidden = error.status !== 401;
    }
  }
  async function request(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 35000);
    try {
      const response = await fetch(path, {credentials: "same-origin", cache: "no-store", ...options, signal: controller.signal,
        headers: {"Content-Type": "application/json", ...(options.headers || {})}});
      const data = await response.json();
      if (!response.ok) { const error = new Error(data.error || "操作未完成，请刷新核对。"); error.status = response.status; throw error; }
      return data;
    } catch (error) {
      if (error.name === "AbortError" || error instanceof TypeError) throw new Error("连接中断或等待超时，结果未确认。请先刷新核对，不要重复取消。");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function showTask(row) {
    task = row;
    $("taskPanel").hidden = false;
    $("loginPanel").hidden = true;
    $("taskTitle").textContent = `任务 #${row.id}`;
    $("taskKind").textContent = row.inventory_source_kind === "inventory_store_restock" ? "门店补货" : "渠道发货";
    const details = $("taskDetails"); details.replaceChildren();
    for (const [label, value] of [["任务编号", row.business_id], ["发货状态", row.status], ["面单状态", row.booking_status],
      ["收件人", row.recipient_name], ["联系电话", row.phone], ["收件地址", row.address],
      ["承运商", row.express_company], ["快递单号", row.tracking_no || "尚无有效单号"], ["物流状态", row.tracking_status || "暂无物流状态"]]) {
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = label; dd.textContent = value || "—"; details.append(dt, dd);
    }
    const items = $("taskItems"); items.replaceChildren();
    row.items.forEach(item => { const li = document.createElement("li"); li.textContent = `${item.name} × ${item.quantity}`; items.append(li); });
    $("cancelHint").textContent = row.can_cancel ? "请核对收件信息和单号后操作。" :
      row.booking_status === "已取消" ? "旧面单已取消，请回新中台刷新并确认后换单。" : "此任务当前不能取消：可能尚未出单、已取消或已签收。";
  }
  async function refresh() {
    const data = await request(`/api/admin/inventory-tasks/${id}`);
    showTask(data.task);
  }
  $("loginForm").addEventListener("submit", async event => {
    event.preventDefault(); if (busy) return;
    const formNode = event.currentTarget, form = new FormData(formNode);
    loading(true); notice("正在登录并读取任务…");
    try {
      await request("/api/login", {method: "POST", body: JSON.stringify({username: form.get("username"), password: form.get("password")})});
      formNode.elements.password.value = "";
      await refresh(); notice("已读取当前任务。取消前请核对。");
    } catch (error) { task = null; $("taskPanel").hidden = true; notice(error.message, true); }
    finally { loading(false); }
  });
  $("refreshTask").addEventListener("click", async () => {
    if (busy) return; loading(true); notice("正在核对最新状态…");
    try { await refresh(); notice("已刷新。请按当前状态操作。"); }
    catch (error) { invalidate(error); notice(error.message, true); }
    finally { loading(false); }
  });
  $("cancelTask").addEventListener("click", async () => {
    if (busy || !task?.can_cancel) return;
    if (!confirm(`确认取消任务 #${id} 的面单 ${task.tracking_no}？只有快递平台确认成功才会回收；本页不会自动换单。`)) return;
    loading(true); $("cancelTask").textContent = "正在请求取消…";
    notice("正在等待快递平台确认，请勿重复操作。");
    try {
      await request(`/api/shipments/${id}/label/cancel`, {method: "POST", body: JSON.stringify({reason: "总部确认库存任务需要换单", expected_cancel_version: task.cancel_version})});
      await refresh(); notice("取消结果已保存。请回新中台刷新任务状态，再决定是否换单。");
    } catch (error) {
      invalidate(error); notice(`${error.message} 请先点击“刷新任务状态”核对，本页不会自动重试。`, true);
    } finally { $("cancelTask").textContent = "取消并回收此面单"; loading(false); }
  });
  if (!/^[1-9][0-9]{0,11}$/.test(id) || parameters.size !== 1) { notice("库存任务链接无效，请从新中台重新打开。", true); return; }
  loading(true);
  refresh().then(() => notice("已读取当前任务。取消前请核对。")).catch(error => {
    task = null; $("loginPanel").hidden = error.status !== 401;
    notice(error.status === 401 ? "请使用总部账号登录后查看此任务。" : error.message, error.status !== 401);
  }).finally(() => loading(false));
})();
