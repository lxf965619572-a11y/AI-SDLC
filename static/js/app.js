/* 多智能体软件开发流水线 - 前端逻辑 */

/* 阶段定义以后端 /api/meta 为单一数据源；以下为兜底默认值（拉取失败时使用） */
let STAGES = ["parse", "requirement", "hld", "lld", "testcase"];
let STAGE_NAMES = {
  parse: "结构化原始数据",
  requirement: "软件需求规格说明书",
  hld: "概要设计说明书",
  lld: "详细设计说明书",
  testcase: "测试用例设计",
};
const STATUS_TEXT = {
  created: "待启动", parsing: "解析中", running: "执行中",
  waiting_review: "等待评审", completed: "已完成", failed: "已失败",
};

let currentProjectId = null;
let currentArtifactStage = null;
let pinnedStage = null;        // 用户手动查看的阶段（pin 期间轮询不自动切换）
let pinnedVersion = null;      // pin 时查看的版本号（同阶段出新版时提示）
let renderToken = 0;           // 项目切换令牌：切换瞬间在途的旧项目异步回包凭此作废，防止覆盖新项目画面

/* ---------- 图表主题：科技感（暗色+动效） / 经典（白底静态，适合插入文档） ---------- */

const TECH_INIT = {
  startOnLoad: false,
  theme: "base",
  securityLevel: "loose",
  flowchart: { curve: "basis", nodeSpacing: 50, rankSpacing: 60, htmlLabels: true },
  sequence: { mirrorActors: false },
  themeVariables: {
    /* 科技感暗色主题 */
    darkMode: true,
    background: "#0b1220",
    primaryColor: "#0f2540",
    primaryTextColor: "#dbeafe",
    primaryBorderColor: "#38bdf8",
    lineColor: "#67e8f9",
    secondaryColor: "#132c4d",
    tertiaryColor: "#0d1b2e",
    fontFamily: '"Segoe UI", "Microsoft YaHei", system-ui, sans-serif',
    fontSize: "13px",
    edgeLabelBackground: "#0b1220",
    clusterBkg: "#0d1b33",
    clusterBorder: "#1e3a5f",
    titleColor: "#e0f2fe",
    actorBkg: "#0f2540", actorBorder: "#38bdf8", actorTextColor: "#dbeafe",
    actorLineColor: "#334155",
    signalColor: "#67e8f9", signalTextColor: "#dbeafe",
    noteBkgColor: "#1e3a5f", noteTextColor: "#bae6fd", noteBorderColor: "#38bdf8",
    classText: "#dbeafe",
    fillType0: "#0f2540", fillType1: "#132c4d", fillType2: "#0d1b2e",
  },
};

const CLASSIC_INIT = {
  startOnLoad: false,
  theme: "default",   /* mermaid 官方浅色主题，白底黑字，适合导出/插入文档 */
  securityLevel: "loose",
  flowchart: { curve: "basis", nodeSpacing: 50, rankSpacing: 60, htmlLabels: true },
  sequence: { mirrorActors: false },
  themeVariables: {
    fontFamily: '"Segoe UI", "Microsoft YaHei", system-ui, sans-serif',
    fontSize: "13px",
  },
};

let diagramTheme = "tech";
try { diagramTheme = localStorage.getItem("diagramTheme") === "classic" ? "classic" : "tech"; } catch (e) {}
const REDUCE_MOTION = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function applyMermaidInit() {
  /* 重新 initialize 会向 head 追加新主题的样式表，位置靠后 → 同优先级下覆盖旧主题残留 */
  mermaid.initialize(diagramTheme === "tech" ? TECH_INIT : CLASSIC_INIT);
}
applyMermaidInit();

/* 连线光晕色 = 科技感容器背景色，让交叉/贴边的线视觉分离 */
const HALO_COLOR = "#0b1220";

function updateDiagramThemeUI() {
  const bc = document.getElementById("btnThemeClassic");
  const bt = document.getElementById("btnThemeTech");
  if (bc) bc.classList.toggle("active", diagramTheme === "classic");
  if (bt) bt.classList.toggle("active", diagramTheme === "tech");
}

function renderMarkdown(md) {
  const body = document.getElementById("artifactBody");
  body.innerHTML = marked.parse(md || "");
  // 将 ```mermaid 代码块转为 mermaid 容器；dataset.src 保存源码供主题切换时重渲染
  body.querySelectorAll("pre code.language-mermaid, pre code[class*=language-mermaid]").forEach(code => {
    const div = document.createElement("div");
    div.className = "mermaid" + (diagramTheme === "classic" ? " mermaid-classic" : "");
    div.dataset.src = code.textContent;
    div.textContent = code.textContent;
    code.closest("pre").replaceWith(div);
  });
  runDiagrams(Array.from(body.querySelectorAll(".mermaid")));
}

/* 渲染一批 mermaid 容器；仅科技感主题做增强。
   注意：mermaid.run 在同一页面二次调用时会复用上一次的临时元素导致图形混杂，
   因此改用 mermaid.render + 全局唯一 id 自行写入 svg。 */
let renderSeq = 0;
async function runDiagrams(nodes) {
  if (!nodes || !nodes.length) return;
  for (const c of nodes) {
    const src = c.dataset.src || c.textContent;
    const id = "wbm-" + Date.now().toString(36) + "-" + (++renderSeq);
    try {
      const { svg } = await mermaid.render(id, src);
      c.innerHTML = svg;
    } catch (e) {
      console.warn("mermaid:", e);
      window.__diagErr = (window.__diagErr || "") + "mermaid.render:" + e.message + "; ";
      const tmp = document.getElementById(id);
      if (tmp) tmp.remove();
      c.innerHTML = `<pre style="text-align:left;color:#b91c1c">图表渲染失败：${e.message}</pre>`;
      continue;
    }
    // svg 已写入；科技感主题立即增强（轮询兜底以防异步时序）
    if (diagramTheme === "tech") enhanceWhenReady(c);
    attachZoomButton(c);
  }
}

/* 图表容器右上角挂"放大"按钮，点击进入全屏查看（可缩放/平移） */
function attachZoomButton(container) {
  if (container.querySelector(".zoom-btn")) return;
  const btn = document.createElement("button");
  btn.className = "zoom-btn";
  btn.textContent = "⤢ 放大";
  btn.title = "放大查看（可缩放、拖动）";
  btn.onclick = e => { e.stopPropagation(); openDiagramZoom(container); };
  container.appendChild(btn);
}

/* ---------- 图表全屏放大查看 ----------
 * 清晰度关键：不用 CSS transform:scale() 缩放（那会把 SVG 先栅格化成位图再拉伸，
 * 放大后模糊），而是直接改 svg 的 width/height —— SVG 是矢量，每次尺寸变化都
 * 按目标像素重新绘制，任意倍率下线条和文字都锐利。
 */
const dzState = { scale: 1, tx: 0, ty: 0, natW: 800, natH: 600, fit: 1 };

function dzApply() {
  const holder = document.getElementById("dzHolder");
  const svg = holder && holder.querySelector("svg");
  if (!svg) return;
  // 矢量缩放：按当前倍率直接设置渲染尺寸
  svg.style.width = (dzState.natW * dzState.scale) + "px";
  svg.style.height = (dzState.natH * dzState.scale) + "px";
  holder.style.transform = `translate(calc(-50% + ${dzState.tx}px), calc(-50% + ${dzState.ty}px))`;
  document.getElementById("dzScale").textContent = Math.round(dzState.scale * 100) + "%";
}

/* 让图表以 90% 视口尺寸完整显示（fit） */
function dzFit() {
  const vp = document.getElementById("dzViewport");
  if (!vp.clientWidth || !vp.clientHeight) return;
  const fit = Math.min((vp.clientWidth * 0.92) / dzState.natW,
                       (vp.clientHeight * 0.9) / dzState.natH);
  dzState.fit = fit;
  dzState.scale = Math.max(0.05, Math.min(fit, 16));
  dzState.tx = 0; dzState.ty = 0;
  dzApply();
}

/* 读取 svg 自然尺寸：优先 viewBox，其次 width 属性，兜底测量渲染尺寸 */
function dzMeasure(svg) {
  let w = 0, h = 0;
  const vb = svg.viewBox && svg.viewBox.baseVal;
  if (vb && vb.width > 0 && vb.height > 0) { w = vb.width; h = vb.height; }
  if (!w || !h) {
    const aw = parseFloat(svg.getAttribute("width")) || 0;
    const ah = parseFloat(svg.getAttribute("height")) || 0;
    if (aw > 0 && ah > 0) { w = aw; h = ah; }
  }
  if (!w || !h) {
    const r = svg.getBoundingClientRect();
    w = r.width || 800; h = r.height || 600;
  }
  return { w, h };
}

function openDiagramZoom(container) {
  const svg = container.querySelector("svg");
  if (!svg) { toast("图表尚未渲染完成", true); return; }
  const overlay = document.getElementById("diagramZoomOverlay");
  const holder = document.getElementById("dzHolder");
  holder.innerHTML = "";
  // 克隆 svg（保留内联样式/滤镜），并给浮层里的 svg 解除 max-width 限制
  const clone = svg.cloneNode(true);
  clone.style.maxWidth = "none";
  clone.style.maxHeight = "none";
  clone.style.background = "transparent";
  clone.removeAttribute("height");
  holder.appendChild(clone);
  const m = dzMeasure(clone);
  dzState.natW = m.w; dzState.natH = m.h;
  // 标题：取容器前最近的标题文本
  let title = "图表预览";
  let prev = container.previousElementSibling;
  while (prev) {
    if (/^H[1-6]$/.test(prev.tagName)) { title = prev.textContent; break; }
    prev = prev.previousElementSibling;
  }
  document.getElementById("dzTitle").textContent = title;
  overlay.style.display = "flex";
  dzState.scale = 1; dzState.tx = 0; dzState.ty = 0;
  dzFit();
}

function closeDiagramZoom() {
  document.getElementById("diagramZoomOverlay").style.display = "none";
  document.getElementById("dzHolder").innerHTML = "";
}

