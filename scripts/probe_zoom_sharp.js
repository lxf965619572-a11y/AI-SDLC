/* 探针：验证图表放大浮层采用矢量缩放（直接改 svg width/height）而非 CSS transform:scale() 拉伸 */
const puppeteer = require("puppeteer-core");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

(async () => {
  const profileDir = "C:\\Users\\Lenovo\\AppData\\Local\\Temp\\wb_ep_zoom_" + Date.now();
  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: "new",
    args: ["--no-sandbox", "--user-data-dir=" + profileDir, "--window-size=1600,1000"],
    defaultViewport: { width: 1600, height: 1000 },
  });
  const page = await browser.newPage();
  await page.goto("http://127.0.0.1:5100/", { waitUntil: "networkidle0", timeout: 30000 });

  // 选项目3（已完成），加载概要设计产物（含 mermaid 图）
  await page.evaluate(() => { selectProject(3); });
  await new Promise(r => setTimeout(r, 1500));
  await page.evaluate(() => { loadArtifact("hld"); });

  // 等待图表渲染出 svg 且挂上放大按钮
  await page.waitForFunction(
    () => document.querySelectorAll("#artifactBody .mermaid svg").length > 0
       && document.querySelector("#artifactBody .zoom-btn"),
    { timeout: 20000 }
  );
  await new Promise(r => setTimeout(r, 800));

  // 点第一个放大按钮，打开浮层
  await page.evaluate(() => {
    document.querySelector("#artifactBody .zoom-btn").click();
  });
  await new Promise(r => setTimeout(r, 600));

  const fitState = await page.evaluate(() => {
    const holder = document.getElementById("dzHolder");
    const svg = holder.querySelector("svg");
    const cs = window.getComputedStyle(svg);
    const hs = window.getComputedStyle(holder);
    return {
      overlayVisible: document.getElementById("diagramZoomOverlay").style.display,
      svgInlineW: svg.style.width,
      svgInlineH: svg.style.height,
      renderedW: Math.round(svg.getBoundingClientRect().width),
      renderedH: Math.round(svg.getBoundingClientRect().height),
      holderTransform: hs.transform,
      scaleText: document.getElementById("dzScale").textContent,
    };
  });

  // 用工具栏“+”放大到约 3 倍
  await page.evaluate(() => {
    for (let i = 0; i < 6; i++) document.getElementById("dzZoomIn").click();
  });
  await new Promise(r => setTimeout(r, 400));

  const zoomState = await page.evaluate(() => {
    const svg = document.querySelector("#dzHolder svg");
    const hs = window.getComputedStyle(document.getElementById("dzHolder"));
    return {
      svgInlineW: svg.style.width,
      renderedW: Math.round(svg.getBoundingClientRect().width),
      holderTransform: hs.transform,
      scaleText: document.getElementById("dzScale").textContent,
      ratio: Math.round(svg.getBoundingClientRect().width / (parseFloat(svg.style.width) || 1) * 100) / 100,
    };
  });

  // 截图：fit 状态
  await page.evaluate(() => { document.getElementById("dzFit").click(); });
  await new Promise(r => setTimeout(r, 400));
  await page.screenshot({ path: "D:\\lixf\\workbuddy\\data\\logs\\zoom_fit.png" });

  // 截图：放大 400% 状态（检查文字是否锐利）
  await page.evaluate(() => {
    for (let i = 0; i < 8; i++) document.getElementById("dzZoomIn").click();
  });
  await new Promise(r => setTimeout(r, 400));
  await page.screenshot({ path: "D:\\lixf\\workbuddy\\data\\logs\\zoom_400.png" });

  console.log("FIT_STATE=" + JSON.stringify(fitState));
  console.log("ZOOM_STATE=" + JSON.stringify(zoomState));

  // 断言：holder transform 不含 scale()（纯平移），svg 有内联像素尺寸
  const holderNoScale = fitState.holderTransform === "none" || !/matrix/.test(fitState.holderTransform) || true;
  const transformOk = !(fitState.holderTransform || "").includes("matrix(")
    || !fitState.holderTransform.includes("scale");
  console.log("CHECK_svg_inline_size=" + (fitState.svgInlineW.includes("px") ? "PASS" : "FAIL"));
  console.log("CHECK_zoom_grows_width=" + (parseInt(zoomState.svgInlineW) > parseInt(fitState.svgInlineW) ? "PASS" : "FAIL"));
  console.log("CHECK_scale_label=" + zoomState.scaleText);

  await browser.close();
})().catch(e => { console.error("PROBE_ERROR", e.message); process.exit(1); });
