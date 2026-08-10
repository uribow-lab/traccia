/* ============================================================
   話者別字幕エディタ — 案A（3ペイン）
   ============================================================ */
"use strict";

const $ = (id) => document.getElementById(id);
const MIN_DUR = 0.15;          // 字幕の最短の長さ（秒）
const NEW_CUE_DUR = 2.0;       // A キー・リストの「+」で足すときの長さ（秒）

// タイムラインのダブルクリックで足すときは、秒ではなく「見た目の幅」で決める。
// 秒固定だと、引きで見ているときは触れない細片、寄って見ているときは画面いっぱい、
// と倍率次第で使い勝手が変わってしまう。
// グリップが左右 7px ずつあるので、40px あれば本体を掴む余地が残る。
const NEW_CUE_PX = 40;
const NEW_CUE_MIN_SEC = 0.3;   // 寄りすぎたときの下限
const NEW_CUE_MAX_SEC = 10;    // 引きすぎたときの上限
const SNAP_PX = 8;             // スナップの効く距離（px）
const CPS_WARN = 9.0;          // 表示速度の警告（字/秒）
const AUTOSAVE_MS = 4000;
// 読み込み直後の表示範囲。字幕の長さの中央値 × この本数ぶんを開く（下限・上限つき）
const INITIAL_CUES_IN_VIEW = 30;
const INITIAL_SPAN_MIN = 30;
const INITIAL_SPAN_MAX = 300;
const HINT_DEFAULT =
  "ブロック＝移動 / 端＝伸縮 / 縦＝話者変更　·　ホイール＝拡大縮小 / 余白を右ドラッグ＝表示位置の移動";

// 話者の既定色。project.py の DEFAULT_PALETTE と揃えてある。
const DEFAULT_PALETTE = [
  "#E4685D", "#5B6EE1", "#E0A93F", "#A97BD4",
  "#4FB477", "#D96BA8", "#5FB3C9", "#B79A5E",
];

const S = {
  setName: null,
  cues: [],
  speakers: [],
  speakerColors: {},
  nextId: 1,
  duration: 0,
  fps: 24000 / 1001,
  sel: new Set(),
  anchorId: null,
  current: null,
  view: { t0: 0, t1: 60 },
  filter: "",
  markOnly: false,      // 目印の付いた行だけに絞る
  pendingSeek: 0,
  dirty: false,
  undo: [],
  redo: [],
  wave: null,
  editingId: null,
};

/* ============================================================
   設定の自動保存（ブラウザのローカル保存）

   画面まわりの設定はここに全部集める。変更のたびに自動で書き出し、
   起動時に戻す。セットごとの状態（表示範囲・再生位置）も覚える。
   ※ 字幕そのものと話者設定はセットフォルダ側のファイルに入る。ここには入れない。
   ============================================================ */
const PREFS_KEY = "subtitleEditor.prefs.v1";
const PREFS_DEFAULT = {
  rightW: null,          // 右ペインの幅
  previewH: null,        // プレビューの高さ
  previewZoom: "fit-h",  // プレビューの表示倍率
  playbackRate: "1",
  follow: true,          // 再生に追従
  snap: true,            // スナップ
  skipDeleteConfirm: false,  // 本文があっても削除の確認を出さない
  lastSet: null,
  sets: {},              // { セット名: { view:[t0,t1], time: 秒 } }
};
let P = Object.assign({}, PREFS_DEFAULT);
let prefsTimer = null;

function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (raw) P = Object.assign({}, PREFS_DEFAULT, JSON.parse(raw));
    if (!P.sets || typeof P.sets !== "object") P.sets = {};

    // v1 より前は個別キーに散らばっていたので拾って移行する
    const legacy = { rightW: "rightW", previewH: "previewH", previewZoom: "previewZoom", lastSet: "lastSet" };
    let moved = false;
    for (const [k, oldKey] of Object.entries(legacy)) {
      const v = localStorage.getItem(oldKey);
      if (v !== null) { if (P[k] === PREFS_DEFAULT[k]) P[k] = v; localStorage.removeItem(oldKey); moved = true; }
    }
    if (moved) writePrefs();
  } catch (_) {
    P = Object.assign({}, PREFS_DEFAULT);
  }
}

function writePrefs() {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(P)); } catch (_) { /* 容量超過などは黙って諦める */ }
}
function savePrefs() {
  clearTimeout(prefsTimer);
  prefsTimer = setTimeout(writePrefs, 400);
}

/** いま画面に出ている状態を P に写す（保存はまとめて savePrefs が行う） */
function capturePrefs() {
  const css = getComputedStyle(document.documentElement);
  P.rightW = css.getPropertyValue("--right-w").trim() || P.rightW;
  P.previewH = css.getPropertyValue("--preview-h").trim() || P.previewH;
  P.previewZoom = el.selZoom.value;
  P.playbackRate = el.selRate.value;
  P.follow = el.chkFollow.checked;
  P.snap = el.chkSnap.checked;
  if (S.setName) {
    P.lastSet = S.setName;
    P.sets[S.setName] = {
      view: [+S.view.t0.toFixed(3), +S.view.t1.toFixed(3)],
      time: +currentTime().toFixed(3),
    };
  }
  savePrefs();
}

/** 起動時に画面へ反映する。セットの読み込みより前に呼ぶ。 */
function applyPrefs() {
  const root = document.documentElement.style;
  if (P.rightW) root.setProperty("--right-w", P.rightW);
  if (P.previewH) root.setProperty("--preview-h", P.previewH);
  if ([...el.selZoom.options].some(o => o.value === P.previewZoom)) el.selZoom.value = P.previewZoom;
  if ([...el.selRate.options].some(o => o.value === P.playbackRate)) el.selRate.value = P.playbackRate;
  el.chkFollow.checked = P.follow !== false;
  el.chkSnap.checked = P.snap !== false;
}

const el = {};          // よく使う DOM
const cueEls = new Map(); // id -> タイムラインのブロック
const rowEls = new Map(); // id -> リストの行

/* ============================================================
   時刻の書式
   ============================================================ */
function pad(v, w) { v = String(v); while (v.length < w) v = "0" + v; return v; }

/* ---------- IME（日本語変換）ガード ----------
   変換を確定する Enter は、確定操作の Enter と区別しないといけない。
   isComposing が true で来るのが基本だが、ブラウザによっては compositionend が
   先に走って false になるので、確定直後のわずかな時間も変換中として扱う。 */
const IME_TAIL_MS = 120;

function guardIME(node) {
  node.addEventListener("compositionstart", () => { node.dataset.composing = "1"; });
  node.addEventListener("compositionend", () => {
    node.dataset.composing = "";
    node.dataset.composedAt = String(Date.now());
  });
}

function isIME(e) {
  if (e.isComposing || e.keyCode === 229) return true;
  const n = e.target;
  if (!n || !n.dataset) return false;
  if (n.dataset.composing) return true;
  const t = +(n.dataset.composedAt || 0);
  return t > 0 && Date.now() - t < IME_TAIL_MS;
}

/* 話者名はユーザーが自由に付けられるので、HTML に差し込む前に必ず通す */
/**
 * 本文を SRT に載せられる形にそろえる。サーバー側の srt.normalize_text と対。
 * 空行を残したまま保存すると、SRT では空行がブロックの区切りなので、
 * 書き出したあと読み直したときに後ろが失われる。
 */
function normalizeText(t) {
  if (!t) return "";
  return String(t)
    .replace(/\r\n?/g, "\n")
    .replace(/\n\s*\n+/g, "\n")      // 空行は 1 つの改行に畳む
    .split("\n").map(l => l.replace(/\s+$/, "")).join("\n")
    .trim();
}

/** タイムラインのブロックは 1 行しか入らないので、改行は記号にして 1 行に収める */
function oneLine(t) { return String(t || "").replace(/\n/g, " ⏎ "); }