(function initDiagramZoom() {
  const overlay = document.getElementById("diagramZoomOverlay");
  const vp = document.getElementById("dzViewport");
  document.getElementById("dzClose").onclick = closeDiagramZoom;
  document.getElementById("dzFit").onclick = dzFit;
  // “1:1”= 按图的自然像素尺寸显示
  document.getElementById("dzReset").onclick = () => { dzState.scale = 1; dzState.tx = 0; dzState.ty = 0; dzApply(); };
  document.getElementById("dzZoomIn").onclick = () => { dzState.scale = Math.min(16, dzState.scale * 1.25); dzApply(); };
  document.getElementById("dzZoomOut").onclick = () => { dzState.scale = Math.max(0.05, dzState.scale / 1.25); dzApply(); };
  overlay.addEventListener("click", e => { if (e.target === overlay) closeDiagramZoom(); });
  document.addEventListener("keydown", e => {
    if (overlay.style.display !== "none" && overlay.style.display !== "") {
      if (e.key === "Escape") closeDiagramZoom();
      if (e.key === "+") { dzState.scale = Math.min(16, dzState.scale * 1.25); dzApply(); }
      if (e.key === "-") { dzState.scale = Math.max(0.05, dzState.scale / 1.25); dzApply(); }
    }
  });
  // 滚轮缩放：以鼠标位置为中心（指哪放大哪，便于细看复杂图）
  vp.addEventListener("wheel", e => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    zoomAt(e.clientX, e.clientY, factor);
  }, { passive: false });
  // 拖动平移
  let dragging = false, sx = 0, sy = 0, ox = 0, oy = 0;
  vp.addEventListener("mousedown", e => {
    dragging = true; vp.classList.add("dragging");
    sx = e.clientX; sy = e.clientY; ox = dzState.tx; oy = dzState.ty;
  });
  window.addEventListener("mousemove", e => {
    if (!dragging) return;
    dzState.tx = ox + (e.clientX - sx);
    dzState.ty = oy + (e.clientY - sy);
    dzApply();
  });
  window.addEventListener("mouseup", () => { dragging = false; vp.classList.remove("dragging"); });
  vp.addEventListener("dblclick", dzFit);
  // 窗口尺寸变化时重新适应
  window.addEventListener("resize", () => {
    if (overlay.style.display === "flex") dzFit();
  });
})();

/* 以视口内某一点 (cx, cy) 为中心缩放，保持该点下的内容不动 */
function zoomAt(cx, cy, factor) {
  const vp = document.getElementById("dzViewport");
  const rect = vp.getBoundingClientRect();
  const s1 = dzState.scale;
  const s2 = Math.max(0.05, Math.min(16, s1 * factor));
  if (s2 === s1) return;
  const vcx = rect.left + rect.width / 2;
  const vcy = rect.top + rect.height / 2;
  const k = s2 / s1;
  dzState.tx = (cx - vcx) * (1 - k) + dzState.tx * k;
  dzState.ty = (cy - vcy) * (1 - k) + dzState.ty * k;
  dzState.scale = s2;
  dzApply();
}

/* 主题切换：记忆偏好 → 重新 initialize → 用保存的源码重渲染页面内所有图表 */
function setDiagramTheme(theme) {
  if (theme !== "classic" && theme !== "tech") return;
  if (theme === diagramTheme) { updateDiagramThemeUI(); return; }
  diagramTheme = theme;
  try { localStorage.setItem("diagramTheme", theme); } catch (e) {}
  applyMermaidInit();
  updateDiagramThemeUI();
  const nodes = [];
  document.querySelectorAll(".mermaid").forEach(c => {
    const src = c.dataset.src;
    if (!src) return;
    /* 换新容器元素重渲染，避免复用旧容器时 mermaid 内部状态错乱 */
    const fresh = document.createElement("div");
    fresh.className = "mermaid" + (diagramTheme === "classic" ? " mermaid-classic" : "");
    fresh.dataset.src = src;
    fresh.textContent = src;
    c.replaceWith(fresh);
    nodes.push(fresh);
  });
  runDiagrams(nodes);
}

/* 轮询等待容器内出现渲染好的 svg 后执行科技感增强 */
function enhanceWhenReady(container) {
  const start = Date.now();
  const timer = setInterval(() => {
    if (container.querySelector("svg")) {
      clearInterval(timer);
      try {
        enhanceTechDiagram(container);
        container.dataset.enhanced = "1";
      } catch (e) {
        console.warn("diagram enhance failed:", e);
        window.__diagErr = (window.__diagErr || "") + "enhance:" + e.message + "; ";
      }
    } else if (Date.now() - start > 15000) {
      clearInterval(timer);
    }
  }, 200);
}

/* 科技感增强：连线光晕（避免与元素/其他线视觉重合）+ 流动虚线（数据流向动画）
   注意：mermaid 会向页面注入样式类（edge-pattern-solid / edge-thickness-normal），
   覆盖 class 级 CSS，因此这里一律使用内联样式。 */
function enhanceTechDiagram(container) {
  const svg = container.querySelector("svg");
  if (!svg) return;

  // 1) 连线光晕：在每条连线下方垫一条背景色宽描边，
  //    线与元素贴边、线与线交叉时都能清晰分离
  const edgeSel = ".flowchart-link, path.path, .messageLine0, .messageLine1, .relation";
  svg.querySelectorAll(edgeSel).forEach(p => {
    if (p.classList.contains("flow-halo") || p.closest("marker")) return;
    const halo = p.cloneNode(false);
    halo.classList.add("flow-halo");
    /* 内联样式优先级高于 mermaid 注入的类样式 */
    halo.style.stroke = HALO_COLOR;
    halo.style.strokeWidth = "7";
    halo.style.fill = "none";
    halo.removeAttribute("marker-start");
    halo.removeAttribute("marker-end");
    halo.removeAttribute("filter");
    p.parentNode.insertBefore(halo, p);
  });

  // 2) 流动虚线动画：数据流向可视化（错峰启动，方向感更强）
  //    语义保护：
  //    - 序列图（含 .messageLine0）：整体保持静态，不做流动动画（用户要求）。
  //    - 类图（含 .relation）：实线关系（edge-pattern-solid）保持静态；
  //      仅虚线关系（edge-pattern-dashed，本来就是虚线）做流动动画。
  const isClass = !!svg.querySelector("path.relation");
  const isSeq = !!svg.querySelector(".messageLine0");
  let i = 0;
  if (!isSeq) {
    svg.querySelectorAll(edgeSel).forEach(p => {
      if (p.classList.contains("flow-halo")) return;
      if (isClass && p.classList.contains("edge-pattern-solid")) return;  // 实线关系保持静态
      p.classList.add("flow-dash");
      p.style.strokeDasharray = "7 5";
      if (!REDUCE_MOTION) {
        p.style.animation = "flowDashMove 1.1s linear infinite";
        p.style.animationDelay = `${(i % 7) * 0.22}s`;
      }
      i++;
    });
  }

  // 3) 发光滤镜：节点与连线描边带霓虹光感
  let defs = svg.querySelector("defs");
  if (!defs) { defs = document.createElementNS("http://www.w3.org/2000/svg", "defs"); svg.insertBefore(defs, svg.firstChild); }
  if (!defs.querySelector("#techGlow")) {
    defs.insertAdjacentHTML("beforeend",
      `<filter id="techGlow" x="-40%" y="-40%" width="180%" height="180%">
         <feDropShadow dx="0" dy="0" stdDeviation="2.2" flood-color="#38bdf8" flood-opacity="0.55"/>
       </filter>
       <filter id="techGlowSoft" x="-40%" y="-40%" width="180%" height="180%">
         <feDropShadow dx="0" dy="0" stdDeviation="1.4" flood-color="#67e8f9" flood-opacity="0.5"/>
       </filter>`);
  }
  svg.querySelectorAll(".flowchart-link, path.path, .messageLine0, .messageLine1, .relation")
    .forEach(p => {
      if (p.classList.contains("flow-halo")) return;
      // 关键：直线段（垂直线 bbox 宽=0 / 水平线 bbox 高=0）套用 feDropShadow 百分比滤镜时
      // 滤镜区域会退化为零，整条线连同箭头被裁剪消失。这类线跳过发光，保持原样。
      try {
        const bb = p.getBBox();
        if (bb.width < 1 || bb.height < 1) return;
      } catch (e) { return; }
      p.setAttribute("filter", "url(#techGlowSoft)");
    });
  svg.querySelectorAll(".node .label-container, .node rect, .node polygon, .node circle, .node ellipse, .actor, .classGroup rect, .note")
    .forEach(el => { if (!el.getAttribute("filter")) el.setAttribute("filter", "url(#techGlow)"); });

  // 4) 箭头同色发光
  //    UML 类图 & 序列图跳过：类图箭头含空心三角形（继承/实现，fill=none 靠描边显形），
  //    序列图含开放箭头（回复），统一改填充会破坏箭头语义；保持 mermaid 原始箭头形状与颜色。
  if (!svg.querySelector("path.relation") && !svg.querySelector(".messageLine0")) {
    svg.querySelectorAll("marker path").forEach(mp => {
      mp.setAttribute("stroke", "none");
      const fill = mp.getAttribute("fill") || "";
      if (!fill || fill === "none" || fill === "transparent") mp.setAttribute("fill", "#67e8f9");
    });
  }
}
async function api(path, options = {}) {
  const resp = await fetch(path, options);
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { msg = (await resp.json()).error || msg; } catch (e) {}
    throw new Error(msg);
  }
  return resp.json();
}

