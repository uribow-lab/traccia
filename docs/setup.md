# セットアップと起動

> [Traccia](../README.md) のドキュメント。入れ方と、動かし方。

---

## セットアップ

### Mac（編集用）

```bash
cd /path/to/traccia
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

編集だけなら PyAV しか要らないが、`requirements.txt` 一式を入れておけば
Mac 単体でも（遅いが）文字起こしできる。

### Windows（文字起こし用）

CUDA 版 PyTorch の入れ方を含め、[文字起こしマニュアル](transcribe.md#windowscuda--gpuでのセットアップ)
に手順がある。

HuggingFace のトークンは環境変数で持たせる。一度だけ実行しておく。

```
setx HF_TOKEN hf_xxxxxxxx
```

**トークンの取り方は [`api-keys.md`](api-keys.md) にまとめてある。**
pyannote は利用条件への同意が必要な gated モデルなので、トークンを作るだけでは足りない。

> **トークンをファイルに書かないこと。** 起動コマンドを控えるメモに
> `--hf-token hf_xxx` ごと貼ってしまうのがよくある事故なので、環境変数で渡す形を崩さない。
> 扱いの全体は [SECURITY.md](../SECURITY.md) にある。

---

## 起動

### ダブルクリック

| | ファイル |
|---|---|
| Mac・エディタ | `Traccia.command` |
| Windows・エディタ | `Traccia.bat` |
| Windows・文字起こし | `Traccia 文字起こし.bat`（動画をドラッグ＆ドロップ）|

`Traccia.command` は Dock に置いておける。終了はそのウィンドウで `Ctrl+C`。

### コマンド

```bash
cd /path/to/traccia

.venv/bin/python -m traccia edit                     # エディタ（既定: ./resources）
.venv/bin/python -m traccia edit ~/別の素材 --port 9000
.venv/bin/python -m traccia edit --no-browser

.venv/bin/python -m traccia transcribe 動画.mp4 --diarize --speakers 4 --split-speakers
```

Windows は `.venv\Scripts\python -m traccia ...`。

`python` というコマンドは macOS には無い。**必ず `.venv/bin/python`** を使う。
毎回打つのが面倒なら `~/.bash_profile` に置く。

```bash
alias traccia='/path/to/traccia/.venv/bin/python -m traccia'
```

> エディタの既定ポートは 8791。macOS では 8770 番を `sharingd` が握っていることがあるため
> 避けている。使用中のときは自動で別のポートを選び、その旨を起動時に表示する。

これまでの `python transcribe.py 動画.mp4 --diarize ...` もそのまま動く
（`transcribe.py` は `traccia/transcribe.py` へ転送するだけの薄いファイル）。
古い書き方を控えてあるメモは、そのままで構わない。

