/* 钩织玩偶 3D 重建 - 前端逻辑 */

const state = {
  files: [],        // [{file, url}]，第一张为主视图
  accessCode: localStorage.getItem("accessCode") || "",
  currentJobId: null,
  pollTimer: null,
  historyTimer: null,
};

const $ = (id) => document.getElementById(id);

/* ---------- API 封装：自动带口令，401 时弹窗要口令 ---------- */

async function api(path, options = {}, retry = true) {
  const headers = Object.assign(
    { "X-Access-Code": state.accessCode },
    options.headers || {}
  );
  const resp = await fetch(path, { ...options, headers });
  if (resp.status === 401 && retry) {
    const code = await askAccessCode();
    if (code) {
      state.accessCode = code;
      localStorage.setItem("accessCode", code);
      return api(path, options, false);
    }
    throw new Error("需要访问口令");
  }
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || `请求失败 (${resp.status})`);
  return body;
}

function askAccessCode() {
  return new Promise((resolve) => {
    const dialog = $("code-dialog");
    const input = $("code-input");
    input.value = "";
    input.onkeydown = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        $("code-ok").click();
      }
    };
    dialog.showModal();
    input.focus();
    dialog.onclose = () =>
      resolve(dialog.returnValue === "ok" ? input.value.trim() : null);
  });
}

/* ---------- 上传 ---------- */

const dropzone = $("dropzone");
const fileInput = $("file-input");

dropzone.onclick = () => fileInput.click();
dropzone.ondragover = (e) => { e.preventDefault(); dropzone.classList.add("drag"); };
dropzone.ondragleave = () => dropzone.classList.remove("drag");
dropzone.ondrop = (e) => {
  e.preventDefault();
  dropzone.classList.remove("drag");
  addFiles(e.dataTransfer.files);
};
fileInput.onchange = () => { addFiles(fileInput.files); fileInput.value = ""; };

function addFiles(list) {
  for (const file of list) {
    if (state.files.length >= 8) { alert("最多 8 张照片"); break; }
    if (!/^image\/(jpeg|png|webp)$/.test(file.type)) { alert(`${file.name} 不是支持的图片格式`); continue; }
    state.files.push({ file, url: URL.createObjectURL(file) });
  }
  renderThumbs();
}

function renderThumbs() {
  const box = $("thumbs");
  box.classList.toggle("hidden", state.files.length === 0);
  box.innerHTML = "";
  state.files.forEach((item, index) => {
    const div = document.createElement("div");
    div.className = "thumb" + (index === 0 ? " main" : "");
    div.innerHTML = `
      <img src="${item.url}" alt="">
      ${index === 0 ? '<span class="tag">主图</span>' : '<button title="移除" data-del>×</button>'}
      ${index === 0 ? "" : '<button class="make-main" data-main>设为主图</button>'}
    `;
    div.querySelector("[data-del]").onclick = () => {
      URL.revokeObjectURL(item.url);
      state.files.splice(index, 1);
      renderThumbs();
    };
    const main = div.querySelector("[data-main]");
    if (main) main.onclick = () => {
      const [picked] = state.files.splice(index, 1);
      state.files.unshift(picked);
      renderThumbs();
    };
    box.appendChild(div);
  });
  $("submit-btn").disabled = state.files.length === 0 || !!state.currentJobId;
}

/* ---------- 提交任务 ---------- */

$("submit-btn").onclick = async () => {
  const btn = $("submit-btn");
  if (state.files.length === 0 || state.currentJobId) return;
  btn.disabled = true;
  btn.textContent = "提交中…";
  try {
    const form = new FormData();
    form.append("name", $("toy-name").value.trim());
    form.append("access_code", state.accessCode);
    state.files.forEach((f) => form.append("files", f.file));
    const resp = await fetch("/api/jobs", {
      method: "POST",
      headers: { "X-Access-Code": state.accessCode },
      body: form,
    });
    const body = await resp.json().catch(() => ({}));
    if (resp.status === 401) {
      const code = await askAccessCode();
      if (code) { state.accessCode = code; localStorage.setItem("accessCode", code); btn.disabled = false; btn.textContent = "开始重建 ✨"; renderThumbs(); }
      return;
    }
    if (!resp.ok) throw new Error(body.detail || "提交失败");
    state.currentJobId = body.id;
    clearFiles();
    showProgress(body);
    startPolling();
    refreshHistory();
  } catch (err) {
    alert(err.message);
    btn.disabled = false;
    btn.textContent = "开始重建 ✨";
  }
};