function toast(msg, isError = false) {
  const el = document.createElement("div");
  el.style.cssText = `position:fixed;top:20px;right:20px;z-index:999;padding:12px 20px;
    border-radius:8px;color:#fff;font-size:14px;box-shadow:0 4px 12px rgba(0,0,0,.2);
    background:${isError ? "#dc2626" : "#16a34a"};`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

/* ---------- 项目列表 ---------- */
async function loadProjects() {
  const projects = await api("/api/projects");
  const list = document.getElementById("projectList");
  list.innerHTML = "";
  projects.forEach(p => {
    const div = document.createElement("div");
    div.className = "project-item" + (p.id === currentProjectId ? " active" : "");
    const busy = p.status === "running" || p.status === "parsing";
    div.innerHTML = `<div class="pi-top">
        <div class="pi-name">${escapeHtml(p.name)}</div>
        <button class="pi-del" aria-label="删除项目 ${escapeHtml(p.name)}"
          title="${busy ? "流水线运行中，取消并停止后才能删除" : "删除项目"}"${
          busy ? " disabled" : ""}>🗑</button>
      </div>
      <div class="pi-meta">
        <span class="status-pill st-${p.status}" style="padding:1px 8px;font-size:11px">${STATUS_TEXT[p.status] || p.status}</span>
        <span>${p.created_at}</span>
      </div>`;
    div.onclick = () => selectProject(p.id);
    // 整行是可点的（切换项目），删除按钮必须拦掉冒泡，否则删完还顺带切一次项目
    div.querySelector(".pi-del").onclick = e => {
      e.stopPropagation();
      deleteProject(p.id, p.name);
    };
    list.appendChild(div);
  });
  return projects;
}

/* 删除项目：库内记录、检查点、磁盘上的导出件与验证证据一并清掉，不可恢复。
 * 删的正是当前项目时，还要把详情区、轮询与 SSE 一起收干净再回空状态，
 * 否则界面上会留着一个已经不存在的项目。 */
async function deleteProject(pid, name) {
  if (!confirm(`删除项目「${name}」？\n\n上传文档、各阶段产物、评审记录、导出件与验证证据会一并删除，且不可恢复。`)) return;
  let res;
  try {
    res = await api(`/api/projects/${pid}`, { method: "DELETE" });
  } catch (e) {
    toast("删除失败：" + e.message, true);
    loadProjects().catch(() => {});   // 可能刚被启动：刷新徽标与按钮禁用态
    return;
  }
  const r = (res && res.removed) || {};
  toast(`项目「${name}」已删除（产物 ${r.artifacts || 0} 份 / 文档 ${r.documents || 0} 份）`);
  delete lastStatusByProject[pid];
  if (currentProjectId !== pid) { await loadProjects(); return; }

  currentProjectId = null;
  renderToken++;               // 作废该项目所有在途回包
  closeLiveStream();
  endLiveStage();
  releasePin();
  stopPolling();
  currentArtifactStage = null;
  resetDetailUI();
  const ctx = document.getElementById("chatCtx");
  if (ctx) ctx.textContent = "未选择项目";
  document.getElementById("detail").style.display = "none";
  document.getElementById("emptyState").style.display = "block";
  await loadProjects();
}

function selectProject(pid) {
  if (currentProjectId === pid) return;
  currentProjectId = pid;
  renderToken++;               // 作废所有在途的旧项目异步回包
  closeLiveStream();           // 旧项目的 SSE 连接与流式缓冲一并作废
  endLiveStage();
  releasePin();  // 切换项目时释放查看锁定
  currentArtifactStage = null;
  resetDetailUI();             // 立即清空上一个项目的内容区，避免残留
  document.getElementById("emptyState").style.display = "none";
  document.getElementById("detail").style.display = "block";
  loadProjects();
  refreshDetail();
  startPolling();
}

/* 切换项目时重置详情区：隐藏产物卡、清空正文、收起日志与文档列表，
 * 阶段轨道交给紧随其后的 refreshDetail 重新渲染。 */
function resetDetailUI() {
  document.getElementById("artifactCard").style.display = "none";
  document.getElementById("artifactBody").innerHTML = "";
  document.getElementById("artifactTitle").textContent = "";
  document.getElementById("artifactVersion").textContent = "";
  document.getElementById("reviewPanel").style.display = "none";
  document.getElementById("reviewComments").value = "";
  document.getElementById("stageTrack").innerHTML = "";
  document.getElementById("logBox").innerHTML = "";
  document.getElementById("exportCard").style.display = "none";
  document.getElementById("btnReloadPinned").style.display = "none";
  document.getElementById("btnCancel").style.display = "none";
  document.getElementById("btnResume").style.display = "none";
  // 追溯矩阵与产物告警都属于上一个项目，一并清掉
  document.getElementById("traceCard").style.display = "none";
  document.getElementById("traceTableWrap").innerHTML = "";
  document.getElementById("traceSummary").innerHTML = "";
  document.getElementById("traceGaps").style.display = "none";
  document.getElementById("traceNotice").style.display = "none";
  traceFingerprint = "";
  traceData = null;
  renderArtifactWarnings(null);
}

/* ---------- 轮询（按项目状态动态变速 + 标签页隐藏时暂停） ---------- */
const POLL_FAST = 2500;      // 运行中/解析中：需要盯进度
const POLL_REVIEW = 8000;    // 等待评审：变化只来自人，慢一点即可
const POLL_IDLE = 15000;     // 待启动/已完成/已失败：几乎不变，低频保活

let pollInterval = POLL_FAST;
let pollTimer = null;
/* 侧栏状态徽标只在状态真正变化时重建：轮询每几秒一次，
 * 无脑重建列表会在用户要点项目时把节点换掉。 */
const lastStatusByProject = {};

function pollIntervalFor(status) {
  if (status === "running" || status === "parsing") return POLL_FAST;
  if (status === "waiting_review") return POLL_REVIEW;
  return POLL_IDLE;
}

function startPolling() {
  stopPolling();
  scheduleNextPoll();
}

/* 每次刷新完成后按最新间隔排下一次（状态变了频率自动跟着变） */
function scheduleNextPoll() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  if (!currentProjectId) return;
  pollTimer = setTimeout(async () => {
    if (document.visibilityState === "visible") await refreshDetail();
    scheduleNextPoll();
  }, pollInterval);
}

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}

/* 标签页切到后台停止轮询，回到前台立即刷新并恢复调度（省电省流量） */
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    // pollTimer 为 null 说明确实被暂停过，才需要恢复
    if (currentProjectId && pollTimer === null) {
      refreshDetail().finally(scheduleNextPoll);
    }
  } else if (document.visibilityState === "hidden") {
    stopPolling();
  }
});

/* ---------- 详情刷新 ---------- */
async function refreshDetail() {
  if (!currentProjectId) return;
  const token = renderToken;     // 快照：回包后若项目已切换则整包作废
  const pid = currentProjectId;
  try {
      const st = await api(`/api/projects/${pid}/status`);
      if (token !== renderToken || pid !== currentProjectId) return; // 已切换，丢弃旧项目回包
      pollInterval = pollIntervalFor(st.status);  // 按最新状态调整下一轮轮询间隔
      if (lastStatusByProject[pid] !== st.status) {
        lastStatusByProject[pid] = st.status;
        loadProjects().catch(() => {});   // 刷新侧栏「执行中/等待评审/已完成」徽标
      }
      renderHeader(st);
    renderDocs(st.documents);
    renderStageTrack(st);
    renderExport(st);
    syncTraceCard(st);
    renderLogs();
    // 运行态挂上 SSE 实时流，非运行态关掉连接（不空转）
    if (st.status === "running" || st.status === "parsing") ensureLiveStream(pid);
    else closeLiveStream();
    // 实时生成期间产物卡由增量驱动，轮询到此为止：
    // 再往下走会用 DB 里的上一版覆盖正在生长的正文
    if (liveVisible()) return;
    // 用户手动查看（pin）期间：保持显示 pin 的阶段，绝不自动切换
    if (pinnedStage) {
      if (currentArtifactStage !== pinnedStage) await loadArtifact(pinnedStage);
      if (token !== renderToken) return;
      await checkPinnedVersion(st);
      return;
    }
    // 等待评审时自动加载当前阶段产物
    if (st.status === "waiting_review" && st.current_stage) {
      if (currentArtifactStage !== st.current_stage) {
        await loadArtifact(st.current_stage);
      }
    } else if (st.status === "completed" && !currentArtifactStage) {
      // 已完成项目：自动展示最后一个阶段的产物，
      // 避免切换项目后中间内容区残留或空白
      const last = STAGES[STAGES.length - 1];
      if (st.artifacts && st.artifacts[last]) await loadArtifact(last);
    } else if (st.status !== "waiting_review") {
      // 非评审状态：若已展示过产物则隐藏评审面板按钮
      if (currentArtifactStage && document.getElementById("artifactCard").style.display !== "none") {
        document.getElementById("reviewPanel").style.display = "none";
      }
    }
  } catch (e) {
    console.error(e);
  }
}

function renderHeader(st) {
  document.getElementById("projName").textContent = st.name;
  // 助手面板上下文标签跟随当前项目
  const ctxEl = document.getElementById("chatCtx");
  if (ctxEl) ctxEl.textContent = "上下文：" + st.name;
  const pill = document.getElementById("projStatus");
  pill.textContent = STATUS_TEXT[st.status] || st.status;
  pill.className = "status-pill st-" + st.status;
  if (st.error) pill.title = st.error;
  // 运行中（含解析中）显示取消按钮
  const running = st.status === "running" || st.status === "parsing";
  document.getElementById("btnCancel").style.display = running ? "inline-block" : "none";
  // 失败且存在可续跑检查点时显示断点续跑按钮
  const resumable = st.status === "failed" && st.resumable;
  document.getElementById("btnResume").style.display = resumable ? "inline-block" : "none";
  // 待启动项目：自动展开上传区（折叠状态会挡住上传/启动入口）
  if (st.status === "created") {
    document.getElementById("uploadCard").classList.remove("collapsed");
  }
}

function renderDocs(docs) {
  const el = document.getElementById("docList");
  if (!docs.length) { el.innerHTML = '<div class="hint">尚未上传文档</div>'; return; }
  el.innerHTML = docs.map(d => `
    <div class="doc-item">
      <span>${d.status === "parsed" ? '<span class="ok">✔</span>' :
        d.status === "failed" ? '<span class="err">✘</span>' : "📄"}</span>
      <span>${escapeHtml(d.filename)}</span>
      <span class="hint">${d.file_type}</span>
      <button class="btn btn-sm" style="margin-left:auto;padding:2px 8px;font-size:12px"
        onclick="deleteDoc(${d.id})">删除</button>
    </div>`).join("");
}

async function deleteDoc(docId) {
  if (!confirm("确定删除该文档？")) return;
  try {
    await api(`/api/projects/${currentProjectId}/documents/${docId}`, { method: "DELETE" });
    toast("文档已删除");
    refreshDetail();
  } catch (e) {
    toast("删除失败：" + e.message, true);
  }
}

async function cancelPipeline() {
  try {
    await api(`/api/projects/${currentProjectId}/cancel`, { method: "POST" });
    toast("取消请求已发送，流水线将在当前批次结束后停止");
  } catch (e) {
    toast("取消失败：" + e.message, true);
  }
}

