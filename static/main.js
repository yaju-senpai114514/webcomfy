const $ = (id) => document.getElementById(id);
let OPTIONS = {};
let CONFIGS = [];      // config metadata list [{id,name,created,modified}]
let SELECTED = null;   // {id, name} of the active config
let SERVERS = [];      // ComfyUI server registry [{id,name,base_url,key_name,enabled}]
let GEN_SERVER = null; // server id targeted by interactive 생성
let LOCAL = {};        // 로컬 저장소 모델 목록 (서버에 없으면 생성 시 자동 전송됨)

// --- persisted UI view-state (localStorage) -------------------------------
const LS = {
  get(k, d) { try { const v = localStorage.getItem("cw:" + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("cw:" + k, JSON.stringify(v)); } catch {} },
};

// Remember a (vertically resizable) textarea's height under `key`.
function persistSize(el, key) {
  const saved = LS.get(key, null);
  if (saved) el.style.height = saved + "px";
  const ro = new ResizeObserver(() => {
    const h = el.offsetHeight;
    if (h > 24) LS.set(key, h);  // ignore 0 (panel collapsed) and stray tiny values
  });
  ro.observe(el);
}

// Persist each collapsible panel's open/closed state (keyed by its title).
function initPanels() {
  document.querySelectorAll("details.panel").forEach((d) => {
    const sum = d.querySelector("summary");
    const key = "panel:" + (sum ? sum.textContent.trim() : "");
    const saved = LS.get(key, null);
    if (saved !== null) d.open = saved;
    d.addEventListener("toggle", () => LS.set(key, d.open));
  });
}

function fillSelect(el, items, value) {
  el.innerHTML = "";
  for (const it of items) {
    const o = document.createElement("option");
    o.value = it; o.textContent = it;
    el.appendChild(o);
  }
  if (value !== undefined && value !== null) {
    if (![...el.options].some(o => o.value === value)) {
      const o = document.createElement("option");
      o.value = value; o.textContent = value + " (미설치?)";
      el.appendChild(o);
    }
    el.value = value;
  }
}

// 모델류 드롭다운: 서버 설치본 + 로컬 저장소 전용본(선택 시 생성 직전 자동 전송)을 합쳐 채운다.
function fillModelSelect(el, key, value) {
  const server = OPTIONS[key] || [];
  const localOnly = (LOCAL[key] || []).filter((n) => !server.includes(n));
  el.innerHTML = "";
  for (const n of server) {
    const o = document.createElement("option");
    o.value = n; o.textContent = n;
    el.appendChild(o);
  }
  for (const n of localOnly) {
    const o = document.createElement("option");
    o.value = n; o.textContent = n + " (로컬→자동전송)";
    el.appendChild(o);
  }
  if (value !== undefined && value !== null) {
    if (![...el.options].some((o) => o.value === value)) {
      const o = document.createElement("option");
      o.value = value; o.textContent = value + " (미설치?)";
      el.appendChild(o);
    }
    el.value = value;
  }
}

// --- wildcard candidate parsing (line grammar ↔ table rows) ----------------
// Mirrors prompt.py:parse_wildcards. A candidate line is `[# ][|n| ]text`
// where `#` disables it (comment) and `NOPROMPT` text means consume-only.
const NOPROMPT = "NOPROMPT";
const WC_WEIGHT_RE = /^\|\s*([0-9]*\.?[0-9]+)\s*\|(.*)$/;

function parseWildcards(str) {
  const rows = [];
  for (const raw of (str || "").split("\n")) {
    let line = raw.trim();
    if (!line) continue;            // blank line → no row (matches parse_wildcards)
    let enabled = true;
    if (line.startsWith("#")) { enabled = false; line = line.slice(1).trim(); }
    let weight = 1;
    const m = WC_WEIGHT_RE.exec(line);
    if (m) { weight = parseFloat(m[1]); line = m[2].trim(); }
    rows.push({ enabled, weight, text: line });
  }
  return rows;
}

// Grow a textarea to fit its content (used for the wrapping candidate-text cell).
function autoGrow(el) { el.style.height = "auto"; el.style.height = (el.scrollHeight + 2) + "px"; }

function serializeWildcards(rows) {
  return rows.map((r) => {
    let line = r.text || "";
    const w = Number(r.weight);
    if (Number.isFinite(w) && w !== 1) line = `|${w}| ${line}`;
    if (!r.enabled) line = `# ${line}`;
    return line;
  }).join("\n");
}

// Grow every candidate textarea inside a container once it's laid out.
function growAll(container) {
  requestAnimationFrame(() => container.querySelectorAll(".c-t").forEach(autoGrow));
}

// A children container shows its tree connectors only when it actually holds blocks.
function refreshKidsBox(kidsBox) {
  kidsBox.classList.toggle("has-kids", !!kidsBox.querySelector(":scope > .block"));
}

let dragBlock = null;  // the .block element currently being dragged (for reparenting)

// --- wildcard substitution blocks (recursive tree) -------------------------
// A block = {input, items:[{enabled,weight,text}], children:[block]}. After the
// block substitutes its `input` token, its child blocks roll further wildcards
// (recursively) — typically resolving __token__s a chosen candidate introduced.
function blockRow(block) {
  const wrap = document.createElement("div");
  wrap.className = "block";
  const head = document.createElement("div");
  head.className = "block-head";
  const input = document.createElement("input");
  input.placeholder = "input 토큰 (콤마 구분, 예: __hair__)";
  input.value = block.input || "";

  // ----- items table -----
  const table = document.createElement("div");
  table.className = "wc-table";
  const thead = document.createElement("div");
  thead.className = "wc-head";
  for (const [t, cls] of [["", "c-en"], ["가중치", "c-w"], ["후보 텍스트 (NOPROMPT = 삽입 안 함)", "c-t"], ["", "c-x"]]) {
    const s = document.createElement("span"); s.className = cls; s.textContent = t; thead.appendChild(s);
  }
  const tbody = document.createElement("div");
  tbody.className = "wc-body";

  const wcItem = (it) => {
    const row = document.createElement("div");
    row.className = "wc-row";
    const en = document.createElement("input");
    en.type = "checkbox"; en.className = "c-en"; en.checked = it.enabled !== false; en.title = "활성 / 비활성(주석)";
    const w = document.createElement("input");
    w.type = "number"; w.className = "c-w"; w.step = "0.1"; w.min = "0"; w.value = it.weight ?? 1; w.title = "가중치 (0이면 선택 안 됨)";
    const t = document.createElement("textarea");  // textarea so long candidates line-wrap
    t.className = "c-t"; t.rows = 1; t.spellcheck = false;
    t.value = it.text || ""; t.placeholder = "예: blue eyes  ·  twintails, __len__  ·  NOPROMPT";
    t.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });  // single-line
    const rm = document.createElement("button");
    rm.type = "button"; rm.className = "x c-x"; rm.textContent = "✕"; rm.title = "후보 삭제";
    rm.addEventListener("click", () => row.remove());
    const syncRow = () => {
      row.classList.toggle("off", !en.checked);
      row.classList.toggle("noprompt", t.value.trim() === NOPROMPT);
    };
    en.addEventListener("change", syncRow);
    t.addEventListener("input", () => { syncRow(); autoGrow(t); });
    syncRow();
    row.append(en, w, t, rm);
    row._get = () => ({ enabled: en.checked, weight: parseFloat(w.value), text: t.value.replace(/\s*\n\s*/g, " ").trim() });
    return row;
  };
  const addItem = (it) => {
    const row = wcItem(it || { enabled: true, weight: 1, text: "" });
    tbody.appendChild(row);
    if (!table.classList.contains("hidden")) autoGrow(row.querySelector(".c-t"));
    return row;
  };
  const tableItems = () => [...tbody.children].map((r) => r._get());
  const toItems = (items) => { tbody.innerHTML = ""; for (const it of items || []) addItem(it); };

  const tableActions = document.createElement("div");
  tableActions.className = "wc-actions";
  const addCand = document.createElement("button");
  addCand.type = "button"; addCand.className = "sm"; addCand.textContent = "+ 후보";
  addCand.addEventListener("click", () => addItem());
  const addNop = document.createElement("button");
  addNop.type = "button"; addNop.className = "sm"; addNop.textContent = "+ NOPROMPT";
  addNop.addEventListener("click", () => addItem({ enabled: true, weight: 1, text: NOPROMPT }));
  tableActions.append(addCand, addNop);
  table.append(thead, tbody, tableActions);

  // ----- text (raw) mode — bulk-edit the items as |n| text lines -----
  const list = document.createElement("textarea");
  list.className = "wildcards";
  list.spellcheck = false;
  list.placeholder = "한 줄에 하나씩. |2| 가중치, # 주석, NOPROMPT = consume만";
  // 줄바꿈(wrap) 토글: 끔=한 줄당 한 항목 + 줄무늬 / 켬=긴 줄 자동 줄바꿈
  const wrapBtn = document.createElement("button");
  wrapBtn.type = "button"; wrapBtn.className = "sm wrapbtn"; wrapBtn.title = "긴 줄 자동 줄바꿈 토글";
  let wrapped = LS.get("wrapDefault", false);  // 마지막으로 쓴 값을 기본값으로
  const syncWrap = () => {
    list.wrap = wrapped ? "soft" : "off";
    list.classList.toggle("wrap-on", wrapped);
    wrapBtn.textContent = wrapped ? "줄바꿈: 켬" : "줄바꿈: 끔";
    wrapBtn.classList.toggle("on", wrapped);
  };
  wrapBtn.addEventListener("click", () => { wrapped = !wrapped; LS.set("wrapDefault", wrapped); syncWrap(); });
  syncWrap();
  persistSize(list, "size:wildcards");  // 와일드카드 박스 크기 공유 기억

  // ----- view mode (table ⇄ text) for the items editor; children show below either way -----
  let mode = LS.get("wcViewMode", "table");
  const viewBtn = document.createElement("button");
  viewBtn.type = "button"; viewBtn.className = "sm viewbtn"; viewBtn.title = "표 / 텍스트 편집 전환";
  const applyMode = () => {
    const isTable = mode === "table";
    table.classList.toggle("hidden", !isTable);
    list.classList.toggle("hidden", isTable);
    wrapBtn.classList.toggle("hidden", isTable);
    viewBtn.textContent = isTable ? "텍스트로" : "표로";
    if (isTable) growAll(table);  // scrollHeight is 0 while hidden / before DOM attach
  };
  toItems(block.items || []);
  list.value = serializeWildcards(tableItems());
  viewBtn.addEventListener("click", () => {
    if (mode === "table") { list.value = serializeWildcards(tableItems()); mode = "text"; }
    else { toItems(parseWildcards(list.value)); mode = "table"; }
    LS.set("wcViewMode", mode);
    applyMode();
  });
  applyMode();

  // ----- nested child blocks (the recursive tree), at block level -----
  const kidsBox = document.createElement("div");
  kidsBox.className = "wc-children";
  const addChild = document.createElement("button");
  addChild.type = "button"; addChild.className = "sm wc-addchild"; addChild.textContent = "+ 하위 블록";
  kidsBox.appendChild(addChild);
  const childBlocks = () => [...kidsBox.querySelectorAll(":scope > .block")];
  addChild.addEventListener("click", () => {
    const cb = blockRow({ input: "", items: [], children: [] });
    kidsBox.insertBefore(cb, addChild);
    growAll(cb);
    refreshKidsBox(kidsBox);
  });
  for (const ch of block.children || []) kidsBox.insertBefore(blockRow(ch), addChild);
  refreshKidsBox(kidsBox);

  // ----- head: fold caret · drag handle · input · move · insert · view · delete -----
  const caret = document.createElement("button");
  caret.type = "button"; caret.className = "bfold"; caret.textContent = "▾"; caret.title = "블록 접기/펼치기 (하위 포함)";
  caret.addEventListener("click", () => {
    const folded = wrap.classList.toggle("collapsed");
    caret.textContent = folded ? "▸" : "▾";
  });
  const handle = document.createElement("span");
  handle.className = "bdrag"; handle.textContent = "⠿"; handle.draggable = true;
  handle.title = "드래그해서 다른 블록의 하위 블록으로 이동";
  handle.addEventListener("dragstart", (e) => {
    dragBlock = wrap; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", "");
    try { e.dataTransfer.setDragImage(wrap, 12, 12); } catch {}
    requestAnimationFrame(() => wrap.classList.add("dragging"));
  });
  handle.addEventListener("dragend", () => {
    wrap.classList.remove("dragging"); dragBlock = null;
    document.querySelectorAll(".drop-into").forEach((el) => el.classList.remove("drop-into"));
  });
  // wrap is a drop target: dropping a dragged block makes it a child of this block.
  // acceptDrop() is the shared reparent used by both native DnD (desktop) and the
  // pointer-based touch fallback below; expose it so a touch-drop can reach an
  // arbitrary target block's closure via the target element.
  const acceptDrop = (srcBlock) => {
    if (!srcBlock || srcBlock === wrap || srcBlock.contains(wrap)) return false;
    const src = srcBlock.parentElement;
    kidsBox.insertBefore(srcBlock, addChild);
    if (wrap.classList.contains("collapsed")) { wrap.classList.remove("collapsed"); caret.textContent = "▾"; }
    refreshKidsBox(kidsBox);
    if (src && src.classList.contains("wc-children")) refreshKidsBox(src);
    growAll(srcBlock);
    return true;
  };
  wrap._acceptDrop = acceptDrop;
  const canDrop = () => dragBlock && dragBlock !== wrap && !dragBlock.contains(wrap);
  wrap.addEventListener("dragover", (e) => {
    if (!canDrop()) return;
    e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = "move";
    wrap.classList.add("drop-into");
  });
  wrap.addEventListener("dragleave", (e) => {
    if (!wrap.contains(e.relatedTarget)) wrap.classList.remove("drop-into");
  });
  wrap.addEventListener("drop", (e) => {
    if (!canDrop()) return;
    e.preventDefault(); e.stopPropagation(); wrap.classList.remove("drop-into");
    acceptDrop(dragBlock);
  });

  // touch fallback: HTML5 DnD never fires from touch, so drive it with pointer
  // events. Only for touch — desktop mouse keeps using native DnD above.
  let touchDragging = false;
  const clearDropHints = () => document.querySelectorAll(".drop-into").forEach((el) => el.classList.remove("drop-into"));
  const targetAt = (x, y) => {
    const el = document.elementFromPoint(x, y);
    return el ? el.closest(".block") : null;
  };
  handle.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "touch") return;
    e.preventDefault();
    touchDragging = true; dragBlock = wrap;
    wrap.classList.add("dragging");
    try { handle.setPointerCapture(e.pointerId); } catch {}
  });
  handle.addEventListener("pointermove", (e) => {
    if (!touchDragging) return;
    e.preventDefault();
    clearDropHints();
    const tgt = targetAt(e.clientX, e.clientY);
    if (tgt && tgt !== wrap && !wrap.contains(tgt)) tgt.classList.add("drop-into");
  });
  const endTouchDrag = (e, doDrop) => {
    if (!touchDragging) return;
    touchDragging = false;
    if (doDrop) {
      const tgt = targetAt(e.clientX, e.clientY);
      if (tgt && tgt._acceptDrop) tgt._acceptDrop(wrap);
    }
    clearDropHints();
    wrap.classList.remove("dragging"); dragBlock = null;
  };
  handle.addEventListener("pointerup", (e) => endTouchDrag(e, true));
  handle.addEventListener("pointercancel", (e) => endTouchDrag(e, false));

  const up = document.createElement("button");
  up.type = "button"; up.className = "bmv"; up.textContent = "▲"; up.title = "위로 이동";
  up.addEventListener("click", () => { const p = wrap.previousElementSibling; if (p) wrap.parentNode.insertBefore(wrap, p); });
  const down = document.createElement("button");
  down.type = "button"; down.className = "bmv"; down.textContent = "▼"; down.title = "아래로 이동";
  down.addEventListener("click", () => { const n = wrap.nextElementSibling; if (n && n.classList.contains("block")) wrap.parentNode.insertBefore(n, wrap); });
  const ins = document.createElement("button");
  ins.type = "button"; ins.className = "bmv"; ins.textContent = "＋"; ins.title = "이 블록 바로 아래에 블록 추가";
  ins.addEventListener("click", () => {
    const nb = blockRow({ input: "", items: [], children: [] });
    wrap.parentNode.insertBefore(nb, wrap.nextElementSibling);  // sits before a trailing add-child button if any
    const pk = wrap.parentElement;
    if (pk && pk.classList.contains("wc-children")) refreshKidsBox(pk);
    growAll(nb);
  });
  const rm = document.createElement("button");
  rm.className = "x"; rm.textContent = "✕"; rm.title = "블록 삭제";
  rm.addEventListener("click", () => { const src = wrap.parentElement; wrap.remove(); if (src && src.classList.contains("wc-children")) refreshKidsBox(src); });
  head.append(caret, handle, input, up, down, ins, viewBtn, wrapBtn, rm);
  wrap.append(head, table, list, kidsBox);
  wrap._get = () => ({
    input: input.value,
    items: mode === "table" ? tableItems() : parseWildcards(list.value),
    children: childBlocks().map((b) => b._get()),
  });
  return wrap;
}