function clearFiles() {
  state.files.forEach((f) => URL.revokeObjectURL(f.url));
  state.files = [];
  renderThumbs();
  $("toy-name").value = "";
}

/* ---------- 进度 ---------- */

const STEP_ORDER = ["uploading", "reconstructing", "succeeded"];

function showProgress(meta) {
  $("progress-card").classList.remove("hidden");
  $("viewer-card").classList.add("hidden");
  $("progress-title").textContent = `${meta.name} · 重建中`;
  renderProgress(meta);
}

function renderProgress(meta) {
  const current = STEP_ORDER.indexOf(meta.status) === -1 ? 0 : STEP_ORDER.indexOf(meta.status);
  document.querySelectorAll("#stepper li").forEach((li, i) => {
    li.classList.toggle("done", meta.status !== "failed" && i < current);
    li.classList.toggle("active", meta.status !== "failed" && i === current && meta.status !== "succeeded");
  });
  const detail = $("progress-detail");
  const error = $("progress-error");
  error.classList.toggle("hidden", meta.status !== "failed");
  if (meta.status === "failed") {
    detail.textContent = "";
    error.textContent = `出错了：${meta.error || "未知原因"}`;
  } else if (meta.status === "succeeded") {
    detail.textContent = "重建完成！";
  } else {
    const minutes = Math.max(0, Math.round((Date.now() - meta.createdAt * 1000) / 60000));
    detail.textContent = `${meta.statusText} · 已等待 ${minutes} 分钟`;
  }
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    if (!state.currentJobId) return stopPolling();
    try {
      const meta = await api(`/api/jobs/${state.currentJobId}`);
      renderProgress(meta);
      if (meta.status === "succeeded") { showViewer(meta); stopPolling(); state.currentJobId = null; renderThumbs(); refreshHistory(); }
      if (meta.status === "failed") { stopPolling(); state.currentJobId = null; renderThumbs(); refreshHistory(); }
    } catch { /* 网络抖动，下个周期重试 */ }
  }, 3000);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

/* ---------- 查看器 ---------- */

function showViewer(meta) {
  $("viewer-card").classList.remove("hidden");
  $("progress-card").classList.add("hidden");
  state.currentViewedJob = meta.id;
  $("viewer-title").textContent = `${meta.name} · 3D 模型`;
  $("download-btn").href = `/api/jobs/${meta.id}/model.glb`;
  const imported = meta.source === "imported";
  $("viewer-meta").textContent =
    `创建于 ${new Date(meta.createdAt * 1000).toLocaleString("zh-CN")} · ` +
    `${meta.imageCount} 张输入照片` +
    (imported ? " · 从旧项目导入" : "");
  const viewer = $("viewer");
  viewer.src = `/api/jobs/${meta.id}/model.glb`;

  const strip = $("viewer-images");
  strip.innerHTML = "";
  for (let i = 0; i < (meta.imageCount || 0); i++) {
    const img = document.createElement("img");
    img.src = `/api/jobs/${meta.id}/images/${i}.jpg`;
    img.loading = "lazy";
    img.alt = `输入照片 ${i + 1}`;
    strip.appendChild(img);
  }
  strip.classList.toggle("hidden", strip.children.length === 0);

  // 重置针数面板
  $("stitch-result").classList.add("hidden");
  $("stitch-error").classList.add("hidden");
  $("axis-info").textContent = "";
  $("cal-info").textContent = "";
  $("cal-round").value = "";
  $("cal-count").value = "";
  viewer.scrollIntoView({ behavior: "smooth", block: "start" });
}

