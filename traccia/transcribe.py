#!/usr/bin/env python3
"""動画/音声ファイルから文字起こしを行い、.srt と .txt を出力するツール。

ローカルの faster-whisper を使用（オフライン・無料）。
--diarize を付けると pyannote.audio で話者分離を行い、話者ラベルを付与する。

使い方:
    python -m traccia transcribe sample.mp4
    python -m traccia transcribe sample.mp4 --model medium --language ja
    python -m traccia transcribe sample.mp4 --diarize                # 話者分離あり
    python -m traccia transcribe sample.mp4 --diarize --speakers 2   # 話者数を指定
    python -m traccia transcribe sample.mp4 --diarize --hf-token hf_xxx

    # これまでどおりの書き方も動く（../transcribe.py が転送する）
    python transcribe.py sample.mp4 --diarize --speakers 4 --split-speakers

このファイルは話者分離のワーカーとして自分自身を別プロセスで起動する
（run_diarization を参照）。そのためパッケージ内の相対 import は使わず、
単体のスクリプトとしても動く状態を保つこと。

話者分離には HuggingFace のアクセストークンが必要。
環境変数 HF_TOKEN でも指定可能。

初回実行時はモデル（数百MB〜1GB程度）を自動ダウンロードします。
"""
import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# torch と ctranslate2 が別々の OpenMP ランタイムをリンクするため、
# 重複ロードのクラッシュ (OMP Error #15) を回避する。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


@dataclass
class Line:
    """1区間の文字起こし結果。話者は未分離なら None。"""
    start: float
    end: float
    text: str
    speaker: str | None = None


# 空白を前に置きたくない文字。「そうですね。」を「そうですね 」にしないためのもの。
CLOSING_RE = re.compile(r"\s+(?=[」』）〉》】〕］｝、！？…‥)\]},.!?])")


def drop_periods(s: str) -> str:
    """本文から「。」を取る。文の途中なら半角スペース、行末なら何も残さない。

    字幕は 1 区間が 1 発話なので、末尾の「。」は場所を取るだけで意味が無い。
    途中の「。」は文の切れ目が見えなくなると読みにくいので、空白に置き換える。
    閉じ括弧の直前だけは詰める（「そうですね 」と空白が浮くのを避ける）。

    traccia/gemini.py にも同じものがある。このファイルは話者分離のワーカーとして
    単体で起動するため（冒頭の注記）パッケージ内 import ができず、共通化できない。
    """
    t = re.sub(r"\s+", " ", str(s or "").replace("。", " "))
    return CLOSING_RE.sub("", t).strip()


def format_timestamp(seconds: float) -> str:
    """秒数を SRT 形式のタイムスタンプ (HH:MM:SS,mmm) に変換する。"""
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def pad_lines(lines: list[Line], duration: float | None) -> list[Line]:
    """先頭(00:00:00)と末尾(duration)に空テキスト字幕を足し、時間範囲を揃える。

    duration が None、または lines が空のときは何もしない。
    """
    if duration is None or not lines:
        return lines
    eps = 0.001
    padded = list(lines)
    if padded[0].start > eps:
        padded.insert(0, Line(start=0.0, end=padded[0].start, text=""))
    if padded[-1].end < duration - eps:
        padded.append(Line(start=padded[-1].end, end=duration, text=""))
    return padded


def write_srt(
    lines: list[Line],
    path: Path,
    show_speaker: bool = True,
    duration: float | None = None,
) -> None:
    """セグメント列を SRT 字幕ファイルとして書き出す。

    show_speaker=False のときは話者ラベルを本文に付けない（話者別ファイル用）。
    duration を渡すと先頭・末尾に空テキスト字幕を足して時間範囲を揃える。
    """
    entries = pad_lines(lines, duration)
    with path.open("w", encoding="utf-8") as f:
        for i, ln in enumerate(entries, start=1):
            prefix = f"{ln.speaker}: " if (show_speaker and ln.speaker) else ""
            f.write(f"{i}\n")
            f.write(f"{format_timestamp(ln.start)} --> {format_timestamp(ln.end)}\n")
            f.write(f"{prefix}{ln.text.strip()}\n\n")


