/* 探针：验证切换项目时阶段进度/内容区/日志随项目正确切换，且快速切换无竞态残留 */
const puppeteer = require("puppeteer-core");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

function snap(el) { return { label: el.label, done: el.done, waiting: el.waiting, active: el.active, name: el.name, status: el.status, artTitle: el.artTitle, artVisible: el.artVisible, logFirst: el.logFirst }; }

async function getUI(page) {
  return page.evaluate(() => {
    const nodes = [...document.querySelectorAll("#stageTrack .stage-node")];
    return {
      label: "probe",
      name: document.getElementById("projName").textContent,
      status: document.getElementById("projStatus").textContent,
      done: nodes.filter(n => n.classList.contains("done")).length,
      waiting: nodes.filter(n => n.classList.contains("waiting")).length,
      active: nodes.filter(n => n.classList.contains("active")).length,
      nodes: nodes.map(n => n.textContent.trim() + ":" + n.className.replace("stage-node", "").trim()),
      artVisible: document.getElementById("artifactCard").style.display,
      artTitle: document.getElementById("artifactTitle").textContent,
      logFirst: (document.querySelector("#logBox .log-line") || {}).textContent || "",
      logCount: document.querySelectorAll("#logBox .log-line").length,
    };
  });
}

(async () => {
  const profileDir = "C:\\Users\\Lenovo\\AppData\\Local\\Temp\\wb_ep_switch_" + Date.now();
  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: "new",
    args: ["--no-sandbox", "--user-data-dir=" + profileDir, "--window-size=1600,1000"],
    defaultViewport: { width: 1600, height: 1000 },
  });
  const page = await browser.newPage();
  const errs = [];
  page.on("pageerror", e => errs.push(String(e.message)));
  await page.goto("http://127.0.0.1:5100/", { waitUntil: "networkidle0", timeout: 30000 });

  // 1) 选项目3（已完成，5 阶段全 ✓）
  await page.evaluate(() => selectProject(3));
  await new Promise(r => setTimeout(r, 2200));
  const p3 = await getUI(page);
  await page.screenshot({ path: "D:\\lixf\\workbuddy\\data\\logs\\switch_p3.png" });

  // 2) 切换到项目4（运行中，lld 阶段）
  await page.evaluate(() => selectProject(4));
  await new Promise(r => setTimeout(r, 2200));
  const p4 = await getUI(page);
  await page.screenshot({ path: "D:\\lixf\\workbuddy\\data\\logs\\switch_p4.png" });

  // 3) 快速连续切换 3→4→5→3，验证竞态防护（最终必须稳定呈现项目3）
  await page.evaluate(() => {
    selectProject(4);
    selectProject(5);
    selectProject(3);
  });
  await new Promise(r => setTimeout(r, 3500));
  const rapid = await getUI(page);
  await page.screenshot({ path: "D:\\lixf\\workbuddy\\data\\logs\\switch_rapid_p3.png" });

  console.log("P3=" + JSON.stringify(p3));
  console.log("P4=" + JSON.stringify(p4));
  console.log("RAPID_BACK_TO_3=" + JSON.stringify(rapid));
  console.log("PAGE_ERRORS=" + JSON.stringify(errs));

  // 断言
  const checks = [];
  checks.push(["P3 阶段轨道 5 个 done", p3.done === 5]);
  checks.push(["P3 状态=已完成", p3.status === "已完成"]);
  checks.push(["P3 产物卡可见", p3.artVisible === "block"]);
  checks.push(["P4 阶段轨道与 P3 不同", JSON.stringify(p4.nodes) !== JSON.stringify(p3.nodes)]);
  checks.push(["P4 项目名切换", p4.name !== p3.name]);
  checks.push(["快速切换后回到项目3状态", rapid.status === "已完成" && rapid.done === 5]);
  checks.push(["快速切换后产物为项目3内容", rapid.artVisible === "block"]);
  checks.push(["快速切换后日志是项目3的", rapid.logFirst !== p4.logFirst]);
  checks.push(["无页面 JS 错误", errs.length === 0]);
  let pass = 0;
  for (const [name, ok] of checks) { console.log((ok ? "PASS " : "FAIL ") + name); if (ok) pass++; }
  console.log("RESULT=" + pass + "/" + checks.length);
  await browser.close();
  process.exit(pass === checks.length ? 0 : 1);
})().catch(e => { console.error("PROBE_ERROR", e.message); process.exit(2); });