$("spin-btn").onclick = () => {
  const viewer = $("viewer");
  const enabled = !viewer.hasAttribute("auto-rotate");
  enabled ? viewer.setAttribute("auto-rotate", "") : viewer.removeAttribute("auto-rotate");
  $("spin-btn").textContent = `自动旋转：${enabled ? "开" : "关"}`;
  $("spin-btn").classList.toggle("active", enabled);
};
$("reset-btn").onclick = () => {
  const viewer = $("viewer");
  viewer.resetTurntableRotation();
  viewer.cameraOrbit = "0deg 75deg auto";
};

/* ---------- 针数估算 ---------- */

$("yarn-preset").onchange = () => {
  const v = $("yarn-preset").value;
  if (v === "custom") return;
  const [w, h] = v.split(",");
  $("gauge-w").value = w;
  $("gauge-h").value = h;
};

$("calc-btn").onclick = async () => {
  if (!state.currentViewedJob) return;
  const btn = $("calc-btn");
  btn.disabled = true;
  btn.textContent = "计算中…（首次约几秒）";
  $("stitch-error").classList.add("hidden");
  $("stitch-result").classList.add("hidden");
  const params = new URLSearchParams({
    axis: $("axis-select").value,
    gaugeH: $("gauge-h").value || 3.0,
  });
  if ($("yarn-preset").value === "auto") {
    params.set("gaugeW", "auto");
    params.set("autoScale", "1");
  } else {
    params.set("gaugeW", $("gauge-w").value || 2.6);
  }
  const realSize = $("real-size").value;
  if (realSize) params.set("realSize", realSize);
  params.set("access_code", state.accessCode);
  try {
    const r = await api(`/api/jobs/${state.currentViewedJob}/stitches?${params}`);
    renderStitches(r);
  } catch (err) {
    $("stitch-error").textContent = err.message;
    $("stitch-error").classList.remove("hidden");
  }
  btn.disabled = false;
  btn.textContent = "计算针数";
};

function renderStitches(r) {
  state.lastStitchResult = r;
  $("stitch-result").classList.remove("hidden");
  $("stat-total").textContent = r.totalStitches.toLocaleString();
  $("stat-rounds").textContent = r.roundCount;
  $("stat-start").textContent = r.startStitches;
  $("stat-max").textContent = r.maxRound ? `${r.maxRound.stitches} 针(第${r.maxRound.round}圈)` : "-";
  $("stat-delta").textContent = `${r.increases} / ${r.decreases}`;
  $("axis-info").textContent =
    `模型尺寸 ${r.modelAxesCm.x} × ${r.modelAxesCm.y} × ${r.modelAxesCm.z} cm` +
    (r.scale !== 1 ? ` · 已按实际尺寸校准（×${r.scale}）` : "") +
    ` · 表面积 ${r.areaCm2} cm²`;
  if (r.auto) {
    const line =
      r.auto.quality === "ok"
        ? `🔍 自动检测: ${r.auto.detectedRounds} 圈直接测得针数 · 有效密度 ${r.auto.gaugeWEff} 针/cm` +
          (r.scale !== 1 ? ` · 自动尺寸校准 → ${r.auto.suggestRealSizeCm}cm` : "")
        : `🔍 自动检测: ${r.auto.message || "针目信号不足，已按密度换算"}`;
    $("axis-info").textContent += " · " + line;
  }
  $("stitch-note").textContent =
    `${r.note} 面积法对照值 ${r.areaMethodTotal.toLocaleString()} 针（含重建双层壳噪声，通常偏高），以逐圈法为主。` +
    `钩织起点对应所选轴的负方向端。` +
    (r.segments.length > 1
      ? ` 分段参考：` + r.segments.map((s, i) => `段${i + 1}(圈${s.from}-${s.to})≈${s.stitches}针`).join(" · ")
      : "");

  const active = r.rounds.filter((x) => x.stitches > 0);
  const maxS = Math.max(...active.map((x) => x.stitches), 1);
  const chart = $("round-chart");
  chart.innerHTML = "";
  for (const x of r.rounds) {
    const bar = document.createElement("div");
    bar.className =
      "bar" + (x.tangent ? " tan" : x.delta > 0 ? " inc" : x.delta < 0 ? " dec" : "");
    bar.style.height = x.stitches > 0 ? `${Math.max(3, (x.stitches / maxS) * 100)}%` : "1px";
    bar.title = `第${x.round}圈 · ${x.stitches}针${x.tangent ? " ⚠轮廓凸起，可能偏高" : ""}`;
    chart.appendChild(bar);
  }
  const tbody = $("round-rows");
  tbody.innerHTML = "";
  for (const x of r.rounds) {
    const tr = document.createElement("tr");
    if (x.stitches === 0) tr.className = "idle";
    if (x.tangent) tr.className = "tangent";
    const deltaHtml =
      x.delta == null ? "" : x.delta > 0 ? `<span class="inc">+${x.delta}</span>` : x.delta < 0 ? `<span class="dec">${x.delta}</span>` : "0";
    const st = x.tangent ? `${x.stitches} ⚠` : x.detected ? `<b>${x.stitches}</b> ✓` : x.stitches || "-";
    tr.innerHTML = `<td>第 ${x.round} 圈</td><td>${x.posCm}</td><td>${x.perimeterCm || "-"}</td><td>${st}</td><td>${deltaHtml}</td>`;
    tbody.appendChild(tr);
  }
}