def write_srt_per_speaker(
    lines: list[Line], outdir: Path, stem: str, duration: float | None = None
) -> list[Path]:
    """話者ごとに分割した SRT を書き出し、生成したパスのリストを返す。

    各ファイルは該当話者の発話のみを含み、字幕番号は 1 から振り直す。
    タイムスタンプは元動画の時刻のまま（動画にそのまま重ねられる）。
    duration を渡すと全ファイルの先頭・末尾を空テキストで揃える。
    """
    by_speaker: dict[str, list[Line]] = {}
    for ln in lines:
        by_speaker.setdefault(ln.speaker or "話者不明", []).append(ln)

    # Windows で使えない文字 (\ / : * ? " < > |) をファイル名から除去する
    def safe_name(name: str) -> str:
        for ch in '\\/:*?"<>|':
            name = name.replace(ch, "_")
        return name

    written = []
    for speaker, spk_lines in sorted(by_speaker.items()):
        path = outdir / f"{stem}.{safe_name(speaker)}.srt"
        write_srt(spk_lines, path, show_speaker=False, duration=duration)
        written.append(path)
    return written


def write_txt(lines: list[Line], path: Path) -> None:
    """セグメント列をプレーンテキストとして書き出す。

    話者がある場合は「話者A: 本文」の形式。連続する同一話者はまとめる。
    """
    with path.open("w", encoding="utf-8") as f:
        prev_speaker = object()  # 初回は必ず不一致
        for ln in lines:
            text = ln.text.strip()
            if ln.speaker:
                if ln.speaker != prev_speaker:
                    f.write(f"\n{ln.speaker}: {text}\n")
                else:
                    f.write(f"{text}\n")
                prev_speaker = ln.speaker
            else:
                f.write(text + "\n")


def decode_audio_16k(path: str):
    """PyAV で音声を 16kHz モノラル float32 の numpy 配列に変換する（mp4 等も可）。

    faster-whisper(ctranslate2) を import しないため、話者分離 worker 側で
    torch との OpenMP 競合を避けられる。
    """
    import av
    import numpy as np

    container = av.open(path)
    try:
        stream = next(s for s in container.streams if s.type == "audio")
        resampler = av.audio.resampler.AudioResampler(
            format="flt", layout="mono", rate=16000
        )
        chunks = []
        for frame in container.decode(stream):
            for rframe in resampler.resample(frame):
                chunks.append(rframe.to_ndarray().reshape(-1))
    finally:
        container.close()
    return np.concatenate(chunks).astype(np.float32)


def load_peaks_module():
    """peaks.py を読み込む。

    このファイルはワーカーとして単体スクリプトのまま起動されることがあるので、
    パッケージ経由と直接実行の両方で通るようにしておく。
    """
    try:
        from . import peaks          # traccia.transcribe として読まれたとき
        return peaks
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import peaks                 # transcribe.py が単体で走っているとき
        return peaks