// --- LoRA rows ------------------------------------------------------------
function loraRow(lora) {
  const row = document.createElement("div");
  row.className = "row";
  const name = document.createElement("select");
  name.className = "lname"; fillModelSelect(name, "loras", lora.name);
  const str = document.createElement("input");
  str.className = "lstr"; str.type = "number"; str.step = "0.05"; str.value = lora.strength ?? 1;
  const mode = document.createElement("select");
  mode.className = "lmode";
  for (const m of ["conditional", "always"]) { const o = document.createElement("option"); o.value = o.textContent = m; mode.appendChild(o); }
  mode.value = lora.mode || "conditional";
  const trig = document.createElement("input");
  trig.className = "ltrig"; trig.placeholder = "트리거 토큰"; trig.value = lora.trigger || "";
  const syncMode = () => { trig.disabled = mode.value === "always"; trig.classList.toggle("dim", mode.value === "always"); };
  mode.addEventListener("change", syncMode); syncMode();
  const rm = document.createElement("button");
  rm.className = "x lx"; rm.textContent = "✕"; rm.title = "삭제";
  rm.addEventListener("click", () => row.remove());
  row.append(name, str, mode, trig, rm);
  row._get = () => ({ name: name.value, strength: parseFloat(str.value), mode: mode.value, trigger: trig.value });
  return row;
}

