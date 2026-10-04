/* 真实Chrome批量上传验收，只读取当前实验目录中的公开合成样本。 */
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const output = path.join(root, "artifacts/experiments/2026-10-03_multi-upload");

/** 记录Gradio完整SSE业务事件，避免CodeMirror虚拟化造成只读取部分JSON。 */
function readEvent(message, results) {
  if (!["process_completed", "process_generating"].includes(message.msg)) return;
  // 第二次yield的Code从空串更新为完整JSON；Gradio将它放在append差量数组中。
  for (const value of (message.output?.data || []).flat(Infinity)) {
    if (typeof value !== "string" || !value.trimStart().startsWith("{")) continue;
    try { results.push(JSON.parse(value)); } catch { /* 不完整增量不作为业务成功。 */ }
  }
}

/** 一次选择公开TXT/MD/大PDF，验证独立发布、状态和默认预算模型回答。 */
async function verify() {
  const state = JSON.parse(fs.readFileSync(path.join(root, ".local/runtime/service.json")));
  assert(state.anonymous);
  const browser = await chromium.launch({
    headless: true, executablePath: process.env.CHROME_EXECUTABLE,
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const results = [];
  const errors = [];
  let stage = "load";
  page.on("pageerror", (error) => errors.push(error.name));
  // 当前Gradio可能通过fetch读取SSE；克隆真实响应流，不修改业务请求或结果。
  await page.exposeFunction("multiUploadReceive", (message) => readEvent(message, results));
  await page.addInitScript(() => {
    const originalFetch = window.fetch;
    window.fetch = async (...args) => {
      const response = await originalFetch(...args);
      if (response.url.includes("/gradio_api/queue/data") && response.body) {
        (async () => {
          const reader = response.clone().body.getReader();
          const decoder = new TextDecoder();
          let pending = "";
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            pending += decoder.decode(value, { stream: true });
            const lines = pending.split("\n");
            pending = lines.pop();
            for (const line of lines) {
              if (!line.startsWith("data: ")) continue;
              try { await window.multiUploadReceive(JSON.parse(line.slice(6))); }
              catch { /* 跳过心跳与进度消息。 */ }
            }
          }
        })().catch(() => {});
      }
      return response;
    };
  });
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.enable");
  cdp.on("Network.eventSourceMessageReceived", (message) => {
    try { readEvent(JSON.parse(message.data), results); } catch { /* 跳过非业务心跳。 */ }
  });
  try {
    await page.goto(`${state.api}/product-workbench/`, { waitUntil: "networkidle" });
    const names = ["alpha.txt", "beta.md", "large-policy.pdf"];
    await page.locator("input[type=file]").setInputFiles(
      names.map((name) => path.join(output, "samples", name)),
    );
    stage = "upload";
    await page.getByRole("button", { name: "上传并发布", exact: true }).click();
    await page.waitForFunction(() => document.body.innerText.includes('"count": 3'),
      null, { timeout: 300000 });
    const batch = results.find((item) => item.count === 3);
    assert(batch);
    assert.equal(batch.status, "passed");
    const ids = batch.results.map((item) => {
      assert.equal(item.result.status_code, 201);
      assert.equal(item.result.data.version_status, "published");
      assert.equal(item.result.data.generation_status, "active");
      return item.result.data.document_id;
    });
    assert.equal(new Set(ids).size, 3);
    // 完整JSON来自浏览器真实SSE；只含公开样本的元数据。
    fs.writeFileSync(path.join(output, "browser-upload.json"), JSON.stringify(batch, null, 2) + "\n");
    console.log(JSON.stringify({ stage: "batch-upload", status: "passed", documents: 3 }));
    stage = "status";
    for (const id of ids) {
      const response = await page.request.get(`${state.api}/api/v1/documents/${id}/status`);
      assert.equal(response.status(), 200);
      assert.equal((await response.json()).document_status, "ready");
    }
    await page.getByRole("button", { name: "查询状态", exact: true }).click();
    await page.waitForFunction(() => document.body.innerText.includes('"document_status": "ready"'),
      null, { timeout: 30000 });
    await page.getByRole("tab", { name: "检索问答", exact: true }).click();
    stage = "query";
    const numberInputs = page.locator('input[type="number"]');
    assert.equal(await numberInputs.count(), 3);
    assert.equal(await numberInputs.nth(2).inputValue(), "2048");
    await page.getByLabel("问题", { exact: true }).fill("Lyra项目的维护窗口是什么时间，回滚由谁负责？");
    await page.getByRole("button", { name: "查询并回答", exact: true }).click();
    await page.waitForFunction(() => [...document.querySelectorAll("textarea,input")].some(
      (element) => element.value.includes("正在检索并生成回答")),
      null, { timeout: 15000 });
    const deadline = Date.now() + 310000;
    while (!results.some((item) => item.data?.answer_status || item.error_kind) && Date.now() < deadline) {
      await page.waitForTimeout(250);
    }
    stage = "query-result";
    const answer = results.find((item) => item.data?.answer_status);
    fs.writeFileSync(path.join(output, "browser-answer.json"), JSON.stringify(
      answer || results.find((item) => item.error_kind) || { status: "missing_result" }, null, 2) + "\n");
    assert.equal(answer?.data?.citation_validation, "passed");
    assert.equal(answer.status, "passed");
    assert(answer.data.retrieval_trace.reranker_used);
    assert(answer.data.message.includes("22:20") && answer.data.message.includes("22:50"));
    stage = "screenshots";
    await page.screenshot({ path: path.join(output, "desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(async () => { await document.fonts.ready; });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    await page.screenshot({ path: path.join(output, "mobile.png") });
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "browser.json"), JSON.stringify({
      status: "passed", transport: "real_browser_http", files: names,
      independent_documents: 3, documents_ready: true, default_max_tokens: 2048,
      immediate_pending_status: true, answer_status: "answered", citation_validation: "passed",
      page_errors: errors,
    }, null, 2) + "\n");
    console.log(JSON.stringify({ stage: "default-query", status: "passed" }));
  } catch (error) {
    fs.writeFileSync(path.join(output, "browser-failure.json"), JSON.stringify({
      status: "failed", stage, error_type: error.name, completed_events: results.length,
    }, null, 2) + "\n");
    console.error(JSON.stringify({ status: "failed", stage, error_type: error.name }));
    throw error;
  } finally {
    await browser.close();
  }
}

// 不回显内部请求/正文；公开证据已单独落盘，失败只输出异常类别。
verify().catch((error) => {
  console.error(JSON.stringify({ status: "failed", error_type: error.name }));
  process.exitCode = 1;
});
