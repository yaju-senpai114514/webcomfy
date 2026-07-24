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
let folderSortAsc = true;
try { folderSortAsc = localStorage.getItem("viewer.folderSort") !== "desc"; } catch {}

function syncFolderSortButton() {
  $("folderSort").textContent = folderSortAsc ? "ASC" : "DESC";
  $("folderSort").title = `폴더명 ${folderSortAsc ? "오름차순" : "내림차순"} · 클릭해서 전환`;
}

function renderBreadcrumb(dir) {
  const nav = $("folderBreadcrumb");
  nav.textContent = "";
  const parts = dir ? dir.split("/") : [];
  const addCrumb = (label, path) => {
    const b = el("button", "folder-crumb", label);
    b.type = "button";
    b.addEventListener("click", () => loadFolders(path));
    nav.append(b);
  };
  addCrumb("output", "");
  let path = "";
  for (const part of parts) {
    nav.append(el("span", "folder-sep", "›"));
    path = path ? `${path}/${part}` : part;
    addCrumb(part, path);
  }
}

async function loadFolders(dir = currentDir || "") {
  const data = await getJSON("/api/folders?dir=" + encodeURIComponent(dir));
  currentDir = data.current;
  $("folderRoot").textContent = data.root ? "· " + data.root.split("/").slice(-1)[0] : "";
  renderBreadcrumb(data.current);
  const list = $("folderList");
  list.textContent = "";

  if (data.parent !== null) {
    const up = el("div", "folder folder-up");
    up.append(el("div", "folder-name", "↰ 상위 폴더"));
    up.dataset.dir = data.parent;
    up.addEventListener("click", () => loadFolders(data.parent));
    list.append(up);
  }

  const folders = [...data.folders].sort((a, b) => {
    const cmp = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
    return folderSortAsc ? cmp : -cmp;
  });
  for (const f of folders) {
    const row = el("div", "folder");
    const name = el("div", "folder-name", f.name);
    name.append(el("span", "folder-enter", "›"));
    row.append(name);
    const detail = f.count ? `${f.count}장` : "이미지 없음";
    row.append(el("div", "folder-sub", `${detail}${f.has_children ? " · 하위 폴더 있음" : ""}`));
    row.dataset.dir = f.dir;
    row.addEventListener("click", () => loadFolders(f.dir));
    list.append(row);
  }

  if (!data.folders.length) {
    list.append(el("div", "muted pad", "하위 폴더가 없습니다."));
  }
  await selectFolder(data.current);
}
async function selectFolder(dir) {
  currentDir = dir;
  const data = await getJSON("/api/images?dir=" + encodeURIComponent(dir));
  galleryImages = data.images;
  $("galleryHead").textContent = `${dir || "(루트)"} — ${data.images.length}장`;
  const grid = $("thumbs");
  grid.textContent = "";
  if (!data.images.length) {
    grid.append(el("div", "muted pad", "이 폴더에는 이미지가 없습니다."));
    return;
  }
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

// 다중 백엔드 분산 AFK: 서버별 인페이지 카드
// (이름 · N장 · 진행바 · 상태 · 최종 프롬프트 · 라이브 프리뷰/base/hires 프레임)
const LIVE_SRV = {};  // server_id → {root, name, cnt, fill, stat, resolved, prevW, prevF, baseF, finalF, node}
function liveSrvRow(id, name) {
  if (LIVE_SRV[id]) {
    if (name) LIVE_SRV[id].name.textContent = name;
    return LIVE_SRV[id];
  }
  const root = el("div", "live-srv");
  const head = el("div", "ls-head");
  const nm = el("span", "ls-name", name || id);
  const cnt = el("span", "ls-cnt", "");
  head.append(nm, cnt);
  const bar = el("div", "bar mini");
  const fill = el("div");
  bar.append(fill);
  const stat = el("div", "ls-stat", "대기 중");
  const res = el("div", "ls-resolved");
  const framesBox = el("div", "ls-frames");
  const mkFrame = (cap, cls) => {
    const wrap = el("div", "ls-fwrap" + (cls ? " " + cls : ""));
    wrap.append(el("div", "ls-cap", cap));
    const f = el("div", "frame");
    wrap.append(f);
    framesBox.append(wrap);
    return { wrap, f };
  };
  const prev = mkFrame("라이브 프리뷰", "ls-prev hidden");  // 첫 프레임 도착 시 표시
  const base = mkFrame("① base");
  base.f.append(el("span", "empty", "—"));
  const fin = mkFrame("② hires");
  fin.f.append(el("span", "empty", "—"));
  root.append(head, bar, stat, res, framesBox);
  $("liveSrvs").append(root);
  LIVE_SRV[id] = {
    root, name: nm, cnt, fill, stat, resolved: res,
    prevW: prev.wrap, prevF: prev.f, baseF: base.f, finalF: fin.f, node: "",
  };
  return LIVE_SRV[id];
}
function clearLiveSrvs() {
  $("liveSrvs").textContent = "";
  for (const k of Object.keys(LIVE_SRV)) delete LIVE_SRV[k];
}

function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
function setLiveStatus(t, err) { const s = $("liveStatus"); s.textContent = t; s.className = "status mt8" + (err ? " err" : ""); }
function setLiveImgEl(frameEl, blob) {
  const img = new Image();
  img.src = URL.createObjectURL(blob);
  frameEl.innerHTML = ""; frameEl.appendChild(img);
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
  if (running !== liveRunning) {
    liveRunning = running;
    if (running) clearLiveSrvs();  // 새 런 → 이전 런의 서버 행 제거
    loadLiveConfig();              // run changed → refresh config
  }
  // 서버별 워커 배지 (분산 AFK: 각 서버가 자기 행에 카운트/정지/에러 표시)
  for (const w of st.workers || []) {
    const r = liveSrvRow(w.id, w.name);
    r.cnt.textContent = `${w.count}장` + (w.running ? "" : " · 정지");
    r.root.classList.toggle("stopped", !w.running);
    r.root.classList.toggle("errored", !!w.last_error);
    r.root.title = w.last_error || "";
  }
  const tgt = st.target ? ` / ${st.target}` : "";
  if (running) {
    setLiveStatus(`AFK 동작 중 · ${st.count}${tgt}장 저장${st.last_error ? " · 최근 에러: " + st.last_error : ""}`, !!st.last_error);
  } else {
    setLiveStatus(`AFK 정지됨${st.count ? ` · 총 ${st.count}장 저장` : ""}${st.last_error ? " · 마지막 에러: " + st.last_error : ""}`, !!st.last_error);
    $("barFill").style.width = "0%";
  }
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
      // 모든 이미지 프레임은 해당 서버 카드에 렌더링 (플로팅 프리뷰 없음).
      const prow = livePending.server_id ? liveSrvRow(livePending.server_id, livePending.server) : null;
      if (prow) {
        if (livePending.type === "preview") { prow.prevW.classList.remove("hidden"); setLiveImgEl(prow.prevF, e.data); }
        else if (livePending.label === "intermediate") setLiveImgEl(prow.baseF, e.data);
        else if (livePending.label === "final") setLiveImgEl(prow.finalF, e.data);
      }
      livePending = null; return;
    }
    const ev = JSON.parse(e.data);
    const row = ev.server_id ? liveSrvRow(ev.server_id, ev.server) : null;
    // 다중 서버 분산 시 진행 상태(node/progress)는 서버별 행에만 표시 —
    // 공용 상태줄/바는 단일 서버일 때만 미러링해 서로 덮어쓰지 않게 한다.
    const single = Object.keys(LIVE_SRV).length <= 1;
    switch (ev.type) {
      case "resolved":
        showResolved(ev.positive, ev.loras, ev.master_seed);
        if (row) row.resolved.textContent = ev.positive;
        break;
      case "node":
        if (single) setLiveStatus("실행 중: " + ev.node, false);
        if (row) { row.node = ev.node; row.stat.textContent = "실행 중: " + ev.node; }
        break;
      case "progress":
        if (ev.max) {
          const pct = `${Math.round(ev.value / ev.max * 100)}%`;
          if (single) $("barFill").style.width = pct;
          if (row) {
            row.fill.style.width = pct;
            row.stat.textContent = (row.node ? "실행 중: " + row.node + " · " : "") + pct;
          }
        }
        break;
      case "image": case "preview": livePending = ev; break;
      case "saved":
        if (ev.path) setLiveStatus(`저장됨${ev.server ? " [" + ev.server + "]" : ""}: ${ev.path}`, false);
        if (row && ev.path) { row.stat.textContent = "저장됨 ✓"; row.node = ""; }
        break;
      case "done":
        if (single) $("barFill").style.width = "100%";
        if (row) { row.fill.style.width = "100%"; row.stat.textContent = "완료 ✓"; row.node = ""; }
        break;
      case "afk": applyLiveStatus(ev); break;
    }
  };
  liveWs.onclose = () => { liveWs = null; };
  liveWs.onerror = () => { try { liveWs.close(); } catch {} };
}
function closeLiveStream() { if (liveWs) { try { liveWs.close(); } catch {} liveWs = null; } }

async function startLive() {
  liveRunning = null;
  await pollLive();
  openLiveStream();
  if (!livePoll) livePoll = setInterval(pollLive, 4000);  // backup poll alongside the ws
}
function stopLive() {
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
  syncFolderSortButton();
  $("folderSort").addEventListener("click", (e) => {
    e.stopPropagation();  // side-head click would otherwise fold the sidebar
    folderSortAsc = !folderSortAsc;
    try { localStorage.setItem("viewer.folderSort", folderSortAsc ? "asc" : "desc"); } catch {}
    syncFolderSortButton();
    loadFolders();
  });
  $("refreshBtn").addEventListener("click", () => {
    if (!$("viewGallery").classList.contains("hidden")) loadFolders();
    else if (!$("viewConfigs").classList.contains("hidden")) loadConfigs();
    else loadLiveConfig();
  });
  // live tab: server-card frames zoom into the lightbox
  $("liveSrvs").addEventListener("click", (e) => {
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
