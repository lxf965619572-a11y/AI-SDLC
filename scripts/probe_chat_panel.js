/* 探针：AI 助手面板 + 双侧栏收起
 * 验证点：
 * 1. 页面加载无 JS 错误
 * 2. 助手面板默认收起，点击展开/收起正常（宽度变化）
 * 3. 左侧项目栏点击 ☰ 可收起/展开
 * 4. 选中项目后 chatCtx 显示项目名
 * 5. 发送消息：用户气泡立即出现 → 流式回复最终非空（SSE）
 * 6. 清空按钮清空对话
 */
const puppeteer = require("puppeteer-core");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:5100";

let pass = 0, fail = 0;
function check(name, ok, extra = "") {
  console.log((ok ? "PASS" : "FAIL") + " | " + name + (extra ? " | " + extra : ""));
  ok ? pass++ : fail++;
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: "new",
    args: ["--no-sandbox", "--window-size=1440,900"],
    defaultViewport: { width: 1440, height: 900 },
  });
  const page = await browser.newPage();
  const jsErrors = [];
  page.on("pageerror", e => jsErrors.push(String(e)));

  await page.goto(BASE, { waitUntil: "networkidle0", timeout: 30000 });
  await sleep(1500);
  check("页面加载无 JS 错误", jsErrors.length === 0, jsErrors.join("; "));

  // 1) 面板默认收起
  const w0 = await page.$eval("#chatPanel", el => el.getBoundingClientRect().width);
  check("助手面板默认收起（宽度≈0）", w0 < 5, "width=" + w0);

  // 2) 展开面板
  await page.click("#btnToggleChat");
  await sleep(500);
  const w1 = await page.$eval("#chatPanel", el => el.getBoundingClientRect().width);
  check("点击「AI 助手」展开面板", w1 > 300, "width=" + w1);

  // 3) 侧栏收起/展开
  const sw0 = await page.$eval("#sidebar", el => el.getBoundingClientRect().width);
  await page.click("#btnToggleSidebar");
  await sleep(500);
  const sw1 = await page.$eval("#sidebar", el => el.getBoundingClientRect().width);
  await page.click("#btnToggleSidebar");
  await sleep(500);
  const sw2 = await page.$eval("#sidebar", el => el.getBoundingClientRect().width);
  check("左侧项目栏可收起/展开", sw0 > 200 && sw1 < 5 && sw2 > 200,
    `${sw0} -> ${sw1} -> ${sw2}`);

  // 4) 选中第一个项目，验证 chatCtx 更新
  await page.evaluate(() => {
    const item = document.querySelector(".project-item");
    if (item) item.click();
  });
  await sleep(2500);
  const ctxText = await page.$eval("#chatCtx", el => el.textContent);
  const hasProject = await page.evaluate(() => !!document.querySelector(".project-item.active"));
  check("选中项目后助手上下文标签更新", hasProject && ctxText.indexOf("上下文") !== -1,
    "chatCtx=" + JSON.stringify(ctxText));

  // 5) 发送消息，等待流式回复（真实模式可能较慢，最多等 90s）
  await page.focus("#chatInput");
  await page.keyboard.type("请用一句话说明你能帮我做什么");
  await page.click("#btnChatSend");
  await sleep(800);
  const userMsgCount = await page.$$eval(".chat-msg.user", els => els.length);
  check("发送后出现用户气泡", userMsgCount === 1, "user msgs=" + userMsgCount);

  let replyOk = false, replyLen = 0, waited = 0;
  while (waited < 90000) {
    const state = await page.evaluate(() => {
      const msgs = document.querySelectorAll(".chat-msg.assistant");
      if (!msgs.length) return { len: 0, busy: true, err: false };
      const last = msgs[msgs.length - 1];
      const busy = !!last.querySelector(".chat-cursor");
      const err = last.closest(".chat-msg").classList.contains("error");
      return { len: last.textContent.trim().length, busy, err };
    });
    replyLen = state.len;
    if (!state.busy) { replyOk = !state.err && state.len > 0; break; }
    await sleep(1500);
    waited += 1500;
  }
  check("流式回复完成且内容非空", replyOk, "len=" + replyLen);

  // 6) 清空
  await page.click("#btnChatClear");
  await sleep(300);
  const afterClear = await page.$$eval(".chat-msg", els => els.length);
  check("清空后无残留消息", afterClear === 0, "msgs=" + afterClear);

  // 7) 收起面板
  await page.click("#btnChatCollapse");
  await sleep(500);
  const w2 = await page.$eval("#chatPanel", el => el.getBoundingClientRect().width);
  check("✕ 收起助手面板", w2 < 5, "width=" + w2);

  check("全程无 JS 错误", jsErrors.length === 0, jsErrors.join("; "));

  console.log(`\n=== 结果: ${pass} PASS / ${fail} FAIL ===`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error("PROBE ERROR:", e); process.exit(2); });