/* 图解标定：用一圈真实针数反推横向密度 */
$("cal-btn").onclick = () => {
  const r = state.lastStitchResult;
  const roundNo = parseInt($("cal-round").value, 10);
  const count = parseInt($("cal-count").value, 10);
  const info = $("cal-info");
  if (!r) { info.textContent = "请先点「计算针数」得到逐圈表"; return; }
  if (!roundNo || !count) { info.textContent = "请填写圈号和该圈实际针数"; return; }
  const round = r.rounds.find((x) => x.round === roundNo);
  if (!round || !round.perimeterCm) { info.textContent = `第 ${roundNo} 圈没有周长数据，换一圈试试`; return; }
  const gauge = count / round.perimeterCm;
  $("yarn-preset").value = "custom";
  $("gauge-w").value = gauge.toFixed(2);
  info.textContent = `已标定：第 ${roundNo} 圈周长 ${round.perimeterCm}cm ÷ ${count} 针 → 横向密度 ${gauge.toFixed(2)} 针/cm，正在重算…`;
  $("calc-btn").click();
};

/* ---------- 历史 ---------- */

function timeAgo(ts) {
  const diff = Date.now() - ts * 1000;
  if (diff < 60000) return "刚刚";
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`;
  return new Date(ts * 1000).toLocaleDateString("zh-CN");
}

async function refreshHistory() {
  try {
    const jobs = await api("/api/jobs");
    const count = $("history-count");
    count.textContent = jobs.length > 0 ? `${jobs.length} 条` : "";
    count.classList.toggle("hidden", jobs.length === 0);
    const box = $("history");
    if (jobs.length === 0) { box.innerHTML = '<p class="hint">还没有记录</p>'; return; }
    box.innerHTML = "";
    for (const job of jobs) {
      const card = document.createElement("div");
      card.className = "job-card";
      const chipClass = job.status === "succeeded" ? "succeeded" : job.status === "failed" ? "failed" : "running";
      const chipText = job.status === "succeeded" ? "完成" : job.status === "failed" ? "失败" : "进行中";
      const imported = job.source === "imported" ? '<span class="chip imported">导入</span>' : "";
      card.innerHTML = `
        <img src="/api/jobs/${job.id}/images/0.jpg" alt="" loading="lazy">
        <div class="meta">
          <div class="name">${escapeHtml(job.name)}</div>
          <div class="sub">
            <span class="chip ${chipClass}">${chipText}</span>
            ${imported}
            <span>${timeAgo(job.createdAt)}</span>
            <button class="del" title="删除记录">🗑</button>
          </div>
        </div>`;
      card.querySelector(".del").onclick = async (e) => {
        e.stopPropagation();
        if (!confirm(`删除「${job.name}」的记录和模型？`)) return;
        await api(`/api/jobs/${job.id}`, { method: "DELETE" });
        refreshHistory();
      };
      card.onclick = () => {
        if (job.status === "succeeded") showViewer(job);
        else if (job.status !== "failed") { state.currentJobId = job.id; showProgress(job); startPolling(); }
      };
      box.appendChild(card);
    }
  } catch { /* 静默 */ }
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

/* ---------- 图解库 ---------- */

let allPatterns = [];

function renderPatternList(filter = "") {
  const box = $("pattern-list");
  const list = allPatterns.filter((p) => p.name.includes(filter) || p.file.includes(filter));
  if (list.length === 0) { box.innerHTML = '<p class="hint">没有匹配的图解</p>'; return; }
  box.innerHTML = "";
  for (const p of list) {
    const card = document.createElement("div");
    card.className = "job-card";
    card.innerHTML = `
      <div class="pat-body">
        <div class="name">${escapeHtml(p.name)}</div>
        <div class="pat-stats">
          <span class="stat-mini"><b>${p.total.toLocaleString()}</b>针</span>
          <span class="stat-mini"><b>${p.roundCount}</b>圈</span>
          <span class="stat-mini"><b>${p.partCount}</b>部件</span>
        </div>
        <div class="sub">${p.yarn ? escapeHtml(p.yarn) + " · " : ""}${escapeHtml(p.hook)}${p.unparsed ? ` · <span class="miss">${p.unparsed}圈未解析</span>` : ""}</div>
      </div>`;
    card.onclick = () => showPatternDetail(p.id);
    box.appendChild(card);
  }
}

async function showPatternDetail(pid) {
  try {
    const p = await api(`/api/patterns/${encodeURIComponent(pid)}`);
    const box = $("pattern-detail");
    const rows = p.parts
      .map(
        (part) => `
      <div class="pat-part">
        <div class="pat-part-name">${escapeHtml(part.name)} <span>小计 ${part.total} 针</span></div>
        <table class="round-table">
          <thead><tr><th>圈</th><th>表达式</th><th>针数</th></tr></thead>
          <tbody>
            ${part.rounds
              .map(
                (rd) => `<tr>
                  <td>${rd.rEnd > rd.r ? `${rd.r}-${rd.rEnd}` : rd.r}</td>
                  <td class="expr">${escapeHtml(rd.expr.slice(0, 44))}</td>
                  <td>${rd.count ?? "?"}</td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>`
      )
      .join("");
    box.innerHTML = `
      <h3>${escapeHtml(p.name)}</h3>
      <p class="hint">${p.yarn ? escapeHtml(p.yarn) + " · " : ""}${escapeHtml(p.hook)} · 共 ${p.roundCount} 圈 · 总计 <b>${p.total.toLocaleString()}</b> 针${p.unparsed ? ` · ${p.unparsed} 圈未能解析` : ""}</p>
      ${rows}`;
    $("pattern-dialog").showModal();
  } catch (err) {
    alert(err.message);
  }
}

$("pattern-search").oninput = () => renderPatternList($("pattern-search").value.trim());
$("pattern-dialog").addEventListener("close", () => {});

/* ---------- 启动 ---------- */

(async function init() {
  try {
    const health = await api("/api/health");
    $("mock-badge").classList.toggle("hidden", !health.mock);
    if (!health.keyConfigured && !health.mock) {
      $("upload-card").insertAdjacentHTML(
        "beforeend",
        '<p class="error">⚠️ 服务端未配置 AHOLO_API_KEY，重建将无法进行。请看 README 配置后重启。</p>'
      );
    }
  } catch { /* 忽略 */ }
  renderThumbs();
  refreshHistory();
  state.historyTimer = setInterval(refreshHistory, 15000);
  try {
    allPatterns = await api("/api/patterns");
    const count = $("patterns-count");
    count.textContent = `${allPatterns.length} 份`;
    count.classList.remove("hidden");
    renderPatternList();
  } catch { /* 图解库不可用时静默 */ }
})();
