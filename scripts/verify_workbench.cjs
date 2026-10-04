/* 浏览器验收只使用公开合成样本；token 从本地私有文件读取，绝不输出。 */
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const output = path.join(root, "artifacts/experiments",
  process.env.ACCEPTANCE_REPORT_NAME || "2026-10-03_runtime-acceptance");

/** 在真实 Chrome 中完成 UI 上传、状态查询和问答，并保存不含凭据的截图。 */
async function verify() {
  fs.mkdirSync(output, { recursive: true });
  const state = JSON.parse(fs.readFileSync(path.join(root, ".local/runtime/service.json")));
  const token = state.anonymous ? "" : fs.readFileSync(
    path.join(root, ".local/runtime/acceptance-token.txt"), "utf8");
  const caseName = process.env.ACCEPTANCE_CASE || "browser";
  const completed = [];
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROME_EXECUTABLE ? { executablePath: process.env.CHROME_EXECUTABLE } : {}),
  });
  const errors = [];
  let page;
  let stage = "load";
  let resolveAnswer;
  const answerReceived = new Promise((resolve) => { resolveAnswer = resolve; });
  /** 从真实SSE事件中提取业务输出，忽略心跳、进度与非问答响应。 */
  function receiveEvent(event) {
    if (event.msg !== "process_completed") return;
    for (const value of event.output?.data || []) {
      if (typeof value !== "string" || !value.startsWith("{")) continue;
      const result = JSON.parse(value);
      completed.push(result);
      if (result.data?.answer_status) resolveAnswer(result);
    }
  }
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on("pageerror", (error) => errors.push(error.name));
    // Gradio独立入口可能用fetch读取不关闭的SSE；逐块复制读取，不等待response结束。
    await page.exposeFunction("acceptanceReceiveEvent", receiveEvent);
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
                if (line.startsWith("data: ")) {
                  try {
                    await window.acceptanceReceiveEvent(JSON.parse(line.slice(6)));
                  } catch { /* 心跳和进度不是问答结果。 */ }
                }
              }
            }
          })().catch(() => {});
        }
        return response;
      };
    });
    // EventSource长连接不一定结束，CDP逐条接收事件，无需等待response.text关闭流。
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Network.enable");
    cdp.on("Network.eventSourceMessageReceived", (message) => {
      try { receiveEvent(JSON.parse(message.data)); } catch { /* 心跳不是业务输出。 */ }
    });
    // CodeMirror会虚拟化长JSON，DOM只含可见行；完整trace从浏览器真实SSE结果核验。
    page.on("response", async (response) => {
      if (!response.url().includes("/gradio_api/queue/data")) return;
      try {
        const stream = await response.text();
        for (const line of stream.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          receiveEvent(JSON.parse(line.slice(6)));
        }
      } catch {
        // 非JSON心跳或关闭的连接不作为成功证据；主流程的超时会明确失败。
      }
    });
    // 旧7860兼容入口同样复用产品UI，单独验收时不覆盖原8001的证据目录。
    await page.goto(process.env.WORKBENCH_URL || `${state.api}/product-workbench/`,
      { waitUntil: "networkidle" });
    if (state.anonymous) {
      assert.equal(await page.getByLabel("开发 Token", { exact: true }).count(), 0);
    } else {
      await page.getByLabel("开发 Token", { exact: true }).fill(token);
    }
    const identity = `ui-acceptance-${Date.now()}`;
    // 直接上传公开 Buffer，避免从私人知识库或用户文件读取正文。
    if (process.env.ACCEPTANCE_FILE) {
      // 只接收本任务公开合成样本目录，不能把任意私人文件归档为验收正文。
      const filename = path.resolve(process.env.ACCEPTANCE_FILE);
      assert(filename.startsWith(path.join(output, "samples") + path.sep));
      await page.locator('input[type="file"]').setInputFiles(filename);
    } else {
      await page.locator('input[type="file"]').setInputFiles({
        name: "ui-acceptance.md", mimeType: "text/markdown",
        buffer: Buffer.from("# 浏览器验收\n\nAurora 项目的发布冻结窗口是每周三 20:00-22:00，时区为 Asia/Shanghai。\n"),
      });
    }
    if (!state.anonymous) {
      await page.getByLabel("Source ID", { exact: true }).fill(identity);
      await page.getByLabel("标题", { exact: true }).fill("浏览器验收");
      await page.getByLabel("幂等键", { exact: true }).fill(identity);
    }
    stage = "upload";
    await page.getByRole("button", { name: "上传并发布", exact: true }).click();
    await page.waitForFunction(() => document.body.innerText.includes('"version_status": "published"'),
      null, { timeout: 150000 });
    assert((await page.getByLabel("Document ID", { exact: true }).inputValue()).startsWith("doc_"));
    const upload = completed.find((result) => result.data?.version_status === "published");
    assert(upload && upload.status_code === 201);
    fs.writeFileSync(path.join(output, `${caseName}-upload.json`), JSON.stringify(upload, null, 2) + "\n");
    console.log('{"stage":"upload","status":"passed"}');
    stage = "status";
    await page.getByRole("button", { name: "查询状态", exact: true }).click();
    await page.waitForFunction(() => document.body.innerText.includes('"document_status": "ready"'),
      null, { timeout: 30000 });
    await page.getByRole("tab", { name: "检索问答", exact: true }).click();
    stage = "query";
    await page.getByLabel("问题", { exact: true }).fill(
      process.env.ACCEPTANCE_QUESTION || "Aurora 项目的发布冻结窗口是什么时间？");
    // Gradio Slider 同时提供数值输入，设置足够的输出预算避免 JSON 被截断。
    const numericInputs = page.locator('input[type="number"]');
    assert.equal(await numericInputs.count(), 3);
    await numericInputs.nth(2).fill("1024");
    await numericInputs.nth(2).press("Tab");
    await page.getByRole("button", { name: "查询并回答", exact: true }).click();
    await page.waitForFunction(() => document.body.innerText.includes('"answer_status": "answered"'),
      null, { timeout: 150000 });
    stage = "query-trace";
    let resultTimer;
    let result;
    try {
      result = await Promise.race([
        answerReceived,
        new Promise((_, reject) => {
          resultTimer = setTimeout(() => reject(new Error("result timeout")), 30000);
        }),
      ]);
    } finally {
      clearTimeout(resultTimer);
    }
    assert.equal(result.data.citation_validation, "passed");
    assert.equal(result.data.retrieval_trace.mode, "normal");
    assert.equal(result.data.retrieval_trace.reranker_used, true);
    assert(result.data.retrieval_trace.scopes >= (state.anonymous ? 1 : 2));
    const expected = (process.env.ACCEPTANCE_EXPECT || "").split("|").filter(Boolean);
    for (const word of expected) assert(result.data.message.includes(word));
    fs.writeFileSync(path.join(output, `${caseName}-answer.json`), JSON.stringify(result, null, 2) + "\n");
    // 结果事件先于Gradio卸载loading状态，等祖先透明度恢复再截图，避免淡出动画遮住答案。
    await page.waitForFunction(() => {
      let element = [...document.querySelectorAll(".cm-editor")].pop();
      while (element) {
        if (Number(getComputedStyle(element).opacity) < 0.9) return false;
        element = element.parentElement;
      }
      return true;
    }, null, { timeout: 30000 });
    // 清空 token 后截图，截图仅包含本脚本输入和公开合成样本的回答。
    if (!state.anonymous) await page.getByLabel("开发 Token", { exact: true }).fill("");
    await page.screenshot({ path: path.join(output, `${caseName}-desktop.png`), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    // 等待响应式布局和字体重排结束；移动端只拍视口，避免超长编辑器拼接截图。
    await page.evaluate(async () => {
      await document.fonts.ready;
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    });
    assert.equal(await page.getByRole("heading", { name: "本地 P0 产品工作台" }).count(), 1);
    const horizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth + 1,
    );
    assert.equal(horizontalOverflow, false);
    await page.screenshot({ path: path.join(output, `${caseName}-mobile.png`) });
    await page.locator(".cm-editor").last().scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, `${caseName}-mobile-answer.png`) });
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, `${caseName}.json`), JSON.stringify({
      status: "passed", transport: "real_browser_http", upload_published: true,
      anonymous: state.anonymous === true, metadata_left_empty: state.anonymous === true,
      document_ready: true, answer_status: "answered", citation_validation: "passed",
      normal_reranked: true, desktop: [1440, 1000], mobile: [390, 844],
      horizontal_overflow: horizontalOverflow, page_errors: errors,
    }, null, 2) + "\n");
    console.log('{"browser_acceptance":"passed"}');
  } catch (error) {
    if (page) {
      await page.getByLabel("开发 Token", { exact: true }).fill("").catch(() => {});
      await page.screenshot({ path: path.join(output, "workbench-failed.png"), fullPage: true });
    }
    console.error(JSON.stringify({ stage, error_type: error.name }));
    throw error;
  } finally {
    await browser.close();
  }
}

// 顶层只回显异常类别，不输出浏览器字段、token 或私人网络响应。
verify().catch((error) => {
  console.error(JSON.stringify({ browser_acceptance: "failed", error_type: error.name }));
  process.exitCode = 1;
});
