/* 差分を見る画面。
 *
 * エディタ本体（app.js）とは別に持っている。読むだけの画面で、編集の状態を
 * 一切さわらないため。別タブで開いて、エディタと並べて見られるようにしてある。
 */

const qs = new URLSearchParams(location.search);
const SET = qs.get("set") || "";
let DATA = null;
let filter = "all";

const el = id => document.getElementById(id);
const tc = t => {
  const s = Math.max(0, t || 0);
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
};
const esc = s => String(s ?? "").replace(/[&<>]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

/* 語単位のインライン差分。どこが変わったかを見せる */
function inlineDiff(a, b) {
  a = String(a || ""); b = String(b || "");
  if (!a || !b) return [esc(a), esc(b)];
  // 前後の共通部分を削って、真ん中だけを強調する
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head++;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head &&
         a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail++;
  const mark = (s) => {
    const mid = s.slice(head, s.length - tail);
    return esc(s.slice(0, head)) + (mid ? `<em>${esc(mid)}</em>` : "") +
           esc(s.slice(s.length - tail));
  };
  return [mark(a), mark(b)];
}

async function load() {
  el("cmpSet").textContent = SET || "（セットが指定されていません）";
  if (!SET) return;
  el("cmpMsg").textContent = "突き合わせています…";
  const left = el("cmpLeft").value || qs.get("left") || "current";
  const right = el("cmpRight").value || qs.get("right") || "manual";
  try {
    const r = await fetch(`/api/sets/${encodeURIComponent(SET)}/compare` +
      `?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "失敗しました");
    DATA = d;
    render();
    el("cmpMsg").textContent = "";
  } catch (e) {
    el("cmpMsg").textContent = e.message;
    el("cmpMsg").className = "cmp-msg err";
    el("cmpRows").innerHTML = "";
  }
}

function renderPickers() {
  const opts = (sel, cur) => DATA.choices.map(c =>
    `<option value="${c.id}"${c.id === cur ? " selected" : ""}${c.exists ? "" : " disabled"}>` +
    `${c.label}${c.exists ? `（${c.count ?? "?"} 件）` : "（まだ無い）"}</option>`).join("");
  el("cmpLeft").innerHTML = opts("left", DATA.leftId);
  el("cmpRight").innerHTML = opts("right", DATA.rightId);
  el("cmpThL").textContent = DATA.left.name;
  el("cmpThR").textContent = DATA.right.name;
  const a = el("cmpAudio");
  if (a.dataset.src !== DATA.mediaUrl) { a.src = DATA.mediaUrl; a.dataset.src = DATA.mediaUrl; }
}

function render() {
  renderPickers();
  const s = DATA.summary;

  el("cmpSum").innerHTML = [
    ["一致した行", `${s.same}`, `${s.sameRate}%`],
    ["並べた行", `${s.rows}`, `左 ${DATA.left.count} / 右 ${DATA.right.count}`],
    ["学習に使える", `${s.learn}`, "演出の書き直しは除いた数"],
    ["要判断", `${s.eye}`, "機械では決められない"],
    ["時刻のずれ 中央", s.dStartMedian == null ? "—" : `${s.dStartMedian > 0 ? "+" : ""}${s.dStartMedian}s`,
      s.dStartP10 == null ? "" : `1割目 ${s.dStartP10}s / 9割目 ${s.dStartP90}s`],
    ["2 秒以上ずれた行", `${s.outliers}`, ""],
  ].map(([k, v, d]) =>
    `<div class="cmp-stat"><div class="k">${k}</div><div class="v">${v}</div>` +
    `<div class="d">${d}</div></div>`).join("");

  el("cmpShift").hidden = !s.shift;
  if (s.shift) {
    el("cmpShift").innerHTML =
      `<b>左は全体が ${s.shift > 0 ? "+" : ""}${s.shift} 秒ずれています。</b>` +
      `素材の基準そのものが違う（Filmora で頭を詰めた等）ので、` +
      `このずれは 1 回だけ報告し、下の表では<b>差し引いた残りの差</b>を出しています。`;
  }

  const kinds = Object.entries(DATA.kinds).filter(([, v]) => v.count > 0)
    .sort((a, b) => b[1].count - a[1].count);
  el("cmpFilters").innerHTML =
    `<button data-f="all" class="${filter === "all" ? "on" : ""}">すべて ${s.rows}</button>` +
    `<button data-f="eye" class="${filter === "eye" ? "on" : ""}">要判断 ${s.eye}</button>` +
    `<button data-f="learn" class="${filter === "learn" ? "on" : ""}">学習に使える ${s.learn}</button>` +
    kinds.map(([k, v]) =>
      `<button data-f="k:${k}" class="${filter === "k:" + k ? "on" : ""}" title="${v.note}">` +
      `${k} ${v.count}</button>`).join("");
  el("cmpFilters").querySelectorAll("button").forEach(b =>
    b.addEventListener("click", () => { filter = b.dataset.f; render(); }));

  const rows = DATA.rows.filter(r =>
    filter === "all" ? true :
    filter === "eye" ? r.eye :
    filter === "learn" ? r.learn :
    r.kind === filter.slice(2));
  el("cmpCount").textContent = `${rows.length} 行`;

  el("cmpRows").innerHTML = rows.map(r => {
    const [lt, rt] = (r.left && r.right)
      ? inlineDiff(r.left.text, r.right.text)
      : [esc(r.left?.text || ""), esc(r.right?.text || "")];
    const side = (c, txt, extra) => c
      ? `<div class="cmp-cue"><span class="cmp-i">${c.i}</span>` +
        `<span class="cmp-spk ${extra}">${esc(c.speaker)}</span>` +
        `<span class="cmp-txt">${txt}</span></div>`
      : `<div class="cmp-none">—</div>`;
    const spkDiff = r.left && r.right && r.left.speaker !== r.right.speaker ? "diff" : "";
    const bundle = (list, side_) => list.length > 1
      ? `<div class="cmp-bundle">${list.map(c =>
          `<span>${c.i}. ${esc(c.text)}</span>`).join("")}</div>` : "";
    const at = r.right ? r.right.start : (r.left ? r.left.start : 0);
    return `<tr class="cmp-row k-${r.kind}" data-at="${at}">
      <td class="cmp-kind"><span class="badge b-${r.kind}">${r.kind}</span>
        ${r.learn ? "" : '<span class="cmp-skip">学習に使わない</span>'}</td>
      <td class="cmp-time">${tc(at)}</td>
      <td class="cmp-side">${side(r.left, lt, spkDiff)}${bundle(r.leftIds)}</td>
      <td class="cmp-d">${r.dStart == null ? "" :
        `<span class="${Math.abs(r.dStart) >= 2 ? "big" : ""}">${r.dStart > 0 ? "+" : ""}${r.dStart}s</span>`}</td>
      <td class="cmp-side">${side(r.right, rt, spkDiff)}${bundle(r.rightIds)}</td>
    </tr>`;
  }).join("");

  el("cmpRows").querySelectorAll("tr").forEach(tr =>
    tr.addEventListener("click", () => play(parseFloat(tr.dataset.at))));
}

/* 行をクリックしたら、その位置から少し前を再生する。
 * 少し前から鳴らすのは、境目を判断したいときに頭が切れると分からないため。 */
let stopAt = 0;
function play(at) {
  const a = el("cmpAudio");
  if (!a.src) return;
  a.currentTime = Math.max(0, at - 0.4);
  stopAt = at + 4;
  a.play().catch(() => {});
  el("cmpPlaying").textContent = `${tc(at)} から再生中`;
}
el("cmpAudio").addEventListener("timeupdate", () => {
  const a = el("cmpAudio");
  if (stopAt && a.currentTime >= stopAt) { a.pause(); stopAt = 0; }
});

for (const id of ["cmpLeft", "cmpRight"])
  el(id).addEventListener("change", load);

load();

/* ---- この差分から分かること（TRAC-24） ----
 *
 * 測った作法と、そこから作った提案。反映は承認制で、いつでも取り消せる。
 * 全自動にしないのは、素材が変われば作法も変わるため（手元の 5 本で 1.26〜1.94 秒の
 * 幅があった）。黙って変わると、なぜ出力が変わったのかが分からなくなる。
 */
let SUG = null;

async function loadSuggestions() {
  if (!SET) return;
  try {
    SUG = await (await fetch(`/api/sets/${encodeURIComponent(SET)}/style`)).json();
    renderSuggestions();
  } catch { /* 提案が出せなくても比較は見られる */ }
}

function renderSuggestions() {
  if (!SUG) return;
  el("sug").hidden = false;
  const c = SUG.current, m = SUG.measured;
  const src = { set: "このセットの確定版", folder: "他のセットから", default: "既定値" };
  el("sugNow").innerHTML =
    `<span><span class="k">いま使っている目安</span><b>${c.sec} 秒 / ${c.chars} 文字</b>` +
    `　<span class="k">${esc(c.from || src[c.source] || "")}</span></span>` +
    (m.source !== "default"
      ? `<span><span class="k">確定版を測ると</span><b>${m.sec} 秒 / ${m.chars} 文字</b>` +
        `　<span class="k">${m.cues} 行から</span></span>` : "");

  const list = SUG.suggestions || [];
  el("sugList").innerHTML = list.length
    ? list.map(s => `<label class="sug-row">
        <input type="checkbox" value="${esc(s.id)}">
        <span class="sug-b"><span class="sug-t">${esc(s.title)}</span>
          <div class="sug-w">根拠: ${esc(s.why)}</div>
          <div class="sug-d">${esc(s.detail)}</div></span>
      </label>`).join("")
    : `<div class="sug-empty">${SUG.applied
        ? "いまの作法は反映済みです。新しい提案はありません。"
        : SUG.measured.source === "default"
          ? "確定版がまだありません。wfp を取り込むか、手作業で直し終えたら測れます。"
          : "いま反映できる提案はありません。"}</div>`;

  const boxes = el("sugList").querySelectorAll("input");
  const sync = () => el("sugApply").disabled =
    ![...boxes].some(b => b.checked);
  boxes.forEach(b => b.addEventListener("change", sync));
  sync();
  el("sugUndo").hidden = !SUG.canUndo;
  el("sugMsg").textContent = SUG.applied
    ? `反映済み（${new Date(SUG.appliedAt * 1000).toLocaleString("ja-JP")}）` : "";
}

el("sugApply").addEventListener("click", async () => {
  const ids = [...el("sugList").querySelectorAll("input:checked")].map(b => b.value);
  if (!ids.length) return;
  el("sugApply").disabled = true;
  el("sugMsg").textContent = "反映しています…";
  try {
    SUG = await (await fetch(`/api/sets/${encodeURIComponent(SET)}/style/apply`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    })).json();
    renderSuggestions();
    el("sugMsg").textContent = "反映しました。次の文字起こしから効きます。";
  } catch (e) {
    el("sugMsg").textContent = "失敗しました: " + e.message;
  }
});

el("sugUndo").addEventListener("click", async () => {
  el("sugUndo").disabled = true;
  try {
    SUG = await (await fetch(`/api/sets/${encodeURIComponent(SET)}/style/undo`,
                             { method: "POST" })).json();
    renderSuggestions();
    el("sugMsg").textContent = "1 つ前に戻しました。";
  } finally {
    el("sugUndo").disabled = false;
  }
});

loadSuggestions();