function renderStageTrack(st) {
  const track = document.getElementById("stageTrack");
  track.innerHTML = "";
  STAGES.forEach((s, i) => {
    if (i > 0) {
      const line = document.createElement("div");
      line.className = "stage-line";
      if (st.artifacts[s] || stageIndex(st.current_stage) > i) line.className += " done";
      track.appendChild(line);
    }
    const node = document.createElement("div");
    node.className = "stage-node";
    const art = st.artifacts[s];
    let dotContent = i + 1;
    if (st.status === "waiting_review" && st.current_stage === s) {
      node.className += " waiting"; dotContent = "⏸";
    } else if (art && art.status === "approved") {
      node.className += " done"; dotContent = "✓";
    } else if (art && art.status === "rejected") {
      node.className += " rejected"; dotContent = "↻";
    } else if ((st.status === "running" || st.status === "parsing") && st.current_stage === s) {
      node.className += " active";
    } else if (art) {
      node.className += " active";
    }
    // 当前 pin 查看的阶段加高亮
    if (pinnedStage === s) node.className += " pinned";
    node.innerHTML = `<div class="stage-dot">${dotContent}</div>
      <div class="stage-name">${STAGE_NAMES[s]}</div>`;
    node.style.cursor = art ? "pointer" : "default";
    if (art) node.onclick = () => loadArtifact(s);
    track.appendChild(node);
  });
}

function stageIndex(s) { return STAGES.indexOf(s); }

function renderExport(st) {
  const card = document.getElementById("exportCard");
  const a = st.artifacts || {};
  const hasCase = !!a["testcase"];
  /* 测评报告要有判据才导出：静态检查或验证执行至少产出过一份，
   * 否则导出的是一份通篇「未产出」的空报告，那比不导出更容易误导评审。 */
  const hasReport = !!(a["report"] || a["exec"] || a["static"]);
  /* 工程包以源码基线为本体：没有代码产物就装配不出包，按钮不出现。 */
  const hasBundle = !!a["code"];
  document.getElementById("caseExportRow").style.display = hasCase ? "flex" : "none";
  document.getElementById("reportExportRow").style.display = hasReport ? "flex" : "none";
  document.getElementById("bundleExportRow").style.display = hasBundle ? "flex" : "none";
  card.style.display = (hasCase || hasReport || hasBundle) ? "block" : "none";
  if (hasBundle) refreshBundlePlan(bundleKeyOf(a));
  else { bundlePlanKey = ""; bundlePlan = null; }
}

/* ---------- 软件工程包 ----------
 * 出包前先把装配计划摘要拉回来摆在按钮旁边：基线是哪一版、与执行证据对不对得上、
 * 有哪些告警。交付件的诚实性必须在下手之前可见，而不是解压之后才发现。
 * 计划按「相关产物版本号」做键缓存，轮询期间不重复请求。 */
let bundlePlan = null;
let bundlePlanKey = "";

function bundleKeyOf(a) {
  const v = s => (a[s] ? a[s].version : 0);
  return [currentProjectId, v("code"), v("test_impl"), v("exec"),
          v("static"), v("report")].join(":");
}

async function refreshBundlePlan(key) {
  if (key === bundlePlanKey) return;
  bundlePlanKey = key;
  const pid = currentProjectId;
  const el = document.getElementById("bundleState");
  const box = document.getElementById("bundleWarns");
  el.className = "bundle-state";
  el.textContent = "正在核对源码基线与验证证据…";
  box.style.display = "none";
  try {
    const plan = await api(`/api/projects/${pid}/export/bundle?format=plan`);
    if (pid !== currentProjectId || key !== bundlePlanKey) return;   // 已切项目/已有新计划
    bundlePlan = plan;
    paintBundlePlan(plan);
  } catch (e) {
    if (pid !== currentProjectId) return;
    bundlePlanKey = "";                 // 失败不缓存，下次轮询再试
    bundlePlan = null;
    el.className = "bundle-state bundle-warn";
    el.textContent = "工程包装配计划读取失败：" + e.message;
  }
}

function paintBundlePlan(p) {
  const el = document.getElementById("bundleState");
  const box = document.getElementById("bundleWarns");
  const b = p.baseline || {};
  const exec = b.exec_version
    ? `exec v${b.exec_version} ${b.exec_ok ? "通过" : "未通过"}`
    : "未做验证执行";
  const align = p.aligned === true ? "与执行证据逐字节一致"
    : (p.aligned === false ? "与执行证据不一致" : "无执行证据可核对");
  const runs = p.evidence_runs || [];
  el.textContent = [
    `基线 代码 v${b.code_version || "-"} + 测试实现 v${b.test_version || "-"}`,
    `${exec}（标签 ${b.tag || "-"}）`,
    align,
    `文档 ${p.doc_count} 份`,
    `源码 ${p.source_files} 个`,
    `证据 ${runs.length} 轮`,
    (p.missing_stages || []).length ? `未产出 ${p.missing_stages.length} 项` : "",
  ].filter(Boolean).join(" · ");
  el.className = "bundle-state " +
    (p.aligned === false ? "bundle-warn" : (p.warnings || []).length ? "bundle-warn" : "bundle-ok");
  el.title = "基线来源：" + (b.reason || "-");

  const warns = p.warnings || [];
  box.innerHTML = "";
  if (!warns.length) { box.style.display = "none"; return; }
  box.style.display = "flex";
  warns.slice(0, 3).forEach(w => {
    const line = document.createElement("div");
    line.className = "bundle-warn-line" + (p.aligned === false ? " bad" : "");
    line.textContent = "⚠ " + w;
    line.title = w;
    box.appendChild(line);
  });
  if (warns.length > 3) {
    const more = document.createElement("div");
    more.className = "bundle-more";
    more.textContent = `另有 ${warns.length - 3} 条告警，详见包内 00_包清单/包清单.md`;
    box.appendChild(more);
  }
}

/* ---------- 产物渲染 ---------- */
async function loadArtifact(stage, opts = {}) {
  // 该阶段正在实时生成：直接接回流式画面，别拿 DB 里的上一版覆盖它
  if (liveStage === stage) { paintLiveFrame(true); return; }
  currentArtifactStage = stage;
  const pin = opts.pin !== false;  // 流式收尾时不 pin，好让下一阶段的实时流继续接管
  const token = renderToken;   // 快照：加载期间切换项目则丢弃本次渲染
  const pid = currentProjectId;
  try {
    const art = await api(`/api/projects/${pid}/stages/${stage}/artifact`);
    if (token !== renderToken || pid !== currentProjectId) return; // 已切换，作废
    const card = document.getElementById("artifactCard");
    card.style.display = "block";
    document.getElementById("artifactTitle").textContent = art.title;
    document.getElementById("artifactVersion").textContent = `v${art.version}`;
    const exportBtns = document.querySelector(".export-btns");
    if (exportBtns) exportBtns.style.visibility = "visible";  // 恢复实时期间隐藏的导出
    if (pin) {
      pinnedStage = stage;               // 打开产物即锁定，轮询不再自动切走
      pinnedVersion = art.version;
      document.getElementById("pinBar").style.display = "flex";
      document.getElementById("pinHint").textContent =
        `正在查看：${STAGE_NAMES[stage] || stage} v${art.version}（已锁定，轮询不会切换）`;
    } else {
      releasePin();
    }
  document.getElementById("btnReloadPinned").style.display = "none";
  document.getElementById("btnExportDocx").onclick = () =>
    window.open(`/api/projects/${pid}/stages/${stage}/export?format=docx`, "_blank");
  document.getElementById("btnExportMd").onclick = () =>
    window.open(`/api/projects/${pid}/stages/${stage}/export?format=md`, "_blank");
  renderArtifactWarnings(art.meta && art.meta._warnings);
  renderMarkdown(art.markdown);
    // 评审面板仅在等待评审且是当前阶段时显示
    const st = await api(`/api/projects/${pid}/status`);
    if (token !== renderToken || pid !== currentProjectId) return;
    const showReview = st.status === "waiting_review" && st.current_stage === stage;
    document.getElementById("reviewPanel").style.display = showReview ? "block" : "none";
    card.scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    if (token === renderToken && pid === currentProjectId) {
      toast("加载产物失败：" + e.message, true);
    }
  }
}

/* 释放查看锁定：恢复轮询自动跟随当前评审阶段 */
function releasePin() {
  pinnedStage = null;
  pinnedVersion = null;
  document.getElementById("pinBar").style.display = "none";
}

/* 跟随当前进度：释放锁定并立刻刷新到最新评审阶段 */
async function followCurrent() {
  releasePin();
  currentArtifactStage = null;
  if (liveStage) paintLiveFrame(true);   // 有正在生成的阶段：立刻接回实时画面
  await refreshDetail();
}

/* 重新加载当前 pin 阶段（pin 期间该阶段产生新版本时由轮询提示后调用） */
async function reloadPinned() {
  const s = pinnedStage;
  pinnedStage = null;  // 临时释放，loadArtifact 会重新 pin 并滚动
  pinnedVersion = null;
  if (s) await loadArtifact(s);
}

/* 轮询期间检查 pin 的阶段是否有新版本（如驳回后重出 v2） */
async function checkPinnedVersion(st) {
  if (!pinnedStage || !pinnedVersion) return;
  const art = st.artifacts && st.artifacts[pinnedStage];
  if (art && art.version > pinnedVersion) {
    const bar = document.getElementById("pinBar");
    const btn = document.getElementById("btnReloadPinned");
    if (btn.style.display === "none") {
      btn.style.display = "inline-block";
      btn.textContent = `该阶段已有新版本 v${art.version}，点此刷新`;
      btn.onclick = reloadPinned;
    }
  }
}

/* ---------- 需求追溯矩阵 ----------
 * 数据由后端按各阶段最新产物的元数据实时算出（core/trace 纯函数），前端只做展示。
 * 轮询很频繁，所以用「产物版本指纹」当缓存键：版本没变就不重复请求。
 */
let traceFingerprint = "";   // 当前已渲染矩阵对应的产物版本指纹
let traceData = null;        // 最近一次矩阵数据（切筛选时重绘用，不必重新请求）
let traceOnlyGaps = false;   // 「只看待补全」筛选