def write_peaks_from_samples(audio, sample_rate: int, peaks_out: str) -> None:
    """すでにメモリにある音声からピークを書く。デコードし直さない。"""
    try:
        peaks = load_peaks_module()
        peaks.save(peaks_out, peaks.build(audio, sample_rate))
        print(f"波形を書き出しました: {peaks_out}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        # 波形は無くても字幕は使えるので、失敗しても止めない
        print(f"波形の書き出しに失敗しました（字幕には影響しません）: {e}", file=sys.stderr)


def diarize_worker(
    path: str, hf_token: str, num_speakers: int | None, device: str = "auto",
    peaks_out: str | None = None,
) -> int:
    """別プロセスで実行される話者分離 worker。

    結果の (start, end, speaker) を JSON で stdout に出力する。
    torch/pyannote のみを読み込み、ctranslate2 とは別プロセスにすることで
    OpenMP の重複ロードによるデッドロックを回避する。
    """
    import json
    import torch
    from pyannote.audio import Pipeline

    # torch>=2.6 は torch.load の既定が weights_only=True に変わり、pyannote 3.4.0 の
    # チェックポイント読み込み（TorchVersion 等の global を含む）が失敗する。
    # 信頼できる配布元（HuggingFace の pyannote 公式）のモデルなので、従来の
    # weights_only=False に戻す。torch<2.6 では既定が False なので影響なし。
    _orig_torch_load = torch.load

    def _patched_torch_load(*a, **kw):
        # 呼び出し元が weights_only=True を明示することがあるため強制的に上書きする
        kw["weights_only"] = False
        return _orig_torch_load(*a, **kw)

    torch.load = _patched_torch_load

    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"

    print("話者分離モデルを読み込み中...（初回はダウンロードに時間がかかります）", file=sys.stderr)
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token,
    )
    if device == "cuda":
        pipeline.to(torch.device("cuda"))
        print("話者分離: GPU (cuda) を使用", file=sys.stderr)

    audio = decode_audio_16k(path)

    # 話者分離のためにどのみち全体をデコードしている。
    # ここでピークも作っておけば、動画を読み直す手間がまるごと省ける。
    if peaks_out:
        write_peaks_from_samples(audio, 16000, peaks_out)

    waveform = torch.from_numpy(audio).unsqueeze(0)  # (1, n)
    inputs = {"waveform": waveform, "sample_rate": 16000}

    print("話者分離を実行中...", file=sys.stderr)
    if num_speakers:
        diarization = pipeline(inputs, num_speakers=num_speakers)
    else:
        diarization = pipeline(inputs)

    turns = [
        [turn.start, turn.end, label]
        for turn, _, label in diarization.itertracks(yield_label=True)
    ]
    print(json.dumps(turns))
    return 0


def run_diarization(path: str, hf_token: str, num_speakers: int | None, device: str = "auto",
                    peaks_out: str | None = None):
    """話者分離を別プロセスで実行し、(start, end, speaker) のリストを返す。"""
    import json
    import subprocess

    cmd = [sys.executable, __file__, "--diarize-worker", path, "--hf-token", hf_token,
           "--device", device]
    if num_speakers:
        cmd += ["--speakers", str(num_speakers)]
    if peaks_out:
        cmd += ["--peaks-out", peaks_out]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    # worker の進捗メッセージは stderr 経由でそのまま見せる
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"話者分離プロセスが失敗しました (exit {proc.returncode})")
    return [tuple(t) for t in json.loads(proc.stdout.strip().splitlines()[-1])]