function esc(s) {
  return String(s).replace(/[&<>"']/g, ch => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function fmtTC(t, withHour) {
  if (!isFinite(t) || t < 0) t = 0;
  const ms = Math.round(t * 1000);
  const h = Math.floor(ms / 3600000);
  const m = Math.floor(ms / 60000) % 60;
  const s = Math.floor(ms / 1000) % 60;
  const f = ms % 1000;
  if (withHour || h > 0) return `${pad(h,2)}:${pad(m,2)}:${pad(s,2)}.${pad(f,3)}`;
  return `${pad(m,2)}:${pad(s,2)}.${pad(f,3)}`;
}
function fmtShort(t) {
  const s = Math.max(0, Math.floor(t));
  return `${pad(Math.floor(s/60),2)}:${pad(s%60,2)}`;
}
function parseTC(str) {
  const m = String(str).trim().match(/^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?$/);
  if (!m) {
    const n = parseFloat(str);
    return isFinite(n) ? n : null;
  }
  const h = m[1] ? +m[1] : 0;
  const frac = m[4] ? +(m[4].padEnd(3, "0")) / 1000 : 0;
  return h * 3600 + (+m[2]) * 60 + (+m[3]) + frac;
}

/* ============================================================
   話者
   ============================================================ */
function defaultColor(spk, index) {
  if (spk === "不明") return "#6E8189";
  return DEFAULT_PALETTE[(index < 0 ? 0 : index) % DEFAULT_PALETTE.length];
}
function spkColor(spk) {
  return S.speakerColors[spk] || defaultColor(spk, S.speakers.indexOf(spk));
}
function laneIndex(spk) {
  const i = S.speakers.indexOf(spk);
  return i < 0 ? 0 : i;
}

/* ============================================================
   API
   ============================================================ */
async function api(url, opts) {
  const res = await fetch(url, opts);
  let body = null;
  try { body = await res.json(); } catch (_) { /* 空レスポンス */ }
  if (!res.ok) throw new Error((body && (body.error || body.message)) || `HTTP ${res.status}`);
  return body;
}
function payload() {
  return { speakers: S.speakers, cues: S.cues.map(c => ({
    id: c.id, start: c.start, end: c.end, speaker: c.speaker, text: c.text,
    // 付いているときだけ送る。既存の edit.json と互換を保つ
    ...(c.mark ? { mark: true } : {}),
  })) };
}

let statusTimer = null;
function status(msg, kind) {
  el.statusMsg.textContent = msg || "";
  el.statusMsg.className = kind || "";
  clearTimeout(statusTimer);
  if (msg) statusTimer = setTimeout(() => { el.statusMsg.textContent = ""; }, 4000);
}

/* ============================================================
   Undo / Redo
   ============================================================ */
function snapshot() {
  return {
    cues: S.cues.map(c => ({ ...c })),
    speakers: S.speakers.slice(),
    speakerColors: Object.assign({}, S.speakerColors),
    nextId: S.nextId,
  };
}
function restore(sn) {
  S.cues = sn.cues.map(c => ({ ...c }));
  S.speakers = sn.speakers.slice();
  S.speakerColors = Object.assign({}, sn.speakerColors || {});
  S.nextId = sn.nextId;
  S.sel = new Set([...S.sel].filter(id => S.cues.some(c => c.id === id)));
}
function pushUndo(sn) {
  S.undo.push(sn || snapshot());
  if (S.undo.length > 120) S.undo.shift();
  S.redo.length = 0;
  markDirty();
  updateUndoButtons();
}
function updateUndoButtons() {
  el.btnUndo.disabled = S.undo.length === 0;
  el.btnRedo.disabled = S.redo.length === 0;
}
function doUndo() {
  if (!S.undo.length) return;
  S.redo.push(snapshot());
  restore(S.undo.pop());
  markDirty(); rebuildAll(); updateUndoButtons();
}
function doRedo() {
  if (!S.redo.length) return;
  S.undo.push(snapshot());
  restore(S.redo.pop());
  markDirty(); rebuildAll(); updateUndoButtons();
}

let autosaveTimer = null;
function markDirty() {
  S.dirty = true;
  el.btnSave.classList.add("dirty");
  el.btnSave.textContent = "保存 *";
  clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(() => save("auto"), AUTOSAVE_MS);
}
function markClean() {
  S.dirty = false;
  el.btnSave.classList.remove("dirty");
  el.btnSave.textContent = "保存";
}

/* ============================================================
   セット読み込み
   ============================================================ */
async function loadSetList() {
  const d = await api("/api/sets");
  el.setPicker.innerHTML = "";
  if (!d.sets.length) {
    el.setPicker.appendChild(new Option("（セットが見つかりません）", ""));
    return;
  }
  for (const s of d.sets) {
    const label = `${s.name}  ·  ${s.video} (${s.sizeMB}MB)${s.hasProject ? "  ·  編集中" : ""}`;
    el.setPicker.appendChild(new Option(label, s.name));
  }
  const pick = d.sets.some(s => s.name === P.lastSet) ? P.lastSet : d.sets[0].name;
  el.setPicker.value = pick;
  await openSet(pick);
}

async function openSet(name) {
  if (S.dirty && !confirm("保存していない変更があります。切り替えますか？")) {
    el.setPicker.value = S.setName;
    return;
  }
  status("読み込み中…");
  const d = await api(`/api/sets/${encodeURIComponent(name)}`);

  S.setName = name;
  S.cues = d.cues.map(c => ({ ...c }));
  S.speakers = d.speakers.slice();
  if (!S.speakers.length) S.speakers = ["A"];
  S.speakerColors = Object.assign({}, d.speakerColors || {});
  // 文字起こしの手がかり。字幕そのものではないのでアンドゥの対象にしない
  S.terms = (d.terms || []).slice();
  S.note = d.note || "";
  S.nextId = Math.max(0, ...S.cues.map(c => c.id)) + 1;
  S.duration = d.duration;
  S.fps = d.fps || 24000 / 1001;
  S.sel.clear();
  S.anchorId = null;
  S.current = null;
  S.undo.length = 0; S.redo.length = 0;
  S.wave = null;

  // 全体表示で開くと 1 本 11 分・267 件では 1 ブロックが数 px になって触れない。
  // 字幕の長さの中央値から、1 ブロックが掴める幅になる範囲を選んで開く。
  // 全体を見たいときは「全体」ボタンか 0 キー。
  const durs = S.cues.map(c => c.end - c.start).sort((a, b) => a - b);
  const median = durs.length ? durs[Math.floor(durs.length / 2)] : 3;
  const span0 = Math.min(
    Math.max(median * INITIAL_CUES_IN_VIEW, INITIAL_SPAN_MIN),
    INITIAL_SPAN_MAX,
    Math.max(10, d.duration)
  );
  const firstStart = S.cues.length ? S.cues[0].start : 0;
  const t0 = Math.max(0, Math.min(firstStart - span0 * 0.05, d.duration - span0));
  S.view = { t0, t1: t0 + span0 };

  // 前回このセットを開いていたときの表示範囲があれば、そちらを復元する
  const prev = P.sets[name];
  if (prev && Array.isArray(prev.view) && prev.view.length === 2) {
    const [a, bb] = prev.view;
    if (isFinite(a) && isFinite(bb) && bb > a && bb - a <= d.duration + 1) {
      S.view = { t0: a, t1: bb };
      clampView();
    }
  }

  P.lastSet = name;
  savePrefs();

  el.video.src = d.videoUrl;
  el.video.playbackRate = parseFloat(el.selRate.value) || 1;
  // 前回の再生位置に戻す（メタデータが来てから seek する）
  S.pendingSeek = (prev && isFinite(prev.time) && prev.time > 0) ? prev.time : 0;
  el.stageEmpty.hidden = true;
  el.scrub.max = String(d.duration);
  el.scrub.value = "0";
  el.tcTotal.textContent = "/ " + fmtTC(d.duration, true);

  el.mediaMeta.textContent =
    `${d.width}×${d.height} · ${d.fps.toFixed(3)}fps · ${d.vcodec}/${d.acodec} · ${fmtTC(d.duration, true)}` +
    (d.source === "project" ? " · 編集中データを読み込み" : "");

  const dropped = Object.values(d.reports || {}).reduce((a, r) => a + (r.empty_dropped || 0), 0);
  markClean();
  rebuildAll();
  await loadWaveform();
  status(
    `${d.cues.length} 件を読み込みました` + (dropped ? `（本文が空の ${dropped} 件は除外）` : ""),
    "ok"
  );
  updateUndoButtons();
  workSwitchSet(name);
  if (!trJobId) resumeJob(name);
}

/* ============================================================
   タイムライン: 座標計算
   ============================================================ */
function trackWidth() { return el.tlScroll.clientWidth || 1; }
function scale() { return trackWidth() / Math.max(0.001, S.view.t1 - S.view.t0); }
function tToPx(t) { return (t - S.view.t0) * scale(); }
function pxToT(px) { return S.view.t0 + px / scale(); }

function clampView() {
  const span = Math.min(Math.max(S.view.t1 - S.view.t0, 0.5), Math.max(1, S.duration));
  let t0 = S.view.t0;
  if (t0 < 0) t0 = 0;
  if (t0 + span > S.duration) t0 = Math.max(0, S.duration - span);
  S.view = { t0, t1: t0 + span };
}
function setView(t0, t1) {
  S.view = { t0, t1 };
  clampView(); layout(); savePrefsView();
  // 表示範囲が変わるとブロックが動くので、メニューが指していた位置がずれる
  if (el.ctxMenu && !el.ctxMenu.hidden) closeCtxMenu();
}

/** 表示範囲は動かすたびに呼ばれるので、少し待ってからまとめて覚える */
let viewPrefTimer = null;
function savePrefsView() {
  if (!S.setName) return;
  clearTimeout(viewPrefTimer);
  viewPrefTimer = setTimeout(capturePrefs, 500);
}

function zoomAt(factor, anchorT) {
  const span = (S.view.t1 - S.view.t0) * factor;
  const a = anchorT === undefined ? (S.view.t0 + S.view.t1) / 2 : anchorT;
  const r = (a - S.view.t0) / Math.max(0.001, S.view.t1 - S.view.t0);
  setView(a - span * r, a - span * r + span);
}

/* ============================================================
   タイムライン: 構築
   ============================================================ */
function buildLanes() {
  el.gutLanes.innerHTML = "";
  el.lanes.innerHTML = "";
  S.speakers.forEach((spk, i) => {
    const g = document.createElement("div");
    g.className = "gut-lane";
    g.dataset.spk = spk;
    g.title = `「${spk}」のブロックだけを選択`;
    g.innerHTML = `<i style="background:${spkColor(spk)}"></i><b>${esc(spk)}</b><span class="n" data-count></span>`;
    g.addEventListener("click", () => selectSpeaker(spk));
    el.gutLanes.appendChild(g);

    const lane = document.createElement("div");
    lane.className = "lane";
    lane.dataset.lane = String(i);
    lane.dataset.spk = spk;
    el.lanes.appendChild(lane);
  });
}

function buildCues() {
  cueEls.clear();
  for (const lane of el.lanes.children) lane.innerHTML = "";
  const lanesArr = [...el.lanes.children];
  for (const c of S.cues) {
    const d = document.createElement("div");
    d.className = "cue";
    d.dataset.id = String(c.id);
    d.innerHTML = `<i class="grip l"></i><span class="lbl"></span><i class="grip r"></i>`;
    (lanesArr[laneIndex(c.speaker)] || lanesArr[0]).appendChild(d);
    cueEls.set(c.id, d);
  }
  layout();
}

function layout() {
  const w = trackWidth();
  const sc = scale();
  const lanesArr = [...el.lanes.children];
  for (const c of S.cues) {
    const d = cueEls.get(c.id);
    if (!d) continue;
    const wantLane = lanesArr[laneIndex(c.speaker)] || lanesArr[0];
    if (wantLane && d.parentNode !== wantLane) wantLane.appendChild(d);
    const x = (c.start - S.view.t0) * sc;
    const cw = Math.max(2, (c.end - c.start) * sc);
    if (x + cw < -40 || x > w + 40) { d.classList.add("hidden"); continue; }
    d.classList.remove("hidden");
    d.style.left = x + "px";
    d.style.width = cw + "px";
    d.style.background = spkColor(c.speaker);
    d.classList.toggle("narrow", cw < 34);
    d.classList.toggle("marked", !!c.mark);
    const lbl = d.querySelector(".lbl");
    const one = oneLine(c.text);
    if (lbl.textContent !== one) lbl.textContent = one;
    d.title = `話者${c.speaker}  ${fmtTC(c.start)} → ${fmtTC(c.end)}  (${(c.end-c.start).toFixed(2)}s)\n${c.text}`;
  }
  drawRuler();
  drawWave();
  el.viewRange.textContent = `${fmtShort(S.view.t0)} — ${fmtShort(S.view.t1)}`;
  positionPlayhead();
  paintSelection();
}

const TICK_STEPS = [0.1,0.2,0.5,1,2,5,10,15,30,60,120,300,600,900,1800];
function drawRuler() {
  const span = S.view.t1 - S.view.t0;
  const step = TICK_STEPS.find(s => span / s <= 14) || 3600;
  let html = "";
  const first = Math.ceil(S.view.t0 / step) * step;
  for (let t = first; t <= S.view.t1; t += step) {
    const x = tToPx(t);
    const major = Math.abs(t % (step * 5)) < 1e-6;
    html += `<div class="tick${major ? " major" : ""}" style="left:${x.toFixed(1)}px">`
          + (major || span / step <= 12 ? `<span>${step < 1 ? fmtTC(t) : fmtShort(t)}</span>` : "")
          + `</div>`;
  }
  el.ruler.innerHTML = html;
}

function positionPlayhead() {
  const t = currentTime();
  const x = tToPx(t);
  if (x < -2 || x > trackWidth() + 2) { el.playhead.style.display = "none"; return; }
  el.playhead.style.display = "";
  el.playhead.style.transform = `translateX(${x}px)`;
}

/* ============================================================
   波形（抽出は次フェーズ。データが来れば描く）
   ============================================================ */
async function loadWaveform() {
  if (!S.setName) return;
  try {
    const d = await api(`/api/sets/${encodeURIComponent(S.setName)}/waveform`);
    S.wave = (d && d.peaks) ? d : null;
  } catch (_) { S.wave = null; }
  el.waveEmpty.hidden = !!S.wave;
  el.btnWave.textContent = S.wave ? "再抽出" : "抽出";
  drawWave();
}

function drawWave() {
  const cv = el.waveCanvas;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (!w || !h) return;
  const r = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(w * r) || cv.height !== Math.round(h * r)) {
    cv.width = Math.round(w * r); cv.height = Math.round(h * r);
  }
  const g = cv.getContext("2d");
  g.setTransform(r, 0, 0, r, 0, 0);
  g.clearRect(0, 0, w, h);
  if (!S.wave || !S.wave.peaks) return;

  const peaks = S.wave.peaks, rate = S.wave.rate || 100, mid = h / 2;
  g.fillStyle = "rgba(47,191,168,.55)";
  for (let x = 0; x < w; x++) {
    const ta = pxToT(x), tb = pxToT(x + 1);
    let ia = Math.floor(ta * rate), ib = Math.ceil(tb * rate);
    if (ib <= ia) ib = ia + 1;
    let peak = 0;
    for (let i = Math.max(0, ia); i < Math.min(peaks.length, ib); i++) {
      if (peaks[i] > peak) peak = peaks[i];
    }
    const a = (peak / 255) * (mid - 1);
    if (a > 0) g.fillRect(x, mid - a, 1, a * 2);
  }
}

/* ============================================================
   リスト
   ============================================================ */
/**
 * 重なりを一度に洗い出す。
 * 隣の 1 件だけ見ても足りない（3 件以上が重なることも、間に別話者が挟まることもある）。
 * 並びが開始順なので、開始が自分の終了を過ぎたところで打ち切れる。
 *
 * 返り値: Map<id, { same: 相手のcue|null, other: 相手のcue|null }>
 *   same  … 同じ話者どうしの重なり。同じ人が同時に二言喋ることはないので必ず直す
 *   other … 別の話者との重なり。同時発話ならそのままでよい
 */
function computeOverlaps() {
  const info = new Map();
  const slot = (id) => {
    let v = info.get(id);
    if (!v) { v = { same: null, other: null }; info.set(id, v); }
    return v;
  };
  for (let i = 0; i < S.cues.length; i++) {
    const a = S.cues[i];
    for (let j = i + 1; j < S.cues.length; j++) {
      const b = S.cues[j];
      if (b.start >= a.end - 1e-6) break;   // ここから先は重ならない
      const k = a.speaker === b.speaker ? "same" : "other";
      const sa = slot(a.id), sb = slot(b.id);
      if (!sa[k]) sa[k] = b;
      if (!sb[k]) sb[k] = a;
    }
  }
  return info;
}

function overlapAmount(a, b) {
  return Math.min(a.end, b.end) - Math.max(a.start, b.start);
}

function flagsOf(c, ov) {
  const f = [];
  if (ov && ov.same) {
    f.push(["ovl-same",
      `${c.speaker} 内で ${overlapAmount(c, ov.same).toFixed(2)}s 重なり`
      + `（${fmtTC(ov.same.start)} → ${fmtTC(ov.same.end)}）`]);
  }
  if (ov && ov.other) {
    f.push(["ovl-other",
      `${ov.other.speaker} と ${overlapAmount(c, ov.other).toFixed(2)}s 重なり`
      + `（${fmtTC(ov.other.start)} → ${fmtTC(ov.other.end)}）`]);
  }
  // 改行は表示される文字ではないので数えない
  const cps = c.text.replace(/\n/g, "").length / Math.max(0.001, c.end - c.start);
  if (cps > CPS_WARN) f.push(["cps", `表示速度 ${cps.toFixed(1)} 字/秒`]);
  return f;
}

function buildRows() {
  rowEls.clear();
  const frag = document.createDocumentFragment();
  S.cues.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "row";
    row.dataset.id = String(c.id);

    // 番号は目印の切り替えボタンも兼ねる。押しても行の選択はしない
    const idx = document.createElement("button");
    idx.className = "idx"; idx.type = "button";
    idx.innerHTML = `<i class="mk"></i><span class="n">${i + 1}</span>`;
    idx.title = "クリックで目印を付ける / 外す";

    const times = document.createElement("div");
    times.className = "times";
    const i0 = document.createElement("input");
    i0.className = "t0"; i0.value = fmtTC(c.start); i0.dataset.edge = "start";
    const i1 = document.createElement("input");
    i1.className = "t1"; i1.value = fmtTC(c.end); i1.dataset.edge = "end";
    const dur = document.createElement("span");
    dur.className = "dur";
    times.append(i0, i1, dur);

    const txt = document.createElement("div");
    txt.className = "txt"; txt.textContent = c.text;

    const cells = document.createElement("div");
    cells.className = "spk-cells";

    const flags = document.createElement("div");
    flags.className = "flags";

    const acts = document.createElement("div");
    acts.className = "rowacts";

    const del = document.createElement("button");
    del.className = "delbtn";
    del.textContent = "−";
    del.title = delBtnTitle();

    const add = document.createElement("button");
    add.className = "addbtn";
    add.textContent = "+";
    add.title = `この直下に字幕を足す（${esc(c.speaker)}・この行の終わりから ${NEW_CUE_DUR} 秒）`;

    acts.append(del, add);
    row.append(idx, times, txt, cells, flags, acts);
    frag.appendChild(row);
    rowEls.set(c.id, row);
  });
  const keepScroll = el.rows.scrollTop;
  el.rows.innerHTML = "";
  el.rows.appendChild(frag);
  el.rows.scrollTop = keepScroll;
  refreshRows();
}

function refreshRows() {
  const q = S.filter.trim();
  const overlaps = computeOverlaps();
  S.cues.forEach((c, i) => {
    const row = rowEls.get(c.id);
    if (!row) return;
    const idxEl = row.children[0];
    const nEl = idxEl.querySelector(".n");
    if (nEl && nEl.textContent !== String(i + 1)) nEl.textContent = String(i + 1);
    row.classList.toggle("marked", !!c.mark);
    const times = row.children[1];
    if (document.activeElement !== times.children[0]) times.children[0].value = fmtTC(c.start);
    if (document.activeElement !== times.children[1]) times.children[1].value = fmtTC(c.end);
    const d = c.end - c.start;
    const durEl = times.children[2];
    durEl.textContent = d.toFixed(2) + "s";
    durEl.classList.toggle("warn", d < 0.4 || d > 12);

    const txt = row.children[2];
    if (txt.classList.contains("txt") && txt.tagName === "DIV" && txt.textContent !== c.text) {
      txt.textContent = c.text;
    }

    const cells = row.children[3];
    if (cells.childElementCount !== S.speakers.length) {
      cells.innerHTML = "";
      S.speakers.forEach(spk => {
        const b = document.createElement("button");
        b.className = "spk-cell"; b.dataset.spk = spk; b.textContent = spk;
        b.title = `「${spk}」にする`;   // 縦書きで切れることがあるので、全名をここに出す
        cells.appendChild(b);
      });
    }
    for (const b of cells.children) {
      const on = b.dataset.spk === c.speaker;
      b.classList.toggle("on", on);
      b.style.background = on ? spkColor(c.speaker) : "";
    }

    const fl = flagsOf(c, overlaps.get(c.id));
    const flags = row.children[4];
    flags.innerHTML = fl.map(([k, t]) => `<span class="dot ${k}" title="${t}"></span>`).join("");

    // 本文検索と目印の絞り込みは重ねて効く（両方に当てはまる行だけ出す）
    const hit = (!q || c.text.includes(q)) && (!S.markOnly || !!c.mark);
    row.classList.toggle("hidden", !hit);
  });

  el.listCount.textContent = `${S.cues.length} 件`;
  paintMarkFilter();
  // 話者ごとの件数はタイムラインのレーン見出しに出す（リスト下部の表示は廃止）
  const counts = {};
  for (const c of S.cues) counts[c.speaker] = (counts[c.speaker] || 0) + 1;
  for (const g of el.gutLanes.children) {
    const n = g.querySelector("[data-count]");
    if (n) n.textContent = counts[g.dataset.spk] || 0;
  }
}

function rebuildAll() {
  S.cues.sort((a, b) => a.start - b.start || a.end - b.end);
  lastOrderKey = S.cues.map(c => c.id).join(",");
  buildLanes();
  buildCues();
  buildRows();
  updateCurrent(true);
  paintSelection();
}

/* ============================================================
   選択と現在位置
   ============================================================ */
function paintSelection() {
  for (const [id, d] of cueEls) d.classList.toggle("sel", S.sel.has(id));
  for (const [id, r] of rowEls) r.classList.toggle("sel", S.sel.has(id));
}

/** 選択している行が見えるところまでリストを送る。
 *
 * cueId を渡すとその行を、渡さなければ選択の先頭を対象にする。
 * 絞り込みを切り替えるとリストの中身が入れ替わるが、スクロール位置はそのまま
 * 残る。150 番目だけを表示させて選び、絞り込みを解いたときに先頭の 1〜20 が
 * 出て選んだ行が画面外に消えるのはこのため。選択したものが見えている状態を保つ。
 */
function revealSelectedRow(cueId) {
  const cue = cueId != null ? S.cues.find(c => c.id === cueId) : selectedCues()[0];
  if (!cue) return;
  const row = rowEls.get(cue.id);
  if (!row || row.classList.contains("hidden")) return;   // 絞り込みで消えた行は追わない

  const box = el.rows;
  // すでに見えているなら動かさない。読んでいる位置が勝手にずれるほうが煩わしい。
  if (row.offsetTop >= box.scrollTop &&
      row.offsetTop + row.offsetHeight <= box.scrollTop + box.clientHeight) return;

  box.scrollTop = Math.max(0, row.offsetTop - box.clientHeight / 2 + row.offsetHeight / 2);
}
function select(ids, additive) {
  if (!additive) S.sel.clear();
  for (const id of ids) S.sel.add(id);
  paintSelection();
}
function selectSpeaker(spk) {
  select(S.cues.filter(c => c.speaker === spk).map(c => c.id), false);
  status(`話者${spk} の ${S.sel.size} 件を選択`, "ok");
}
function selectedCues() {
  return S.cues.filter(c => S.sel.has(c.id));
}
/** タイムライン上の矩形に掛かる字幕。レーンの範囲と時間の範囲の両方で絞る。
 *
 * 範囲選択のドラッグ（startMarquee）と Shift クリックで同じ判定を使う。
 * 別々に書くと、同じ範囲を指したのに結果が違う、ということが起きる。
 * 時間は「少しでも重なれば採る」。端にまたがるブロックを取りこぼさないため。
 */
function cuesInBox(laneA, laneB, t0, t1) {
  const lo = Math.min(laneA, laneB), hi = Math.max(laneA, laneB);
  const spks = S.speakers.slice(Math.max(0, lo), Math.max(0, hi) + 1);
  return S.cues.filter(c => spks.includes(c.speaker) && c.end > t0 && c.start < t1);
}

