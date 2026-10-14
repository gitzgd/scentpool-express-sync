/* Synthetic local fixture only; consumes one demo label cancellation. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("playwright");

(async () => {
  const manifest = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const origin = new URL(manifest.base_url).origin;
  assert.equal(new URL(origin).hostname, "127.0.0.1");
  assert.equal(manifest.production_ready, false);
  const [success, rejected, cancelled] = manifest.inventory_task_samples;
  const artifacts = fs.mkdtempSync(path.join(os.tmpdir(), "inventory-ui-synthetic-"));
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || "chrome"});
  try {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({viewport: {width, height: 900}});
      const page = await context.newPage();
      const errors = [], unexpected = [];
      let cancels = 0;
      page.on("pageerror", error => errors.push(error.message));
      page.on("request", request => {
        assert.equal(new URL(request.url()).origin, origin);
        if (request.url().endsWith("/label/cancel")) cancels++;
      });
      page.on("response", response => { if (response.status() >= 400 && ![401, 502].includes(response.status())) unexpected.push(response.status()); });
      await page.goto(`${origin}/?inventory_task=${rejected}`);
      await page.locator("#loginPanel").waitFor({state: "visible"});
      await page.locator('[name="username"]').fill("admin");
      await page.locator('[name="password"]').fill("local-demo-only-2026");
      await page.getByRole("button", {name: "登录并查看此任务"}).click();
      await page.locator("#taskPanel").waitFor({state: "visible"});
      assert.equal(cancels, 0, "opening and logging in must not cancel");
      assert.equal(await page.locator("#taskItems li").count(), 12);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({path: path.join(artifacts, `${width}-task.png`), fullPage: true});
      await page.locator("#cancelTask").scrollIntoViewIfNeeded();
      const bounds = await page.locator("#cancelTask").boundingBox();
      assert(bounds.x >= 0 && bounds.x + bounds.width <= width && bounds.height >= 44);
      page.once("dialog", dialog => dialog.dismiss());
      await page.locator("#cancelTask").click();
      assert.equal(cancels, 0);
      page.once("dialog", dialog => dialog.accept());
      await page.locator("#cancelTask").click();
      await page.waitForFunction(() => document.getElementById("notice").textContent.includes("合成平台拒绝"));
      assert.equal(cancels, 1);
      assert.equal(await page.locator("#cancelTask").isDisabled(), true);
      assert.match(await page.locator("#taskDetails").innerText(), /LOCAL-DEMO/);
      await page.screenshot({path: path.join(artifacts, `${width}-rejected.png`), fullPage: true});
      await page.locator("#refreshTask").click();
      await page.waitForFunction(() => !document.getElementById("cancelTask").disabled);
      await page.route(`**/api/admin/inventory-tasks/${rejected}`, route => route.fulfill({status: 401, contentType: "application/json", body: JSON.stringify({error: "登录已过期"})}));
      await page.locator("#refreshTask").click();
      await page.locator("#loginPanel").waitFor({state: "visible"});
      assert.equal(await page.locator("#taskPanel").isHidden(), true);
      assert.equal(await page.locator("#taskDetails").innerText(), "");
      await page.unroute(`**/api/admin/inventory-tasks/${rejected}`);
      await page.goto(`${origin}/?inventory_task=${cancelled}`);
      await page.locator("#taskPanel").waitFor({state: "visible"});
      assert.match(await page.locator("#cancelHint").innerText(), /已取消/);
      assert.equal(await page.locator("#cancelTask").isDisabled(), true);
      if (width === 1440) {
        await page.goto(`${origin}/?inventory_task=${success}`);
        await page.waitForFunction(() => !document.getElementById("cancelTask").disabled);
        const before = cancels;
        page.once("dialog", dialog => dialog.accept());
        await page.evaluate(() => { document.getElementById("cancelTask").click(); document.getElementById("cancelTask").click(); });
        await page.waitForFunction(() => document.getElementById("notice").textContent.includes("取消结果已保存"));
        assert.equal(cancels, before + 1);
        assert.match(await page.locator("#taskDetails").innerText(), /已取消/);
        assert.equal(await page.locator("#cancelTask").isDisabled(), true);
      }
      assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
      await context.close();
    }
    console.log(JSON.stringify({ok: true, widths: [1440, 390], artifacts, synthetic_only: true}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