function loadDefaults(d) {
  const p = d.positive || { base: "", blocks: [] };
  $("base").value = p.base || "";
  $("blocks").innerHTML = "";
  for (const b of p.blocks || []) $("blocks").appendChild(blockRow(b));
  $("negative").value = d.negative || "";

  fillModelSelect($("unet_name"), "unets", d.models.unet_name);
  fillModelSelect($("clip_name"), "clips", d.models.clip_name);
  fillModelSelect($("vae_name"), "vaes", d.models.vae_name);
  $("width").value = d.size.width; $("height").value = d.size.height; $("batch_size").value = d.size.batch_size;

  $("loraRows").innerHTML = "";
  for (const l of d.loras || []) $("loraRows").appendChild(loraRow(l));

  $("s1_seed").value = d.stage1.seed; $("s1_steps").value = d.stage1.steps;
  $("s1_cfg").value = d.stage1.cfg; $("s1_denoise").value = d.stage1.denoise;
  fillSelect($("s1_sampler_name"), OPTIONS.samplers || [], d.stage1.sampler_name);
  fillSelect($("s1_scheduler"), OPTIONS.schedulers || [], d.stage1.scheduler);

  fillModelSelect($("up_model_name"), "upscale_models", d.upscale.model_name);
  $("up_scale_by").value = d.upscale.scale_by;

  $("s2_noise_seed").value = d.stage2.noise_seed; $("s2_steps").value = d.stage2.steps;
  $("s2_start_at_step").value = d.stage2.start_at_step; $("s2_end_at_step").value = d.stage2.end_at_step;
  $("s2_cfg").value = d.stage2.cfg; $("s2_add_noise").value = d.stage2.add_noise;
  fillSelect($("s2_sampler_name"), OPTIONS.samplers || [], d.stage2.sampler_name);
  fillSelect($("s2_scheduler"), OPTIONS.schedulers || [], d.stage2.scheduler);

  const s = d.save || {};
  $("save_enabled").checked = !!s.enabled;
  $("save_dir").value = s.dir ?? "output";
  $("save_template").value = s.path_template ?? "{date}/{time}-{seed}.{ext}";
  $("save_quality").value = s.webp_quality ?? 90;
  $("save_lossless").checked = !!s.webp_lossless;
  $("afk_count").value = s.afk_count ?? 0;

  const a = d.advanced;
  $("adv_shift").value = a.shift; $("adv_smc_preset").value = a.smc_preset;
  $("adv_lambda_l").value = a.lambda_l; $("adv_lambda_h").value = a.lambda_h;
  $("adv_alpha_l").value = a.alpha_l; $("adv_alpha_h").value = a.alpha_h;
  $("adv_smc_lambda").value = a.smc_lambda; $("adv_smc_k").value = a.smc_k;
  $("adv_dcw_enabled").checked = a.dcw_enabled; $("adv_cwm_enabled").checked = a.cwm_enabled;
  updateHints();
  analyzeConfig();  // surface dead branches / unsubstituted tokens for the loaded config
}