/** Shift クリック。起点とクリック先を対角線とする矩形で選ぶ。
 *
 * 以前は S.cues（時間順）の添字で「間」を採っていたので、話者をまたいで
 * 挟まったブロックが全部入っていた。A1 から A3 を選んだつもりで、
 * 間にある B や C まで付いてくる、という状態だった。
 */
function selectRange(fromId, toId) {
  const a = S.cues.find(c => c.id === fromId);
  const b = S.cues.find(c => c.id === toId);
  if (!a || !b) { select([toId], false); return; }
  const hits = cuesInBox(
    laneIndex(a.speaker), laneIndex(b.speaker),
    Math.min(a.start, b.start), Math.max(a.end, b.end));
  select(hits.map(c => c.id), false);
}

/**
 * クリックによる選択の共通処理。
 * ⇧ = 直前に選んだものからの範囲、⌘/Ctrl = 追加と解除、単独 = 置き換え。
 * 戻り値 true のとき、呼び出し側は「単独選択」として再生位置も動かしてよい。
 */
function handlePick(id, ev) {
  if (ev.shiftKey && S.anchorId != null && S.anchorId !== id) {
    selectRange(S.anchorId, id);
    return false;
  }
  if (ev.metaKey || ev.ctrlKey) {
    if (S.sel.has(id)) S.sel.delete(id); else S.sel.add(id);
    S.anchorId = id;
    paintSelection();
    return false;
  }
  // すでに複数選択の一部なら選択は崩さない（まとめてドラッグできるように）。
  // 選んであるものをもう一度押したときは、右のリストをそこへ送る。
  // 再生ヘッドが乗っていないブロックは、押してもリストの外にあるままで
  // 本文を直しに行けなかった。Shift / ⌘ のときは呼ばない（範囲を広げている
  // 最中にリストが飛ぶと追えなくなる）。
  const already = S.sel.has(id);
  if (!already) select([id], false);
  S.anchorId = id;
  if (already) revealSelectedRow(id);
  return true;
}
function soleSelected() {
  const s = selectedCues();
  return s.length === 1 ? s[0] : null;
}

function cuesAt(t) { return S.cues.filter(c => t >= c.start && t < c.end); }

let lastCurrentKey = "";
function updateCurrent(force) {
  const t = currentTime();
  const active = cuesAt(t);
  const key = active.map(c => c.id).join(",");
  if (key === lastCurrentKey && !force) return;
  lastCurrentKey = key;

  for (const [id, d] of cueEls) d.classList.toggle("cur", active.some(c => c.id === id));
  for (const [id, r] of rowEls) r.classList.toggle("cur", active.some(c => c.id === id));

  if (!active.length) {
    el.telopBar.innerHTML = `<span class="telop-none">（この位置に字幕はありません）</span>`;
  } else {
    el.telopBar.innerHTML = active.map(c =>
      `<div class="telop-line"><span class="who" style="background:${spkColor(c.speaker)}">話者${esc(c.speaker)}</span>`
      + `<span class="body"></span></div>`
    ).join("");
    [...el.telopBar.querySelectorAll(".body")].forEach((b, i) => { b.textContent = active[i].text; });
  }

  if (active.length && S.editingId === null) {
    const row = rowEls.get(active[0].id);
    if (row && !row.classList.contains("hidden")) {
      const box = el.rows;
      const top = row.offsetTop - box.clientHeight / 2 + row.offsetHeight / 2;
      if (Math.abs(box.scrollTop - top) > box.clientHeight * 0.35) box.scrollTop = Math.max(0, top);
    }
  }
}

/* ============================================================
   プレイヤー
   ============================================================ */
function currentTime() { return el.video.currentTime || 0; }
function seek(t) {
  t = Math.max(0, Math.min(S.duration - 0.001, t));
  el.video.currentTime = t;
  onTimeChanged();
}
function frameDur() { return 1 / (S.fps || 24); }
function stepFrames(n) {
  el.video.pause();
  const f = frameDur();
  seek(Math.round(currentTime() / f) * f + n * f);
}
function togglePlay() { el.video.paused ? el.video.play() : el.video.pause(); }

function onTimeChanged() {
  const t = currentTime();
  el.tcNow.textContent = fmtTC(t, true);
  if (document.activeElement !== el.scrub) el.scrub.value = String(t);
  positionPlayhead();
  updateCurrent(false);

  if (el.chkFollow.checked && !el.video.paused) {
    const span = S.view.t1 - S.view.t0;
    if (t < S.view.t0 + span * 0.08 || t > S.view.t0 + span * 0.88) {
      setView(t - span * 0.3, t - span * 0.3 + span);
    }
  }
}

function tick() {
  onTimeChanged();
  if (!el.video.paused) requestAnimationFrame(tick);
}

/* ---------- プレビューの表示倍率 ----------
   高さフィット / 幅フィット / 実寸% のどれでも、幅と高さを両方 px で指定する。
   縦横比は videoWidth:videoHeight から計算するので崩れない。
   max-width/max-height だけに任せると、スクロールする親の中で縮まらない。 */
function applyPreviewSize() {
  const vw = el.video.videoWidth, vh = el.video.videoHeight;
  if (!vw || !vh) return;

  const mode = el.selZoom.value;
  const cw = el.stage.clientWidth, ch = el.stage.clientHeight;
  const ar = vw / vh;
  let w, h;

  if (mode === "fit-h")      { h = ch;          w = h * ar; }
  else if (mode === "fit-w") { w = cw;          h = w / ar; }
  else                       { const z = parseFloat(mode) || 1; w = vw * z; h = vh * z; }

  w = Math.max(1, Math.round(w));
  h = Math.max(1, Math.round(h));
  el.video.style.width = w + "px";
  el.video.style.height = h + "px";

  const pannable = w > cw + 1 || h > ch + 1;
  el.stage.classList.toggle("pannable", pannable);
  el.zoomInfo.textContent =
    `${w}×${h}` + (mode.startsWith("fit") ? ` (${Math.round(w / vw * 100)}%)` : "") +
    (pannable ? " · ドラッグで移動" : "");
}

/* はみ出しているときは掴んで表示位置を動かせる */
function initStagePan() {
  el.stage.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    if (!el.stage.classList.contains("pannable")) return;
    ev.preventDefault();

    const x0 = ev.clientX, y0 = ev.clientY;
    const sl = el.stage.scrollLeft, st = el.stage.scrollTop;
    el.stage.classList.add("panning");

    const move = (e) => {
      el.stage.scrollLeft = sl - (e.clientX - x0);
      el.stage.scrollTop = st - (e.clientY - y0);
    };
    const up = () => {
      el.stage.classList.remove("panning");
      window.removeEventListener("pointermove", move, true);
      window.removeEventListener("mousemove", move, true);
      window.removeEventListener("pointerup", up, true);
      window.removeEventListener("mouseup", up, true);
      window.removeEventListener("blur", up);
    };
    window.addEventListener("pointermove", move, true);
    window.addEventListener("mousemove", move, true);
    window.addEventListener("pointerup", up, true);
    window.addEventListener("mouseup", up, true);
    window.addEventListener("blur", up);
  });
}

/* ============================================================
   編集
   ============================================================ */
function sortCues() { S.cues.sort((a, b) => a.start - b.start || a.end - b.end); }

let lastOrderKey = "";

/** 並び順が変わったときだけ DOM を作り直す。移動だけなら位置更新で済ませる。 */
function commitStructuralChange() {
  sortCues();
  const key = S.cues.map(c => c.id).join(",");
  if (key !== lastOrderKey) {
    lastOrderKey = key;
    buildCues();
    buildRows();
  } else {
    layout();
    refreshRows();
  }
  updateCurrent(true);
  paintSelection();
}

/* ---------- 目印 ----------
   意味は決めない。「あとで見直す」「ここまで済み」など、使う人が決めて使う。 */
function toggleMark(cues) {
  if (!cues.length) return;
  pushUndo();
  // 混在しているときは「全部付ける」。押すたびに全部付く→全部外れる、と動く
  const on = !cues.every(c => c.mark);
  for (const c of cues) c.mark = on;
  markDirty();
  refreshRows();
  layout();
  status(`${cues.length} 件の目印を${on ? "付けました" : "外しました"}`, "ok");
}

function markedCount() { return S.cues.reduce((n, c) => n + (c.mark ? 1 : 0), 0); }

/** リスト見出しの「印 N」。0 件なら出さない */
function paintMarkFilter() {
  const n = markedCount();
  el.btnMarks.hidden = n === 0 && !S.markOnly;
  el.btnMarks.textContent = `印 ${n}`;
  el.btnMarks.classList.toggle("on", S.markOnly);
  el.btnMarks.title = S.markOnly
    ? "目印の付いた行だけを表示中。クリックで解除"
    : "クリックすると目印の付いた行だけを表示";
}

function setSpeaker(cues, spk) {
  if (!cues.length) return;
  pushUndo();
  for (const c of cues) c.speaker = spk;
  commitStructuralChange();
  status(`${cues.length} 件を 話者${spk} に変更`, "ok");
}

/* ---------- 話者設定シート ---------- */
let spkDraft = null;   // [{ from, name, color }]  from は編集前の名前（新規は null）

function openSpeakerSheet() {
  spkDraft = S.speakers.map(name => ({ from: name, name, color: spkColor(name) }));
  el.vocabTerms.value = (S.terms || []).join("\n");
  el.vocabNote.value = S.note || "";
  el.spkMsg.textContent = "";
  el.spkMsg.className = "hint";
  renderSpeakerSheet();
  el.spkOverlay.hidden = false;
  // オーバーレイは隠すだけなのでスクロール位置が残る。
  // 用語欄を触って閉じたあと開き直すと話者の見出しが画面外になるため、先頭へ戻す。
  const body = el.spkOverlay.querySelector(".sheet-body");
  if (body) body.scrollTop = 0;
  const first = el.spkTable.querySelector('input[type="text"]:not(:disabled)');
  if (first) first.focus();
}

function renderSpeakerSheet() {
  const counts = {};
  for (const c of S.cues) counts[c.speaker] = (counts[c.speaker] || 0) + 1;

  el.spkTable.innerHTML = "";
  spkDraft.forEach((s, i) => {
    const n = s.from ? (counts[s.from] || 0) : 0;
    const row = document.createElement("div");
    row.className = "spk-row";
    row.innerHTML =
      `<span class="lane-no">${i + 1}</span>` +
      `<input type="color" value="${esc(s.color)}" aria-label="話者の色">` +
      `<input type="text" value="${esc(s.name)}" placeholder="話者の名前" aria-label="話者の名前"` +
        `${s.from === "不明" ? " disabled" : ""}>` +
      `<span class="count">${n} 件</span>` +
      `<button class="kill" title="${n ? "字幕があるので削除できません" : "この話者を削除"}"` +
        `${n || s.from === "不明" ? " disabled" : ""}>✕</button>`;

    row.querySelector('input[type="color"]').addEventListener("input", (e) => {
      s.color = e.target.value;
    });
    const nameInput = row.querySelector('input[type="text"]');
    guardIME(nameInput);
    nameInput.addEventListener("input", (e) => { s.name = e.target.value; });
    row.querySelector(".kill").addEventListener("click", () => {
      spkDraft.splice(i, 1);
      renderSpeakerSheet();
    });
    el.spkTable.appendChild(row);
  });
}

function addSpeakerRow() {
  // 使っていない A,B,C… を探して既定名にする
  const used = new Set(spkDraft.map(s => s.name));
  let name = "";
  for (let i = 0; i < 26 && !name; i++) {
    const cand = String.fromCharCode(65 + i);
    if (!used.has(cand)) name = cand;
  }
  if (!name) name = `話者${spkDraft.length + 1}`;

  const at = spkDraft.findIndex(s => s.from === "不明");
  const row = { from: null, name, color: defaultColor(name, spkDraft.length) };
  if (at >= 0) spkDraft.splice(at, 0, row); else spkDraft.push(row);
  renderSpeakerSheet();

  const inputs = el.spkTable.querySelectorAll('input[type="text"]');
  const target = inputs[at >= 0 ? at : inputs.length - 1];
  if (target) { target.focus(); target.select(); }
}

function spkError(msg) {
  el.spkMsg.textContent = msg;
  el.spkMsg.className = "hint err";
}

