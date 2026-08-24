/* 探针：联网检索开关（方案 B）+ AI 助手回归
 * 验证点：
 * 1. 未配置 WEB_SEARCH_API_KEY 时：/api/meta 返回 web_search_enabled=false，
 *    前端「🌐 联网」开关不显示（配置级总开关）
 * 2. 开关元素存在但默认隐藏（display:none）
 * 3. 联网开关默认未勾选（单次开关默认关）
 * 4. 回归：助手面板展开、发送消息、流式回复正常（BM25 检索静默生效）
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

  // 1) meta 接口：无 key → web_search_enabled=false
  const meta = await page.evaluate(() => fetch("/api/meta").then(r => r.json()));
  check("meta: 未配置 key 时 web_search_enabled=false",
    meta.web_search_enabled === false, "value=" + meta.web_search_enabled);

  // 2) 开关元素存在但隐藏
  const swState = await page.$eval("#webSwitchWrap", el => ({
    exists: true,
    display: getComputedStyle(el).display,
  }));
  check("联网开关元素存在且默认隐藏",
    swState.exists && swState.display === "none", "display=" + swState.display);

  // 3) 开关默认未勾选
  const checked = await page.$eval("#chatWebSwitch", el => el.checked);
  check("联网开关默认关闭（未勾选）", checked === false, "checked=" + checked);

  // 4) 回归：展开面板 → 选项目 → 发送消息 → 流式回复
  await page.click("#btnToggleChat");
  await sleep(500);
  await page.evaluate(() => {
    const item = document.querySelector(".project-item");
    if (item) item.click();
  });
  await sleep(2500);
  await page.focus("#chatInput");
  await page.keyboard.type("你好，请用一句话自我介绍");
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

  check("全程无 JS 错误", jsErrors.length === 0, jsErrors.join("; "));

  console.log(`\n=== 结果: ${pass} PASS / ${fail} FAIL ===`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error("PROBE ERROR:", e); process.exit(2); });
