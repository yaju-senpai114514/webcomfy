// 모델 관리 페이지: 서버별 카테고리/파일 조회, 업로드(진행률), 다운로드, 삭제,
// 해시 트리거, 그리고 두 서버 간 diff + 복사(웹컴피 경유 스트리밍).
const $ = (id) => document.getElementById(id);
const LS = {
  get(k, d) { try { const v = localStorage.getItem("cwm:" + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("cwm:" + k, JSON.stringify(v)); } catch {} },
};

let SERVERS = [];
let SRC = null;        // 조회/업로드 대상 서버 id
let DST = "";          // 비교/복사 대상 서버 id ("" = 없음)
let CAT = null;        // 선택된 category
let CATS = [];         // [{category, file_count, total_size}]
let FILES = [];        // 현재 카테고리 파일 목록 (src)
let DSTFILES = null;   // 대상 서버의 같은 카테고리 파일 목록 (diff용)

function human(n) {
  if (n == null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + " " + units[i];
}
function encName(name) { return name.split("/").map(encodeURIComponent).join("/"); }
function fileUrl(sid, cat, name) { return `/api/models/${sid}/${encodeURIComponent(cat)}/${encName(name)}`; }
function setStatus(el, text, err) { el.textContent = text; el.className = "status" + (err ? " err" : ""); }

// --- servers ---------------------------------------------------------------
// "local"은 webcomfy 백엔드 자체의 모델 저장소 — 서버들처럼 조회/업로드/삭제되고,
// 복사(diff)로 원격 서버와 동기화하는 스테이징 그라운드다.
const LOCAL_ENTRY = { id: "local", name: "로컬 저장소 (webcomfy)", enabled: true };

async function loadServers() {
  const data = await (await fetch("/api/servers")).json();
  SERVERS = [LOCAL_ENTRY, ...(data.servers || [])];
  const src = $("srcSel"); src.innerHTML = "";
  for (const s of SERVERS) {
    const o = document.createElement("option");
    o.value = s.id; o.textContent = s.name + (s.enabled ? "" : " (비활성)");
    src.appendChild(o);
  }
  const saved = LS.get("src", null);
  SRC = (saved && SERVERS.some((s) => s.id === saved)) ? saved : (data.default || SERVERS[0].id);
  if (SRC) src.value = SRC;
  renderDstSel();
}

function renderDstSel() {
  const dst = $("dstSel");
  dst.innerHTML = '<option value="">(없음)</option>';
  for (const s of SERVERS.filter((s) => s.id !== SRC)) {
    const o = document.createElement("option");
    o.value = s.id; o.textContent = s.name;
    dst.appendChild(o);
  }
  const saved = LS.get("dst", "");
  DST = (saved && saved !== SRC && SERVERS.some((s) => s.id === saved)) ? saved : "";
  dst.value = DST;
}

// --- categories ------------------------------------------------------------
async function loadSummary() {
  const box = $("catList");
  box.innerHTML = '<div class="hint">불러오는 중...</div>';
  let data;
  try {
    const r = await fetch(`/api/models/${SRC}`);
    data = await r.json();
    if (!r.ok) throw new Error(data.error ? data.error.message : r.status);
  } catch (e) {
    box.innerHTML = `<div class="status err">모델 API 조회 실패: ${e.message || e}</div>
      <div class="hint">해당 ComfyUI 서버에 webcomfy_models 커스텀 노드가 설치되어 있고 토큰이 맞는지 확인.</div>`;
    return;
  }
  CATS = data.categories || [];
  if (!CATS.some((c) => c.category === CAT)) {
    const first = CATS.find((c) => c.category === "loras" && c.file_count) ||
                  CATS.find((c) => c.file_count) || CATS[0];
    CAT = first ? first.category : null;
  }
  renderCats();
  if (CAT) await loadFiles();
}

function renderCats() {
  const box = $("catList"); box.innerHTML = "";
  for (const c of CATS) {
    const row = document.createElement("div");
    row.className = "m-cat" + (c.category === CAT ? " selected" : "") + (c.file_count ? "" : " empty");
    row.innerHTML = `<span class="mc-name"></span><span class="mc-meta"></span>`;
    row.querySelector(".mc-name").textContent = c.category;
    row.querySelector(".mc-meta").textContent = `${c.file_count} · ${human(c.total_size)}`;
    row.addEventListener("click", () => { CAT = c.category; renderCats(); loadFiles(); });
    box.appendChild(row);
  }
}

// --- files -----------------------------------------------------------------
async function loadFiles() {
  $("fileTitle").textContent = `파일 — ${CAT}`;
  $("upTitle").textContent = `업로드 — ${CAT}`;
  setStatus($("fileStatus"), "불러오는 중...", false);
  FILES = []; DSTFILES = null;
  renderTable();
  try {
    const r = await fetch(`/api/models/${SRC}/${encodeURIComponent(CAT)}`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error ? data.error.message : r.status);
    FILES = data.files || [];
  } catch (e) {
    setStatus($("fileStatus"), "목록 조회 실패: " + (e.message || e), true);
    return;
  }
  if (DST) {
    try {
      const r = await fetch(`/api/models/${DST}/${encodeURIComponent(CAT)}`);
      const data = await r.json();
      DSTFILES = r.ok ? (data.files || []) : null;
    } catch { DSTFILES = null; }
  }
  setStatus($("fileStatus"), `${FILES.length}개 파일`, false);
  renderTable();
}

// 대상 서버와의 비교 상태: same(이름+해시 일치) / diff(해시 다름) / unknown(해시 미계산) / missing
function diffState(f) {
  if (DSTFILES == null) return null;
  const other = DSTFILES.find((o) => o.name === f.name);
  if (!other) return "missing";
  if (f.sha256 && other.sha256) return f.sha256 === other.sha256 ? "same" : "diff";
  return f.size === other.size ? "unknown-same-size" : "diff";
}

const DIFF_LABEL = {
  "missing": ["없음", "dbad"], "same": ["동일", "dok"],
  "diff": ["다름", "dbad"], "unknown-same-size": ["크기 동일(해시?)", "dunk"],
};

function renderTable() {
  const showDiff = DSTFILES != null;
  document.querySelectorAll(".c-diff").forEach((el) => el.classList.toggle("hidden", !showDiff));
  $("copyMissingBtn").classList.toggle("hidden", !showDiff);
  const filter = $("fileFilter").value.trim().toLowerCase();
  const tbody = $("fileRows"); tbody.innerHTML = "";
  for (const f of FILES) {
    if (filter && !f.name.toLowerCase().includes(filter)) continue;
    const tr = document.createElement("tr");

    const name = document.createElement("td"); name.className = "c-name"; name.textContent = f.name;
    const size = document.createElement("td"); size.className = "c-size"; size.textContent = human(f.size);
    const mtime = document.createElement("td"); mtime.className = "c-mtime";
    mtime.textContent = (f.mtime || "").replace("T", " ").replace("Z", "");

    const sha = document.createElement("td"); sha.className = "c-sha";
    if (f.sha256) { sha.textContent = f.sha256.slice(0, 12); sha.title = f.sha256; }
    else if (f.hash_state === "pending") { sha.textContent = "계산 중..."; sha.className += " dim"; }
    else {
      const b = document.createElement("button"); b.className = "sm"; b.textContent = "해시 계산";
      b.addEventListener("click", () => triggerHash(f, b));
      sha.appendChild(b);
    }

    const diff = document.createElement("td"); diff.className = "c-diff" + (showDiff ? "" : " hidden");
    const st = diffState(f);
    if (st) {
      const [label, cls] = DIFF_LABEL[st];
      const badge = document.createElement("span"); badge.className = "dbadge " + cls; badge.textContent = label;
      diff.appendChild(badge);
    }

    const act = document.createElement("td"); act.className = "c-act";
    const dl = document.createElement("a");
    dl.className = "sm btnlike"; dl.textContent = "⬇"; dl.title = "다운로드";
    dl.href = fileUrl(SRC, CAT, f.name);
    act.appendChild(dl);
    if (showDiff && st !== "same") {
      const cp = document.createElement("button");
      cp.className = "sm"; cp.textContent = "복사 →"; cp.title = "대상 서버로 복사 (다르면 덮어쓰기)";
      cp.addEventListener("click", () => copyFile(f, st !== "missing"));
      act.appendChild(cp);
    }
    const del = document.createElement("button");
    del.className = "x"; del.textContent = "✕"; del.title = "삭제";
    del.addEventListener("click", () => deleteFile(f));
    act.appendChild(del);

    tr.append(name, size, mtime, sha, diff, act);
    tbody.appendChild(tr);
  }
}

async function deleteFile(f) {
  if (!confirm(`${CAT}/${f.name} 삭제할까요? (서버에서 파일이 지워집니다)`)) return;
  const r = await fetch(fileUrl(SRC, CAT, f.name), { method: "DELETE" });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    setStatus($("fileStatus"), "삭제 실패: " + (e.error ? e.error.message : r.status), true);
    return;
  }
  setStatus($("fileStatus"), `삭제됨: ${f.name}`, false);
  loadFiles(); loadSummaryCountsOnly();
}

async function triggerHash(f, btn) {
  btn.disabled = true; btn.textContent = "계산 중...";
  await fetch(fileUrl(SRC, CAT, f.name) + "/hash", { method: "POST" });
  // 전역 워커 1개가 순차 계산 — 완료까지 meta 폴링
  const poll = async () => {
    const r = await fetch(fileUrl(SRC, CAT, f.name) + "?meta=1");
    if (r.ok) {
      const meta = await r.json();
      if (meta.hash_state === "done") { loadFiles(); return; }
    }
    setTimeout(poll, 2500);
  };
  setTimeout(poll, 1500);
}

// --- upload (XHR로 진행률 표시) ---------------------------------------------
$("upFile").addEventListener("change", () => {
  const f = $("upFile").files[0];
  if (f && !$("upName").value.trim()) $("upName").value = f.name;
});

function uploadFile() {
  const f = $("upFile").files[0];
  const name = $("upName").value.trim();
  if (!f || !name) { setStatus($("upStatus"), "파일과 저장 이름을 지정하세요", true); return; }
  const replace = $("upReplace").checked ? "?replace=1" : "";
  const xhr = new XMLHttpRequest();
  xhr.open("POST", fileUrl(SRC, CAT, name) + replace);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  $("upBar").classList.remove("hidden");
  $("upBtn").disabled = true;
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) $("upFill").style.width = Math.round(e.loaded / e.total * 100) + "%";
    setStatus($("upStatus"), `업로드 중... ${human(e.loaded)} / ${human(e.total)}`, false);
  };
  xhr.onload = () => {
    $("upBtn").disabled = false; $("upBar").classList.add("hidden"); $("upFill").style.width = "0%";
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch {}
    if (xhr.status === 200 || xhr.status === 201) {
      let msg = `업로드 완료: ${data.name} (${human(data.size)}, sha256 ${String(data.sha256).slice(0, 12)}…)`;
      if (data.warning === "stale_cache") msg += " ⚠ 동명 덮어쓰기 — ComfyUI 재시작 전까지 이전 가중치가 쓰일 수 있음";
      setStatus($("upStatus"), msg, false);
      $("upFile").value = ""; $("upName").value = "";
      loadFiles(); loadSummaryCountsOnly();
    } else {
      setStatus($("upStatus"), "업로드 실패: " + (data.error ? data.error.message : xhr.status), true);
    }
  };
  xhr.onerror = () => {
    $("upBtn").disabled = false; $("upBar").classList.add("hidden");
    setStatus($("upStatus"), "업로드 실패: 네트워크 오류", true);
  };
  xhr.send(f);
}