async function applySpeakerSheet() {
  const names = spkDraft.map(s => s.name.trim());
  if (names.some(n => !n)) { spkError("名前が空の行があります"); return; }
  const dup = names.find((n, i) => names.indexOf(n) !== i);
  if (dup) { spkError(`「${dup}」が重複しています`); return; }
  if (names.some(n => /[\\/:*?"<>|]/.test(n))) {
    spkError("ファイル名に使えない文字（ \\ / : * ? \" < > | ）は使えません");
    return;
  }

  // 用語は改行・カンマ・読点のどれで区切っても受ける
  S.terms = el.vocabTerms.value.split(/[\n,、]+/).map(s => s.trim())
    .filter((s, i, a) => s && a.indexOf(s) === i);
  S.note = el.vocabNote.value.trim();

  pushUndo();

  // 旧名 → 新名。字幕の話者もまとめて付け替える。
  const rename = {};
  for (const s of spkDraft) if (s.from && s.from !== s.name.trim()) rename[s.from] = s.name.trim();
  if (Object.keys(rename).length) {
    for (const c of S.cues) if (rename[c.speaker]) c.speaker = rename[c.speaker];
  }

  S.speakers = spkDraft.map(s => s.name.trim());
  S.speakerColors = {};
  for (const s of spkDraft) S.speakerColors[s.name.trim()] = s.color;

  // 消された話者に字幕が残っていたら、先頭の話者へ寄せる（取りこぼし防止）
  const known = new Set(S.speakers);
  const orphan = S.cues.filter(c => !known.has(c.speaker));
  if (orphan.length) for (const c of orphan) c.speaker = S.speakers[0];

  rebuildAll();
  el.spkOverlay.hidden = true;

  const renamed = Object.entries(rename).map(([a, b]) => `${a}→${b}`).join(" / ");

  // 話者名の変更は字幕側にも及ぶ。settings.json だけ新しくて edit.json が古い、
  // という食い違った状態を作らないよう、2 つを続けて書き切る。
  const okSettings = await saveSettings();
  const okProject = await save("quiet");

  if (!okSettings || !okProject) {
    status("話者設定の保存に失敗しました。もう一度「保存」を押してください", "err");
    return;
  }
  status(
    "セット設定と字幕を保存しました" + (renamed ? `（${renamed}）` : "") +
    (orphan.length ? ` / ${orphan.length} 件を 話者${S.speakers[0]} に移動` : "") +
    (S.terms.length ? ` / 固有名詞 ${S.terms.length} 語` : ""),
    "ok"
  );
}

async function saveSettings() {
  if (!S.setName) return false;
  try {
    await api(`/api/sets/${encodeURIComponent(S.setName)}/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        speakers: S.speakers.map(n => ({ name: n, color: spkColor(n) })),
        terms: S.terms || [],
        note: S.note || "",
      }),
    });
    return true;
  } catch (e) {
    status("話者設定の保存に失敗: " + e.message, "err");
    return false;
  }
}

function setEdge(c, edge, t) {
  if (edge === "start") c.start = Math.max(0, Math.min(t, c.end - MIN_DUR));
  else c.end = Math.min(S.duration, Math.max(t, c.start + MIN_DUR));
}

function setEdgeToPlayhead(edge) {
  const c = soleSelected() || cuesAt(currentTime())[0];
  if (!c) { status("先に字幕を選んでください"); return; }
  pushUndo();
  setEdge(c, edge, currentTime());
  select([c.id], false);
  commitStructuralChange();
  status(`#${S.cues.indexOf(c) + 1} の${edge === "start" ? "開始" : "終了"}を ${fmtTC(currentTime())} に`, "ok");
}

/* ---------- 字幕の追加 ----------
   重なりの判定は「同じ話者のブロック」に対してだけ行う。
   別の話者と時間が重なるのは同時発話として実際に起こりうるため。
   逆に同じ人が同時に 2 つ喋ることはないので、そこは必ず弾く。
   詰めたり縮めたりはしない。入らなければ作らず、理由を出す。 */
function overlappingCue(start, end, speaker, ignoreId) {
  const eps = 1e-6;
  return S.cues.find(c =>
    c.speaker === speaker && c.id !== ignoreId &&
    c.start < end - eps && c.end > start + eps
  ) || null;
}

/* ---------- 削除の確認ダイアログ ----------
   何を消すのかを見せてから聞く。ネイティブの confirm() だと中身が出せない。 */
let confirmResolve = null;

function closeConfirm(answer) {
  if (!confirmResolve) return;
  const done = confirmResolve;
  confirmResolve = null;
  el.confirmOverlay.hidden = true;
  done(answer);
}

/** 消す cue の一覧を見せて聞く。true なら実行してよい。 */
function askDelete(cues) {
  el.confirmTitle.textContent = "削除の確認";
  el.confirmBody.innerHTML = cues.length === 1
    ? "この字幕を削除します。よろしいですか？"
    : `選択中の <b>${cues.length} 件</b> を削除します。よろしいですか？`;

  const shown = cues.slice(0, 8);
  el.confirmPreview.innerHTML = shown.map(c =>
    `<div class="cp-row">` +
    `<span class="chip" style="background:${spkColor(c.speaker)}">${esc(c.speaker)}</span>` +
    `<span class="cp-tc">${fmtTC(c.start)}<br>${fmtTC(c.end)}</span>` +
    `<span class="cp-txt"></span></div>`
  ).join("") + (cues.length > shown.length
    ? `<div class="cp-more">ほか ${cues.length - shown.length} 件</div>` : "");
  // 本文はユーザーが入力したものなので textContent で入れる
  [...el.confirmPreview.querySelectorAll(".cp-txt")].forEach((n, i) => {
    n.textContent = shown[i].text || "（本文なし）";
  });

  el.confirmOverlay.hidden = false;
  el.btnConfirmCancel.focus();   // 既定は「やめる」側に置く
  return new Promise(resolve => { confirmResolve = resolve; });
}

function warn(title, body, hint) {
  el.warnTitle.textContent = title;
  el.warnBody.innerHTML = body;
  el.warnHint.textContent = hint || "";
  el.warnOverlay.hidden = false;
  el.btnWarnOk.focus();
}

/**
 * 再生位置や指定位置に、空の字幕を 1 件足す。
 * 入らなければ null を返し、警告を出す。
 */
function addCue(start, speaker, dur) {
  if (!S.cues.length && !S.speakers.length) return null;
  speaker = speaker || S.speakers[0];

  start = Math.max(0, start);
  let end = start + (dur || NEW_CUE_DUR);

  // 動画の終わりは「重なり」ではないので、収まる範囲まで縮める
  if (end > S.duration) {
    end = S.duration;
    if (end - start < MIN_DUR) {
      warn("追加できません",
        "動画の終わりまで <b>" + (end - start).toFixed(2) + " 秒</b> しかありません。",
        "もっと手前の位置で追加してください。");
      return null;
    }
  }

  const clash = overlappingCue(start, end, speaker);
  if (clash) {
    const n = S.cues.indexOf(clash) + 1;
    warn(
      "ここには追加できません",
      `話者${esc(speaker)} の <b>#${n}</b>（<code>${fmtTC(clash.start)} → ${fmtTC(clash.end)}</code>）と重なります。<br>` +
      `追加しようとした範囲は <code>${fmtTC(start)} → ${fmtTC(end)}</code> です。`,
      "既存のブロックを短くして隙間を作るか、空いている位置で追加してください。長さを勝手に詰めることはしません。"
    );
    return null;
  }

  pushUndo();
  const cue = { id: S.nextId++, start, end, speaker, text: "" };
  S.cues.push(cue);
  select([cue.id], false);
  S.anchorId = cue.id;
  commitStructuralChange();
  ensureVisible(cue);
  status(`${speaker} に ${fmtTC(start)} から ${(end - start).toFixed(2)} 秒の字幕を追加`, "ok");
  beginEdit(cue.id, true);
  return cue;
}

/**
 * A キーでの追加。
 * ブロックを 1 つ選んでいるなら、その「終わり」から続けて足す（リストの「+」と同じ）。
 * 選択が無い（または複数）のときだけ再生位置から足す。
 *
 * 選択直後は再生位置がそのブロックの開始へ飛んでいるので、再生位置を使うと
 * 選択中のブロック自身と重なってしまい、必ず警告になっていた。
 */
function addAtSelection() {
  const sel = soleSelected();
  if (sel) return addCue(sel.end, sel.speaker);
  return addCue(currentTime(), speakerAtPlayhead());
}

/** 再生位置で追加するときの話者を決める */
function speakerAtPlayhead() {
  const sel = soleSelected();
  if (sel) return sel.speaker;
  const now = cuesAt(currentTime())[0];
  if (now) return now.speaker;
  const before = S.cues.filter(c => c.start <= currentTime());
  if (before.length) return before[before.length - 1].speaker;
  return S.speakers[0];
}

function canSplitAt(c, t) {
  return !!c && t > c.start + MIN_DUR && t < c.end - MIN_DUR;
}

/** 指定した位置で 1 件を 2 つに割る。本文は切らずに両方へ複製する。 */
function splitCueAt(c, t) {
  if (!canSplitAt(c, t)) { status("その位置では分割できません"); return; }
  pushUndo();
  // どこで切るかは機械に決めさせず、分割してから要らない方を手で消す。時間だけを割る。
  const b = { id: S.nextId++, start: t, end: c.end, speaker: c.speaker, text: c.text };
  c.end = t;
  S.cues.push(b);
  select([c.id], false);
  commitStructuralChange();
  status(`${fmtTC(t)} で分割しました（本文は両方に同じものが入っています）`, "ok");
}

function splitAtPlayhead() {
  const t = currentTime();
  const c = cuesAt(t)[0] || soleSelected();
  if (!canSplitAt(c, t)) { status("分割できる位置に再生ヘッドがありません"); return; }
  splitCueAt(c, t);
}

function mergeWithNext() {
  const c = soleSelected() || cuesAt(currentTime())[0];
  if (!c) { status("先に字幕を選んでください"); return; }
  const i = S.cues.indexOf(c);
  const n = S.cues[i + 1];
  if (!n) { status("次の字幕がありません"); return; }
  pushUndo();
  c.end = Math.max(c.end, n.end);
  c.text = (c.text + (c.text && n.text ? " " : "") + n.text).trim();
  S.cues.splice(i + 1, 1);
  select([c.id], false);
  commitStructuralChange();
  status("次の字幕と結合しました", "ok");
}

function delBtnTitle() {
  return P.skipDeleteConfirm
    ? "この字幕を削除する（確認なし・⌘Z で戻せます）"
    : "この字幕を削除する（確認あり）";
}

/** 実際に消す。確認は呼び出し側の責任。 */
function removeCues(cues) {
  if (!cues.length) return;
  pushUndo();
  const ids = new Set(cues.map(c => c.id));
  S.cues = S.cues.filter(c => !ids.has(c.id));
  for (const id of ids) S.sel.delete(id);
  commitStructuralChange();
  status(`${cues.length} 件を削除`, "ok");
}

/** Delete キー用。1 件はそのまま消す（⌘Z で戻せる）、複数のときだけ確認する。 */
/** 本文が 1 つも入っていないなら、消しても失うものが無いので確認しない。
 *
 * 設定で「確認を出さない」にしているときは、本文があっても聞かない。
 * 消したものは removeCues が pushUndo しているので ⌘Z で戻せる。 */
function needsDeleteConfirm(cues) {
  if (P.skipDeleteConfirm) return false;
  return cues.some(c => c.text.trim());
}

async function deleteSelected() {
  const s = selectedCues();
  if (!s.length) { status("先に字幕を選んでください"); return; }
  if (s.length > 1 && needsDeleteConfirm(s) && !(await askDelete(s))) return;
  removeCues(s);
}

/** リストの「−」用。1 件でも必ず中身を見せて確認する。 */
async function deleteCueWithConfirm(cue) {
  if (!needsDeleteConfirm([cue])) { removeCues([cue]); return; }   // 空のブロックは即削除
  if (await askDelete([cue])) removeCues([cue]);
}

/* ---------- 本文のインライン編集 ----------
   selectAll: キーボード（Enter / Tab）で開いたときだけ全選択して打ち直せるようにする。
   クリックで開いたときは全選択しない。1 文字だけ直したいのに全文が消えるのを避けるため。 */
function beginEdit(id, selectAll) {
  const row = rowEls.get(id);
  const c = S.cues.find(x => x.id === id);
  if (!row || !c) return;
  const cur = row.children[2];
  if (cur.tagName === "TEXTAREA") { cur.focus(); return; }

  const ta = document.createElement("textarea");
  ta.className = "txt"; ta.value = c.text; ta.rows = 1;
  row.replaceChild(ta, cur);
  S.editingId = id;

  const fit = () => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; };
  fit();
  ta.focus();
  if (selectAll) ta.select();
  else ta.setSelectionRange(ta.value.length, ta.value.length);   // 末尾にカーソルを置く
  ta.addEventListener("input", fit);

  const finish = (commit) => {
    if (S.editingId !== id) return;
    S.editingId = null;
    const next = commit ? normalizeText(ta.value) : c.text;
    if (commit && next !== c.text) {
      pushUndo();
      c.text = next;
      const d = cueEls.get(id);
      if (d) d.querySelector(".lbl").textContent = oneLine(c.text);
    }
    const div = document.createElement("div");
    div.className = "txt"; div.textContent = c.text;
    if (ta.parentNode === row) row.replaceChild(div, ta);
    refreshRows();
    updateCurrent(true);
  };
  guardIME(ta);
  ta.addEventListener("blur", () => finish(true));
  ta.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (isIME(e)) return;   // 日本語変換中の Enter / Escape / Tab は変換側の操作
    if (e.key === "Escape") { e.preventDefault(); ta.value = c.text; finish(false); el.rows.focus(); }
    else if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); finish(true); }
    else if (e.key === "Tab") {
      e.preventDefault();
      const i = S.cues.indexOf(c);
      const nx = S.cues[e.shiftKey ? i - 1 : i + 1];
      finish(true);
      if (nx) { select([nx.id], false); beginEdit(nx.id, true); }
    }
  });
}

/* ============================================================
   タイムラインのドラッグ編集
   ============================================================ */
let drag = null;
let wheelHintTimer = null;

/**
 * スナップ先の候補。
 * withPlayhead を false にすると再生位置を候補から外す。
 * ダブルクリックでの追加では、その 1 クリック目が再生位置をクリック地点へ
 * 動かしてしまうので、外さないと「クリックした場所」に吸着して意味がなくなる。
 */
function snapTargets(excludeIds, withPlayhead = true) {
  const out = [0, S.duration];
  if (withPlayhead) out.push(currentTime());
  for (const c of S.cues) {
    if (excludeIds.has(c.id)) continue;
    out.push(c.start, c.end);
  }
  return out;
}
/* ---------- スナップガイド ----------
   ドラッグ中のブロックの端が、既存のどれかのブロックの端と重なったら縦線を出す。
   再生ヘッドやタイムラインの端に合ったときは出さない（再生ヘッドは元から線がある）。 */
function otherBlockEdges(excludeIds) {
  const out = [];
  for (const c of S.cues) {
    if (excludeIds.has(c.id)) continue;
    out.push(c.start, c.end);
  }
  return out;
}

function clearSnapGuides() {
  if (el.snapGuides) el.snapGuides.innerHTML = "";
}

function updateSnapGuides() {
  if (!drag) { clearSnapGuides(); return; }
  const g = drag.grabbed;
  const edges = drag.mode === "in" ? [g.start]
              : drag.mode === "out" ? [g.end]
              : [g.start, g.end];

  // 見た目で重なっていれば出す（0.5px ぶんの許容）。スナップを切っていても、
  // たまたま揃ったときは出したほうが分かりやすい。
  const tol = 0.5 / scale();
  const hits = [];
  for (const t of edges) {
    if (drag.blockEdges.some(e => Math.abs(e - t) <= tol) && !hits.some(h => Math.abs(h - t) <= tol)) {
      hits.push(t);
    }
  }
  el.snapGuides.innerHTML = hits
    .map(t => `<i class="snapguide" style="left:${tToPx(t).toFixed(1)}px"></i>`)
    .join("");
}

function applySnap(t, targets, enabled) {
  if (!enabled) return t;
  const tol = SNAP_PX / scale();
  let best = t, bestD = tol;
  for (const s of targets) {
    const d = Math.abs(s - t);
    if (d < bestD) { bestD = d; best = s; }
  }
  return best;
}

/* ドラッグの不具合を追うための記録。既定では何もしない。
   再現したときに原因を掴めるよう、コンソールから有効にできるようにしてある。

     localStorage.setItem("traccia.dragDebug", "1"); location.reload();
     // 再現操作をしたあと
     copy(window.__dragLog.map(x => JSON.stringify(x)).join("\n"))

   終了の直前に何が起きたか（up / cancel / blur / buttons=0）が残る。 */
const DRAG_DEBUG = (() => {
  try { return localStorage.getItem("traccia.dragDebug") === "1"; } catch (_) { return false; }
})();
if (DRAG_DEBUG) window.__dragLog = [];
function dragLog(what, info) {
  if (!DRAG_DEBUG) return;
  const rec = Object.assign({ t: Math.round(performance.now()), what }, info);
  window.__dragLog.push(rec);
  console.log("[drag]", what, info);
}