const TRACE_STAGE_LABEL = { hld: "概要设计", lld: "详细设计", testcase: "测试用例" };
/* 各追溯环节对应的产物名（提示用户重跑哪个阶段时用人话而不是字段名） */
const TRACE_LINK_ARTIFACT = {
  source: "需求规格", hld: "概要设计", lld: "详细设计", testcase: "测试用例",
};
/* 执行结果列的取值文字，与 core.trace.EXEC_TEXT / Excel 导出保持同一口径 */
const TRACE_EXEC_TEXT = {
  all: "全部通过", partial: "部分失败", none: "全部失败",
  not_run: "未执行", no_case: "无关联用例",
};
const TRACE_NOT_PRODUCED = "未产出";

function traceFingerprintOf(st) {
  const a = st.artifacts || {};
  /* 验证阶段的产物一落库，矩阵的四列结论就变了：指纹必须带上它们，
   * 否则 exec 跑完前端还显示旧结论，用户会以为没跑。 */
  return ["requirement", "hld", "lld", "testcase",
          "code", "static", "test_impl", "exec"]
    .map(s => `${s}:${a[s] ? a[s].version : "-"}`).join("|");
}

/* 轮询与切换项目时调用：决定卡片可见性，产物变了才重新计算 */
function syncTraceCard(st) {
  const card = document.getElementById("traceCard");
  if (!st.artifacts || !st.artifacts["requirement"]) {
    card.style.display = "none";
    traceFingerprint = "";
    traceData = null;
    return;
  }
  card.style.display = "block";
  const fp = traceFingerprintOf(st);
  if (fp !== traceFingerprint) {
    traceFingerprint = fp;
    loadTraceability();
  }
}

async function loadTraceability() {
  if (!currentProjectId) return;
  const pid = currentProjectId;
  const fp = traceFingerprint;
  const wrap = document.getElementById("traceTableWrap");
  if (!traceData) wrap.innerHTML = '<div class="hint trace-pad">正在计算追溯矩阵…</div>';
  try {
    const data = await api(`/api/projects/${pid}/traceability`);
    if (pid !== currentProjectId || fp !== traceFingerprint) return;  // 已切换或已过期
    traceData = data;
    renderTrace(data);
  } catch (e) {
    if (pid !== currentProjectId || fp !== traceFingerprint) return;
    traceData = null;
    document.getElementById("traceSummary").innerHTML = "";
    document.getElementById("traceGaps").style.display = "none";
    document.getElementById("traceNotice").style.display = "none";
    document.getElementById("traceHint").textContent = "";
    // 走到这里通常是还没有需求产物：给可操作的提示，而不是每轮轮询弹一次错
    wrap.innerHTML = `<div class="hint trace-pad">${escapeHtml(e.message)}</div>`;
  }
}

/* 该阶段是否已产出产物 */
function traceProduced(s, k) {
  return k === "hld" ? !!s.has_hld : (k === "lld" ? !!s.has_lld : !!s.has_tc);
}

/* 该环节的追溯信息是否被记录过。
 * 追溯能力上线前生成的旧产物没有这些字段，链路为空属于「无从判断」，
 * 不能和「新产物里确实漏标」一样报红，否则老项目一打开就是满屏断链。 */
function traceKnown(s, k) {
  return !s.recorded || s.recorded[k] !== false;
}

/* 这个项目有没有任何一环是「可判断」的。老产物整片未记录时，
 * 链路列不能因为「没查出缺口」就报 ✓ 贯通——那是假结论。 */
function traceJudgeable(s) {
  if (traceKnown(s, "source") && (s.fr_total || 0)) return true;
  return ["hld", "lld", "testcase"].some(k => traceProduced(s, k) && traceKnown(s, k));
}

/* 产出了但没记录追溯信息的环节（= 需要重跑才能补全链路的旧产物） */
function traceLegacyLinks(s) {
  const out = [];
  if ((s.fr_total || 0) && !traceKnown(s, "source")) out.push("source");
  ["hld", "lld", "testcase"].forEach(k => {
    if (traceProduced(s, k) && !traceKnown(s, k)) out.push(k);
  });
  return out;
}

/* 还没产出的下游环节。链路列不能因为「暂时查不出缺口」就报 ✓ 贯通：
 * 设计文档都还没生成时，贯通是个假结论，必须显示为「待生成」。 */
function tracePendingStages(s) {
  const keys = Array.isArray(s.pending_links)
    ? s.pending_links
    : ["hld", "lld", "testcase"].filter(k => !traceProduced(s, k));
  return keys.map(k => TRACE_STAGE_LABEL[k] || k);
}

/* 一条需求缺哪几环：只统计已产出且记录了追溯信息的环节。
 * 服务端 row.missing 优先（与 Excel 导出共用同一套规则），本地判定仅作兜底。 */
function traceRowGaps(row, s) {
  if (Array.isArray(row.missing)) return row.missing;
  const gaps = [];
  if (traceKnown(s, "source") && !(row.sources || []).length) gaps.push("素材来源");
  ["hld", "lld", "testcase"].forEach(k => {
    const cell = k === "hld" ? row.hld_modules : (k === "lld" ? row.lld_functions : row.testcases);
    if (traceProduced(s, k) && traceKnown(s, k) && !(cell || []).length) gaps.push(TRACE_STAGE_LABEL[k]);
  });
  return gaps;
}

/* 链路状态：用例跑挂 > 覆盖不足 > 真断链 > 老产物无从判断 > 下游还没产出 > 才算贯通。
 * 验证维度的两个状态排在最前：链路缺了只是追溯不全，用例挂了说明实现与需求对不上。 */
function traceStatusHtml(r, s, gaps) {
  const pending = tracePendingStages(s);
  const st = r.status || (gaps.length ? "gap"
    : !traceJudgeable(s) ? "unrecorded"
    : pending.length ? "pending" : "ok");
  if (st === "exec_fail") {
    return `<span class="trace-fail" title="${escapeHtml(r.status_text || "有用例未通过")}">✗ 执行失败</span>`;
  }
  if (st === "low_cov") {
    return `<span class="trace-bad" title="${escapeHtml(r.status_text || "分支覆盖低于门限")}">◐ 覆盖不足</span>`;
  }
  if (st === "gap") {
    const miss = gaps.length ? gaps : (r.missing || []);
    return `<span class="trace-bad" title="缺：${escapeHtml(miss.join("、"))}">⚠ 待补</span>`;
  }
  if (st === "unrecorded") {
    return `<span class="trace-unknown" title="产物未记录追溯信息，无从判断">— 未记录</span>`;
  }
  if (st === "pending") {
    return `<span class="trace-pending" title="${escapeHtml(pending.join("、"))}尚未产出，链路还没走完">◷ 待生成</span>`;
  }
  return `<span class="trace-ok">✓ 贯通</span>`;
}

function traceChips(items, emptyText) {
  if (!items || !items.length) return `<span class="trace-none">${emptyText}</span>`;
  return items.map(it => {
    const text = typeof it === "string" ? it : (it.text || "");
    const title = (typeof it === "object" && it.title) ? ` title="${escapeHtml(it.title)}"` : "";
    return `<span class="trace-chip"${title}>${escapeHtml(text)}</span>`;
  }).join("");
}

/* ---- 验证维度四列的单元格。None 一律是「未产出」，不能显示成 0 或 ✓，
 * 那等于把「没跑过」说成「跑过了没问题」，在交付件里是硬伤。 ---- */
function traceCodeCell(r, s) {
  if (!s.has_code || r.code_units === null || r.code_units === undefined) {
    return `<span class="trace-none">${TRACE_NOT_PRODUCED}</span>`;
  }
  return traceChips(r.code_units, "无对应代码单元");
}

function traceStaticCell(r, s) {
  const n = r.static_violations;
  if (!s.has_static || n === null || n === undefined) {
    return `<span class="trace-none">${TRACE_NOT_PRODUCED}</span>`;
  }
  return n
    ? `<span class="trace-bad" title="与本需求相关的函数共 ${n} 条违规">⚠ ${n}</span>`
    : `<span class="trace-ok" title="与本需求相关的函数无违规">✓ 0</span>`;
}

function traceExecCell(r, s) {
  const v = r.exec_result;
  if (!s.has_test_impl || v === null || v === undefined) {
    return `<span class="trace-none">${TRACE_NOT_PRODUCED}</span>`;
  }
  const text = TRACE_EXEC_TEXT[v] || v;
  const cls = v === "all" ? "trace-ok"
    : (v === "partial" || v === "none") ? "trace-fail" : "trace-pending";
  return `<span class="${cls}">${v === "all" ? "✓ " : ""}${escapeHtml(text)}</span>`;
}

function traceCovCell(r, s) {
  const cov = r.branch_coverage;
  if (cov !== null && cov !== undefined) {
    const min = s.coverage_min;
    const low = min && cov < min;
    return `<span class="${low ? "trace-bad" : "trace-ok"}"${
      low ? ` title="低于门限 ${min}%"` : ""}>${cov.toFixed(1)}%</span>`;
  }
  /* 执行跑过了却没有这条需求对应函数的覆盖数据，与压根没执行是两回事 */
  return `<span class="trace-none">${s.has_exec ? "无数据" : TRACE_NOT_PRODUCED}</span>`;
}