async function loadSummaryCountsOnly() {
  try {
    const r = await fetch(`/api/models/${SRC}`);
    if (r.ok) { CATS = (await r.json()).categories || []; renderCats(); }
  } catch {}
}

// --- server → server copy jobs ----------------------------------------------
async function copyFile(f, replace) {
  if (!DST) return;
  const r = await fetch("/api/models/copy", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ src_id: SRC, dst_id: DST, category: CAT, name: f.name, replace: !!replace }),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    setStatus($("fileStatus"), "복사 시작 실패: " + (e.error ? e.error.message : (e.detail || r.status)), true);
    return;
  }
  pollJobs();
}

async function copyMissing() {
  if (!DST || DSTFILES == null) return;
  const missing = FILES.filter((f) => diffState(f) === "missing");
  if (!missing.length) { setStatus($("fileStatus"), "누락된 파일 없음", false); return; }
  if (!confirm(`${missing.length}개 파일을 대상 서버로 복사할까요? (총 ${human(missing.reduce((a, f) => a + f.size, 0))})`)) return;
  for (const f of missing) {
    await fetch("/api/models/copy", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ src_id: SRC, dst_id: DST, category: CAT, name: f.name, replace: false }),
    });
  }
  pollJobs();
}

let jobTimer = null;
async function pollJobs() {
  const box = $("jobsBox"), rows = $("jobRows");
  let jobs = [];
  try { jobs = (await (await fetch("/api/model_jobs")).json()).jobs || []; } catch { return; }
  box.classList.toggle("hidden", !jobs.length);
  rows.innerHTML = "";
  let anyRunning = false;
  for (const j of [...jobs].reverse()) {
    if (j.state === "running") anyRunning = true;
    const row = document.createElement("div");
    row.className = "job " + j.state;
    const pct = j.total ? Math.round(j.bytes_done / j.total * 100) : 0;
    const stateTxt = j.state === "running" ? `${pct}% (${human(j.bytes_done)}/${human(j.total)})`
      : j.state === "done" ? "완료 ✓" + (j.warning === "stale_cache" ? " ⚠ 재시작 전 이전 가중치 주의" : "")
      : "실패: " + (j.error || "");
    row.innerHTML = `<div class="job-head"><span class="jname"></span><span class="jstate"></span></div>
      <div class="bar mini"><div class="fill" style="width:${j.state === "done" ? 100 : pct}%"></div></div>`;
    row.querySelector(".jname").textContent = `${j.src.name} → ${j.dst.name} · ${j.category}/${j.name}`;
    row.querySelector(".jstate").textContent = stateTxt;
    rows.appendChild(row);
  }
  if (anyRunning && !jobTimer) jobTimer = setInterval(pollJobs, 1500);
  if (!anyRunning && jobTimer) {
    clearInterval(jobTimer); jobTimer = null;
    loadFiles();  // 복사가 끝났으면 diff 상태 갱신
  }
}

// --- wiring ------------------------------------------------------------------
$("srcSel").addEventListener("change", () => {
  SRC = $("srcSel").value; LS.set("src", SRC);
  renderDstSel(); loadSummary();
});
$("dstSel").addEventListener("change", () => {
  DST = $("dstSel").value; LS.set("dst", DST);
  loadFiles();
});
$("refreshBtn").addEventListener("click", () => loadSummary());
$("fileFilter").addEventListener("input", renderTable);
$("upBtn").addEventListener("click", uploadFile);
$("copyMissingBtn").addEventListener("click", copyMissing);

(async function init() {
  await loadServers();
  if (!SRC) { setStatus($("fileStatus"), "등록된 서버가 없습니다 — 생성 UI에서 서버를 추가하세요", true); return; }
  await loadSummary();
  pollJobs();
})();