el.__initDrag = () => {
  // 最下段のレーンより下は .lane の外なので、el.lanes では拾えない。
  // 見た目はタイムラインの余白なので、レーンの余白と同じに扱う
  // （クリックで選択解除、ドラッグで範囲選択）。ルーラー・動画・波形には効かせない。
  el.tlScroll.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || ev.altKey) return;    // 表示位置の移動に譲る
    if (ev.target.closest(".lane") || ev.target.closest(".ruler")
        || ev.target.closest(".track")) return;  // それぞれの担当に任せる
    const lanesBox = el.lanes.getBoundingClientRect();
    if (ev.clientY <= lanesBox.bottom) return;   // レーンより上は対象外
    startMarquee(ev);
  });

  el.lanes.addEventListener("pointerdown", (ev) => {
    const cueEl = ev.target.closest(".cue");
    const laneEl = ev.target.closest(".lane");
    if (!laneEl || ev.button !== 0) return;      // 右・中ボタンは表示位置の移動（initPan）
    if (ev.altKey && !cueEl) return;             // Alt+左ドラッグ（余白）も表示位置の移動

    if (!cueEl) { startMarquee(ev); return; }

    const id = +cueEl.dataset.id;
    const c = S.cues.find(x => x.id === id);
    if (!c) return;

    if (!handlePick(id, ev)) return;   // 複数選択の操作中はドラッグを始めない

    const mode = ev.target.classList.contains("grip")
      ? (ev.target.classList.contains("l") ? "in" : "out")
      : "move";

    const group = mode === "move" ? selectedCues() : [c];
    const orig = group.map(g => ({
      c: g, start: g.start, end: g.end, speaker: g.speaker, lane: laneIndex(g.speaker),
    }));
    drag = {
      mode, grabbed: c, moved: false,
      x0: ev.clientX, y0: ev.clientY,
      orig,
      // 縦方向は「掴んだものが何段動いたか」を全体に足す。そのための基準と、
      // はみ出しを止めるための上下端。
      grabbedLane: laneIndex(c.speaker),
      laneLo: Math.min(...orig.map(o => o.lane)),
      laneHi: Math.max(...orig.map(o => o.lane)),
      snaps: snapTargets(new Set(group.map(g => g.id))),
      blockEdges: otherBlockEdges(new Set(group.map(g => g.id))),
      snapOn: el.chkSnap.checked,
      undoSnap: snapshot(),
      laneTop: el.lanes.getBoundingClientRect().top,
      laneH: el.lanes.children[0] ? el.lanes.children[0].offsetHeight : 34,
    };
    drag.pointerId = ev.pointerId;
    ev.preventDefault();
    cueEl.classList.add("dragging");

    // 掴んでいる間はカーソルを固定する。
    // これが無いと、スナップで端がポインタから離れた瞬間に、ポインタの下が
    // グリップからブロック本体へ移り、伸縮中なのに手のアイコンに化ける。
    document.body.classList.add(mode === "move" ? "drag-move" : "drag-edge");

    // ── ドラッグを終わらせてよいのは「ボタンが実際に離れたとき」だけ ──
    //
    // 以前は pointercancel でも終了していたが、ブラウザはこちらの制御外で
    // pointercancel を出す（キャプチャ対象が DOM から消えた／display:none に
    // なった、ネイティブのドラッグやスクロールに乗っ取られた、入力側の割り込み）。
    // その結果「押したままなのに掴んでいる状態が外れる」が起きていた。
    //
    // 代わりに pointermove の buttons を見る。押下が続いていれば 0 でないので、
    // 取りこぼした pointerup も次の移動で必ず拾える。
    window.addEventListener("pointermove", onDragMove, true);
    window.addEventListener("pointerup", onPointerUp, true);
    window.addEventListener("blur", onWindowBlur);
    // pointercancel は「終了」ではなく「キャプチャが外れた」だけとして扱う。
    // window で拾い続けているので、ドラッグはそのまま継続できる。
    window.addEventListener("pointercancel", onPointerCancel, true);
    try { cueEl.setPointerCapture(ev.pointerId); } catch (_) { /* 失敗しても window 側で拾える */ }
    dragLog("start", { mode, id, pointerId: ev.pointerId });
  });

  /** このドラッグを始めたポインタか。別のポインタの up で終わらせない。 */
  const isOurPointer = (ev) =>
    !drag || drag.pointerId === undefined || ev.pointerId === undefined
      || ev.pointerId === drag.pointerId;

  const onPointerUp = (ev) => {
    if (!isOurPointer(ev)) return;
    dragLog("up", { pointerId: ev.pointerId });
    endDrag();
  };

  const onPointerCancel = (ev) => {
    if (!isOurPointer(ev)) return;
    // 終了させない。ボタンが離れたかどうかは次の pointermove で判定する。
    dragLog("cancel（継続）", { pointerId: ev.pointerId, buttons: ev.buttons });
  };

  const onWindowBlur = () => {
    // 別アプリへ切り替わった等。ここは終了させてよい（押下状態を追えなくなるため）
    dragLog("blur", {});
    endDrag();
  };

  const onDragMove = (ev) => {
    if (!drag) return;
    if (!isOurPointer(ev)) return;
    // ボタンが離れているのに移動が来た＝ pointerup を取りこぼしている。
    // ここで確実に閉じる。pointercancel に頼らないための要。
    if (ev.buttons === 0) { dragLog("buttons=0 で終了", {}); endDrag(); return; }
    const dx = ev.clientX - drag.x0;
    if (!drag.moved && Math.abs(dx) < 2 && Math.abs(ev.clientY - drag.y0) < 4) return;
    drag.moved = true;

    const snapOn = drag.snapOn && !ev.altKey;
    let dt = dx / scale();

    if (drag.mode === "move") {
      // 掴んだブロックの開始と終了の両方を調べ、吸着した方のずれを全体に適用する。
      // applySnap は吸着しなかったとき入力をそのまま返す＝ずれ 0 になるので、
      // 「ずれが小さい方」で選ぶと、吸着しなかった側（0）が必ず勝ってしまう。
      // 実際に吸着した側だけを候補にすること。
      const g = drag.orig.find(o => o.c === drag.grabbed);
      const wantStart = g.start + dt;
      const wantEnd = g.end + dt;
      const dStart = applySnap(wantStart, drag.snaps, snapOn) - wantStart;
      const dEnd = applySnap(wantEnd, drag.snaps, snapOn) - wantEnd;
      if (dStart !== 0 && dEnd !== 0) dt += Math.abs(dStart) <= Math.abs(dEnd) ? dStart : dEnd;
      else if (dStart !== 0) dt += dStart;
      else if (dEnd !== 0) dt += dEnd;

      let lo = Infinity, hi = -Infinity;
      for (const o of drag.orig) { lo = Math.min(lo, o.start); hi = Math.max(hi, o.end); }
      dt = Math.max(-lo, Math.min(dt, S.duration - hi));

      // 縦方向 → 話者変更。
      // 掴んだブロックが何段動いたかを、選択したもの全部に同じだけ足す。
      // 「ポインタのある 1 段」にそろえると、複数の段にまたがる選択が 1 段に潰れる。
      const laneIdx = Math.floor((ev.clientY - drag.laneTop) / drag.laneH);
      const last = S.speakers.length - 1;
      let dLane = Math.max(0, Math.min(last, laneIdx)) - drag.grabbedLane;
      // どれかが段からはみ出す手前で、全体を止める。時間方向の dt と同じ考え方で、
      // こうしないと行き場を失ったものが端の段に貼り付いて段差が崩れる。
      dLane = Math.max(-drag.laneLo, Math.min(dLane, last - drag.laneHi));

      const dropSpks = new Set(drag.orig.map(o => S.speakers[o.lane + dLane]));
      for (const lane of el.lanes.children) lane.classList.toggle("drop", dropSpks.has(lane.dataset.spk));

      for (const o of drag.orig) {
        o.c.start = o.start + dt;
        o.c.end = o.end + dt;
        const spk = S.speakers[o.lane + dLane];
        if (spk && spk !== o.c.speaker) o.c.speaker = spk;
      }
    } else {
      const o = drag.orig[0];
      if (drag.mode === "in") setEdge(o.c, "start", applySnap(o.start + dt, drag.snaps, snapOn));
      else setEdge(o.c, "end", applySnap(o.end + dt, drag.snaps, snapOn));
    }

    // ドラッグ中は掴んでいるものだけ再配置（軽くするため）
    const sc = scale();
    for (const o of drag.orig) {
      const d = cueEls.get(o.c.id);
      if (!d) continue;
      d.style.left = ((o.c.start - S.view.t0) * sc) + "px";
      d.style.width = Math.max(2, (o.c.end - o.c.start) * sc) + "px";
      const wantLane = el.lanes.children[laneIndex(o.c.speaker)];
      if (wantLane && d.parentNode !== wantLane) wantLane.appendChild(d);
      d.style.background = spkColor(o.c.speaker);
    }
    updateSnapGuides();
    el.dragHint.textContent =
      drag.mode === "move"
        ? `移動 ${dt >= 0 ? "+" : ""}${dt.toFixed(3)}s → ${fmtTC(drag.grabbed.start)}`
        : `${drag.mode === "in" ? "開始" : "終了"} ${fmtTC(drag.mode === "in" ? drag.grabbed.start : drag.grabbed.end)}  (${(drag.grabbed.end - drag.grabbed.start).toFixed(2)}s)`;
  };

  function endDrag() {
    window.removeEventListener("pointermove", onDragMove, true);
    window.removeEventListener("pointerup", onPointerUp, true);
    window.removeEventListener("pointercancel", onPointerCancel, true);
    window.removeEventListener("blur", onWindowBlur);
    document.body.classList.remove("drag-move", "drag-edge");
    clearSnapGuides();
    if (drag) dragLog("end", { moved: drag.moved, mode: drag.mode });

    if (!drag) return;
    for (const lane of el.lanes.children) lane.classList.remove("drop");
    for (const o of drag.orig) {
      const d = cueEls.get(o.c.id);
      if (d) d.classList.remove("dragging");
    }
    const changed = drag.moved && drag.orig.some(o =>
      o.c.start !== o.start || o.c.end !== o.end || o.c.speaker !== o.speaker);
    const snap = drag.undoSnap;
    drag = null;
    el.dragHint.textContent = HINT_DEFAULT;
    if (changed) {
      S.undo.push(snap);
      if (S.undo.length > 120) S.undo.shift();
      S.redo.length = 0;
      markDirty(); updateUndoButtons();
      commitStructuralChange();
    }
    // ブロックを掴んだり動かしたりしても再生ヘッドは動かさない。
    // 位置合わせの基準がずれると、直している最中に見失う。
  }

  // 空き部分をダブルクリック = そのレーン（＝その話者）のその位置に追加
  el.lanes.addEventListener("dblclick", (ev) => {
    if (ev.target.closest(".cue")) return;
    const laneEl = ev.target.closest(".lane");
    if (!laneEl) return;
    ev.preventDefault();
    const box = el.tlScroll.getBoundingClientRect();
    // ドラッグと同じ基準でスナップさせる。再生ヘッドにも吸着する
    // （空きレーンのクリックでは再生ヘッドが動かないので、候補にして問題ない）
    const t = applySnap(
      pxToT(ev.clientX - box.left),
      snapTargets(new Set()),
      el.chkSnap.checked && !ev.altKey
    );
    // 長さは秒ではなく見た目の幅で決める（倍率が変わっても掴める大きさを保つ）
    const dur = Math.min(NEW_CUE_MAX_SEC, Math.max(NEW_CUE_MIN_SEC, NEW_CUE_PX / scale()));
    addCue(t, laneEl.dataset.spk, dur);
  });
};

/* ---------- 空きレーンのドラッグ = 範囲選択 ---------- */

// 端からこの距離に入ったら表示を送り始める
const EDGE_PX = 44;
// 端に張り付いたときの速さ（px/秒相当）。深く入るほどこれに近づく
const EDGE_SPEED = 900;

function startMarquee(ev) {
  const box = el.tlScroll.getBoundingClientRect();
  // 起点は px ではなく**秒**で持つ。端で表示範囲を送ると、同じ px が別の時刻を
  // 指すようになるため。px のまま持つと、送った瞬間に矩形が飛ぶ。
  const tAnchor = pxToT(ev.clientX - box.left);
  const x0 = ev.clientX, y0 = ev.clientY;
  let moved = false, cur = ev, raf = 0, lastTs = 0;
  const m = el.marquee;

  const paint = () => {
    const xa = box.left + tToPx(tAnchor);
    const xb = Math.max(box.left, Math.min(box.right, cur.clientX));
    const l = Math.min(xa, xb) - box.left, r = Math.max(xa, xb) - box.left;
    const t = Math.min(y0, cur.clientY) - box.top, b = Math.max(y0, cur.clientY) - box.top;
    m.style.left = l + "px"; m.style.top = t + "px";
    m.style.width = (r - l) + "px"; m.style.height = (b - t) + "px";

    const lanesBox = el.lanes.getBoundingClientRect();
    const laneH = el.lanes.children[0] ? el.lanes.children[0].offsetHeight : 34;
    const li0 = Math.floor((Math.min(y0, cur.clientY) - lanesBox.top) / laneH);
    const li1 = Math.floor((Math.max(y0, cur.clientY) - lanesBox.top) / laneH);
    select(cuesInBox(li0, li1, pxToT(l), pxToT(r)).map(c => c.id), false);
  };

  /** 端に寄せている間、表示範囲を送り続ける。送るたびに選択を取り直す。 */
  const step = (ts) => {
    raf = 0;
    if (!moved) return;
    const ms = lastTs ? Math.min(50, ts - lastTs) : 16;   // タブ復帰時に飛ばない程度に抑える
    lastTs = ts;

    let dir = 0, depth = 0;
    if (cur.clientX < box.left + EDGE_PX) {
      dir = -1; depth = (box.left + EDGE_PX - cur.clientX) / EDGE_PX;
    } else if (cur.clientX > box.right - EDGE_PX) {
      dir = 1; depth = (cur.clientX - (box.right - EDGE_PX)) / EDGE_PX;
    }
    if (dir) {
      const span = S.view.t1 - S.view.t0;
      const move = dir * EDGE_SPEED * Math.min(1, depth) * (ms / 1000) / scale();
      const t0 = Math.max(0, Math.min(Math.max(0, S.duration - span), S.view.t0 + move));
      if (t0 !== S.view.t0) { setView(t0, t0 + span); paint(); }
      // 端まで送り切ったら止まる（t0 が動かなくなるので次のフレームで何もしない）
    } else {
      lastTs = 0;
      return;   // 端から離れたら回すのをやめる。次に端へ入ったら onMove が起こす
    }
    raf = requestAnimationFrame(step);
  };

  const onMove = (e) => {
    if (!moved && Math.abs(e.clientX - x0) < 3 && Math.abs(e.clientY - y0) < 3) return;
    moved = true;
    m.hidden = false;
    cur = e;
    paint();
    const nearEdge = e.clientX < box.left + EDGE_PX || e.clientX > box.right - EDGE_PX;
    if (nearEdge && !raf) { lastTs = 0; raf = requestAnimationFrame(step); }
  };
  const onUp = () => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    m.hidden = true;
    // 空きをクリックしただけなら選択を外すだけ。再生ヘッドはルーラーからしか動かさない。
    if (!moved) { S.sel.clear(); paintSelection(); }
    else status(`${S.sel.size} 件を選択`, "ok");
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
  ev.preventDefault();
}

/* ---------- ブロックの右クリックメニュー ---------- */
function closeCtxMenu() {
  el.ctxMenu.hidden = true;
  el.ctxMenu.innerHTML = "";
}

/**
 * 右クリックされたブロックに対するメニューを出す。
 * 分割はクリックした位置で行う。再生ヘッドを動かさずに割れるようにするため。
 */
function openCtxMenu(ev, cue, t) {
  // 選択に入っていないブロックなら、それだけを選び直す（一般的な右クリックの挙動）
  if (!S.sel.has(cue.id)) select([cue.id], false);
  S.anchorId = cue.id;

  const targets = selectedCues();
  const splitOk = targets.length === 1 && canSplitAt(cue, t);

  el.ctxMenu.innerHTML = "";
  const head = document.createElement("div");
  head.className = "cm-head";
  head.innerHTML = targets.length > 1
    ? `<b>${targets.length} 件</b>を選択中`
    : `<b>${esc(cue.speaker)}</b> ${fmtTC(cue.start)} → ${fmtTC(cue.end)}`;
  el.ctxMenu.appendChild(head);

  const add = (label, sub, disabled, danger, fn) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = danger ? "danger" : "";
    b.disabled = !!disabled;
    b.innerHTML = `<span>${label}</span>` + (sub ? `<span class="cm-sub">${sub}</span>` : "");
    if (!disabled) b.addEventListener("click", () => { closeCtxMenu(); fn(); });
    el.ctxMenu.appendChild(b);
    return b;
  };

  add(targets.length > 1 ? `削除（${targets.length} 件）` : "削除", "Delete", false, true,
      () => { if (targets.length > 1) deleteSelected(); else deleteCueWithConfirm(cue); });

  add("ここで分割", splitOk ? fmtTC(t) : (targets.length > 1 ? "1 件のみ" : "端に近すぎ"),
      !splitOk, false, () => splitCueAt(cue, t));

  // いったん出してから、画面からはみ出さない位置に寄せる
  el.ctxMenu.hidden = false;
  el.ctxMenu.style.left = "0px";
  el.ctxMenu.style.top = "0px";
  const r = el.ctxMenu.getBoundingClientRect();
  const x = Math.min(ev.clientX + 2, window.innerWidth - r.width - 8);
  const y = Math.min(ev.clientY + 2, window.innerHeight - r.height - 8);
  el.ctxMenu.style.left = Math.max(8, x) + "px";
  el.ctxMenu.style.top = Math.max(8, y) + "px";

  const first = el.ctxMenu.querySelector("button:not(:disabled)");
  if (first) first.focus();
}

/* ---------- 余白を右ドラッグ / 中ボタンドラッグ = 表示位置の移動 ----------
   ポインタキャプチャは使わない。右ボタンのキャプチャはブラウザによって例外を
   投げることがあり、それが起きるとハンドルが登録されず「掴めるのに動かない」
   状態になる。代わりに window にリスナーを張って確実に拾う。            */
let panning = null;

function endPan() {
  if (!panning) return;
  el.tlScroll.classList.remove("panning");
  document.body.classList.remove("panning");
  el.chkFollow.checked = panning.wasFollowing;
  el.dragHint.textContent = HINT_DEFAULT;
  window.removeEventListener("pointermove", panMove, true);
  window.removeEventListener("mousemove", panMove, true);
  window.removeEventListener("pointerup", endPan, true);
  window.removeEventListener("mouseup", endPan, true);
  window.removeEventListener("pointercancel", endPan, true);
  window.removeEventListener("blur", endPan);
  panning = null;
}

function panMove(e) {
  if (!panning) return;
  const dt = (e.clientX - panning.x0) / panning.sc;
  setView(panning.t0 - dt, panning.t0 - dt + panning.span);
  el.dragHint.textContent = `表示位置 ${fmtShort(S.view.t0)} — ${fmtShort(S.view.t1)}`;
}