function traceSummaryHtml(s) {
  const seg = [`<span class="ts-item">需求 ${s.fr_total || 0}</span>`];
  if (s.p0_total) seg.push(`<span class="ts-item">P0 ${s.p0_total}</span>`);
  const gaps = s.uncovered_p0 || {};
  const miss = [];
  let judgeable = false;
  ["hld", "lld", "testcase"].forEach(k => {
    if (!traceProduced(s, k)) return;
    if (!traceKnown(s, k)) return;
    judgeable = true;
    if ((gaps[k] || []).length) miss.push(`${TRACE_STAGE_LABEL[k]} ${gaps[k].length}`);
  });
  if (!judgeable) {
    seg.push(`<span class="ts-item">${
      (s.has_hld || s.has_lld || s.has_tc) ? "产物未记录追溯信息" : "设计阶段尚未产出"}</span>`);
  } else if (miss.length) {
    seg.push(`<span class="ts-item ts-bad" title="P0 需求还没被覆盖到的环节">P0 待补 ${miss.join(" · ")}</span>`);
  } else {
    seg.push(`<span class="ts-item ts-ok">P0 链路完整</span>`);
  }
  if (traceKnown(s, "testcase") && (s.orphan_testcases || []).length) {
    seg.push(`<span class="ts-item ts-bad" title="没有关联任何需求的测试用例">孤立用例 ${s.orphan_testcases.length}</span>`);
  }
  /* 验证维度：只在真的产出过对应产物时才说话，否则不提，避免给出假结论 */
  if (s.has_static) {
    const n = s.static_unattributed || 0;
    seg.push(`<span class="ts-item ${n ? "ts-bad" : "ts-ok"}" title="挂不到具体函数的文件级违规">${
      n ? `静态文件级违规 ${n}` : "静态检查通过"}</span>`);
  }
  if (s.has_exec) {
    const bad = (s.status_counts || {}).exec_fail || 0;
    const low = (s.status_counts || {}).low_cov || 0;
    const bits = [];
    if (bad) bits.push(`执行失败 ${bad}`);
    if (low) bits.push(`覆盖不足 ${low}`);
    seg.push(`<span class="ts-item ${bits.length ? "ts-bad" : "ts-ok"}" title="按需求逐行统计的验证结论">${
      bits.length ? bits.join(" · ") : "验证判据达标"}</span>`);
  }
  return seg.join("");
}

function traceVersionsText(versions) {
  const v = versions || {};
  const parts = [];
  ["requirement", "hld", "lld", "testcase",
   "code", "static", "test_impl", "exec"].forEach(k => {
    if (v[k]) parts.push(`${STAGE_NAMES[k] || k} v${v[k]}`);
  });
  return parts.join(" · ");
}

function renderTraceGaps(s) {
  const box = document.getElementById("traceGaps");
  const gaps = s.uncovered_p0 || {};
  const lines = [];
  ["hld", "lld", "testcase"].forEach(k => {
    if (!traceKnown(s, k)) return;      // 旧产物无从判断，不列缺口
    const ids = gaps[k] || [];
    if (!ids.length) return;
    lines.push(`<div class="tg-line"><span class="tg-label">${TRACE_STAGE_LABEL[k]}未覆盖的 P0</span>` +
      ids.map(id => `<span class="trace-chip chip-bad">${escapeHtml(id)}</span>`).join("") + `</div>`);
  });
  const orphans = s.orphan_testcases || [];
  if (orphans.length && traceKnown(s, "testcase")) {
    lines.push(`<div class="tg-line"><span class="tg-label">未关联需求的用例</span>` +
      orphans.map(id => `<span class="trace-chip chip-bad">${escapeHtml(id)}</span>`).join("") + `</div>`);
  }
  box.innerHTML = lines.join("");
  box.style.display = lines.length ? "block" : "none";
}

/* 旧产物提示：说清哪几环是「没记录」而不是「断了」，以及怎么补 */
function renderTraceNotice(s) {
  const box = document.getElementById("traceNotice");
  const legacy = traceLegacyLinks(s);
  if (!legacy.length) { box.style.display = "none"; box.innerHTML = ""; return; }
  const names = legacy.map(k => TRACE_LINK_ARTIFACT[k] || k);
  box.style.display = "block";
  box.innerHTML = `该项目的「${names.map(escapeHtml).join("、")}」产物生成于追溯能力上线之前，
    没有记录关联信息，下表中标为 <b>—</b>（无从判断，不计为断链）。重跑对应阶段即可补全链路。`;
}

function renderTrace(data) {
  const rows = data.rows || [];
  const s = data.summary || {};
  document.getElementById("traceSummary").innerHTML = traceSummaryHtml(s);
  document.getElementById("traceHint").textContent =
    `${rows.length} 条需求 · ${traceVersionsText(data.versions)}`;
  renderTraceNotice(s);
  renderTraceGaps(s);

  /* 「只看待补全」也要包含验证判据不达标的行：用例跑挂了比文档断链更该被看见 */
  const shown = traceOnlyGaps
    ? rows.filter(r => traceRowGaps(r, s).length
        || r.status === "exec_fail" || r.status === "low_cov")
    : rows;
  const wrap = document.getElementById("traceTableWrap");
  if (!shown.length) {
    const pending = tracePendingStages(s);
    wrap.innerHTML = `<div class="hint trace-pad">${
      !rows.length
        ? (!traceJudgeable(s) ? "产物未记录追溯信息，无从判断链路；重跑对应阶段后可见。"
                              : "需求产物里没有可识别的条目。")
        : (pending.length ? `${pending.map(escapeHtml).join("、")}尚未产出，暂无已确认的缺口。`
                          : "没有待补全的需求，链路完整。")}</div>`;
    return;
  }
  const srcIndex = data.sources || {};
  const body = shown.map(r => {
    const gaps = traceRowGaps(r, s);
    const sources = (r.sources || []).map((id, i) => {
      const info = srcIndex[id] || {};
      const chunks = (info.chunks || []).join(",");
      return { text: (r.source_names && r.source_names[i]) || id,
               title: `${id}${chunks ? " · 出处 " + chunks : ""}` };
    });
    const status = traceStatusHtml(r, s, gaps);
    const rowCls = r.status === "exec_fail" ? "row-fail"
      : (r.status === "low_cov" ? "row-warn" : (gaps.length ? "row-gap" : ""));
    return `<tr class="${rowCls}">
      <td class="tid">${escapeHtml(r.id)}</td>
      <td class="tdesc">${escapeHtml(r.desc || "")}</td>
      <td><span class="pri pri-${escapeHtml(r.priority || "P2")}">${escapeHtml(r.priority || "-")}</span></td>
      <td>${traceChips(sources, traceKnown(s, "source") ? "未标注" : "—")}</td>
      <td>${traceChips(r.hld_modules, traceProduced(s, "hld") && traceKnown(s, "hld") ? "缺" : "—")}</td>
      <td>${traceChips(r.lld_functions, traceProduced(s, "lld") && traceKnown(s, "lld") ? "缺" : "—")}</td>
      <td>${traceChips(r.testcases, traceProduced(s, "testcase") && traceKnown(s, "testcase") ? "缺" : "—")}</td>
      <td>${traceCodeCell(r, s)}</td>
      <td>${traceStaticCell(r, s)}</td>
      <td>${traceExecCell(r, s)}</td>
      <td>${traceCovCell(r, s)}</td>
      <td>${status}</td>
    </tr>`;
  }).join("");
  wrap.innerHTML = `<table class="trace-table">
    <thead><tr>
      <th>编号</th><th>需求描述</th><th>优先级</th><th>素材来源</th>
      <th>概要设计</th><th>详细设计</th><th>测试用例</th>
      <th title="详细设计函数是否已在代码中实现">代码单元</th>
      <th title="与该需求相关函数的静态检查违规条数">静态检查</th>
      <th title="该需求关联用例在验证机上的执行结果">执行结果</th>
      <th title="该需求相关函数中最差的分支覆盖率">分支覆盖</th>
      <th>链路</th>
    </tr></thead>
    <tbody>${body}</tbody></table>`;
}

/* 产物元数据里的追溯告警：软校验放行了，但缺口必须让人看见 */
function renderArtifactWarnings(warnings) {
  const el = document.getElementById("artifactWarnings");
  if (!el) return;
  const list = Array.isArray(warnings) ? warnings.filter(Boolean).map(String) : [];
  if (!list.length) { el.style.display = "none"; el.innerHTML = ""; return; }
  el.style.display = "block";
  el.innerHTML = `<div class="warn-title">⚠ 本版本有 ${list.length} 处追溯缺口（已放行，建议下一版补齐）</div>
    <ul>${list.map(w => `<li>${escapeHtml(w)}</li>`).join("")}</ul>`;
}

/* ---------- 实时流式推送（SSE） ----------
 * 后端 /api/projects/<pid>/stream 在智能体生成的同时逐段推增量，这里把增量拼进
 * liveBuf 并节流渲染，用户不必等整份文档校验通过、落库才看到内容（长文档要等几分钟）。
 * SSE 只是「更快看到」的增强通道：连接失败、事件丢失或浏览器不支持时，
 * 原有轮询仍会在产物落库后渲染出干净版本，不影响正确性。
 */
let liveEs = null;             // 当前 EventSource
let livePid = null;            // 该连接对应的项目（切换项目时据此作废在途事件）
let liveStage = null;          // 正在实时生成的阶段
let liveBuf = "";              // 已收到的增量文本；stage_done 后丢弃，改从 DB 拉干净版
let liveNotes = [];            // 解析阶段的进度行（该阶段没有连续正文可显示）
let liveProgress = "";         // 解析阶段最新进度描述
let liveTimer = null;          // 渲染节流定时器
let liveLastRender = 0;
const LIVE_RENDER_MS = 150;    // 正文重排最小间隔：marked.parse 长文档并不便宜
const LIVE_MAX_NOTES = 60;
let liveThink = "";            // 模型思考增量：推理模型出正文前先思考，长文档这段可达数分钟
let liveStageStart = 0;        // 本阶段开始时刻，用于显示「思考中 · Ns」
let liveTick = null;           // 秒级定时器：没有新增量时也要刷新已等待时长
const LIVE_MAX_THINK = 6000;   // 思考文本只留尾部，避免 DOM 越滚越重

/* 用户 pin 在别的阶段时不打断他的阅读：只在「没 pin」或「pin 的正是本阶段」时接管画面 */
function liveVisible() {
  return !!liveStage && (!pinnedStage || pinnedStage === liveStage);
}

function ensureLiveStream(pid) {
  if (liveEs && livePid === pid) return;
  closeLiveStream();
  if (typeof EventSource === "undefined") return;   // 老浏览器：退回纯轮询
  livePid = pid;
  const es = new EventSource(`/api/projects/${pid}/stream?since=0`);
  liveEs = es;
  es.onmessage = e => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (err) { return; }
    if (livePid !== pid) { try { es.close(); } catch (err) {} return; }
    handleStreamEvent(ev);
  };
  // 断开多因服务端收尾（流水线停到评审门）；真正的重连由 refreshDetail 按状态决定，
  // 这里不主动重试，避免失败时刷屏式重连
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) closeLiveStream(); };
}

