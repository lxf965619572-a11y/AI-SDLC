/* 探针：验证 P1 改造——阶段定义来自后端 /api/meta、动态轮询按状态变速、项目切换正常 */
const puppeteer = require("puppeteer-core");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

(async () => {
  const profileDir = "C:\\Users\\Lenovo\\AppData\\Local\\Temp\\wb_ep_p1_" + Date.now();
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: "new",
    args: ["--no-sandbox", "--user-data-dir=" + profileDir, "--window-size=1600,1000"],
    defaultViewport: { width: 1600, height: 1000 },
  });
  const page = await browser.newPage();
  const errs = [];
  page.on("pageerror", e => errs.push(String(e.message)));

  await page.goto("http://127.0.0.1:5100/", { waitUntil: "networkidle0", timeout: 30000 });
  await new Promise(r => setTimeout(r, 1200));

  // 1) meta 已拉取：全局 STAGES/STAGE_NAMES 已被后端数据覆盖
  const metaCheck = await page.evaluate(() => ({
    stages: typeof STAGES !== "undefined" ? STAGES : null,
    nameOfParse: typeof STAGE_NAMES !== "undefined" ? STAGE_NAMES["parse"] : null,
  }));

  // 2) 选项目3（已完成）：阶段轨道渲染 + 轮询间隔应为 POLL_IDLE(15000)
  await page.evaluate(() => selectProject(3));
  await new Promise(r => setTimeout(r, 2000));
  const idleCheck = await page.evaluate(() => ({
    interval: typeof pollInterval !== "undefined" ? pollInterval : null,
    nodes: document.querySelectorAll("#stageTrack .stage-node").length,
    artVisible: document.getElementById("artifactCard").style.display,
  }));

  // 3) 切项目4（运行中）：轮询间隔应切到 POLL_FAST(2500)
  await page.evaluate(() => selectProject(4));
  await new Promise(r => setTimeout(r, 2000));
  const fastCheck = await page.evaluate(() => ({
    interval: typeof pollInterval !== "undefined" ? pollInterval : null,
    nodes: document.querySelectorAll("#stageTrack .stage-node").length,
    name: document.getElementById("projName").textContent,
  }));

  // 4) 等待一轮轮询，确认轮询循环正常（无重复调度、无报错）
  await new Promise(r => setTimeout(r, 4000));
  const loopCheck = await page.evaluate(() => ({
    timerExists: typeof pollTimer !== "undefined" && pollTimer !== null,
    nodes: document.querySelectorAll("#stageTrack .stage-node").length,
    name: document.getElementById("projName").textContent,
  }));

  console.log("META=" + JSON.stringify(metaCheck));
  console.log("IDLE_PROJECT3=" + JSON.stringify(idleCheck));
  console.log("FAST_PROJECT4=" + JSON.stringify(fastCheck));
  console.log("LOOP=" + JSON.stringify(loopCheck));
  console.log("PAGE_ERRORS=" + JSON.stringify(errs));

  const checks = [];
  checks.push(["后端阶段定义已拉取(含parse共5阶段)",
    metaCheck.stages && metaCheck.stages.length === 5 && metaCheck.stages[0] === "parse"]);
  checks.push(["阶段中文名映射正确", metaCheck.nameOfParse === "\u7ED3\u6784\u5316\u539F\u59CB\u6570\u636E"]);
  checks.push(["已完成项目轮询=15000ms", idleCheck.interval === 15000]);
  checks.push(["项目3阶段轨道渲染(5节点)", idleCheck.nodes === 5]);
  checks.push(["项目3自动展示产物", idleCheck.artVisible === "block"]);
  checks.push(["运行中项目轮询=2500ms", fastCheck.interval === 2500]);
  checks.push(["项目4阶段轨道渲染", fastCheck.nodes === 5]);
  checks.push(["切换后项目名更新", fastCheck.name !== "\u661F\u8F7D\u8BA1\u7B97\u673A\u8F6F\u4EF6"]);
  checks.push(["轮询定时器持续运行", loopCheck.timerExists === true]);
  checks.push(["无页面JS错误", errs.length === 0]);
  let pass = 0;
  for (const [name, ok] of checks) { console.log((ok ? "PASS " : "FAIL ") + name); if (ok) pass++; }
  console.log("RESULT=" + pass + "/" + checks.length);
  await browser.close();
  process.exit(pass === checks.length ? 0 : 1);
})().catch(e => { console.error("PROBE_ERROR", e.message); process.exit(2); });