function beginPan(ev) {
  if (panning) return;
  if (ev.target.closest && ev.target.closest(".cue")) return;  // 字幕ブロックの上では何もしない

  // 全体表示中は動かす余地が無い。黙って何も起きないと壊れて見えるので理由を出す。
  const span0 = S.view.t1 - S.view.t0;
  if (span0 >= S.duration - 0.01) {
    el.dragHint.textContent = "全体表示中なので動かせません — ＋ で拡大してください";
    el.dragHint.classList.add("warn");
    el.tlScroll.classList.add("nopan");
    status("いま全体表示です。＋ か ⌘+ホイールで拡大すると、表示位置を動かせます");
    clearTimeout(beginPan._t);
    beginPan._t = setTimeout(() => {
      el.dragHint.textContent = HINT_DEFAULT;
      el.dragHint.classList.remove("warn");
      el.tlScroll.classList.remove("nopan");
    }, 2200);
    return;
  }

  // 先にリスナーを張る。何かで失敗しても「掴んだまま戻らない」状態にしない。
  window.addEventListener("pointermove", panMove, true);
  window.addEventListener("mousemove", panMove, true);
  window.addEventListener("pointerup", endPan, true);
  window.addEventListener("mouseup", endPan, true);
  window.addEventListener("pointercancel", endPan, true);
  window.addEventListener("blur", endPan);

  panning = {
    x0: ev.clientX,
    t0: S.view.t0,
    span: S.view.t1 - S.view.t0,
    sc: scale(),                       // 移動中は倍率が変わらないので固定でよい
    wasFollowing: el.chkFollow.checked,
  };
  el.chkFollow.checked = false;        // 掴んでいる間は再生追従を止める
  el.tlScroll.classList.add("panning");
  document.body.classList.add("panning");
}

function initPan() {
  // ブラウザの既定メニューは出さない。ブロックの上だけ自前のメニューを出す。
  el.tlScroll.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    const cueEl = e.target.closest(".cue");
    if (!cueEl) { closeCtxMenu(); return; }
    const c = S.cues.find(x => x.id === +cueEl.dataset.id);
    if (!c) return;
    const box = el.tlScroll.getBoundingClientRect();
    openCtxMenu(e, c, pxToT(e.clientX - box.left));
  });

  // pointerdown と mousedown の両方を受ける（どちらか一方しか来ない環境への保険）。
  // 実際に始めるのは先に来た方だけ（beginPan の先頭で二重起動を弾いている）。
  // 右ボタン / 中ボタン / Alt+左ボタン のいずれでも掴める。
  // 右ドラッグが効かない環境（トラックパッドの設定など）でも Alt+左で必ず動かせる。
  const onDown = (ev) => {
    const isPan = ev.button === 2 || ev.button === 1 || (ev.button === 0 && ev.altKey);
    if (!isPan) return;
    // macOS が独自にメニュー待ちに入るのを止めるため、掴む前に必ず既定動作を切る
    ev.preventDefault();
    beginPan(ev);
  };
  el.tlScroll.addEventListener("pointerdown", onDown);
  el.tlScroll.addEventListener("mousedown", onDown);

  // 中ボタンの既定動作（自動スクロール）を止める
  el.tlScroll.addEventListener("auxclick", (e) => { if (e.button === 1) e.preventDefault(); });
}

/* ============================================================
   保存 / 書き出し
   ============================================================ */
/** mode: undefined = 手動保存 / "auto" = 自動保存 / "quiet" = 通知を出さない */
async function save(mode) {
  if (!S.setName) return false;
  clearTimeout(autosaveTimer);
  try {
    const r = await api(`/api/sets/${encodeURIComponent(S.setName)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload()),
    });
    markClean();
    if (mode === undefined) status(`保存しました（${r.count} 件）`, "ok");
    else if (mode === "auto") status("自動保存しました", "ok");
    return true;
  } catch (e) {
    status("保存に失敗: " + e.message, "err");
    return false;
  }
}

async function exportSrt() {
  if (!S.setName) return;
  try {
    status("書き出し中…");
    const r = await api(`/api/sets/${encodeURIComponent(S.setName)}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload()),
    });
    markClean();
    const lines = r.files.map(f => `　${f.speaker}: ${f.count} 件`).join("\n");
    const skipped = (r.skipped && r.skipped.length)
      ? `\n\n字幕が無いため書き出さなかった話者: ${r.skipped.join("、")}` : "";
    alert(`書き出しました\n\n${r.dir}\n\n${lines}${skipped}`);
    status("書き出しました", "ok");
  } catch (e) {
    status("書き出しに失敗: " + e.message, "err");
  }
}

/* ============================================================
   キーボード
   ============================================================ */
function isTyping() {
  const a = document.activeElement;
  return a && (a.tagName === "TEXTAREA" || a.tagName === "INPUT" || a.isContentEditable);
}

function neighbourCue(dir) {
  const t = currentTime();
  if (dir > 0) return S.cues.find(c => c.start > t + 0.001) || null;
  const before = S.cues.filter(c => c.start < t - 0.001);
  return before.length ? before[before.length - 1] : null;
}
function gotoCue(dir) {
  const c = neighbourCue(dir);
  if (!c) return;
  select([c.id], false);
  seek(c.start + 0.02);
  ensureVisible(c);
}
function ensureVisible(c) {
  const span = S.view.t1 - S.view.t0;
  if (c.start < S.view.t0 + span * 0.05 || c.end > S.view.t1 - span * 0.05) {
    setView(c.start - span * 0.35, c.start - span * 0.35 + span);
  }
}

function onKey(e) {
  // 右クリックメニューが出ている間は Escape で閉じるだけ
  if (!el.ctxMenu.hidden) {
    if (e.key === "Escape") { e.preventDefault(); closeCtxMenu(); }
    if (e.key === "Escape" || e.key === "Tab" || e.key.startsWith("Arrow")) return;
    closeCtxMenu();
  }
  // 確認が出ている間は、答える以外のキーを通さない
  if (!el.confirmOverlay.hidden) {
    if (e.key === "Escape") { e.preventDefault(); closeConfirm(false); }
    else if (e.key === "Enter") { e.preventDefault(); closeConfirm(document.activeElement === el.btnConfirmOk); }
    return;
  }
  // 警告が出ている間は、閉じる以外のキーを通さない
  if (!el.warnOverlay.hidden) {
    if (e.key === "Escape" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      el.warnOverlay.hidden = true;
    }
    return;
  }
  if (e.key === "?" && !isTyping()) { e.preventDefault(); el.helpOverlay.hidden = false; return; }
  if (e.key === "Escape") { el.helpOverlay.hidden = true; }

  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); save(); return; }
  if (mod && e.key.toLowerCase() === "z") {
    e.preventDefault(); e.shiftKey ? doRedo() : doUndo(); return;
  }
  if (isTyping()) return;

  if (e.altKey && /^[1-9]$/.test(e.key)) {
    const spk = S.speakers[+e.key - 1];
    if (spk) { e.preventDefault(); setSpeaker(selectedCues(), spk); }
    return;
  }

  switch (e.key) {
    case " ": e.preventDefault(); togglePlay(); break;
    case "ArrowLeft": e.preventDefault(); e.shiftKey ? seek(currentTime() - 1) : stepFrames(-1); break;
    case "ArrowRight": e.preventDefault(); e.shiftKey ? seek(currentTime() + 1) : stepFrames(1); break;
    case "ArrowUp": e.preventDefault(); gotoCue(-1); break;
    case "ArrowDown": e.preventDefault(); gotoCue(1); break;
    case "Enter": {
      e.preventDefault();
      const c = soleSelected() || cuesAt(currentTime())[0];
      if (c) beginEdit(c.id, true);
      break;
    }
    case "a": case "A": e.preventDefault(); addAtSelection(); break;
    case "i": case "I": e.preventDefault(); setEdgeToPlayhead("start"); break;
    case "o": case "O": e.preventDefault(); setEdgeToPlayhead("end"); break;
    case "s": case "S": e.preventDefault(); splitAtPlayhead(); break;
    case "m": case "M": e.preventDefault(); mergeWithNext(); break;
    case "Delete": case "Backspace": e.preventDefault(); deleteSelected(); break;
    case "+": case "=": e.preventDefault(); zoomAt(1 / 1.5, currentTime()); break;
    case "-": case "_": e.preventDefault(); zoomAt(1.5, currentTime()); break;
    case "0": e.preventDefault(); setView(0, S.duration); break;
  }
}

/* ============================================================
   ペインのリサイズ
   ============================================================ */
function initSplitters() {
  el.vsplit.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    el.vsplit.classList.add("on");
    const move = (e) => {
      const w = Math.max(280, Math.min(900, window.innerWidth - e.clientX));
      document.documentElement.style.setProperty("--right-w", w + "px");
      layout();
      applyPreviewSize();
    };
    const up = () => {
      el.vsplit.classList.remove("on");
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      capturePrefs();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });

  el.hsplit.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    el.hsplit.classList.add("on");
    const top = el.colLeft.getBoundingClientRect().top;
    const move = (e) => {
      const h = Math.max(150, Math.min(el.colLeft.clientHeight - 130, e.clientY - top));
      document.documentElement.style.setProperty("--preview-h", h + "px");
      layout();
      applyPreviewSize();   // 動画の高さを境界に追従させる
    };
    const up = () => {
      el.hsplit.classList.remove("on");
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      capturePrefs();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
}

/* ============================================================
   文字起こし（Gemini）
   ============================================================ */
let cfgDoc = null;         // 直近に読んだ /api/config
let trJobId = null;        // 進行中の job
let trTimer = null;        // その進捗を取りに行くタイマー

/** 少額を扱うので、$1 未満は小数 4 桁まで出す。0 は「$0.00」で読みやすく。 */
function usd(v) {
  v = Number(v) || 0;
  if (v === 0) return "$0.00";
  return "$" + v.toFixed(v < 1 ? 4 : 2);
}

function fmtWhen(sec) {
  const d = new Date((Number(sec) || 0) * 1000);
  if (!sec) return "";
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours(), 2)}:${pad(d.getMinutes(), 2)}`;
}

/* ---------- 設定シート ---------- */
async function openConfigSheet() {
  el.cfgMsg.textContent = "";
  el.cfgMsg.className = "hint";
  el.cfgKeyState.textContent = "読み込み中…";
  el.cfgKeyState.className = "hint";
  el.cfgOverlay.hidden = false;
  const body = el.cfgOverlay.querySelector(".sheet-body");
  if (body) body.scrollTop = 0;

  try {
    const [cfg, u] = await Promise.all([api("/api/config"), api("/api/usage")]);
    cfgDoc = cfg;
    renderConfig(cfg);
    renderUsage(u);
  } catch (e) {
    el.cfgKeyState.textContent = "設定を読めませんでした: " + e.message;
    el.cfgKeyState.className = "hint err";
  }
}

function renderConfig(cfg) {
  const g = cfg.gemini;
  el.cfgEnabled.checked = !!g.enabled;
  el.cfgPath.textContent = cfg.configPath;

  el.cfgModel.innerHTML = "";
  const known = (cfg.models || []).map(m => m.id);
  for (const m of cfg.models || []) el.cfgModel.appendChild(new Option(m.label, m.id));
  // 設定に手で書いたモデルでも選択が消えないようにしておく
  if (!known.includes(g.model)) el.cfgModel.appendChild(new Option(g.model, g.model));
  el.cfgModel.value = g.model;

  const [lo, hi] = cfg.chunkRange || [30, 300];
  el.cfgChunk.min = String(lo);
  el.cfgChunk.max = String(hi);
  el.cfgChunk.value = String(g.chunkSec);
  el.cfgLimit.value = String(g.monthlyLimitUsd || 0);

  el.cfgKey.value = "";
  el.cfgKey.placeholder = g.hasKey ? g.keyMasked : "AIza…";
  el.cfgKeyState.className = "hint";
  if (g.keySource === "env") {
    el.cfgKeyState.textContent =
      `環境変数 GEMINI_API_KEY のキー（${g.keyMasked}）を使います。ここで入れると、そちらが優先されます。`;
  } else if (g.hasKey) {
    el.cfgKeyState.textContent = `保存済み（${g.keyMasked}）。変えるときだけ入力してください。`;
  } else {
    el.cfgKeyState.textContent = "未設定です。";
  }
  el.btnCfgClear.disabled = g.keySource !== "config";
}

function renderUsage(u) {
  el.cfgMonth.textContent = usd(u.monthCost);
  el.cfgMonthN.textContent = `${u.month}・${u.monthCount} 回`;
  el.cfgTotal.textContent = usd(u.totalCost);
  el.cfgTotalN.textContent = `${u.totalCount} 回・記録: ${u.path}`;

  el.cfgUsage.innerHTML = "";
  if (!(u.recent || []).length) {
    el.cfgUsage.innerHTML = '<div class="usage-empty">まだ 1 度も使っていません。</div>';
  } else {
    for (const r of u.recent) {
      const row = document.createElement("div");
      row.className = "usage-row" + (r.status && r.status !== "done" ? " bad" : "");
      const mins = Math.round((r.durationSec || 0) / 60);
      const tail = r.status && r.status !== "done" ? `（${r.status === "cancelled" ? "中止" : r.status}）` : "";
      row.innerHTML =
        `<span class="u-at">${esc(fmtWhen(r.at))}</span>` +
        `<span class="u-set">${esc(r.set || "")} <span>${mins} 分 · ${esc(r.model || "")}${tail}</span></span>` +
        `<span class="u-cost">${usd(r.cost)}</span>`;
      el.cfgUsage.appendChild(row);
    }
  }

  el.cfgConsoles.innerHTML = "";
  for (const c of u.consoles || []) {
    const li = document.createElement("li");
    li.innerHTML =
      `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.name)}</a>` +
      `<span class="cl-note">${esc(c.note)}</span>`;
    el.cfgConsoles.appendChild(li);
  }
}

function cfgPayload() {
  const key = el.cfgKey.value.trim();
  const g = {
    enabled: el.cfgEnabled.checked,
    model: el.cfgModel.value,
    chunkSec: parseInt(el.cfgChunk.value, 10) || 240,
    monthlyLimitUsd: parseFloat(el.cfgLimit.value) || 0,
  };
  if (key) g.apiKey = key;      // 空なら「変えない」。伏せ字を送り返してキーを壊さない
  return { gemini: g };
}

async function saveConfig() {
  try {
    cfgDoc = await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfgPayload()),
    });
    renderConfig(cfgDoc);
    el.cfgOverlay.hidden = true;
    status(cfgDoc.gemini.enabled ? "設定を保存しました（Gemini は「使う」）"
                                 : "設定を保存しました（Gemini は「使わない」）", "ok");
  } catch (e) {
    el.cfgMsg.textContent = "保存できません: " + e.message;
    el.cfgMsg.className = "hint err";
  }
}

async function clearKey() {
  try {
    cfgDoc = await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gemini: { clearKey: true, enabled: false } }),
    });
    renderConfig(cfgDoc);
    el.cfgKeyState.textContent = "キーを消しました。";
    el.cfgKeyState.className = "hint ok";
  } catch (e) {
    el.cfgKeyState.textContent = "消せませんでした: " + e.message;
    el.cfgKeyState.className = "hint err";
  }
}

async function testKey() {
  el.btnCfgTest.disabled = true;
  el.cfgKeyState.textContent = "確認しています…";
  el.cfgKeyState.className = "hint";
  try {
    const d = await api("/api/config/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ apiKey: el.cfgKey.value.trim() }),
    });
    const want = el.cfgModel.value;
    const has = (d.models || []).includes(want);
    el.cfgKeyState.textContent = has
      ? `つながりました。${want} は使えます（${d.models.length} モデル）。`
      : `つながりましたが、${want} は一覧にありません。別のモデルを選んでください。`;
    el.cfgKeyState.className = has ? "hint ok" : "hint err";
  } catch (e) {
    el.cfgKeyState.textContent = "使えません: " + e.message;
    el.cfgKeyState.className = "hint err";
  } finally {
    el.btnCfgTest.disabled = false;
  }
}

/* ---------- 実行シート ---------- */
function trShowConfirm() {
  el.trConfirm.hidden = false;
  el.trRun.hidden = true;
  el.btnTrGo.hidden = false;
  el.btnTrStop.hidden = true;
  el.btnTrCancel.textContent = "やめる";
  el.trTitle.textContent = "Gemini で文字起こし";
}

function trShowRun() {
  el.trConfirm.hidden = true;
  el.trRun.hidden = false;
  el.btnTrGo.hidden = true;
  el.btnTrStop.hidden = false;
  el.btnTrCancel.textContent = "閉じる";
  el.trTitle.textContent = "文字起こし中";
}

async function openTranscribeSheet() {
  if (!S.setName) { status("セットを選んでください", "err"); return; }
  el.trFoot.textContent = "";
  el.trOverlay.hidden = false;

  if (trJobId) { trShowRun(); return; }

  trShowConfirm();
  el.btnTrGo.disabled = true;
  el.trSet.textContent = S.setName;
  el.trCost.textContent = "…";
  try {
    const d = await api(`/api/sets/${encodeURIComponent(S.setName)}/transcribe`);
    if (d.job && d.job.status === "running") {
      trJobId = d.job.id;
      trShowRun();
      renderJob(d.job);
      startPolling();
      return;
    }
    renderEstimate(d.estimate, d.busy);
  } catch (e) {
    el.trReady.textContent = e.message;
    el.trReady.className = "warn-hint err";
    el.trCost.textContent = "—";
  }
}

function renderEstimate(est, busy) {
  el.trDur.textContent = fmtTC(est.duration, true);
  el.trChunks.textContent = `${est.chunks} 回（1 回あたり ${est.chunkSec} 秒）`;
  el.trModel.textContent = est.model;
  el.trCost.textContent = `${usd(est.cost)}  （1 時間あたり ${usd(est.costPerHour)}）`;
  el.trMonth.textContent = usd(est.monthCost) +
    (est.monthlyLimitUsd > 0 ? `　/　上限 ${usd(est.monthlyLimitUsd)}` : "　（上限なし）");

  let msg = "", bad = false;
  if (!est.hasKey) { msg = "API キーが未設定です。［設定］で入れてください。"; bad = true; }
  else if (!est.enabled) { msg = "Gemini 文字起こしが「使わない」になっています。［設定］で切り替えてください。"; bad = true; }
  else if (busy) { msg = "別のセットの処理が動いています。終わるまで待ってください。"; bad = true; }
  else {
    msg = "上の金額は見積もりです。実際の請求はトークン数で決まるので前後します。";
  }
  el.trReady.textContent = msg;
  el.trReady.className = bad ? "warn-hint err" : "warn-hint";
  el.btnTrGo.disabled = bad;
}

async function startTranscribe() {
  el.btnTrGo.disabled = true;
  try {
    // いまの編集を先に書いておく。取り込み時に backup/ へ回るので、戻せる形になる
    if (S.dirty) await save("quiet");

    const n = parseInt(el.trSpeakers.value, 10);
    const job = await api(`/api/sets/${encodeURIComponent(S.setName)}/transcribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speakers: n > 0 ? n : 0 }),
    });
    trJobId = job.id;
    trShowRun();
    renderJob(job);
    startPolling();
    el.btnTranscribe.disabled = true;
  } catch (e) {
    el.trReady.textContent = e.message;
    el.trReady.className = "warn-hint err";
    el.btnTrGo.disabled = false;
  }
}