function closeLiveStream() {
  if (liveEs) { try { liveEs.close(); } catch (e) {} }
  liveEs = null;
  livePid = null;
}

function endLiveStage() {
  liveStage = null;
  liveBuf = "";
  liveNotes = [];
  liveProgress = "";
  liveThink = "";
  liveStageStart = 0;
  if (liveTimer) { clearTimeout(liveTimer); liveTimer = null; }
  if (liveTick) { clearInterval(liveTick); liveTick = null; }
  const tag = document.getElementById("liveTag");
  if (tag) tag.style.display = "none";
  const body = document.getElementById("artifactBody");
  if (body) body.classList.remove("live");
}

function handleStreamEvent(ev) {
  switch (ev.type) {
      case "stage_start":
        liveStage = ev.stage;
        liveBuf = "";
        liveNotes = [];
        liveProgress = "";
        liveThink = "";
        // 用服务端时刻：刷新页面后「思考中 · Ns」仍是从阶段真正开始算的
        liveStageStart = ev.started_at ? ev.started_at * 1000 : Date.now();
        startLiveTick();
        if (liveVisible()) paintLiveFrame(true);
        break;
    case "thinking":              // 推理模型出正文前的思考：先把它显示出来，别让用户对着空白等
      if (ev.stage !== liveStage) break;
      liveThink = (liveThink + (ev.text || "")).slice(-LIVE_MAX_THINK);
      scheduleLiveRender();
      break;
    case "delta":
      if (ev.stage !== liveStage) break;
      liveBuf += ev.text || "";
      scheduleLiveRender();
      break;
    case "reset":                 // 校验/网络重试：上一轮输出作废，前端清空重画
      if (ev.stage !== liveStage) break;
      liveBuf = "";
      liveThink = "";
      liveStageStart = Date.now();
      scheduleLiveRender();
      break;
      case "resync":                // 断线重连补发的整段快照
        if (ev.stages && typeof ev.stages[liveStage] === "string") {
          liveBuf = ev.stages[liveStage];
          scheduleLiveRender();
        }
        if (ev.thinking && typeof ev.thinking[liveStage] === "string") {
          liveThink = ev.thinking[liveStage].slice(-LIVE_MAX_THINK);
          scheduleLiveRender();
        }
        if (ev.started_at && typeof ev.started_at[liveStage] === "number") {
          liveStageStart = ev.started_at[liveStage] * 1000;
        }
        break;
    case "progress":
      if (ev.stage !== liveStage) break;
      if (ev.total) {
        liveProgress = `LLM 抽取 ${ev.done}/${ev.total} 批（${ev.pct}%）`;
        pushLiveNote(liveProgress + (ev.cached ? "（缓存命中）" : ""));
      } else if (ev.message) {
        pushLiveNote(ev.message);
      }
      scheduleLiveRender();
      break;
    case "stage_done":
      finishLiveStage(ev);
      break;
    case "stage_error":
      endLiveStage();
      toast(`「${STAGE_NAMES[ev.stage] || ev.stage}」生成失败：${ev.message}`, true);
      refreshDetail();
      break;
    case "run_end":
      endLiveStage();
      break;
    case "closed":                // 服务端本轮结束：关连接，交给轮询
      endLiveStage();
      closeLiveStream();
      refreshDetail();
      break;
  }
}

function pushLiveNote(text) {
  liveNotes.push(text);
  if (liveNotes.length > LIVE_MAX_NOTES) liveNotes.shift();
}

/* 思考期没有增量也要刷新「已等待 Ns」，否则界面看着像卡死 */
function startLiveTick() {
  if (liveTick) return;
  liveTick = setInterval(() => { if (!liveBuf && liveStage) renderLive(); }, 1000);
}

/* 落库后收尾：流式缓冲是未校验的半成品（末尾还挂着 ```json 元数据块，图表也没渲染），
 * 换成 DB 里的干净版本；不 pin，好让下一阶段的实时流能继续接管画面。 */
async function finishLiveStage(ev) {
  const stage = ev.stage;
  const wasVisible = liveVisible();
  endLiveStage();
  if (wasVisible) await loadArtifact(stage, { pin: false });
  await refreshDetail();
}

/* 把产物卡切成「实时生成」形态 */
function paintLiveFrame(reset) {
  if (!liveStage) return;
  const card = document.getElementById("artifactCard");
  const body = document.getElementById("artifactBody");
  card.style.display = "block";
  document.getElementById("artifactTitle").textContent =
    STAGE_NAMES[liveStage] || liveStage;
  document.getElementById("artifactVersion").textContent = "生成中";
  document.getElementById("reviewPanel").style.display = "none";
  document.getElementById("pinBar").style.display = "none";
  document.getElementById("btnReloadPinned").style.display = "none";
  // 产物还没落库，导出按钮此时点了只会 404
  const exportBtns = document.querySelector(".export-btns");
  if (exportBtns) exportBtns.style.visibility = "hidden";
  renderArtifactWarnings(null);   // 上一版的告警不属于正在生成的这一版
  document.getElementById("liveTag").style.display = "inline-flex";
  body.classList.add("live");
  currentArtifactStage = liveStage;
  if (reset) {
    body.innerHTML = "";
    card.scrollIntoView({ behavior: REDUCE_MOTION ? "auto" : "smooth" });
  }
  renderLive();
}

/* 节流渲染：增量到达频率高于人眼需要，合并到固定间隔重排一次 */
function scheduleLiveRender() {
  if (liveTimer) return;
  const wait = Math.max(0, LIVE_RENDER_MS - (performance.now() - liveLastRender));
  liveTimer = setTimeout(() => {
    liveTimer = null;
    liveLastRender = performance.now();
    renderLive();
  }, wait);
}

function renderLive() {
  if (!liveStage || !liveVisible()) return;
  const body = document.getElementById("artifactBody");
  // 替换 innerHTML 前先判断用户是否在底部：正在往上翻阅时不要把他拽回去
  const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 120;
  if (liveStage === "parse") {
    body.innerHTML = (liveNotes.length ? liveNotes : ["正在解析文档并抽取结构化数据…"])
      .map(n => `<div class="live-note">${escapeHtml(n)}</div>`).join("");
  } else if (!liveBuf && liveThink) {
    // 正文还没开始：显示思考流，让用户知道模型在干活而不是卡住了
    body.innerHTML = `<div class="live-think"><div class="live-think-title">模型思考中</div>` +
      `<div class="live-think-body">${escapeHtml(liveThink)}</div></div>` +
      '<span class="chat-cursor"></span>';
  } else {
    // 流式期间不跑 mermaid：图表源码先以代码块呈现，stage_done 后统一渲染成 SVG
    body.innerHTML = marked.parse(liveBuf || "") + '<span class="chat-cursor"></span>';
  }
  if (nearBottom) body.scrollTop = body.scrollHeight;
  document.getElementById("liveTag").textContent = liveStage === "parse"
    ? `● 实时进度${liveProgress ? "：" + liveProgress : ""}`
    : (liveBuf
      ? `● 实时生成中 · ${liveBuf.length.toLocaleString()} 字`
      : `● 模型思考中 · ${Math.max(0, Math.round((Date.now() - liveStageStart) / 1000))}s`);
}