function updateHints() {
  const w = +$("width").value, h = +$("height").value, sc = +$("up_scale_by").value;
  $("upHint").textContent = `결과 ≈ ${Math.round(w*4*sc)} × ${Math.round(h*4*sc)} px`;
  const st = +$("s2_steps").value, sa = +$("s2_start_at_step").value;
  const dn = st > 0 ? Math.max(0, (st - sa) / st) : 0;
  $("s2Hint").textContent = `유효 디노이즈 ≈ ${(dn*100).toFixed(0)}% (${sa}→${st} / ${st} 스텝)`;
}
["width","height","up_scale_by","s2_steps","s2_start_at_step"].forEach(id => $(id).addEventListener("input", updateHints));

function gatherConfig() {
  return {
    positive: {
      base: $("base").value,
      blocks: [...$("blocks").children].map(b => b._get()),
    },
    negative: $("negative").value,
    models: { unet_name: $("unet_name").value, clip_name: $("clip_name").value, vae_name: $("vae_name").value, weight_dtype: "default", clip_type: "stable_diffusion" },
    size: { width: +$("width").value, height: +$("height").value, batch_size: +$("batch_size").value },
    loras: [...$("loraRows").children].map(r => r._get()),
    stage1: { seed: +$("s1_seed").value, steps: +$("s1_steps").value, cfg: +$("s1_cfg").value, denoise: +$("s1_denoise").value, sampler_name: $("s1_sampler_name").value, scheduler: $("s1_scheduler").value },
    upscale: { model_name: $("up_model_name").value, method: "nearest-exact", scale_by: +$("up_scale_by").value },
    stage2: { noise_seed: +$("s2_noise_seed").value, steps: +$("s2_steps").value, start_at_step: +$("s2_start_at_step").value, end_at_step: +$("s2_end_at_step").value, cfg: +$("s2_cfg").value, add_noise: $("s2_add_noise").value, return_with_leftover_noise: "disable", sampler_name: $("s2_sampler_name").value, scheduler: $("s2_scheduler").value },
    advanced: { shift: +$("adv_shift").value, smc_preset: $("adv_smc_preset").value, lambda_l: +$("adv_lambda_l").value, lambda_h: +$("adv_lambda_h").value, alpha_l: +$("adv_alpha_l").value, alpha_h: +$("adv_alpha_h").value, smc_lambda: +$("adv_smc_lambda").value, smc_k: +$("adv_smc_k").value, dcw_enabled: $("adv_dcw_enabled").checked, cwm_enabled: $("adv_cwm_enabled").checked },
    save: { enabled: $("save_enabled").checked, dir: $("save_dir").value, path_template: $("save_template").value, webp_quality: +$("save_quality").value, webp_lossless: $("save_lossless").checked, afk_count: +$("afk_count").value, cname: SELECTED ? SELECTED.name : "" },
  };
}