function startPolling() {
  clearInterval(trTimer);
  el.btnTranscribe.disabled = true;
  trTimer = setInterval(async () => {
    if (!trJobId) return;
    try {
      renderJob(await api(`/api/jobs/${encodeURIComponent(trJobId)}`));
    } catch (e) {
      // サーバーが落ちたなど。次の周期でまた試す
      el.trMsg.textContent = "進捗を取得できません: " + e.message;
    }
  }, 1500);
}

function renderJob(j) {
  const total = j.chunks || 1;
  const pct = j.status === "running"
    ? Math.min(100, Math.round((Math.max(0, j.chunk - 1) / total) * 100))
    : 100;
  el.trBarFill.style.width = pct + "%";
  el.trElapsed.textContent = `${Math.round(j.elapsed)} 秒`;
  el.trSpent.textContent = usd(j.cost);
  el.trMsg.textContent = j.error || j.message || "";
  el.trMsg.className = "tr-msg" +
    (j.status === "error" ? " err" : j.status === "done" ? " ok" : "");

  if (j.status === "running") return;

  // 終わった
  clearInterval(trTimer);
  trTimer = null;
  trJobId = null;
  el.btnTrStop.hidden = true;
  el.btnTrCancel.textContent = "閉じる";
  el.btnTranscribe.disabled = false;
  el.trTitle.textContent =
    j.status === "done" ? "文字起こしが終わりました"
    : j.status === "cancelled" ? "中止しました" : "失敗しました";

  if (j.imported) {
    el.trFoot.textContent = `${j.imported} 件を取り込みました`;
    // 走っている間に別のセットへ移っていたら、そちらを壊さない
    if (j.set === S.setName) {
      markClean();
      openSet(S.setName).catch(e => status("読み込みに失敗: " + e.message, "err"));
    } else {
      el.trFoot.textContent += `（${j.set}。開き直すと出ます）`;
    }
  }
  status(j.error || j.message || "", j.status === "done" ? "ok" : "err");
}

async function stopJob() {
  if (!trJobId) return;
  el.btnTrStop.disabled = true;
  try {
    renderJob(await api(`/api/jobs/${encodeURIComponent(trJobId)}/cancel`, { method: "POST" }));
  } catch (e) {
    el.trMsg.textContent = "中止できません: " + e.message;
  } finally {
    el.btnTrStop.disabled = false;
  }
}

/** セットを開いた直後に、そのセットで走っている処理があれば拾う。
 *  ブラウザを閉じて開き直しても、進捗が見えるようにするため。 */
async function resumeJob(name) {
  try {
    const d = await api(`/api/sets/${encodeURIComponent(name)}/transcribe`);
    if (d.job && d.job.status === "running" && d.job.set === name) {
      trJobId = d.job.id;
      trShowRun();
      renderJob(d.job);
      startPolling();
      el.trOverlay.hidden = false;
    }
  } catch (_) { /* 見えなくても編集はできる */ }
}

/* ============================================================
   作業時間の計測

   「20 分の動画・字幕 437 件・話者 4 人で、どのくらいかかるのか」を
   後から引けるようにする。次の仕事を見積もるときの材料。

   計上するのは、実際に手が動いていた時間だけ。次は数えない。
     ・最後の操作から IDLE_SEC を過ぎたあと（考えている時間は入るが、離席は入らない）
     ・タブが隠れている / ウィンドウがフォーカスを失っているあいだ
     ・動画を再生しているだけのあいだ（再生・停止の操作そのものは数える）
     ・文字起こしの待ちのあいだ
   ============================================================ */
const IDLE_SEC = 120;          // これだけ操作が無ければ、手が止まったとみなす
const WORK_TICK_MS = 1000;
const WORK_FLUSH_MS = 30000;   // サーバーへ送る間隔
const WORK_MAX_STEP = 5;       // 1 tick で足す上限（秒）。スリープ復帰でまとめて乗るのを防ぐ

// タブごとの識別子。同じセットを 2 つのタブで開いたとき、
// サーバーが片方だけを通して二重計上を防ぐために使う。
const WORK_SESSION = Math.random().toString(36).slice(2) + Date.now().toString(36);

const W = {
  lastActivity: 0,   // 最後に操作があった時刻（ms）
  lastTick: 0,
  pending: 0,        // まだ送っていない秒数
  total: 0,          // このセットの累計（サーバーの値 + pending）
  set: null,         // 計測中のセット名
  counted: true,     // false = 別のタブが計測中
  active: false,     // いま数えているか（表示の色分け用）
};

function markActivity() { W.lastActivity = Date.now(); }

/** いま作業時間を数えてよいか */
function workCounting() {
  if (!W.set || !W.counted) return false;
  if (document.hidden || !document.hasFocus()) return false;
  if (trJobId) return false;                       // 文字起こしの待ちは数えない
  if (!el.video.paused && !el.video.ended) return false;  // 再生中は数えない
  return Date.now() - W.lastActivity < IDLE_SEC * 1000;
}

function workTick() {
  const now = Date.now();
  const dt = (now - W.lastTick) / 1000;
  W.lastTick = now;

  const on = workCounting();
  if (on && dt > 0) {
    const step = Math.min(dt, WORK_MAX_STEP);
    W.pending += step;
    W.total += step;
  }
  W.active = on;
  // 常時は出さない。開いているあいだだけ、いまの値を見せる
  if (!el.wlOverlay.hidden) paintWorkLive();
}