/* ---------- 日志 ---------- */
async function renderLogs() {
  if (!currentProjectId) return;
  const token = renderToken;
  const pid = currentProjectId;
  try {
    const logs = await api(`/api/projects/${pid}/logs`);
    if (token !== renderToken || pid !== currentProjectId) return; // 已切换，丢弃旧项目日志
    const box = document.getElementById("logBox");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.innerHTML = logs.map(l =>
      `<div class="log-line"><span class="time">[${l.time}]</span> ` +
      `<span class="lvl-${l.level}">${l.level}</span> ` +
      `${l.stage ? `(${l.stage}) ` : ""}${escapeHtml(l.message)}</div>`).join("");
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (e) {}
}

/* ---------- 评审 ---------- */
async function submitReview(approved) {
  const comments = document.getElementById("reviewComments").value.trim();
  if (!approved && !comments) { toast("驳回时必须填写评审意见", true); return; }
  try {
    await api(`/api/projects/${currentProjectId}/stages/${currentArtifactStage}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved, comments }),
    });
    toast(approved ? "已通过，流水线继续执行" : "已驳回，智能体将根据意见修订");
    document.getElementById("reviewComments").value = "";
    document.getElementById("reviewPanel").style.display = "none";
    releasePin();             // 评审提交后释放锁定，恢复自动跟随
    currentArtifactStage = null;
    ensureLiveStream(currentProjectId);   // 下一阶段马上开始生成，立即挂上实时流
    refreshDetail();
  } catch (e) {
    toast("提交失败：" + e.message, true);
  }
}

/* ---------- 上传与启动 ---------- */
async function uploadFiles() {
  const input = document.getElementById("fileInput");
  if (!input.files.length) { toast("请先选择文件", true); return; }
  for (const f of input.files) {
    const fd = new FormData();
    fd.append("file", f);
    try {
      await api(`/api/projects/${currentProjectId}/upload`, { method: "POST", body: fd });
      toast(`已上传 ${f.name}`);
    } catch (e) {
      toast(`上传 ${f.name} 失败：${e.message}`, true);
    }
  }
  input.value = "";
  refreshDetail();
}

async function startPipeline() {
  try {
    await api(`/api/projects/${currentProjectId}/start`, { method: "POST" });
    toast("流水线已启动");
    ensureLiveStream(currentProjectId);   // 不等下一轮轮询，立刻开始接收增量
    refreshDetail();
  } catch (e) {
    toast("启动失败：" + e.message, true);
  }
}

async function resetProject() {
  if (!confirm("重置将清空该项目已有的全部产物与进度，确定重新开始吗？")) return;
  try {
    await api(`/api/projects/${currentProjectId}/reset`, { method: "POST" });
    toast("已重置，可重新启动流水线");
    releasePin();
    currentArtifactStage = null;
    closeLiveStream();
    endLiveStage();
    document.getElementById("artifactCard").style.display = "none";
    refreshDetail();
  } catch (e) {
    toast("重置失败：" + e.message, true);
  }
}

async function resumePipeline() {
  try {
    await api(`/api/projects/${currentProjectId}/resume`, { method: "POST" });
    toast("已从断点续跑，跳过已完成阶段");
    ensureLiveStream(currentProjectId);
    refreshDetail();
  } catch (e) {
    toast("断点续跑失败：" + e.message, true);
  }
}

function exportCases(fmt) {
  window.open(`/api/projects/${currentProjectId}/export/testcases?format=${fmt}`, "_blank");
}

function exportReport(fmt) {
  window.open(`/api/projects/${currentProjectId}/export/report?format=${fmt}`, "_blank");
}

/* 工程包导出。zip 走 fetch 而不是 window.open：装配失败时后端返回的是 JSON 错误，
 * 新开标签页只会甩给用户一屏 JSON，这里能把它变成一句人话，也能给出装配中的按钮态。 */
async function exportBundle(fmt) {
  const pid = currentProjectId;
  if (!pid) { toast("请先选择项目", true); return; }
  const btn = document.getElementById(fmt === "dir" ? "btnBundleDir" : "btnBundleExport");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = fmt === "dir" ? "生成中…" : "装配中…";
  try {
    if (fmt === "dir") {
      const r = await api(`/api/projects/${pid}/export/bundle?format=dir`);
      toast(`工程包已生成：${r.dir}（${r.file_count} 个文件）`);
    } else {
      const resp = await fetch(`/api/projects/${pid}/export/bundle?format=zip`);
      if (!resp.ok) {
        let msg = `HTTP ${resp.status}`;
        try { msg = (await resp.json()).error || msg; } catch (e) { /* 非 JSON 响应 */ }
        throw new Error(msg);
      }
      const blob = await resp.blob();
      const name = `${(bundlePlan && bundlePlan.bundle_name) || "软件工程包"}.zip`;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      toast(`工程包已下载：${name}`);
    }
    bundlePlanKey = "";        // 出包会重写目录与哈希，回来重新核对一次
  } catch (e) {
    toast("工程包导出失败：" + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
  refreshDetail();
}

/* ---------- 新建项目 ---------- */
async function createProject() {
  const name = document.getElementById("newProjectName").value.trim();
  if (!name) { toast("请输入项目名称", true); return; }
  try {
    const p = await api("/api/projects", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    document.getElementById("newProjectModal").style.display = "none";
    document.getElementById("newProjectName").value = "";
    await loadProjects();
    selectProject(p.id);
  } catch (e) {
    toast("创建失败：" + e.message, true);
  }
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- AI 助手（右侧可收起对话面板） ----------
 * 后端 /api/chat 以 SSE 流式返回 {delta} 增量，前端用 fetch + ReadableStream
 * 逐块解析实时渲染；多轮历史由前端维护（最近 6 轮 = 12 条）随请求带上。
 * 上下文由后端按当前 project_id 组装各阶段产物，前端只需传项目 id。
 */
let chatHistory = [];          // [{role, content}]，发送时作为 history 传给后端
let chatBusy = false;          // 正在流式输出时禁止重复发送

function chatPanel() { return document.getElementById("chatPanel"); }

function toggleChatPanel(forceOpen) {
  const p = chatPanel();
  const open = forceOpen === true ? true : p.classList.contains("collapsed");
  p.classList.toggle("collapsed", !open);
  if (open) document.getElementById("chatInput").focus();
}

function toggleSidebar() {
  document.getElementById("sidebar").classList.toggle("collapsed");
}

function addChatMsg(role, text) {
  const box = document.getElementById("chatMessages");
  const empty = box.querySelector(".chat-empty");
  if (empty) empty.remove();
  const wrap = document.createElement("div");
  wrap.className = "chat-msg " + role;
  const roleLabel = role === "user" ? "我" : role === "error" ? "提示" : "AI 助手";
  const body = document.createElement("div");
  body.className = "cm-body";
  if (role === "user") {
    body.textContent = text;
  } else {
    // 助手回复按 Markdown 渲染；流式未开始/进行中显示闪烁光标（表示"正在回复"）
    body.innerHTML = text ? marked.parse(text) : '<span class="chat-cursor"></span>';
  }
  wrap.innerHTML = `<div class="cm-role">${roleLabel}</div>`;
  wrap.appendChild(body);
  box.appendChild(wrap);
  box.scrollTop = box.scrollHeight;
  return body;
}

/* 把流式累积的纯文本实时渲染为 Markdown（末尾挂闪烁光标） */
function renderStreaming(body, acc) {
  body.innerHTML = marked.parse(acc || "") + '<span class="chat-cursor"></span>';
  const box = document.getElementById("chatMessages");
  box.scrollTop = box.scrollHeight;
}

async function sendChat() {
  if (chatBusy) return;
  const input = document.getElementById("chatInput");
  const msg = input.value.trim();
  if (!msg) return;
  input.value = "";
  addChatMsg("user", msg);
  chatBusy = true;
  document.getElementById("btnChatSend").disabled = true;

  const body = addChatMsg("assistant", "");
  let acc = "";
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: currentProjectId,
        message: msg,
        history: chatHistory.slice(-12),
        // 联网检索开关：默认关，仅当 .env 配置了搜索 key 时才可能出现
        use_web: document.getElementById("chatWebSwitch").checked,
      }),
    });
    if (!resp.ok || !resp.body) {
      let emsg = `HTTP ${resp.status}`;
      try { emsg = (await resp.json()).error || emsg; } catch (e) {}
      throw new Error(emsg);
    }
    // 逐块读取 SSE：每条事件形如  data: {...}\n\n
    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    let streamErr = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // 按 SSE 事件边界（空行）切分
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of raw.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          let payload;
          try { payload = JSON.parse(line.slice(6)); } catch (e) { continue; }
          if (payload.delta) { acc += payload.delta; renderStreaming(body, acc); }
          else if (payload.error) { streamErr = payload.error; }
          else if (payload.done) { /* 正常结束 */ }
        }
      }
    }
    if (streamErr) throw new Error(streamErr);
    if (!acc) acc = "（未收到回复内容）";
    body.innerHTML = marked.parse(acc);
    chatHistory.push({ role: "user", content: msg });
    chatHistory.push({ role: "assistant", content: acc });
  } catch (e) {
    body.closest(".chat-msg").classList.add("error");
    body.innerHTML = "回复失败：" + escapeHtml(e.message);
  } finally {
    chatBusy = false;
    document.getElementById("btnChatSend").disabled = false;
    document.getElementById("chatMessages").scrollTop =
      document.getElementById("chatMessages").scrollHeight;
  }
}

function clearChat() {
  chatHistory = [];
  const box = document.getElementById("chatMessages");
  box.innerHTML = "";
  // 恢复引导占位
  box.insertAdjacentHTML("beforeend",
    `<div class="chat-empty"><p>对话已清空，可继续提问。</p></div>`);
}

/* ---------- 事件绑定 ---------- */
document.getElementById("btnNewProject").onclick = () => {
  document.getElementById("newProjectModal").style.display = "flex";
};
/* 上传区 / 阶段进度卡片标题点击折叠展开（省出内容查看空间） */
["uploadCard", "stageCard", "traceCard"].forEach(id => {
  const card = document.getElementById(id);
  if (card) card.querySelector(".card-title").onclick = () => card.classList.toggle("collapsed");
});
/* 追溯矩阵：手动重算 + 只看待补全（按钮在标题行内，别顺带触发折叠） */
document.getElementById("btnTraceRefresh").onclick = e => {
  e.stopPropagation();
  traceData = null;
  loadTraceability();
};
/* 导出 Excel：服务端直接生成文件，浏览器接管下载 */
document.getElementById("btnTraceExport").onclick = e => {
  e.stopPropagation();
  if (!currentProjectId) return;
  window.open(`/api/projects/${currentProjectId}/export/traceability?format=excel`, "_blank");
};
document.getElementById("traceOnlyGaps").addEventListener("change", e => {
  traceOnlyGaps = e.target.checked;
  if (traceData) renderTrace(traceData);
});
document.getElementById("btnCancelNew").onclick = () => {
  document.getElementById("newProjectModal").style.display = "none";
};
document.getElementById("btnConfirmNew").onclick = createProject;
document.getElementById("newProjectName").onkeydown = e => {
  if (e.key === "Enter") createProject();
};
document.getElementById("btnUpload").onclick = uploadFiles;
document.getElementById("btnStart").onclick = startPipeline;
document.getElementById("btnReset").onclick = resetProject;
document.getElementById("btnResume").onclick = resumePipeline;
document.getElementById("btnCancel").onclick = cancelPipeline;
document.getElementById("btnApprove").onclick = () => submitReview(true);
document.getElementById("btnReject").onclick = () => submitReview(false);
document.getElementById("btnReleasePin").onclick = followCurrent;
document.getElementById("btnThemeClassic").onclick = () => setDiagramTheme("classic");
document.getElementById("btnThemeTech").onclick = () => setDiagramTheme("tech");
updateDiagramThemeUI();

/* AI 助手面板 + 侧栏收起 */
document.getElementById("btnToggleChat").onclick = () => toggleChatPanel(true);
document.getElementById("btnChatCollapse").onclick = () => chatPanel().classList.add("collapsed");
document.getElementById("btnChatClear").onclick = clearChat;
document.getElementById("btnChatSend").onclick = sendChat;
document.getElementById("chatInput").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
document.getElementById("btnToggleSidebar").onclick = toggleSidebar;

/* ---------- 初始化 ---------- */
loadProjects().catch(e => console.error(e));
api("/api/meta").then(m => {
  if (m.mock) document.getElementById("mockBadge").style.display = "block";
  // 阶段定义以后端为单一数据源：覆盖前端兜底默认值
  if (Array.isArray(m.stages) && m.stages.length) STAGES = m.stages;
  if (m.stage_names && typeof m.stage_names === "object") {
    STAGE_NAMES = Object.assign({}, STAGE_NAMES, m.stage_names);
  }
  // 联网检索：仅当后端配置了搜索 key 才显示开关（配置级总开关）
  if (m.web_search_enabled) {
    document.getElementById("webSwitchWrap").style.display = "flex";
  }
  // 若已有选中的项目，按新阶段定义重绘一次阶段轨道
  if (currentProjectId) refreshDetail();
}).catch(() => {});

/* 联网开关样式随勾选状态变化（默认关） */
document.getElementById("chatWebSwitch").addEventListener("change", e => {
  document.getElementById("webSwitchWrap").classList.toggle("on", e.target.checked);
});