def assign_speakers(lines: list[Line], turns) -> None:
    """各文字起こし区間に、時間的に最も重なる話者ラベルを割り当てる。

    pyannote の SPEAKER_00, SPEAKER_01... を 話者A, 話者B... に変換する。
    """
    # 出現順に安定したラベル名を割り当てる
    label_map: dict[str, str] = {}

    def jp_label(raw: str) -> str:
        if raw not in label_map:
            idx = len(label_map)
            label_map[raw] = f"話者{chr(ord('A') + idx)}" if idx < 26 else f"話者{idx + 1}"
        return label_map[raw]

    for ln in lines:
        best_label = None
        best_overlap = 0.0
        for t_start, t_end, label in turns:
            overlap = min(ln.end, t_end) - max(ln.start, t_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_label = label
        ln.speaker = jp_label(best_label) if best_label is not None else "話者不明"


def load_vocab(input_path: Path, terms_arg, note_arg, skip: bool) -> tuple[list[str], str]:
    """このセットの固有名詞と場面説明を決める。

    明示指定が最優先。無ければ入力ファイルと同じ場所の <名前>.settings.json を読む。
    エディタの「セットの設定」で書いた用語がそのまま次の文字起こしに効くようにするため。

    ここでは editor 側を import しない。このファイルは話者分離の worker として
    単体でも起動するので、依存を増やさない。
    """
    terms: list[str] = []
    note = ""

    if not skip:
        f = input_path.parent / f"{input_path.stem}.settings.json"
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                terms = [str(w).strip() for w in (d.get("terms") or []) if str(w).strip()]
                note = str(d.get("note") or "").strip()
                if terms or note:
                    print(f"セット設定を読みました: {f.name}")
            except Exception as e:  # noqa: BLE001
                print(f"セット設定を読めませんでした（無視して続行）: {e}")

    if terms_arg is not None:
        terms = [w.strip() for w in re.split(r"[,、\n]+", terms_arg) if w.strip()]
    if note_arg is not None:
        note = note_arg.strip()

    # 重複を落として順序は保つ
    seen: set[str] = set()
    terms = [w for w in terms if not (w in seen or seen.add(w))]
    return terms, note


def build_hints(terms: list[str], note: str) -> tuple[str | None, str | None]:
    """faster-whisper に渡す 2 つの手がかりを作る。

    hotwords       … 全ウィンドウに毎回入る。固有名詞はこちらが本命
    initial_prompt … 先頭ウィンドウにしか効かない。文体と場面の提示に使う
    """
    hotwords = "、".join(terms) if terms else None

    parts = []
    if note:
        parts.append(note if note[-1] in "。.!?！？" else note + "。")
    if terms:
        parts.append("次の語が出てきます: " + "、".join(terms) + "。")
    initial_prompt = "".join(parts) or None
    return hotwords, initial_prompt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="動画/音声から文字起こしを行い .srt / .txt を出力する"
    )
    parser.add_argument("input", help="入力する動画または音声ファイル")
    parser.add_argument(
        "--model",
        default="medium",
        help="Whisper モデルサイズ tiny/base/small/medium/large-v3 (既定: medium)",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cpu", "cuda"],
        help="実行デバイス。auto は CUDA があれば cuda、なければ cpu (既定: auto)",
    )
    parser.add_argument(
        "--compute-type",
        default="int8",
        help="ctranslate2 の計算精度 int8/int8_float16/float16/float32 (既定: int8)",
    )
    parser.add_argument(
        "--language",
        default="ja",
        help="音声の言語コード。auto で自動判定 (既定: ja)",
    )
    parser.add_argument(
        "--formats",
        nargs="+",
        default=["srt", "txt"],
        choices=["srt", "txt"],
        help="出力フォーマット (既定: srt txt)",
    )
    parser.add_argument(
        "--outdir",
        default=None,
        help="出力先ディレクトリ (既定: 入力ファイルと同じ場所の dest/<ファイル名>/)",
    )
    parser.add_argument(
        "--no-peaks",
        action="store_true",
        help="波形（.peaks.json）を出力しない",
    )
    parser.add_argument(
        "--peaks-out",
        default=None,
        help=argparse.SUPPRESS,  # 内部用: worker にピークの出力先を渡す
    )
    parser.add_argument(
        "--diarize",
        action="store_true",
        help="話者分離を行い話者ラベルを付与する (pyannote.audio)",
    )
    parser.add_argument(
        "--hf-token",
        default=os.environ.get("HF_TOKEN"),
        help="HuggingFace アクセストークン。環境変数 HF_TOKEN でも指定可",
    )
    parser.add_argument(
        "--speakers",
        type=int,
        default=None,
        help="話者数（分かっている場合に指定すると精度が上がる）",
    )
    parser.add_argument(
        "--terms",
        default=None,
        help="出てくる固有名詞をカンマ区切りで。店名・地名・商品名など。"
             "指定が無ければ同じ場所の <名前>.settings.json から読む",
    )
    parser.add_argument(
        "--note",
        default=None,
        help="場面の説明（例: カフェでの雑談。3人のうち1人は店員）。"
             "指定が無ければ settings.json から読む",
    )
    parser.add_argument(
        "--no-vocab",
        action="store_true",
        help="settings.json の固有名詞・場面説明を読まない",
    )
    parser.add_argument(
        "--split-speakers",
        action="store_true",
        help="話者ごとに分割した SRT を別ファイルで出力する（--diarize 必須）",
    )
    parser.add_argument(
        "--diarize-worker",
        action="store_true",
        help=argparse.SUPPRESS,  # 内部用: 話者分離をサブプロセスで実行
    )
    args = parser.parse_args()

    # 話者分離 worker モード（run_diarization が自身を再実行して呼び出す）
    if args.diarize_worker:
        return diarize_worker(args.input, args.hf_token, args.speakers, args.device,
                              peaks_out=args.peaks_out)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"エラー: ファイルが見つかりません: {input_path}", file=sys.stderr)
        return 1

    if args.diarize and not args.hf_token:
        print(
            "エラー: --diarize には HuggingFace トークンが必要です。\n"
            "  --hf-token hf_xxx を指定するか、環境変数 HF_TOKEN を設定してください。",
            file=sys.stderr,
        )
        return 1

    if args.split_speakers and not args.diarize:
        print("エラー: --split-speakers は --diarize と一緒に指定してください。", file=sys.stderr)
        return 1

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print(
            "エラー: faster-whisper が未インストールです。\n"
            "  python3 -m pip install --user faster-whisper",
            file=sys.stderr,
        )
        return 1

    stem = input_path.stem
    # 既定は 入力ファイルの場所/dest/<ファイル名>/。
    # 1 本ぶんの出力がひとつのフォルダにまとまるので、そのまま
    # Traccia の resources/<セット名>/ に移せる。
    outdir = Path(args.outdir) if args.outdir else input_path.parent / "dest" / stem
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"出力先: {outdir}")

    peaks_out = None if args.no_peaks else str(outdir / f"{stem}.peaks.json")

    language = None if args.language == "auto" else args.language

    # device=auto のときは CUDA の有無で自動判定（torch を import せず ctranslate2 で判定）
    device = args.device
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"

    print(f"モデル '{args.model}' を読み込み中... (device={device}, compute_type={args.compute_type})")
    model = WhisperModel(args.model, device=device, compute_type=args.compute_type)

    terms, note = load_vocab(input_path, args.terms, args.note, args.no_vocab)
    hotwords, initial_prompt = build_hints(terms, note)
    if terms:
        print(f"固有名詞 {len(terms)} 語を手がかりに渡します: {'、'.join(terms)}")
    if note:
        print(f"場面の説明: {note}")

    print(f"文字起こし開始: {input_path.name}")
    start_time = time.time()
    segments, info = model.transcribe(
        str(input_path),
        language=language,
        beam_size=5,
        vad_filter=True,
        hotwords=hotwords,
        initial_prompt=initial_prompt,
    )
    print(
        f"検出言語: {info.language} (確度 {info.language_probability:.2f}) / "
        f"音声長: {info.duration:.0f}秒"
    )

    # segments はジェネレータなので一度だけ展開し、進捗を表示する
    lines: list[Line] = []
    for seg in segments:
        text = drop_periods(seg.text)
        lines.append(Line(start=seg.start, end=seg.end, text=text))
        print(f"  [{format_timestamp(seg.start)}] {text}")

    if args.diarize:
        turns = run_diarization(str(input_path), args.hf_token, args.speakers, args.device,
                                peaks_out=peaks_out)
        assign_speakers(lines, turns)
        speakers = sorted({ln.speaker for ln in lines if ln.speaker})
        print(f"検出した話者: {', '.join(speakers)}")
    elif peaks_out:
        # 話者分離を使わないときは音声をデコードしていないので、ここで読む
        print("波形を作成中...")
        try:
            peaks = load_peaks_module()
            peaks.save(peaks_out, peaks.extract(str(input_path)))
            print(f"波形を書き出しました: {peaks_out}")
        except Exception as e:  # noqa: BLE001
            print(f"波形の書き出しに失敗しました（字幕には影響しません）: {e}", file=sys.stderr)

    written = []
    if "srt" in args.formats:
        srt_path = outdir / f"{stem}.srt"
        write_srt(lines, srt_path, duration=info.duration)
        written.append(srt_path)
        if args.split_speakers and args.diarize:
            written.extend(
                write_srt_per_speaker(lines, outdir, stem, duration=info.duration)
            )
    if "txt" in args.formats:
        txt_path = outdir / f"{stem}.txt"
        write_txt(lines, txt_path)
        written.append(txt_path)

    elapsed = time.time() - start_time
    print(f"\n完了 ({elapsed:.0f}秒, {len(lines)}セグメント)")
    for p in written:
        print(f"  出力: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