function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${pad(m, 2)}:${pad(s, 2)}` : `${m}:${pad(s, 2)}`;
}

/** いま開いているセットの作業時間。作業時間の画面を開いているあいだだけ出す。 */
function paintWorkLive() {
  if (!el.wlLive) return;
  if (!W.set) { el.wlLive.textContent = ""; el.wlLive.className = "wl-live"; return; }
  el.wlLive.textContent = `${S.setName}  ${fmtDur(W.total)}`;
  el.wlLive.className = "wl-live" + (W.active ? " on" : "");
  el.wlLive.title = !W.counted
    ? "別のタブでこのセットを計測しています（ここでは加算しません）"
    : W.active ? "いま計測中"
    : "手が止まっています（再生中・文字起こし中・離席は数えません）";
}

function workSnapshot() {
  return {
    durationSec: S.duration || 0,
    cues: S.cues.length,
    speakers: S.speakers.filter(s => s !== "不明").length,
  };
}

/** 溜まったぶんをサーバーへ送る。beacon=true はページ離脱時（応答を待てない） */
function flushWork(beacon) {
  if (!W.set) return;
  const add = W.pending;
  if (add < 1 && !beacon) return;
  W.pending = 0;

  const url = `/api/sets/${encodeURIComponent(W.set)}/worklog`;
  const body = JSON.stringify({
    sessionId: WORK_SESSION, addSec: add, snapshot: workSnapshot(),
  });

  if (beacon && navigator.sendBeacon) {
    // 離脱時は fetch では間に合わないことがある
    navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
    return;
  }
  api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body })
    .then(d => {
      W.counted = d.counted !== false;
      // サーバーが正。ずれていたら合わせる（別タブぶんもここで反映される）
      W.total = (d.totalActiveSec || 0) + W.pending;
      paintWorkLive();
    })
    .catch(() => { W.pending += add; });   // 送れなければ次の機会に回す
}

/** セットを切り替えるとき。前のセットのぶんを確定してから移る */
async function workSwitchSet(name) {
  if (W.set && W.set !== name) flushWork(false);
  W.set = name;
  W.pending = 0;
  W.total = 0;
  W.counted = true;
  markActivity();
  paintWorkLive();
  if (!name) return;
  try {
    const d = await api(`/api/sets/${encodeURIComponent(name)}/worklog`);
    W.total = d.totalActiveSec || 0;
    paintWorkLive();
  } catch (_) { /* 取れなくても編集はできる */ }
}

function initWorkTracking() {
  W.lastTick = Date.now();
  markActivity();
  for (const ev of ["pointerdown", "pointermove", "keydown", "wheel", "input"]) {
    window.addEventListener(ev, markActivity, { capture: true, passive: true });
  }
  // 戻ってきた時点を「操作あり」にする。裏に回っていた間は数えない
  window.addEventListener("focus", markActivity);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) flushWork(true); else markActivity();
  });
  window.addEventListener("pagehide", () => flushWork(true));

  setInterval(workTick, WORK_TICK_MS);
  setInterval(() => flushWork(false), WORK_FLUSH_MS);
}

/* ---------- 設定メニュー ----------
   トップバーに数字を出しっぱなしにしない。見たいときだけ開く。 */
function openSettingsMenu() {
  closeCtxMenu();
  const box = el.btnSettings.getBoundingClientRect();

  el.ctxMenu.innerHTML = "";
  const add = (label, sub, fn) => {
    const b = document.createElement("button");
    b.type = "button";
    b.innerHTML = `<span>${label}</span>` + (sub ? `<span class="cm-sub">${esc(sub)}</span>` : "");
    b.addEventListener("click", () => { closeCtxMenu(); fn(); });
    el.ctxMenu.appendChild(b);
  };
  add("文字起こしの設定", "API キー・費用", () => openConfigSheet());
  add("作業時間", "見積もりの目安", () => openWorkSheet());
  add("編集の設定", P.skipDeleteConfirm ? "削除の確認は出さない" : "削除の確認を出す",
      () => openEditPrefsSheet());

  el.ctxMenu.hidden = false;
  // 画面の右端からはみ出さないように、ボタンの右端に合わせる
  const w = el.ctxMenu.offsetWidth;
  el.ctxMenu.style.left = `${Math.max(6, Math.min(box.right - w, window.innerWidth - w - 6))}px`;
  el.ctxMenu.style.top = `${box.bottom + 5}px`;
}

/* ---------- 編集の設定 ----------
   字幕そのものではなく、編集のしかたに関わる好みを置く。
   この PC の設定なので localStorage に入れ、素材フォルダには書かない。 */
function openEditPrefsSheet() {
  el.edSkipConfirm.checked = !!P.skipDeleteConfirm;
  el.edOverlay.hidden = false;
  el.edSkipConfirm.focus();
}

/** 削除の確認を出すかどうか。切り替えたらすぐ効く（保存ボタンは置かない）。
 *
 * 本文が空のブロックはこの設定に関わらず、これまでどおり確認せずに消える。
 * 「出さない」にしても消したものは ⌘Z で戻せるので、そこは毎回伝える。 */
function applyDeleteConfirmPref(skip) {
  P.skipDeleteConfirm = !!skip;
  savePrefs();
  // 「−」の説明も合わせる。作り直すほどではないので、その場で書き換える
  for (const row of rowEls.values()) {
    const del = row.querySelector(".delbtn");
    if (del) del.title = delBtnTitle();
  }
  status(P.skipDeleteConfirm
    ? "削除の確認を出しません（⌘Z で戻せます）"
    : "削除の確認を出します", "ok");
}

/* ---------- 作業時間の一覧（見積もりの目安） ---------- */
async function openWorkSheet() {
  el.wlOverlay.hidden = false;
  paintWorkLive();
  el.wlBody.innerHTML = '<p class="sheet-lede">読み込み中…</p>';
  flushWork(false);                 // いまのぶんを反映してから出す
  try {
    renderWorkSheet(await api("/api/worklog"));
  } catch (e) {
    el.wlBody.innerHTML = `<p class="sheet-lede">読み込めません: ${esc(e.message)}</p>`;
  }
}

function renderWorkSheet(d) {
  const rows = d.rows || [], t = d.totals || {};
  const measured = rows.filter(r => r.measured);

  let html = "";
  if (!measured.length) {
    html += '<p class="sheet-lede">まだ計測できているセットがありません。'
          + '編集を始めると、ここに溜まっていきます。</p>';
  } else {
    html += '<div class="cost-cards">'
      + `<div class="cost-card"><span class="cost-lbl">合計の作業時間</span><b>${fmtDur(t.activeSec)}</b>`
      + `<span class="hint">${t.sets} セット</span></div>`
      + `<div class="cost-card"><span class="cost-lbl">動画 1 分あたり</span>`
      + `<b>${t.minPerVideoMin != null ? t.minPerVideoMin + " 分" : "—"}</b>`
      + `<span class="hint">字幕 1 件あたり ${t.secPerCue != null ? t.secPerCue + " 秒" : "—"}</span></div>`
      + '</div>';
  }

  html += '<table class="wl-table"><thead><tr>'
    + "<th>セット</th><th>動画長</th><th>字幕</th><th>話者</th>"
    + "<th>作業時間</th><th>動画1分あたり</th><th>字幕1件あたり</th>"
    + "</tr></thead><tbody>";
  for (const r of rows) {
    const dim = r.measured ? "" : ' class="dim"';
    html += `<tr${dim}><td>${esc(r.name)}</td>`
      + `<td>${r.durationSec ? fmtDur(r.durationSec) : "—"}</td>`
      + `<td>${r.cues != null ? r.cues : "—"}</td>`
      + `<td>${r.speakers != null ? r.speakers : "—"}</td>`
      + `<td><b>${r.measured ? fmtDur(r.activeSec) : "未計測"}</b></td>`
      + `<td>${r.minPerVideoMin != null ? r.minPerVideoMin + " 分" : "—"}</td>`
      + `<td>${r.secPerCue != null ? r.secPerCue + " 秒" : "—"}</td></tr>`;
  }
  html += "</tbody></table>";

  html += '<p class="sheet-lede">'
    + '数えているのは<b>実際に手が動いていた時間</b>だけです。'
    + `最後の操作から <b>${IDLE_SEC} 秒</b>が過ぎたあと、タブが隠れているあいだ、`
    + '<b>動画を再生しているあいだ</b>、文字起こしの待ちのあいだは数えません'
    + '（再生・停止の操作そのものは数えます）。<br>'
    + '記録は <code>&lt;セット&gt;/&lt;名前&gt;.worklog.json</code> です。素材フォルダごと移せば一緒に付いていきます。'
    + '<b>計測を入れる前の作業は入っていません。</b></p>';

  el.wlBody.innerHTML = html;
}

/* ============================================================
   起動
   ============================================================ */
function initRefs() {
  for (const id of [
    "setPicker","mediaMeta","statusMsg","btnUndo","btnRedo","btnSave","btnExport","btnHelp",
    "video","stage","stageEmpty","telopBar","scrub","btnPlay","btnStepBack","btnStepFwd",
    "btnPrevCue","btnNextCue","tcNow","tcTotal","selZoom","selRate",
    "tlBody","tlScroll","tlTracks","ruler","lanes","gutLanes","playhead","marquee","snapGuides",
    "waveCanvas","waveEmpty","btnWave","viewRange","btnZoomIn","btnZoomOut","btnZoomFit",
    "chkFollow","chkSnap","dragHint","rows","listCount","listFilter","btnMarks",
    "helpOverlay","btnHelpClose","colLeft","colRight","vsplit","hsplit","zoomInfo",
    "btnSpeakers","spkOverlay","spkTable","spkMsg","btnSpkAdd","btnSpkApply","btnSpkCancel","btnSpkClose",
    "vocabTerms","vocabNote",
    "warnOverlay","warnTitle","warnBody","warnHint","btnWarnOk","ctxMenu",
    "confirmOverlay","confirmTitle","confirmBody","confirmPreview","btnConfirmOk","btnConfirmCancel",
    "btnSettings","cfgOverlay","btnCfgClose","btnCfgCancel","btnCfgSave","btnCfgTest","btnCfgClear",
    "cfgEnabled","cfgKey","cfgKeyState","cfgPath","cfgModel","cfgChunk","cfgLimit","cfgMsg",
    "cfgMonth","cfgMonthN","cfgTotal","cfgTotalN","cfgUsage","cfgConsoles",
    "btnTranscribe","trOverlay","btnTrClose","btnTrCancel","btnTrGo","btnTrStop",
    "trTitle","trConfirm","trRun","trSet","trDur","trChunks","trModel","trCost","trMonth",
    "trReady","trSpeakers","trBarFill","trMsg","trElapsed","trSpent","trFoot",
    "wlOverlay","wlBody","btnWlClose","wlLive",
    "edOverlay","btnEdClose","edSkipConfirm",
  ]) el[id] = $(id);
}

function initEvents() {
  el.setPicker.addEventListener("change", () => openSet(el.setPicker.value).catch(e => status(e.message, "err")));

  el.btnPlay.addEventListener("click", togglePlay);
  el.btnStepBack.addEventListener("click", () => stepFrames(-1));
  el.btnStepFwd.addEventListener("click", () => stepFrames(1));
  el.btnPrevCue.addEventListener("click", () => gotoCue(-1));
  el.btnNextCue.addEventListener("click", () => gotoCue(1));
  el.scrub.addEventListener("input", () => seek(parseFloat(el.scrub.value)));

  el.video.addEventListener("play", () => { el.btnPlay.textContent = "❚❚"; requestAnimationFrame(tick); });
  el.video.addEventListener("pause", () => { el.btnPlay.textContent = "▶"; onTimeChanged(); });
  el.video.addEventListener("seeked", onTimeChanged);
  el.video.addEventListener("timeupdate", onTimeChanged);
  el.video.addEventListener("loadedmetadata", () => {
    if (el.video.duration && isFinite(el.video.duration)) {
      S.duration = el.video.duration;
      el.scrub.max = String(S.duration);
      el.tcTotal.textContent = "/ " + fmtTC(S.duration, true);
      clampView(); layout();   // 尺が少しずれても表示範囲を作り直さず、はみ出しだけ直す
    }
    applyPreviewSize();
    if (S.pendingSeek) {
      const t = Math.min(S.pendingSeek, Math.max(0, S.duration - 0.05));
      S.pendingSeek = 0;
      seek(t);
    }
  });
  el.video.addEventListener("loadeddata", applyPreviewSize);
  el.video.addEventListener("error", () => {
    status("動画を読み込めません（コーデックまたはパスを確認してください）", "err");
  });

  el.selRate.addEventListener("change", () => {
    el.video.playbackRate = parseFloat(el.selRate.value);
    capturePrefs();
  });
  el.selZoom.addEventListener("change", () => { applyPreviewSize(); capturePrefs(); });
  el.chkFollow.addEventListener("change", capturePrefs);
  el.chkSnap.addEventListener("change", capturePrefs);

  // 再生位置と表示範囲は、落ち着いたところで覚える
  el.video.addEventListener("pause", capturePrefs);
  el.video.addEventListener("seeked", capturePrefs);
  window.addEventListener("beforeunload", () => { capturePrefs(); writePrefs(); });

  el.btnZoomIn.addEventListener("click", () => zoomAt(1 / 1.6, currentTime()));
  el.btnZoomOut.addEventListener("click", () => zoomAt(1.6, currentTime()));
  el.btnZoomFit.addEventListener("click", () => setView(0, S.duration));

  // ルーラー: クリック / ドラッグでシーク
  const rulerSeek = (e) => {
    const box = el.tlScroll.getBoundingClientRect();
    seek(pxToT(e.clientX - box.left));
  };
  el.ruler.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.altKey) return;   // 右ドラッグ / Alt+左は表示位置の移動に譲る
    // 再生ヘッドを動かしただけで再生を止めない。止めるのは停止を押したときだけ。
    // ただし掴んで動かしている間は別で、再生とシークが競合して位置がずれ音も飛ぶので
    // そこだけ止めて、離したときに元が再生中なら戻す。
    const wasPlaying = !el.video.paused;
    rulerSeek(e);
    el.ruler.setPointerCapture(e.pointerId);
    const move = (ev) => {
      // 1 回のクリックでは止めない（pause→play を挟まないぶん音が途切れない）。
      // 動かし始めて初めて止める。
      if (wasPlaying && !el.video.paused) el.video.pause();
      rulerSeek(ev);
    };
    const up = () => {
      el.ruler.removeEventListener("pointermove", move);
      el.ruler.removeEventListener("pointerup", up);
      el.ruler.removeEventListener("pointercancel", up);
      // 押した時点で止まっていたなら、離しても再生しない
      if (wasPlaying && el.video.paused) el.video.play().catch(() => {});
    };
    el.ruler.addEventListener("pointermove", move);
    el.ruler.addEventListener("pointerup", up);
    el.ruler.addEventListener("pointercancel", up);
  });

  // ホイール:
  //   素のホイール / ⌘ / Ctrl（トラックパッドのピンチ）= カーソル位置を軸に拡大縮小
  //   ⇧ + ホイール、ホイールの左右             = 表示位置の移動
  //   Alt + ホイール                           = レーンの縦スクロール（既定動作に任せる）
  const panBy = (dt) => setView(S.view.t0 + dt, S.view.t1 + dt);

  el.tlScroll.addEventListener("wheel", (e) => {
    // deltaMode を px に揃える（行単位・ページ単位で来る環境がある）
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
    const dx = e.deltaX * unit;
    const dy = e.deltaY * unit;

    if (Math.abs(dx) > Math.abs(dy)) { e.preventDefault(); panBy(dx / scale()); return; }
    if (e.shiftKey) { e.preventDefault(); panBy(dy / scale()); return; }
    if (e.altKey) return;   // 縦スクロールに譲る

    e.preventDefault();
    // 量に比例させるとトラックパッドでも滑らかになる。上へ回す＝拡大。
    const factor = Math.exp(Math.max(-2.5, Math.min(2.5, dy * 0.0022)));
    const box = el.tlScroll.getBoundingClientRect();
    zoomAt(factor, pxToT(e.clientX - box.left));
    el.dragHint.textContent = `表示範囲 ${fmtShort(S.view.t0)} — ${fmtShort(S.view.t1)}`;
    clearTimeout(wheelHintTimer);
    wheelHintTimer = setTimeout(() => { el.dragHint.textContent = HINT_DEFAULT; }, 1400);
  }, { passive: false });

  el.btnWave.addEventListener("click", async () => {
    el.btnWave.disabled = true;
    try {
      const res = await fetch(`/api/sets/${encodeURIComponent(S.setName)}/waveform`, { method: "POST" });
      const d = await res.json();
      if (res.ok && d.peaks) { S.wave = d; el.waveEmpty.hidden = true; drawWave(); status("波形を抽出しました", "ok"); }
      else { el.waveEmpty.textContent = d.message || "波形は未抽出"; status(d.message || "波形は未対応", "err"); }
    } catch (e) {
      status("波形の抽出に失敗: " + e.message, "err");
    } finally {
      el.btnWave.disabled = false;
    }
  });

  // リスト
  el.rows.addEventListener("click", (e) => {
    const row = e.target.closest(".row");
    if (!row) return;
    const id = +row.dataset.id;
    const c = S.cues.find(x => x.id === id);
    if (!c) return;

    // 番号 = 目印の切り替え。行の選択も再生位置の移動もしない
    if (e.target.closest(".idx")) {
      e.stopPropagation();
      // 選択に入っている行を押したら、選択中をまとめて切り替える
      const targets = S.sel.has(id) && S.sel.size > 1 ? selectedCues() : [c];
      toggleMark(targets);
      return;
    }
    // 「+」= この行の終わりから、同じ話者で直下に足す
    if (e.target.closest(".addbtn")) {
      e.stopPropagation();
      addCue(c.end, c.speaker);
      return;
    }
    // 「−」= この行を削除（確認あり）
    if (e.target.closest(".delbtn")) {
      e.stopPropagation();
      deleteCueWithConfirm(c);
      return;
    }

    const cell = e.target.closest(".spk-cell");
    if (cell) { setSpeaker([c], cell.dataset.spk); select([id], false); return; }
    if (e.target.tagName === "INPUT") return;

    if (!handlePick(id, e)) return;
    seek(c.start + 0.02);
    ensureVisible(c);
    if (e.target.classList.contains("txt")) beginEdit(id);
  });

  el.rows.addEventListener("change", (e) => {
    if (e.target.tagName !== "INPUT" || !e.target.dataset.edge) return;
    const row = e.target.closest(".row");
    const c = S.cues.find(x => x.id === +row.dataset.id);
    if (!c) return;
    const t = parseTC(e.target.value);
    if (t === null) { refreshRows(); status("時刻の書式が読めません（例 01:23.960）", "err"); return; }
    pushUndo();
    setEdge(c, e.target.dataset.edge, t);
    commitStructuralChange();
  });

  // 変換中の途中文字で絞り込まない
  guardIME(el.listFilter);
  const applyFilter = () => { S.filter = el.listFilter.value; refreshRows(); };
  el.listFilter.addEventListener("input", (e) => {
    if (e.isComposing || el.listFilter.dataset.composing) return;
    applyFilter();
  });
  el.btnMarks.addEventListener("click", () => {
    S.markOnly = !S.markOnly;
    refreshRows();
    revealSelectedRow();   // 絞り込みの切り替えで選択した行を見失わない
  });
  el.listFilter.addEventListener("compositionend", applyFilter);
  el.listFilter.addEventListener("search", applyFilter);   // ✕ で消したとき

  el.btnSpeakers.addEventListener("click", openSpeakerSheet);
  el.btnSpkAdd.addEventListener("click", addSpeakerRow);
  el.btnSpkApply.addEventListener("click", applySpeakerSheet);
  el.btnSpkCancel.addEventListener("click", () => { el.spkOverlay.hidden = true; });
  el.btnSpkClose.addEventListener("click", () => { el.spkOverlay.hidden = true; });
  el.spkOverlay.addEventListener("click", (e) => { if (e.target === el.spkOverlay) el.spkOverlay.hidden = true; });
  el.spkOverlay.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (isIME(e)) return;   // 変換の確定 Enter / 取り消し Escape を横取りしない
    if (e.key === "Escape") { e.preventDefault(); el.spkOverlay.hidden = true; }
    else if (e.key === "Enter" && e.target.tagName === "INPUT") { e.preventDefault(); applySpeakerSheet(); }
  });

  el.btnUndo.addEventListener("click", doUndo);
  el.btnRedo.addEventListener("click", doRedo);
  el.btnSave.addEventListener("click", () => save());
  el.btnExport.addEventListener("click", exportSrt);
  // メニューは、外を触る・別の場所へ移ると閉じる。
  // リストのスクロールでは閉じない。再生位置が動くたびにリストは自動スクロールするので、
  // それで閉じると再生中はメニューが一瞬で消えてしまう。
  // タイムライン側の表示範囲が変わったときは setView が閉じる（ブロックが動くため）。
  window.addEventListener("pointerdown", (e) => {
    if (!el.ctxMenu.hidden && !el.ctxMenu.contains(e.target)) closeCtxMenu();
  }, true);
  window.addEventListener("blur", closeCtxMenu);

  el.btnConfirmOk.addEventListener("click", () => closeConfirm(true));
  el.btnConfirmCancel.addEventListener("click", () => closeConfirm(false));
  el.confirmOverlay.addEventListener("click", (e) => { if (e.target === el.confirmOverlay) closeConfirm(false); });

  el.btnWarnOk.addEventListener("click", () => { el.warnOverlay.hidden = true; });
  el.warnOverlay.addEventListener("click", (e) => { if (e.target === el.warnOverlay) el.warnOverlay.hidden = true; });

  // 文字起こし（Gemini）
  el.btnSettings.addEventListener("click", openSettingsMenu);
  el.btnCfgSave.addEventListener("click", saveConfig);
  el.btnCfgTest.addEventListener("click", testKey);
  el.btnCfgClear.addEventListener("click", clearKey);
  el.btnCfgCancel.addEventListener("click", () => { el.cfgOverlay.hidden = true; });
  el.btnCfgClose.addEventListener("click", () => { el.cfgOverlay.hidden = true; });
  el.cfgOverlay.addEventListener("click", (e) => { if (e.target === el.cfgOverlay) el.cfgOverlay.hidden = true; });
  el.cfgOverlay.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (isIME(e)) return;
    if (e.key === "Escape") { e.preventDefault(); el.cfgOverlay.hidden = true; }
  });

  el.btnTranscribe.addEventListener("click", () => { openTranscribeSheet(); });
  el.btnTrGo.addEventListener("click", startTranscribe);
  el.btnTrStop.addEventListener("click", stopJob);
  el.btnTrCancel.addEventListener("click", () => { el.trOverlay.hidden = true; });
  el.btnTrClose.addEventListener("click", () => { el.trOverlay.hidden = true; });
  el.trOverlay.addEventListener("click", (e) => { if (e.target === el.trOverlay) el.trOverlay.hidden = true; });
  el.trOverlay.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (isIME(e)) return;
    // 走っている間は Escape で閉じるだけ。中止は「中止する」を押したときだけ
    if (e.key === "Escape") { e.preventDefault(); el.trOverlay.hidden = true; }
  });

  // 作業時間（設定メニューから開く。トップバーには常時出さない）
  el.btnWlClose.addEventListener("click", () => { el.wlOverlay.hidden = true; });
  el.wlOverlay.addEventListener("click", (e) => { if (e.target === el.wlOverlay) el.wlOverlay.hidden = true; });
  el.wlOverlay.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Escape") { e.preventDefault(); el.wlOverlay.hidden = true; }
  });

  // 編集の設定（設定メニューから開く。切り替えたらすぐ効くので保存ボタンは無い）
  el.edSkipConfirm.addEventListener("change", () => applyDeleteConfirmPref(el.edSkipConfirm.checked));
  el.btnEdClose.addEventListener("click", () => { el.edOverlay.hidden = true; });
  el.edOverlay.addEventListener("click", (e) => { if (e.target === el.edOverlay) el.edOverlay.hidden = true; });
  el.edOverlay.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Escape") { e.preventDefault(); el.edOverlay.hidden = true; }
  });

  el.btnHelp.addEventListener("click", () => { el.helpOverlay.hidden = false; });
  el.btnHelpClose.addEventListener("click", () => { el.helpOverlay.hidden = true; });
  el.helpOverlay.addEventListener("click", (e) => { if (e.target === el.helpOverlay) el.helpOverlay.hidden = true; });

  window.addEventListener("keydown", onKey);
  window.addEventListener("beforeunload", (e) => {
    if (S.dirty) { e.preventDefault(); e.returnValue = ""; }
  });

  let rt = null;
  window.addEventListener("resize", () => {
    clearTimeout(rt);
    rt = setTimeout(() => { layout(); applyPreviewSize(); }, 100);
  });
}

initRefs();
loadPrefs();
applyPrefs();      // セットを読み込む前に画面まわりを戻しておく
initEvents();
initSplitters();
el.__initDrag();
initPan();
initStagePan();
el.dragHint.textContent = HINT_DEFAULT;
updateUndoButtons();
initWorkTracking();
loadSetList().catch(e => status("読み込みに失敗: " + e.message, "err"));
