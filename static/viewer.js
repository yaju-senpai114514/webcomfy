"use strict";
// Read-only viewer: browse output images + stored configs. No editing, no comfy.
const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.status + " " + url);
  return r.json();
}
function fmtBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}
function fmtTime(epoch) {
  const d = new Date(epoch * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ---------- read-only config rendering (shared: configs tab + image meta) ----------
function renderBlocks(blocks, into) {
  for (const b of blocks || []) {
    const box = el("div", "ro-block");
    const head = el("div", "ro-bhead");
    head.append(el("span", "ro-trig", b.input || "(컨테이너)"));
    const items = b.items || [];
    head.append(el("span", "muted", ` ${items.length} 후보`));
    box.append(head);
    if (items.length) {
      const tbl = el("div", "ro-items");
      for (const it of items) {
        const row = el("div", "ro-irow" + (it.enabled ? "" : " off"));
        row.append(el("span", "ro-en", it.enabled ? "✓" : "✕"));
        row.append(el("span", "ro-w", String(it.weight)));
        const isNo = (it.text || "").trim() === "NOPROMPT";
        row.append(el("span", "ro-t" + (isNo ? " noprompt" : ""), it.text || ""));
        tbl.append(row);
      }
      box.append(tbl);
    }
    if (b.children && b.children.length) {
      const kids = el("div", "ro-children");
      renderBlocks(b.children, kids);
      box.append(kids);
    }
    into.append(box);
  }
}

function kvTable(obj) {
  const t = el("div", "ro-kv");
  for (const [k, v] of Object.entries(obj || {})) {
    t.append(el("span", "ro-k", k));
    let s;
    if (v === null || v === undefined) s = "—";
    else if (typeof v === "object") s = JSON.stringify(v);
    else s = String(v);
    t.append(el("span", "ro-v", s));
  }
  return t;
}

function section(title) {
  const s = el("section", "ro-sec");
  s.append(el("h3", null, title));
  return s;
}

function renderConfig(cfg) {
  const wrap = el("div", "ro-config");
  // positive
  const pos = section("프롬프트 (Positive)");
  const base = el("div", "ro-base");
  base.append(el("div", "ro-k", "base"));
  base.append(el("div", "ro-pre", (cfg.positive && cfg.positive.base) || "(없음)"));
  pos.append(base);
  if (cfg.positive && cfg.positive.blocks && cfg.positive.blocks.length) {
    const blocks = el("div", "ro-blocks");
    renderBlocks(cfg.positive.blocks, blocks);
    pos.append(blocks);
  }
  wrap.append(pos);
  // negative
  if (cfg.negative) {
    const neg = section("프롬프트 (Negative)");
    neg.append(el("div", "ro-pre", cfg.negative));
    wrap.append(neg);
  }
  // loras (list)
  if (cfg.loras && cfg.loras.length) {
    const ls = section("LoRA");
    for (const lo of cfg.loras) { ls.append(kvTable(lo)); }
    wrap.append(ls);
  }
  // remaining scalar sections, generic
  const SECTIONS = [["size", "이미지 크기"], ["models", "모델"], ["stage1", "1단계"],
    ["upscale", "업스케일"], ["stage2", "2단계"], ["advanced", "고급"], ["save", "저장"]];
  for (const [key, label] of SECTIONS) {
    if (cfg[key] && typeof cfg[key] === "object") {
      const s = section(label);
      s.append(kvTable(cfg[key]));
      wrap.append(s);
    }
  }
  return wrap;
}

// ---------- tabs ----------
const TABS = { gallery: "viewGallery", configs: "viewConfigs", live: "viewLive" };
function showTab(name) {
  for (const [n, id] of Object.entries(TABS)) $(id).classList.toggle("hidden", n !== name);
  $("tabGallery").classList.toggle("active", name === "gallery");
  $("tabConfigs").classList.toggle("active", name === "configs");
  $("tabLive").classList.toggle("active", name === "live");
  if (name === "configs" && !configsLoaded) loadConfigs();
  if (name === "live") startLive(); else stopLive();
}

// ---------- gallery ----------
let currentDir = null;
let galleryImages = [];   // images of the folder currently shown (lightbox nav source)
async function loadFolders() {
  const data = await getJSON("/api/folders");
  $("folderRoot").textContent = data.root ? "· " + data.root.split("/").slice(-1)[0] : "";
  const list = $("folderList");
  list.textContent = "";
  if (!data.folders.length) {
    list.append(el("div", "muted pad", "이미지가 없습니다."));
    $("thumbs").textContent = "";
    $("galleryHead").textContent = "";
    return;
  }
  for (const f of data.folders) {
    const row = el("div", "folder");
    row.append(el("div", "folder-name", f.dir || "(루트)"));
    row.append(el("div", "folder-sub", `${f.count}장 · ${fmtTime(f.mtime)}`));
    row.dataset.dir = f.dir;
    row.addEventListener("click", () => selectFolder(f.dir, row));
    list.append(row);
  }
  selectFolder(data.folders[0].dir, list.firstChild);
}
async function selectFolder(dir, row) {
  currentDir = dir;
  document.querySelectorAll("#folderList .folder").forEach((r) => r.classList.toggle("selected", r === row));
  const data = await getJSON("/api/images?dir=" + encodeURIComponent(dir));
  galleryImages = data.images;
  $("galleryHead").textContent = `${dir || "(루트)"} — ${data.images.length}장`;
  const grid = $("thumbs");
  grid.textContent = "";
  for (const im of data.images) {
    const card = el("div", "thumb");
    const img = el("img");
    img.loading = "lazy"; img.src = im.url; img.alt = im.name;
    card.append(img);
    const cap = el("div", "thumb-cap");
    cap.append(el("span", null, im.name));
    cap.append(el("span", "muted", fmtBytes(im.size)));
    card.append(cap);
    card.addEventListener("click", () => openLightbox(im));
    grid.append(card);
  }
}

// ---------- lightbox ----------
let lbList = null;   // images navigable with arrows/swipe (null = single, no nav)
let lbIndex = -1;
let lbMetaFolded = true;  // mobile detail panel state, sticky across opens

function setMetaFolded(folded) {
  lbMetaFolded = folded;
  $("lbMeta").classList.toggle("folded", folded);
  $("lbMetaToggle").textContent = folded ? "상세 정보 ▸" : "상세 정보 ▾";
}

function openImgLightbox(src) {
  lbList = null; lbIndex = -1;          // floating-preview image: no gallery nav
  $("lbImg").src = src;
  $("lbMeta").textContent = "";
  $("lbMeta").classList.add("hidden");   // image-only (no metadata panel)
  $("lbMetaToggle").classList.add("hidden");
  $("lightbox").classList.remove("hidden");
  document.body.classList.add("lb-open");
}

function openLightbox(im) {
  lbList = galleryImages;
  lbIndex = galleryImages.indexOf(im);
  $("lightbox").classList.remove("hidden");
  document.body.classList.add("lb-open");
  setMetaFolded(lbMetaFolded);  // apply last fold choice (not reset per image)
  renderLightbox(im);
}

// move to prev (-1) / next (+1) image within the current folder, wrapping around
function navLightbox(delta) {
  if (!lbList || lbList.length < 2 || lbIndex < 0) return;
  lbIndex = (lbIndex + delta + lbList.length) % lbList.length;
  renderLightbox(lbList[lbIndex]);
}

async function renderLightbox(im) {
  $("lbImg").src = im.url;
  const meta = $("lbMeta");
  meta.classList.remove("hidden");
  $("lbMetaToggle").classList.remove("hidden");
  meta.textContent = "";
  meta.append(el("div", "lb-title", im.name));
  meta.append(el("div", "muted small", `${fmtBytes(im.size)} · ${fmtTime(im.mtime)}`));
  $("lightbox").classList.remove("hidden");
  let data;
  try { data = await getJSON("/api/meta?path=" + encodeURIComponent(im.path)); }
  catch { data = { meta: null }; }
  if ($("lbImg").src.indexOf(im.url) === -1) return; // user moved on
  const m = data.meta;
  if (!m) { meta.append(el("div", "muted small pad", "임베드된 메타데이터 없음")); return; }
  if (m.master_seed != null) {
    const seed = el("div", "lb-row");
    seed.append(el("span", "ro-k", "master seed"));
    seed.append(el("span", "ro-v sel", String(m.master_seed)));
    meta.append(seed);
  }
  if (m.resolved && m.resolved.positive) {
    meta.append(el("div", "lb-sub", "최종 프롬프트"));
    meta.append(el("div", "ro-pre sel", m.resolved.positive));
  }
  if (m.config) {
    const det = el("details", "lb-cfg");
    det.append(el("summary", null, "전체 구성"));
    det.append(renderConfig(m.config));
    meta.append(det);
  }
}
function closeLightbox() { $("lightbox").classList.add("hidden"); document.body.classList.remove("lb-open"); $("lbImg").src = ""; lbList = null; lbIndex = -1; }
function lightboxOpen() { return !$("lightbox").classList.contains("hidden"); }

// ---------- configs tab ----------
let configsLoaded = false;
let configsData = null;
async function loadConfigs() {
  configsLoaded = true;
  const data = await getJSON("/api/configs");
  configsData = data;
  $("cfgCount").textContent = "· " + data.configs.length;
  const list = $("cfgList");
  list.textContent = "";
  for (const c of data.configs) {
    const row = el("div", "cfg-item");
    row.append(el("div", "cfg-name", c.name + (c.id === data.selected ? "  ●" : "")));
    row.append(el("div", "cfg-dates", `수정 ${c.modified.replace("T", " ")}`));
    row.addEventListener("click", () => selectConfig(c, row));
    list.append(row);
  }
  const initial = data.configs.find((c) => c.id === data.selected) || data.configs[0];
  if (initial) selectConfig(initial, list.children[data.configs.indexOf(initial)]);
}
function selectConfig(c, row) {
  document.querySelectorAll("#cfgList .cfg-item").forEach((r) => r.classList.toggle("selected", r === row));
  const det = $("cfgDetail");
  det.textContent = "";
  const h = el("div", "ro-head");
  h.append(el("span", "ro-title", c.name));
  h.append(el("span", "muted small", `생성 ${c.created.replace("T", " ")} · 수정 ${c.modified.replace("T", " ")}`));
  det.append(h);
  det.append(renderConfig(c.config));
}

// ---------- live (AFK loop mirror) ----------
let liveWs = null, livePending = null, livePoll = null, liveRunning = null;

function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
function setLiveStatus(t, err) { const s = $("liveStatus"); s.textContent = t; s.className = "status mt8" + (err ? " err" : ""); }
function setLiveImg(frameId, sizeId, blob) {
  const url = URL.createObjectURL(blob);
  const img = new Image();
  img.onload = () => { if (sizeId) $(sizeId).textContent = `${img.naturalWidth}×${img.naturalHeight}`; };
  img.src = url;
  const f = $(frameId); f.innerHTML = ""; f.appendChild(img);
}
function showResolved(positive, loras, masterSeed) {
  const lr = (loras && loras.length) ? escapeHtml(loras.join(", ")) : "(없음)";
  const ms = (masterSeed != null) ? `<b>master seed:</b> ${escapeHtml(String(masterSeed))}<br>` : "";
  $("resolved").innerHTML = `${ms}<b>적용 LoRA:</b> ${lr}<br><b>최종 프롬프트:</b> ${escapeHtml(positive)}`;
}

async function loadLiveConfig() {
  let data;
  try { data = await getJSON("/api/afk/config"); } catch { return null; }
  const box = $("liveConfig");
  box.textContent = "";
  if (data.config) box.append(renderConfig(data.config));
  else box.append(el("div", "muted pad", data.available === false
    ? "생성기와 함께 실행되어야 AFK 구성을 볼 수 있습니다."
    : "현재 생성 중인 구성이 없습니다."));
  return data;
}
function applyLiveStatus(st) {
  if (st.available === false) { setLiveStatus("AFK 루프에 연결할 수 없습니다 (생성기와 함께 실행 필요).", true); return; }
  const running = !!st.running;
  const tgt = st.target ? ` / ${st.target}` : "";
  if (running) {
    setLiveStatus(`AFK 동작 중 · ${st.count}${tgt}장 저장${st.last_error ? " · 최근 에러: " + st.last_error : ""}`, !!st.last_error);
  } else {
    setLiveStatus(`AFK 정지됨${st.count ? ` · 총 ${st.count}장 저장` : ""}${st.last_error ? " · 마지막 에러: " + st.last_error : ""}`, !!st.last_error);
    $("barFill").style.width = "0%";
  }
  if (running !== liveRunning) { liveRunning = running; loadLiveConfig(); }  // run changed → refresh config
}
async function pollLive() {
  try {
    const d = await getJSON("/api/afk/config");
    applyLiveStatus({ ...d.status, available: d.available });
  } catch { /* ignore */ }
}
function openLiveStream() {
  if (liveWs && (liveWs.readyState === 0 || liveWs.readyState === 1)) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  liveWs = new WebSocket(`${proto}://${location.host}/ws/afk`);
  liveWs.binaryType = "blob"; livePending = null;
  liveWs.onmessage = (e) => {
    if (typeof e.data !== "string") {
      if (!livePending) return;
      if (livePending.type === "preview") { $("previewCard").classList.remove("hidden"); setLiveImg("previewFrame", null, e.data); }
      else if (livePending.label === "intermediate") setLiveImg("baseFrame", "baseSize", e.data);
      else if (livePending.label === "final") setLiveImg("finalFrame", "finalSize", e.data);
      livePending = null; return;
    }
    const ev = JSON.parse(e.data);
    switch (ev.type) {
      case "resolved": showResolved(ev.positive, ev.loras, ev.master_seed); break;
      case "node": setLiveStatus("실행 중: " + ev.node, false); break;
      case "progress": if (ev.max) $("barFill").style.width = `${Math.round(ev.value / ev.max * 100)}%`; break;
      case "image": case "preview": livePending = ev; break;
      case "saved": if (ev.path) setLiveStatus("저장됨: " + ev.path, false); break;
      case "done": $("barFill").style.width = "100%"; break;
      case "afk": applyLiveStatus(ev); break;
    }
  };
  liveWs.onclose = () => { liveWs = null; };
  liveWs.onerror = () => { try { liveWs.close(); } catch {} };
}
function closeLiveStream() { if (liveWs) { try { liveWs.close(); } catch {} liveWs = null; } }

async function startLive() {
  $("floatPreview").classList.remove("hidden");
  liveRunning = null;
  // seed the preview with the last saved image, if any
  fetch("/api/afk/last.webp").then((r) => (r.ok ? r.blob() : null)).then((b) => { if (b) setLiveImg("finalFrame", "finalSize", b); }).catch(() => {});
  await pollLive();
  openLiveStream();
  if (!livePoll) livePoll = setInterval(pollLive, 4000);  // backup poll alongside the ws
}
function stopLive() {
  $("floatPreview").classList.add("hidden");
  closeLiveStream();
  if (livePoll) { clearInterval(livePoll); livePoll = null; }
}

// ---------- init ----------
// foldable sidebar: whole header row toggles, state sticks across reloads
function initSideFold(mainId, headId, storeKey) {
  const main = $(mainId);
  const apply = (folded) => {
    main.classList.toggle("side-folded", folded);
    try { localStorage.setItem(storeKey, folded ? "1" : "0"); } catch {}
  };
  $(headId).addEventListener("click", () => apply(!main.classList.contains("side-folded")));
  let folded = false;
  try { folded = localStorage.getItem(storeKey) === "1"; } catch {}
  apply(folded);
}

function init() {
  initSideFold("viewGallery", "sideHeadGallery", "viewer.sideFold.gallery");
  initSideFold("viewConfigs", "sideHeadConfigs", "viewer.sideFold.configs");
  $("tabGallery").addEventListener("click", () => showTab("gallery"));
  $("tabConfigs").addEventListener("click", () => showTab("configs"));
  $("tabLive").addEventListener("click", () => showTab("live"));
  $("refreshBtn").addEventListener("click", () => {
    if (!$("viewGallery").classList.contains("hidden")) loadFolders();
    else if (!$("viewConfigs").classList.contains("hidden")) loadConfigs();
    else loadLiveConfig();
  });
  // floating preview: collapse + click-to-zoom
  $("fpToggle").addEventListener("click", () => {
    const c = $("floatPreview").classList.toggle("collapsed");
    $("fpToggle").textContent = c ? "▸" : "▾";
  });
  $("floatPreview").addEventListener("click", (e) => {
    if (e.target.tagName === "IMG" && e.target.closest(".frame")) openImgLightbox(e.target.src);
  });
  $("lightbox").addEventListener("click", (e) => { if (e.target === $("lightbox") || e.target.classList.contains("lb-imgwrap")) closeLightbox(); });
  // keyboard: Esc closes, ←/→ step through the folder (PC)
  document.addEventListener("keydown", (e) => {
    if (!lightboxOpen()) return;
    if (e.key === "Escape") closeLightbox();
    else if (e.key === "ArrowLeft") { e.preventDefault(); navLightbox(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); navLightbox(1); }
  });
  // mobile detail fold
  $("lbMetaToggle").addEventListener("click", () => setMetaFolded(!lbMetaFolded));
  // touch: horizontal swipe steps through the folder (mobile). Direction is
  // locked on the first ~10px of movement; once horizontal, touchmove is
  // preventDefault-ed so the browser never turns the gesture into a scroll /
  // pull-to-refresh / history swipe. Vertical gestures are left native.
  // (CSS backs this up: touch-action:pan-y on the image, overscroll containment.)
  const lbImg = $("lbImg");
  let swStartX = null, swStartY = null, swDir = null; // "h" | "v" | null
  const swReset = () => { swStartX = null; swDir = null; };
  lbImg.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) { swReset(); return; } // pinch etc. → not a swipe
    swStartX = e.touches[0].clientX; swStartY = e.touches[0].clientY; swDir = null;
  }, { passive: true });
  lbImg.addEventListener("touchmove", (e) => {
    if (swStartX == null) return;
    if (e.touches.length !== 1) { swReset(); return; }
    const dx = e.touches[0].clientX - swStartX, dy = e.touches[0].clientY - swStartY;
    if (!swDir && (Math.abs(dx) > 10 || Math.abs(dy) > 10))
      swDir = Math.abs(dx) > Math.abs(dy) * 1.5 ? "h" : "v";
    if (swDir === "h" && e.cancelable) e.preventDefault(); // claim it from the browser
  }, { passive: false });
  lbImg.addEventListener("touchend", (e) => {
    if (swStartX == null) return;
    const dx = e.changedTouches[0].clientX - swStartX;
    const wasH = swDir === "h";
    swReset();
    if (wasH && Math.abs(dx) > 45) navLightbox(dx < 0 ? 1 : -1); // left → next
  });
  lbImg.addEventListener("touchcancel", swReset);

  loadFolders().catch((err) => { $("folderList").textContent = "로드 실패: " + err.message; });
}
document.addEventListener("DOMContentLoaded", init);
