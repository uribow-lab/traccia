# 文字起こし マニュアル

> このドキュメントは **Traccia** の文字起こし側（`transcribe`）の詳細マニュアルです。
> ツール全体の説明と字幕エディタの使い方は [`README.md`](../README.md) にあります。
> API キーの取得手順は [`api-keys.md`](api-keys.md) にあります。
>
> 本文中の `python transcribe.py ...` は現在も動きます。
> 新しい書き方は `python -m traccia transcribe ...` で、どちらでも同じです。

動画/音声から文字起こしを行い、字幕（`.srt`）とテキスト（`.txt`）を出力するツールです。
ローカルで動作し（オフライン・無料）、話者分離（誰が話したか）にも対応します。

- 文字起こしエンジン: [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- 話者分離エンジン: [pyannote.audio](https://github.com/pyannote/pyannote-audio)

---

## 目次

1. [できること](#できること)
2. [Intel Mac での実行手順](#intel-mac-での実行手順)
3. [M1 / M2 Mac（Apple Silicon）での実行手順](#m1--m2-macapple-siliconでの実行手順)
4. [Windows（CUDA / GPU）でのセットアップ](#windowscuda--gpuでのセットアップ)
5. [Windows での実行手順](#windows-での実行手順)
6. [コマンドオプション一覧](#コマンドオプション一覧)
7. [出力ファイル](#出力ファイル)
8. [HuggingFace トークンの準備（話者分離に必要）](#huggingface-トークンの準備話者分離に必要)
9. [処理時間の目安](#処理時間の目安)
10. [トラブルシューティング](#トラブルシューティング)

---

## できること

- 動画/音声（mp4, mov, wav, mp3 等）からの文字起こし
- 字幕ファイル `.srt`（タイムスタンプ付き）とプレーンテキスト `.txt` の出力
- 話者分離（`話者A` / `話者B` …）— `--diarize`
- 話者ごとに分割した `.srt` の出力 — `--split-speakers`
- 全 `.srt` の先頭・末尾を空テキストで揃える（自動）
- CPU / NVIDIA GPU（CUDA）の切り替え — `--device`

**このマニュアルはローカル実行（無料）についてのもの。**
Traccia のエディタから **Gemini** に書き起こさせる道もある（有料・1 時間の動画あたり $2 前後）。
そちらは [`gemini.md`](gemini.md) を参照。
Windows へ動画を持っていかずに済むが、費用がかかる。

内部の処理（faster-whisper と pyannote を別プロセスに分けている理由など）は
[`internals.md`](internals.md#文字起こしの処理の流れ) にある。

---

## Intel Mac での実行手順

> **環境前提**: Intel Mac（GPUなし）。PyTorch が Intel Mac + Python 3.13 に非対応のため、
> **Python 3.12 の仮想環境（venv）** を使います。

### 初回セットアップ

```bash
cd /path/to/traccia

# ffmpeg（未インストールなら）
brew install ffmpeg

# Python 3.12 の venv を作成して依存をインストール
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 実行（文字起こしのみ）

```bash
cd /path/to/traccia
.venv/bin/python transcribe.py sample.mp4
```

→ `sample.srt` と `sample.txt` が同じフォルダに出力されます。

### 実行（話者分離あり）

```bash
.venv/bin/python transcribe.py sample.mp4 \
  --diarize --speakers 3 \
  --hf-token hf_xxxxxxxxxxxxxxxxx
```

話者数が分かっている場合は `--speakers N` を付けると精度が上がります。

### 実行（話者ごとに .srt を分割）

```bash
.venv/bin/python transcribe.py sample.mp4 \
  --diarize --speakers 3 --split-speakers \
  --hf-token hf_xxxxxxxxxxxxxxxxx
```

→ `sample.srt` に加えて `sample.話者A.srt` / `sample.話者B.srt` … が出力されます。

### 進捗の確認方法

ログをファイルに残すと、別ターミナルで `tail -f` して進捗を確認できます。

```bash
.venv/bin/python transcribe.py sample.mp4 --diarize --speakers 3 \
  --hf-token hf_xxx 2>&1 | tee run.log
# 別ターミナルで:
tail -f run.log
```

> **注意**: 必ず `.venv/bin/python` で実行してください。システムの `python3` だと
> PyTorch が無いため話者分離が動きません（文字起こしのみは動きます）。
>
> Intel Mac は GPU が使えないため処理は遅めです（[処理時間の目安](#処理時間の目安)参照）。
> 長尺の動画は `--model small` を推奨します。

---

## M1 / M2 Mac（Apple Silicon）での実行手順

> **環境前提**: Apple Silicon（M1 / M2 / M3 …）の Mac。
> Intel Mac と違い、**最新の Python（3.13 でも可）と arm64 ネイティブの PyTorch** が
> そのまま使えます。`requirements.txt` の Intel 用バージョン固定（`torch==2.2.2` /
> `numpy<2` 等）は **不要**で、最新版でクリーンに入ります。
>
> ⚠️ 文字起こしエンジン（faster-whisper / ctranslate2）は **MPS（GPU）に非対応**のため、
> 処理は **CPU で実行**されます（`--device cuda` も使えません）。ただし M1/M2 の CPU は
> Intel Mac より大幅に速く、実用的な時間で処理できます（[処理時間の目安](#処理時間の目安)参照）。

### 初回セットアップ

```bash
cd /path/to/traccia

# ffmpeg（未インストールなら）
brew install ffmpeg

# venv を作成（Python 3.12 / 3.13 どちらでも可）
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip

# 依存をインストール（Apple Silicon は最新版でOK。requirements.txt は使わない）
# torch / torchaudio は arm64 ネイティブ wheel が自動で入る
.venv/bin/python -m pip install \
  faster-whisper av "pyannote.audio==3.4.0" "huggingface_hub<1.0" \
  torch torchaudio
```

> ※ `pyannote.audio==3.4.0` と `huggingface_hub<1.0` の固定は**プラットフォーム共通**で必要です
> （4.x は torch>=2.8 を要求し、`huggingface_hub` 1.x は `use_auth_token` 廃止で 3.4.0 が動かないため）。
> 一方 `torch` / `torchaudio` / `numpy` は Apple Silicon では固定不要なので、上記のとおり最新版を入れます。

インストール確認:
```bash
.venv/bin/python -c "import torch, pyannote.audio; print('torch', torch.__version__); print('pyannote', pyannote.audio.__version__)"
```

### 実行（文字起こしのみ）

```bash
cd /path/to/traccia
.venv/bin/python transcribe.py sample.mp4
```

→ `sample.srt` と `sample.txt` が同じフォルダに出力されます（`--device` は省略で自動的に CPU）。

### 実行（話者分離あり）

```bash
.venv/bin/python transcribe.py sample.mp4 \
  --diarize --speakers 3 \
  --hf-token hf_xxxxxxxxxxxxxxxxx
```

### 実行（話者ごとに .srt を分割）

```bash
.venv/bin/python transcribe.py sample.mp4 \
  --diarize --speakers 3 --split-speakers \
  --hf-token hf_xxxxxxxxxxxxxxxxx
```

### 進捗の確認方法

```bash
.venv/bin/python transcribe.py sample.mp4 --diarize --speakers 3 \
  --hf-token hf_xxx 2>&1 | tee run.log
# 別ターミナルで:
tail -f run.log
```

> **注意**: 必ず `.venv/bin/python` で実行してください（システムの `python3` だと torch が無く
> 話者分離が動きません）。
>
> Apple Silicon は MPS 非対応で CPU 実行ですが Intel より速いです。それでも長尺の動画は
> `--model small` にすると体感が大きく改善します。

---

## Windows（CUDA / GPU）でのセットアップ

> **環境前提**: Windows 10 / NVIDIA GPU（CUDA対応）。
> GPU を使うことで Mac（CPU）より **1〜2桁高速** になります。
> Intel Mac で必要だったバージョン固定（numpy<2 等）は **Windows では不要**で、最新版でクリーンに入ります。

> ⚠️ **Mac から一式コピーする場合、`.venv/` はコピーしないでください。**
> `.venv` の中身は OS・CPU 専用のバイナリ（Mac 用の torch / ctranslate2 等）で Windows では動きません。
> フォルダ構成も異なり（Mac: `.venv/bin/python` ／ Windows: `.venv\Scripts\python.exe`）、
> 入れるパッケージのバージョンも別物（Mac は torch 固定、Windows は CUDA 版 torch）です。
>
> **コピーするのは `transcribe.py` / `traccia/` / `requirements.txt` などのソース一式のみ**で、
> `.venv/` は除外し、Windows 側で下記の手順4〜5で**新規に作成**します。
> モデルキャッシュも不要です（初回実行時に自動ダウンロードされます）。

### 1. Python のインストール

[python.org](https://www.python.org/downloads/windows/) から **Python 3.11〜3.13** を入れます
（インストール時に「Add python.exe to PATH」にチェック）。
※ 3.13 でも動作確認済み（その場合 CUDA 版 PyTorch は後述の `cu124` を使う）。

インストール済みバージョンの確認:
```
py --list
```

### 2. ffmpeg のインストール

```powershell
winget install Gyan.FFmpeg
```

または手動で [ffmpeg.org](https://ffmpeg.org/download.html) から入れて PATH を通します。

> **文字化けする場合**: 標準のコマンドプロンプトだと winget の進捗表示が化けます。
> **Windows Terminal** を使うか、実行前に `chcp 65001`（UTF-8化）してください。表示が化けても
> インストール自体は成功していることが多いので、新しい窓で `ffmpeg -version` で確認します。

### 3. NVIDIA ドライバ / CUDA ランタイム

- 最新の [NVIDIA GeForce ドライバ](https://www.nvidia.com/Download/index.aspx) を入れる
- faster-whisper（ctranslate2）は **cuBLAS と cuDNN** を必要とします。
  以下のどちらかで用意します:
  - **方法A（簡単・推奨）**: PyTorch の CUDA 版を入れると必要な CUDA/cuDNN DLL が同梱されます（手順5）。
    多くの環境ではこれだけで ctranslate2 も GPU を認識します。
  - **方法B**: [NVIDIA CUDA Toolkit](https://developer.nvidia.com/cuda-downloads) と
    [cuDNN](https://developer.nvidia.com/cudnn) を個別にインストールして PATH に追加。

### 4. 仮想環境の作成

```powershell
cd D:\path\to\traccia
py -3.13 -m venv .venv          # 3.12 でも可。手順1の py --list で確認したバージョンに合わせる
.venv\Scripts\python -m pip install --upgrade pip
```

### 5. 依存パッケージのインストール

**インストール順が重要です。** CUDA 版 PyTorch を**先に**入れ、`pyannote.audio` は
**バージョンを 3.4.0 に固定**します（最新の 4.x は torch>=2.8 を要求し、CUDA版 torch を
入れ替えて壊すため。本ツールも 3.4.0 のAPI前提）。

```powershell
# 1) CUDA 版 PyTorch（Python 3.13 は cu124 を指定。GPU認識DLL同梱。約2.5GBで時間がかかる）
.venv\Scripts\python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124

# 2) 文字起こし + 話者分離（pyannote は 3.4.0 固定）
.venv\Scripts\python -m pip install faster-whisper "pyannote.audio==3.4.0" av

# 3) huggingface_hub を 1.0 未満に固定（最新版だと use_auth_token 廃止で pyannote 3.4.0 が動かない）
.venv\Scripts\python -m pip install "huggingface_hub<1.0"
```

> ※ Windows + CUDA では `requirements.txt`（Intel Mac 用にバージョン固定）は使いません。
>
> ⚠️ **torch のインストールは2.5GBの展開に5〜15分かかります**（特にHDD + Defenderのスキャン）。
> `9/11 [torch]` で止まって見えても**中断しない**でください。タスクマネージャーのディスク/CPUが
> 動いていれば処理中です。Defenderの除外（管理者PowerShellで
> `Add-MpPreference -ExclusionPath "D:\path\to\traccia\.venv"`）を入れると速くなります。
>
> ⚠️ `pyannote.audio==3.4.0` のインストール時に `Building wheel for tensorboardX...` 等で
> 数十秒止まって見えても**中断しない**でください。

### 6. インストール確認

```powershell
.venv\Scripts\python -c "import torch, pyannote.audio; print('torch', torch.__version__, '| cuda', torch.version.cuda, '| avail', torch.cuda.is_available()); print('pyannote', pyannote.audio.__version__)"
.venv\Scripts\python -c "import ctranslate2; print('ct2 cuda:', ctranslate2.get_cuda_device_count())"
```

以下のように表示されればGPU利用可能です:
- `torch 2.6.0+cu124 | cuda 12.4 | avail True`
- `pyannote 3.4.0`
- `ct2 cuda: 1`

> **`avail False` になる場合**: `pyannote.audio` のインストール時に torch が CPU版へ
> 入れ替わっています。次でCUDA版を入れ直します（torchのみ・依存を巻き込まない）:
> ```
> .venv\Scripts\python -m pip install --force-reinstall --no-deps torch torchaudio --index-url https://download.pytorch.org/whl/cu124
> ```

---

## Windows での実行手順

### 文字起こしのみ（GPU自動利用）

```powershell
.venv\Scripts\python transcribe.py sample.mp4 --device cuda --compute-type int8
```

- `--device cuda` … GPU を使用（省略時 `auto` でGPUがあれば自動的に使用）
- `--compute-type int8` … GTX 1060 など Pascal 世代では `int8` が最適
  （`float16` は Pascal では遅いので避ける。RTX 以降なら `float16` も可）

### 話者分離あり（GPU）

```powershell
.venv\Scripts\python transcribe.py sample.mp4 `
  --diarize --speakers 3 --split-speakers `
  --device cuda --compute-type int8 `
  --hf-token hf_xxxxxxxxxxxxxxxxx
```

### 大きいモデルで高精度に（VRAMに注意）

```powershell
.venv\Scripts\python transcribe.py sample.mp4 --model large-v3 --device cuda --compute-type int8 --diarize
```

> **VRAM 6GB の注意**: `large-v3`（float16で約3GB）と話者分離（torch）を**同時に**載せると
> 6GB を超える恐れがあります。本ツールは文字起こしと話者分離を**別プロセスで順番に**実行するため、
> 6GB でも基本的に問題ありません。それでも不足する場合は `--compute-type int8` または `--model medium` に下げてください。

---

## コマンドオプション一覧

| オプション | 既定値 | 説明 |
|---|---|---|
| `input`（位置引数） | — | 入力する動画/音声ファイル |
| `--model` | `medium` | モデルサイズ `tiny`/`base`/`small`/`medium`/`large-v3` |
| `--device` | `auto` | `auto`/`cpu`/`cuda`。auto は CUDA があれば自動でGPU使用 |
| `--compute-type` | `int8` | 計算精度 `int8`/`int8_float16`/`float16`/`float32` |
| `--language` | `ja` | 言語コード。`auto` で自動判定 |
| `--formats` | `srt txt` | 出力形式（`srt` `txt` を空白区切りで複数指定可） |
| `--outdir` | 入力と同じ場所 | 出力先ディレクトリ |
| `--diarize` | （無効） | 話者分離を有効化（要 HuggingFace トークン） |
| `--speakers` | （自動） | 話者数を固定。分かっていれば指定すると精度↑ |
| `--split-speakers` | （無効） | 話者ごとに分割した `.srt` を出力（`--diarize` 必須） |
| `--hf-token` | 環境変数 `HF_TOKEN` | HuggingFace アクセストークン |

---

## 出力ファイル

**出力先は入力ファイルと同じ場所の `dest/<ファイル名>/`**（`-o` で変えられる）。
入力が `sample.mp4` の場合:

```
sample.mp4
dest/
  sample/
    sample.srt          全体（本文の先頭に「話者A: 」）
    sample.txt          読み用（同じ話者が続くとまとめる）
    sample.話者A.srt    話者別（--split-speakers 時）
    sample.話者B.srt
    sample.peaks.json   波形
```

| ファイル | 内容 |
|---|---|
| `sample.txt` | 文字起こし全文（話者分離時は `話者A:` ごとにまとめ） |
| `sample.srt` | 字幕（話者分離時は各行頭に `話者A:` ラベル付き） |
| `sample.話者A.srt` 等 | `--split-speakers` 時。話者ごとの字幕（番号は1から振り直し） |
| `sample.peaks.json` | 波形のピーク列。エディタのタイムラインが使う |

`sample.srt` は本文の先頭に `話者A: ` のような**話者プレフィクス**が付く。
**Traccia のエディタはこの形式を読む。**

この `dest/sample/` に動画を入れて Mac の `resources/` へ置けば、そのまま 1 セットになる
（→ [素材の置き方](workflow.md#素材の置き方)）。

- 全 `.srt` は先頭（`00:00:00`）と末尾（動画終端）に空テキスト字幕を入れて時間範囲を揃えています。
- 話者別 `.srt` のタイムスタンプは元動画の時刻のままなので、そのまま動画に重ねられます。

---

## HuggingFace トークンの準備（話者分離に必要）

`--diarize` を使うには、無料の HuggingFace トークンと、モデル利用規約への同意が必要です（初回のみ）。

1. [HuggingFace](https://huggingface.co/join) でアカウント作成（無料）
2. 以下2つのモデルページで「Agree and access repository」を押す（**両方必須**）
   - https://huggingface.co/pyannote/speaker-diarization-3.1
   - https://huggingface.co/pyannote/segmentation-3.0
3. [トークン発行ページ](https://huggingface.co/settings/tokens) で **Read** タイプのトークンを作成
4. `--hf-token hf_xxx` で渡す、または環境変数に設定:
   - Mac/Linux: `export HF_TOKEN=hf_xxx`
   - Windows (PowerShell): `$env:HF_TOKEN="hf_xxx"`

> 費用は一切かかりません（pyannote.audio はオープンソース・モデルもローカル実行）。

---

## 処理時間の目安

1時間（3600秒）の動画を `medium` + 話者分離で処理した場合の目安:

| 環境 | 文字起こし | + 話者分離 | 合計目安 |
|---|---|---|---|
| Intel Mac i7-7700K（CPU） | 3〜5時間 | +約1時間 | **4〜6時間** |
| M1 Mac（CPU/NEON） | 1〜1.5時間 | +約40分 | **1.5〜2時間** |
| M2 Mac 8GB（CPU/NEON） | 1〜1.5時間 | +約40分 | **1.5〜2時間** |
| **Win10 + GTX 1060（CUDA）** | 6〜12分 | +10〜15分 | **20〜30分** |

モデル別（GTX 1060 / CUDA / 1時間動画 / 話者分離込み）:

| モデル | 合計目安 |
|---|---|
| `small` | 約15〜20分 |
| `medium` | 約20〜30分 |
| `large-v3` | 約30〜40分 |

> いずれも `sample.mp4`（53.8秒）の実測からの外挿で、内容や無音割合により変動します。

---

## トラブルシューティング

### `torch` が見つからない / 話者分離が動かない（Mac）
システムの `python3` で実行している可能性があります。必ず `.venv/bin/python` を使ってください。

### `use_auth_token` got an unexpected keyword argument（Mac / Windows 共通）
`huggingface_hub` が新しすぎます（1.x で `use_auth_token` が廃止）。1.0 未満に下げてください:
```
.venv\Scripts\python -m pip install "huggingface_hub<1.0"      # Windows
.venv/bin/python   -m pip install "huggingface_hub<1.0"        # Mac（requirements.txt にも記載）
```

### `pyannote-audio 4.0.4 requires torch>=2.8.0` の警告 / torch が CPU版に化ける（Windows）
`pyannote.audio` を `>=3.1` で入れると最新の 4.x が入り、CUDA版 torch を壊します。
`pyannote.audio==3.4.0` に固定してください（手順5）。torch が CPU 版になった場合は
`--force-reinstall --no-deps torch torchaudio --index-url .../cu124` で入れ直します。

### `WeightsUnpickler error ... weights_only`（torch>=2.6 / 主に Windows）
torch 2.6 から `torch.load` の既定が `weights_only=True` になり、pyannote 3.4.0 の
モデル読み込みが失敗します。`transcribe.py` 内の `diarize_worker` で `weights_only=False` に
戻すパッチ済みです。古い `transcribe.py` を使っている場合は最新版に差し替えてください
（`findstr weights_only transcribe.py` で確認できます）。

### `NumPy 1.x cannot be run in NumPy 2.x` / `OMP: Error #15`（Mac）
それぞれ「numpy が新しすぎ」「OpenMP 重複ロード」です。`requirements.txt` 通りの構成
（`numpy<2`）と、本ツールのサブプロセス方式（自動）で回避済みです。

### GPU が使われない（Windows）
- `.venv\Scripts\python -c "import torch; print(torch.cuda.is_available())"` が `False` の場合、
  CUDA 版 PyTorch が入っていません。手順5の `--index-url .../cu124` を確認。
- ctranslate2 が GPU を認識しない場合、cuDNN DLL が見つかっていません。手順3の方法A/Bを確認。
- 最新ドライバへ更新してください。

### VRAM 不足（CUDA out of memory）
`--compute-type int8` にする、`--model` を下げる（`medium`→`small`）と改善します。
```