// --- static config analysis (dead branches / never-substituted tokens) -----
const ISSUE_TAG = { dead_block: "never-enabled", no_candidate: "후보 없음", unsubstituted_token: "치환 불가 토큰", out_of_scope: "부모 밖 트리거", strict_leak: "트리 누수" };
function renderIssues(issues) {
  const out = $("analyzeOut");
  out.innerHTML = "";
  if (!issues || !issues.length) {
    out.innerHTML = '<div class="ana-ok">✓ 문제 없음 — dead branch나 치환 불가 토큰이 없습니다.</div>';
    return;
  }
  const errs = issues.filter((i) => i.severity === "error").length;
  const head = document.createElement("div");
  head.className = "ana-head" + (errs ? " err" : "");
  head.textContent = `⚠ 오류 ${errs} · 경고 ${issues.length - errs}`;
  out.appendChild(head);
  for (const i of issues) {
    const d = document.createElement("div");
    d.className = "issue " + i.severity;
    const tag = document.createElement("span"); tag.className = "ikind"; tag.textContent = ISSUE_TAG[i.kind] || i.kind;
    d.appendChild(tag);
    if (i.path) { const p = document.createElement("span"); p.className = "ipath"; p.textContent = i.path; d.appendChild(p); }
    const m = document.createElement("div"); m.className = "imsg"; m.textContent = i.message; d.appendChild(m);
    out.appendChild(d);
  }
}
async function analyzeConfig() {
  const out = $("analyzeOut");
  out.innerHTML = '<div class="ana-busy">검증 중…</div>';
  try {
    const r = await fetch("/api/analyze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(gatherConfig()) });
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    renderIssues(data.issues);
    return data.issues || [];
  } catch {
    out.innerHTML = '<div class="ana-head err">검증 요청 실패</div>';
    return [];
  }
}

function setImg(frameId, sizeId, blob) {
  const url = URL.createObjectURL(blob);
  const img = new Image();
  img.onload = () => { if (sizeId) $(sizeId).textContent = `${img.naturalWidth}×${img.naturalHeight}`; };
  img.src = url;
  const frame = $(frameId); frame.innerHTML = ""; frame.appendChild(img);
}

function setImgEl(frameEl, blob) {
  const img = new Image();
  img.src = URL.createObjectURL(blob);
  frameEl.innerHTML = ""; frameEl.appendChild(img);
}

// 투명 프로비저닝(로컬 저장소 → 서버 모델 자동 전송) 상태 문구
function provisionText(ev) {
  const pct = ev.total ? ` ${Math.round((ev.bytes_done || 0) / ev.total * 100)}%` : "";
  const srv = ev.server ? ` → ${ev.server}` : "";
  if (ev.state === "uploading") return `모델 자동 전송 중${srv}: ${ev.name}${pct}`;
  if (ev.state === "done") return `모델 전송 완료${srv}: ${ev.name}`;
  return `모델 전송 실패${srv}: ${ev.name} — ${ev.error || ""}`;
}

function showResolved(positive, loras, masterSeed) {
  const el = $("resolved");
  const lr = (loras && loras.length) ? loras.join(", ") : "(없음)";
  const ms = (masterSeed != null) ? `<b>master seed:</b> ${masterSeed}<br>` : "";
  el.innerHTML = `${ms}<b>적용 LoRA:</b> ${lr}<br><b>최종 프롬프트:</b> ${positive}`;
}

let ws = null;
function generate() {
  if (ws) { try { ws.close(); } catch {} }
  if (SELECTED) saveCurrentConfig();  // 생성 시 현재 구성 자동저장(→ 저장이 린터 체크까지 트리거)
  const cfg = gatherConfig();
  $("genBtn").disabled = true;
  setStatus("연결 중...", false);
  $("barFill").style.width = "0%";
  $("resolved").textContent = "";
  $("baseFrame").innerHTML = '<span class="empty">…</span>';
  $("finalFrame").innerHTML = '<span class="empty">…</span>';

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/generate`);
  ws.binaryType = "blob";
  let pending = null;

  ws.onopen = () => {
    const ms = $("repro_seed").value.trim();
    if (ms) cfg.master_seed = ms;  // 빈칸이면 서버가 랜덤 마스터 시드 선택
    if (GEN_SERVER) cfg.server_id = GEN_SERVER;  // 헤더에서 고른 생성 대상 서버
    ws.send(JSON.stringify(cfg));
  };
  ws.onmessage = (e) => {
    if (typeof e.data !== "string") {            // binary image frame
      if (!pending) return;
      if (pending.type === "preview") {
        $("previewCard").classList.remove("hidden");
        setImg("previewFrame", null, e.data);
      } else if (pending.label === "intermediate") {
        setImg("baseFrame", "baseSize", e.data);
      } else if (pending.label === "final") {
        setImg("finalFrame", "finalSize", e.data);
      }
      pending = null;
      return;
    }
    const ev = JSON.parse(e.data);
    switch (ev.type) {
      case "queued": setStatus(`큐 등록됨 (prompt ${ev.prompt_id.slice(0,8)})`, false); break;
      case "provision":
        setStatus(provisionText(ev), ev.state === "failed");
        if (ev.state === "uploading" && ev.total) $("barFill").style.width = `${Math.round(ev.bytes_done / ev.total * 100)}%`;
        if (ev.state === "done") $("barFill").style.width = "0%";
        break;
      case "resolved": showResolved(ev.positive, ev.loras, ev.master_seed); break;
      case "node": setStatus(`실행 중: ${ev.node}`, false); break;
      case "progress":
        if (ev.max) $("barFill").style.width = `${Math.round(ev.value/ev.max*100)}%`;
        break;
      case "image": case "preview": pending = ev; break;
      case "saved":
        if (ev.error) setStatus("저장 실패: " + ev.error, true);
        else setStatus("저장됨: " + ev.path, false);
        break;
      case "error": setStatus("에러: " + JSON.stringify(ev.data), true); finish(); break;
      case "done": $("barFill").style.width = "100%"; setStatus("완료 ✓", false); finish(); break;
    }
  };
  ws.onerror = () => { setStatus("웹소켓 오류", true); finish(); };
  ws.onclose = () => finish();
}
function finish() { $("genBtn").disabled = false; $("previewCard").classList.add("hidden"); }
function setStatus(t, err) { const s = $("status"); s.textContent = t; s.className = "status" + (err ? " err" : ""); }

// --- ComfyUI server registry (multi-server orchestration) ------------------
async function loadServers() {
  const data = await (await fetch("/api/servers")).json();
  SERVERS = data.servers || [];
  const enabled = SERVERS.filter((s) => s.enabled);
  const saved = LS.get("genServer", null);
  GEN_SERVER = (saved && enabled.some((s) => s.id === saved))
    ? saved
    : (data.default || (enabled[0] ? enabled[0].id : null));
  renderServerSel();
  renderServerPanel();
  renderAfkServers();
}

function renderServerSel() {
  const sel = $("serverSel");
  sel.innerHTML = "";
  for (const s of SERVERS.filter((s) => s.enabled)) {
    const o = document.createElement("option");
    o.value = s.id; o.textContent = s.name;
    sel.appendChild(o);
  }
  if (GEN_SERVER) sel.value = GEN_SERVER;
}

// Reload dropdown options from the currently-selected generation server,
// keeping the form's values (re-fills selects; unknown values get "(미설치?)").
async function reloadOptions() {
  const q = GEN_SERVER ? "?server_id=" + encodeURIComponent(GEN_SERVER) : "";
  const data = await (await fetch("/api/options" + q)).json();
  OPTIONS = data.options || {};
  LOCAL = data.local || {};
  $("baseUrl").textContent = "→ " + (data.base_url || "");
  if (OPTIONS.error) setStatus("ComfyUI 연결 실패: " + OPTIONS.error, true);
  return data;
}

function srvRow(s) {
  const row = document.createElement("div");
  row.className = "srv-row" + (s.enabled ? "" : " off");
  const dot = document.createElement("span");
  dot.className = "dot unknown"; dot.dataset.dot = s.id; dot.textContent = "●";
  const en = document.createElement("input");
  en.type = "checkbox"; en.checked = s.enabled; en.title = "활성/비활성";
  en.addEventListener("change", async () => {
    await fetch("/api/servers/" + s.id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: en.checked }) });
    loadServers();
  });
  const nm = document.createElement("b"); nm.className = "srv-name"; nm.textContent = s.name;
  const mapi = document.createElement("span");
  mapi.className = "srv-mapi hidden"; mapi.dataset.mapi = s.id; mapi.textContent = "M";
  mapi.title = "모델 관리 API 사용 가능";
  const url = document.createElement("span"); url.className = "srv-url"; url.textContent = s.base_url;
  const edit = document.createElement("button");
  edit.className = "sm"; edit.textContent = "✎"; edit.title = "이름/URL/토큰 수정";
  edit.addEventListener("click", () => srvEdit(s));
  const rm = document.createElement("button");
  rm.className = "x"; rm.textContent = "✕"; rm.title = "서버 삭제";
  rm.addEventListener("click", () => srvDel(s));
  row.append(dot, en, nm, mapi, url, edit, rm);
  return row;
}

function renderServerPanel() {
  const box = $("srvList"); box.innerHTML = "";
  for (const s of SERVERS) box.appendChild(srvRow(s));
  if (!SERVERS.length) box.innerHTML = '<span class="hint">등록된 서버 없음 — + 서버 추가</span>';
  for (const s of SERVERS) checkHealth(s.id);
}

async function checkHealth(id) {
  const dot = document.querySelector(`[data-dot="${id}"]`);
  const mapi = document.querySelector(`[data-mapi="${id}"]`);
  if (!dot) return;
  dot.className = "dot unknown"; dot.title = "확인 중...";
  try {
    const h = await (await fetch(`/api/servers/${id}/health`)).json();
    dot.className = "dot " + (h.ok ? "ok" : "bad");
    dot.title = h.ok
      ? `응답 OK · 큐 실행 ${h.queue_running} / 대기 ${h.queue_pending}` + (h.models_api ? " · 모델 API OK" : " · 모델 API 없음")
      : "연결 실패: " + (h.error || "");
    if (mapi) mapi.classList.toggle("hidden", !h.models_api);
  } catch {
    dot.className = "dot bad"; dot.title = "헬스체크 실패";
  }
}

async function srvAdd() {
  const name = prompt("서버 이름", "gpu-" + (SERVERS.length + 1)); if (!name) return;
  const base_url = prompt("ComfyUI 주소 (http://host:port)", "http://"); if (!base_url) return;
  const key_name = prompt("서명 키 이름 (keys/<name>.key, 빈칸=무인증)", "") ?? "";
  const r = await fetch("/api/servers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, base_url, key_name }) });
  if (!r.ok) { setStatus("서버 추가 실패", true); return; }
  await loadServers();
  setStatus(`서버 '${name}' 추가됨`, false);
}

async function srvEdit(s) {
  const name = prompt("서버 이름", s.name); if (name === null) return;
  const base_url = prompt("ComfyUI 주소", s.base_url); if (base_url === null) return;
  const key_name = prompt("서명 키 이름 (keys/<name>.key, 빈칸=무인증)", s.key_name || ""); if (key_name === null) return;
  const r = await fetch("/api/servers/" + s.id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, base_url, key_name }) });
  if (!r.ok) { setStatus("서버 수정 실패", true); return; }
  await loadServers();
  reloadOptions();
}

async function srvDel(s) {
  if (!confirm(`서버 '${s.name}' 삭제할까요? (파일은 건드리지 않음)`)) return;
  await fetch("/api/servers/" + s.id, { method: "DELETE" });
  await loadServers();
}

// AFK 분산 대상 체크박스 (기본: 활성 서버 전부)
function renderAfkServers() {
  const box = $("afkServers"); box.innerHTML = "";
  const saved = LS.get("afkServers", null);  // 저장된 선택(id 배열), null = 전부
  for (const s of SERVERS.filter((s) => s.enabled)) {
    const lbl = document.createElement("label"); lbl.className = "inline afk-srv";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = s.id;
    cb.checked = !saved || saved.includes(s.id);
    cb.addEventListener("change", () => LS.set("afkServers", afkSelectedIds()));
    lbl.append(cb, document.createTextNode(" " + s.name));
    box.appendChild(lbl);
  }
  if (!box.children.length) box.innerHTML = '<span class="hint">활성 서버 없음</span>';
}
function afkSelectedIds() {
  return [...$("afkServers").querySelectorAll("input:checked")].map((c) => c.value);
}
function afkSetAll(on) {
  $("afkServers").querySelectorAll("input").forEach((c) => { if (!c.disabled) c.checked = on; });
  LS.set("afkServers", afkSelectedIds());
}

// --- AFK background loop + live stream -------------------------------------
let afkTimer = null;
let afkWs = null;
let afkRunning = false;
function setAfk(text, err) { const s = $("afkStatus"); s.textContent = text; s.className = "status" + (err ? " err" : ""); }

// Per-server live cards: each AFK worker renders its own progress/frames so
// several servers generating at once don't fight over the shared 대화형 cards.
const AFK_CARDS = {};  // server_id → {root, stat, fill, prog, resolved, baseF, finalF, node}
function afkCard(id, name) {
  if (AFK_CARDS[id]) return AFK_CARDS[id];
  const root = document.createElement("div");
  root.className = "afk-card";
  root.innerHTML =
    '<h3><span class="acname"></span><span class="acstat"></span></h3>' +
    '<div class="bar mini"><div class="fill"></div></div>' +
    '<div class="acprog">대기 중</div>' +
    '<div class="resolved acres"></div>' +
    '<div class="ac-frames">' +
    '<div class="ac-fwrap ac-prev hidden"><div class="ac-cap">라이브 프리뷰</div><div class="frame"></div></div>' +
    '<div class="ac-fwrap"><div class="ac-cap">① base</div><div class="frame"><span class="empty">—</span></div></div>' +
    '<div class="ac-fwrap"><div class="ac-cap">② hires</div><div class="frame"><span class="empty">—</span></div></div>' +
    '</div>';
  root.querySelector(".acname").textContent = name || id;
  $("afkCards").appendChild(root);
  const frames = root.querySelectorAll(".ac-frames .frame");
  AFK_CARDS[id] = {
    root,
    stat: root.querySelector(".acstat"),
    fill: root.querySelector(".fill"),
    prog: root.querySelector(".acprog"),
    resolved: root.querySelector(".acres"),
    prevW: root.querySelector(".ac-prev"),  // 프리뷰 프레임 도착 전까지 숨김
    prevF: frames[0], baseF: frames[1], finalF: frames[2],
    node: "",  // 마지막 node 이벤트 — progress %와 합쳐서 표시
  };
  return AFK_CARDS[id];
}
function clearAfkCards() {
  $("afkCards").innerHTML = "";
  for (const k of Object.keys(AFK_CARDS)) delete AFK_CARDS[k];
}

// Apply a status object (from polling or from a ws "afk" event) to the UI.
function applyAfkStatus(st) {
  afkRunning = !!st.running;
  $("afkStart").disabled = st.running;
  $("afkStop").disabled = !st.running;
  $("afk_count").disabled = st.running;
  // 실행 중엔 분산 대상 변경 불가(서버 목록은 시작 시점 스냅샷) — 잠가서 명확히
  $("afkServers").querySelectorAll("input").forEach((c) => { c.disabled = !!st.running; });
  $("afkSrvAll").disabled = $("afkSrvNone").disabled = !!st.running;
  const hdr = $("afkBtn");  // header shortcut mirrors the loop state
  hdr.textContent = st.running ? "■ AFK 정지" : "AFK ▶";
  hdr.classList.toggle("running", !!st.running);

  // Per-server worker badges (multi-server fan-out).
  const workers = st.workers || [];
  for (const w of workers) {
    const c = afkCard(w.id, w.name);
    c.stat.textContent = `${w.count}장` + (w.running ? "" : " · 정지") + (w.last_error ? " · ⚠" : "");
    c.stat.title = w.last_error || "";
    c.root.classList.toggle("stopped", !w.running);
    c.root.classList.toggle("errored", !!w.last_error);
  }
  const alive = workers.filter((w) => w.running).length;
  const srv = workers.length > 1 ? ` · 서버 ${alive}/${workers.length}` : "";

  const tgt = st.target ? ` / ${st.target}` : "";
  if (st.running) {
    setAfk(`AFK 동작 중 · ${st.count}${tgt}장 저장${srv}${st.last_error ? " · 최근 에러: " + st.last_error : ""}`, !!st.last_error);
    openAfkStream();
    if (!afkTimer) afkTimer = setInterval(afkPoll, 3000);  // 백업 폴링
  } else {
    setAfk(`AFK 정지됨${st.count ? ` · 총 ${st.count}장 저장` : ""}${st.last_error ? " · 마지막 에러: " + st.last_error : ""}`, !!st.last_error);
    if (afkTimer) { clearInterval(afkTimer); afkTimer = null; }
    closeAfkStream();
  }
}

async function afkPoll() {
  let st;
  try { st = await (await fetch("/api/afk/status")).json(); } catch { return; }
  applyAfkStatus(st);
}

// Live stream of the background loop — renders into the same cards as 대화형 생성.
function openAfkStream() {
  if (afkWs && (afkWs.readyState === 0 || afkWs.readyState === 1)) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  afkWs = new WebSocket(`${proto}://${location.host}/ws/afk`);
  afkWs.binaryType = "blob";
  let pending = null;
  afkWs.onmessage = (e) => {
    if (typeof e.data !== "string") {
      if (!pending) return;
      const card = pending.server_id ? afkCard(pending.server_id, pending.server) : null;
      if (pending.type === "preview") {
        // 다중 백엔드 분산: 라이브 프리뷰는 서버별 카드에 — 공용 플로팅
        // 프리뷰는 단일 서버일 때만 미러링 (여러 서버 프레임이 뒤섞이지 않게).
        if (card) { card.prevW.classList.remove("hidden"); setImgEl(card.prevF, e.data); }
        if (!card || Object.keys(AFK_CARDS).length <= 1) {
          $("previewCard").classList.remove("hidden"); setImg("previewFrame", null, e.data);
        }
      } else if (pending.label === "intermediate") {
        if (card) setImgEl(card.baseF, e.data);
        setImg("baseFrame", "baseSize", e.data);  // 공용 카드(v2 플로팅 미리보기)도 갱신
      } else if (pending.label === "final") {
        if (card) setImgEl(card.finalF, e.data);
        setImg("finalFrame", "finalSize", e.data);
      }
      pending = null; return;
    }
    const ev = JSON.parse(e.data);
    const card = ev.server_id ? afkCard(ev.server_id, ev.server) : null;
    // 다중 서버 분산 시 진행 상태(node/progress)는 서버별 카드에만 표시 —
    // 공용 상단 상태줄/바는 단일 서버일 때만 미러링해 서로 덮어쓰지 않게 한다.
    const single = Object.keys(AFK_CARDS).length <= 1;
    switch (ev.type) {
      case "queued":
        if (card) { card.node = ""; card.prog.textContent = "큐 등록됨"; card.fill.style.width = "0%"; }
        break;
      case "provision":
        if (single) setStatus(provisionText(ev), ev.state === "failed");
        if (card) {
          card.prog.textContent = provisionText(ev);
          card.prog.classList.toggle("err", ev.state === "failed");
          if (ev.state === "uploading" && ev.total) card.fill.style.width = `${Math.round(ev.bytes_done / ev.total * 100)}%`;
          if (ev.state === "done") card.fill.style.width = "0%";
        }
        break;
      case "resolved":
        showResolved(ev.positive, ev.loras, ev.master_seed);
        if (card) card.resolved.textContent = ev.positive;
        break;
      case "node":
        if (single) setStatus(`AFK${ev.server ? "[" + ev.server + "]" : ""} 실행 중: ${ev.node}`, false);
        if (card) { card.node = ev.node; card.prog.classList.remove("err"); card.prog.textContent = `실행 중: ${ev.node}`; }
        break;
      case "progress":
        if (ev.max) {
          const pct = `${Math.round(ev.value / ev.max * 100)}%`;
          if (single) $("barFill").style.width = pct;
          if (card) {
            card.fill.style.width = pct;
            card.prog.textContent = (card.node ? `실행 중: ${card.node} · ` : "") + pct;
          }
        }
        break;
      case "image": case "preview": pending = ev; break;
      case "saved":
        if (ev.path) setStatus(`AFK${ev.server ? "[" + ev.server + "]" : ""} 저장: ${ev.path}`, false);
        if (card && ev.path) { card.prog.textContent = "저장됨 ✓"; card.node = ""; }
        break;
      case "done":
        if (card) { card.fill.style.width = "100%"; card.prog.textContent = "완료 ✓"; card.node = ""; }
        break;
      case "error":
        if (card) {
          const msg = ev.data && ev.data.message ? ev.data.message : JSON.stringify(ev.data || {});
          card.prog.textContent = "에러: " + msg;
          card.prog.classList.add("err");
          card.node = "";
        }
        break;
      case "afk": applyAfkStatus(ev); break;
    }
  };
  afkWs.onclose = () => { afkWs = null; };
  afkWs.onerror = () => { try { afkWs.close(); } catch {} };
}
function closeAfkStream() { if (afkWs) { try { afkWs.close(); } catch {} afkWs = null; } }

async function afkStart() {
  const server_ids = afkSelectedIds();
  if (!server_ids.length) { setAfk("시작 실패: AFK 분산 대상 백엔드를 하나 이상 체크하세요 (저장/AFK 패널)", true); return; }
  const names = SERVERS.filter((s) => server_ids.includes(s.id)).map((s) => s.name).join(", ");
  setAfk(`AFK 시작 중... → ${names}`, false);
  if (SELECTED) saveCurrentConfig();  // AFK 시작 시 현재 구성 자동저장(→ 린터 체크까지 트리거)
  const r = await fetch("/api/afk/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config: gatherConfig(), server_ids }) });
  if (!r.ok) { const e = await r.json().catch(() => ({})); setAfk("시작 실패: " + (e.error || r.status), true); return; }
  $("previewCard").classList.add("hidden");
  $("barFill").style.width = "0%";
  clearAfkCards();
  openAfkStream();
  afkPoll();
}
async function afkStop() {
  await fetch("/api/afk/stop", { method: "POST" });
  afkPoll();
}

// --- named-config manager -------------------------------------------------
function fmtDate(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso || "";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function sortConfigs(list) {
  const k = $("cfgSort").value;
  const arr = [...list];
  if (k === "name") arr.sort((a, b) => a.name.localeCompare(b.name));
  else arr.sort((a, b) => (b[k] || "").localeCompare(a[k] || ""));  // 날짜 내림차순
  return arr;
}
function renderConfigList() {
  const box = $("cfgList"); box.innerHTML = "";
  for (const c of sortConfigs(CONFIGS)) {
    const row = document.createElement("div");
    row.className = "cfg-row" + (SELECTED && c.id === SELECTED.id ? " selected" : "");
    const nm = document.createElement("div"); nm.className = "cfg-name"; nm.textContent = c.name;
    const dt = document.createElement("div"); dt.className = "cfg-dates";
    dt.textContent = `생성 ${fmtDate(c.created)} · 수정 ${fmtDate(c.modified)}`;
    row.append(nm, dt);
    row.addEventListener("click", () => selectConfig(c.id));
    box.appendChild(row);
  }
}
function upsertMeta(meta) {
  const i = CONFIGS.findIndex((c) => c.id === meta.id);
  if (i >= 0) CONFIGS[i] = meta; else CONFIGS.push(meta);
  SELECTED = { id: meta.id, name: meta.name };
}
async function loadConfigs() {
  const data = await (await fetch("/api/configs")).json();
  CONFIGS = data.configs || [];
  SELECTED = data.selected ? (CONFIGS.find((c) => c.id === data.selected) || null) : null;
  SELECTED = SELECTED && { id: SELECTED.id, name: SELECTED.name };
  if (data.current) loadDefaults(data.current);
  renderConfigList();
}
async function selectConfig(id) {
  const sc = await (await fetch("/api/configs/" + id)).json();
  await fetch("/api/configs/select", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
  SELECTED = { id: sc.id, name: sc.name };
  loadDefaults(sc.config);
  renderConfigList();
  setStatus(`구성 '${sc.name}' 불러옴`, false);
}
// Flash the 구성 저장 button itself so the save result is visible right there.
function flashSaveBtn(ok, label) {
  const b = $("saveBtn");
  if (b._t) clearTimeout(b._t);
  b.textContent = label || (ok ? "저장됨 ✓" : "저장 실패");
  b.classList.toggle("saved", ok);
  b.classList.toggle("savefail", !ok);
  b._t = setTimeout(() => { b.textContent = "구성 저장"; b.classList.remove("saved", "savefail"); b._t = null; }, 1600);
}

async function saveCurrentConfig() {
  if (!SELECTED) return cfgNew();
  const r = await fetch("/api/configs/" + SELECTED.id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config: gatherConfig() }) });
  if (!r.ok) { setStatus("구성 저장 실패", true); flashSaveBtn(false); return; }
  upsertMeta(await r.json()); renderConfigList(); setStatus("구성 저장됨 ✓", false); flashSaveBtn(true);
  analyzeConfig();  // 저장 시 자동 린터 체크
}
async function cfgNew() {
  const name = prompt("새 구성 이름", "새 구성"); if (!name) return;
  const r = await fetch("/api/configs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, config: gatherConfig() }) });
  if (!r.ok) { setStatus("구성 생성 실패", true); flashSaveBtn(false); return; }
  upsertMeta(await r.json()); renderConfigList(); setStatus(`구성 '${name}' 생성됨 ✓`, false); flashSaveBtn(true, "생성됨 ✓");
  analyzeConfig();  // 생성 시 자동 린터 체크
}
async function cfgDup() {
  if (!SELECTED) return;
  const r = await fetch(`/api/configs/${SELECTED.id}/duplicate`, { method: "POST" });
  if (!r.ok) { setStatus("복제 실패", true); return; }
  const meta = await r.json(); upsertMeta(meta);
  const sc = await (await fetch("/api/configs/" + meta.id)).json(); loadDefaults(sc.config);
  renderConfigList(); setStatus(`복제됨: ${meta.name}`, false);
}
async function cfgRename() {
  if (!SELECTED) return;
  const name = prompt("새 이름", SELECTED.name); if (!name) return;
  const r = await fetch("/api/configs/" + SELECTED.id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
  if (!r.ok) { setStatus("이름변경 실패", true); return; }
  upsertMeta(await r.json()); renderConfigList();
}
async function cfgDel() {
  if (!SELECTED) return;
  if (!confirm(`구성 '${SELECTED.name}' 삭제할까요?`)) return;
  const r = await fetch("/api/configs/" + SELECTED.id, { method: "DELETE" });
  const data = await r.json();
  CONFIGS = CONFIGS.filter((c) => c.id !== SELECTED.id);
  if (data.selected) {
    const sc = await (await fetch("/api/configs/" + data.selected)).json();
    SELECTED = { id: sc.id, name: sc.name }; loadDefaults(sc.config);
  } else { SELECTED = null; }
  renderConfigList(); setStatus("구성 삭제됨", false);
}

$("cfgSort").addEventListener("change", () => { LS.set("cfgSort", $("cfgSort").value); renderConfigList(); });
$("cfgNew").addEventListener("click", cfgNew);
$("cfgDup").addEventListener("click", cfgDup);
$("cfgRename").addEventListener("click", cfgRename);
$("cfgDel").addEventListener("click", cfgDel);

$("genBtn").addEventListener("click", generate);
$("serverSel").addEventListener("change", async () => {
  GEN_SERVER = $("serverSel").value || null;
  LS.set("genServer", GEN_SERVER);
  await reloadOptions();
  loadDefaults(gatherConfig());  // 새 서버 옵션으로 드롭다운 재구성(폼 값 유지)
});
$("srvAdd").addEventListener("click", srvAdd);
$("srvRefresh").addEventListener("click", () => { for (const s of SERVERS) checkHealth(s.id); });
$("addBlock").addEventListener("click", () => $("blocks").appendChild(blockRow({ input: "", items: [], children: [] })));
$("analyzeBtn").addEventListener("click", analyzeConfig);
$("addLora").addEventListener("click", () => $("loraRows").appendChild(loraRow({ mode: "conditional", strength: 1, trigger: "", name: (OPTIONS.loras||[])[0] })));
$("swapBtn").addEventListener("click", () => { const w = $("width").value; $("width").value = $("height").value; $("height").value = w; updateHints(); });
$("afkStart").addEventListener("click", afkStart);
$("afkStop").addEventListener("click", afkStop);
$("afkSrvAll").addEventListener("click", () => afkSetAll(true));
$("afkSrvNone").addEventListener("click", () => afkSetAll(false));
$("afkBtn").addEventListener("click", () => (afkRunning ? afkStop() : afkStart()));
$("repro_file").addEventListener("change", async (e) => {
  const f = e.target.files && e.target.files[0]; if (!f) return;
  setStatus("webp에서 복구 중...", false);
  const fd = new FormData(); fd.append("file", f);
  const r = await fetch("/api/reproduce", { method: "POST", body: fd });
  if (!r.ok) { setStatus("복구 실패: 임베드된 메타가 없거나 잘못된 파일", true); e.target.value = ""; return; }
  const data = await r.json();
  loadDefaults(data.config);
  $("repro_seed").value = data.master_seed;
  setStatus(`복구됨 · master seed ${data.master_seed} — 생성 ▶ 누르면 재현`, false);
  e.target.value = "";
});
$("saveBtn").addEventListener("click", saveCurrentConfig);
// Ctrl/Cmd+S → 현재 폼을 선택 구성에 저장 (브라우저 저장 대화상자 차단)
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "s" || e.key === "S")) {
    e.preventDefault();
    saveCurrentConfig();
  }
});

// view-state that doesn't depend on the options fetch
initPanels();
persistSize($("base"), "size:base");
persistSize($("negative"), "size:negative");
$("cfgSort").value = LS.get("cfgSort", "modified");

(async function init() {
  await loadServers();     // registry first — options come from the selected server
  await reloadOptions();
  await loadConfigs();     // fills the form from the selected config (needs OPTIONS first)
  afkPoll();  // reflect any AFK loop already running on the server (opens live stream)
})();
