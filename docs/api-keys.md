# API キーの取得手順

Traccia で使う 3 種類のキーの取り方。**どれも必須ではありません。** 何をしたいかで必要なものが変わります。

| キー | 何に使うか | 無いとどうなるか |
|---|---|---|
| `HF_TOKEN` | ローカルの**話者分離**（pyannote のモデル取得）| 文字起こしはできるが、話者が分かれない |
| `DEEPGRAM_API_KEY` | 文字起こし＋話者分離を Deepgram に任せる | ローカル実行のみになる |
| `ASSEMBLYAI_API_KEY` | 同じことを AssemblyAI に任せる | 同上 |
| `GEMINI_API_KEY` | 同じことを Gemini に任せる（固有名詞を指示できる）| 同上 |

字幕の**編集だけ**（`Traccia.command`）なら、キーは一切要りません。

> **単価・無料枠は変動します。** この文書の数字は目安で、必ず各社の現在の表示を確認してください。

> **`bench/` はこのリポジトリに含まれません。**
> 以降に出てくる `bench/compare.py` や `bench/usage.py` は、どの文字起こし経路が使えるかを
> 調べるために手元で作った検証用スクリプトです。実行結果に素材動画の書き起こしがそのまま
> 残るため、`.gitignore` で除外しています。
> **Traccia 本体の動作には不要**なので、クローンした人が用意する必要はありません。
> 数字を引用している箇所は、その検証で実測した値です。

---

## 目次

1. [HF_TOKEN（HuggingFace）](#hf_tokenhuggingface)
2. [DEEPGRAM_API_KEY](#deepgram_api_key)
3. [ASSEMBLYAI_API_KEY](#assemblyai_api_key)
4. [GEMINI_API_KEY](#gemini_api_key)
5. [環境変数に設定する](#環境変数に設定する)
6. [取れているか確認する](#取れているか確認する)
7. [使った分と請求を確認する](#使った分と請求を確認する)
8. [安全に扱う](#安全に扱う)
9. [うまくいかないとき](#うまくいかないとき)

---

## HF_TOKEN（HuggingFace）

**いちばん手順が多く、詰まりやすいのがこれです。** 3 段階あります。

pyannote のモデルは「利用条件に同意した人だけ」が取得できる形（gated）で置かれています。
トークンは**そのダウンロードの鍵**で、音声を送る先ではありません。処理は手元で走ります。

### 手順 1 — アカウントを作る

<https://huggingface.co/join>

メールアドレスとパスワードだけ。無料です。

### 手順 2 — モデルの利用条件に同意する

ここを飛ばすと、トークンを作っても `Cannot access gated repo` で止まります。

以下の 2 ページを開き、それぞれで同意します。

| ページ | 役割 |
|---|---|
| <https://huggingface.co/pyannote/speaker-diarization-3.1> | Traccia が読み込むパイプライン本体 |
| <https://huggingface.co/pyannote/segmentation-3.0> | 上記が内部で使う発話区間の検出モデル |

> 3.1 で `segmentation-3.0` の同意が必須かは公式に明記されていませんが、
> 過去のバージョンでは両方必要でした。**両方同意しておけば確実です。**

ページを開くと、同意の前に入力欄が出ます。

```
Company/university   ← 所属。個人なら "individual" や "personal" で通ります
Website              ← 無ければ GitHub や SNS の URL で構いません
```

入力して同意すると、そのページの表示が「アクセス権あり」に変わります。

### 手順 3 — トークンを作る

<https://huggingface.co/settings/tokens> → **Create new token**

トークンには 2 種類あります。

**Read タイプ（簡単・おすすめ）**

種類で `Read` を選ぶだけ。gated リポジトリも読めます。

**Fine-grained タイプ（権限を絞りたい場合）**

`Repositories` の項目で、次にチェックを入れてください。

```
☑ Read access to contents of all public gated repos you can access
```

**これが最大の落とし穴です。** 既定では入っていないため、チェックを忘れると
条件に同意済みでも `401` / `403` になります。書き込み・課金・Webhook 系は不要です。

作成すると `hf_xxxxxxxx…` が一度だけ表示されます。**この画面を離れると再表示できません。**
控えるか、すぐ環境変数に入れてください。

---

## DEEPGRAM_API_KEY

文字起こしと話者分離を 1 回のリクエストで返します。

### 手順

1. <https://console.deepgram.com/signup> でアカウントを作る
2. コンソールにログイン → 左メニューの **API Keys**
3. **Create a New API Key** → 名前を付けて作成（用途がわかる名前にしておくと後で失効させやすい）
4. 表示されたキーを控える（**再表示できません**）

### 無料枠

登録すると **$200 のクレジット**が付き、**クレジットカードの登録は不要**です。
**期限はありません。** Nova で約 43,000 分ぶんに相当するので、検証には十分です。

### 料金の目安

Nova-3 が **1 時間あたり約 $0.26**。話者分離は追加料金なし。

---

## ASSEMBLYAI_API_KEY

こちらも文字起こしと話者分離を 1 回で返します。

### 手順

1. <https://www.assemblyai.com/dashboard/signup> でアカウントを作る
2. ログイン後の**ダッシュボードにキーがそのまま表示**されます（コピーするだけ）

Deepgram より手数が少ないのが利点です。

### 無料枠

登録で **$50 のクレジット**が付きます。**期限はありません** — 数週間〜数ヶ月放置しても消えず、
カードを登録して有料に移行しても未使用分は残ります（公式FAQに明記）。
Universal-2 なら約 290 時間ぶんに相当します。

> 例外: 申請制の **Startup Program** のクレジットは毎月リフレッシュ制で、
> 前月の未使用分は繰り越されず失効します。通常の登録なら関係ありません。

**API キー自体にも公式な期限の記載はありません。** 失効させるか差し替えるまで有効です。

### 料金の目安

| | 1 時間あたり |
|---|---|
| Universal-2 | $0.15 |
| Universal-3.5 Pro | $0.21 |
| **話者分離（録音済み）** | **+$0.02** |

Universal-2 + 話者分離で **約 $0.17/時間**。Deepgram（約 $0.26/時間）より安いです。

**SLAM-1 は廃止済み**で、`universal-3-pro` への移行が案内されています。

### 注意

**日本語で話者分離（`speaker_labels`）が使えるかは、実際に投げて確かめてください。**
言語によって対応状況が異なります。`bench/compare.py` は、本文だけ返って話者が付かない場合に
その旨を表示します。

モデル名でエラーが出たら `--aai-model universal-3-pro` も試してください。

---

## GEMINI_API_KEY

**音声を受け取れる LLM** なので、文字起こしと話者分離を一度に返す上に、
「何人の会話か」「どんな固有名詞が出るか」を指示できる。専用 ASR にはできない使い方。

**Traccia のエディタから直接使える唯一のキー**でもある（ほかは `bench/` の検証用）。

### 手順

1. <https://aistudio.google.com/apikey> を開く（Google アカウントでログイン）
2. **Create API key** → プロジェクトを選ぶ（無ければ新規作成）
3. 表示されたキーをコピー

### エディタに入れる

環境変数でも動くが、**エディタの「設定」から入れるほうが扱いやすい**。

1. `Traccia.command` でエディタを開く
2. 右上の **「設定」** → API キーを貼って「確認」（疎通を見るだけなので費用はかからない）
3. **「Gemini 文字起こしを使う」にチェック** → 「保存」

保存先は `~/.traccia/config.json`。本人だけが読める権限（0600）で書かれ、
**素材フォルダには入らない**ので、素材ごと渡してもキーは付いていかない。
環境変数 `GEMINI_API_KEY` があれば、キー未設定のときはそちらを使う。

同じ画面に**これまでの費用**（今月・累積・履歴）と、**今月の上限**の設定がある。
既定は「使わない」なので、キーを入れただけでは課金されない。

Google Cloud のプロジェクトは要るが、AI Studio 側で作れる。カード登録は無料枠の範囲では不要。

### 無料枠

AI Studio 経由には**無料のレート制限枠**がある（1 分あたり・1 日あたりのリクエスト数で制限）。
検証程度なら課金されない範囲で収まる。上限はモデルごとに変わるので
<https://ai.google.dev/gemini-api/docs/rate-limits> で確認する。

### 料金の目安

音声入力は **1 秒あたり約 32 トークン**。45 秒で約 1,400 トークン。

`gemini-3.6-flash` で 45 秒のクリップを 1 本流したときの**実測**。

```
入力  1,346 tok (音声 1,125 + テキスト 221) × $1.50/M = $0.00202
出力  3,086 tok (本文   856 + 思考   2,230) × $7.50/M = $0.02314
                                              合計      $0.02516
                                        動画 1 時間換算  $2.01
```

**思考トークン（`thoughtsTokenCount`）も出力として課金される。**
実測では出力の **72%** が思考だった。返ってくる字幕の長さだけで見積もると大きく外す。

1 時間あたりで並べると、Gemini は専用 ASR の 8〜12 倍。

| 経路 | 1 時間あたり |
|---|---|
| ローカル | $0 |
| AssemblyAI | $0.17 |
| Deepgram | $0.26 |
| **Gemini 3.6 Flash** | **$2.01** |

単価は変わるので <https://ai.google.dev/gemini-api/docs/pricing> で確認すること。

### モデル名に注意

入れ替わりが早く、古い名前は 404 になる（`gemini-2.5-flash` は新規提供終了）。
**そのキーで使えるものを一覧できる。**

```bash
.venv/bin/python bench/gemini_models.py
```

### 使い方

固有名詞と場面を渡せるのが要点。

```bash
.venv/bin/python bench/compare.py bench/out/bluebottle_200s_45s.wav --only gemini \
  --speakers 3 \
  --terms "代官山,中目黒,リコッタチーズ,コルネッティ,フラーゴラ,ルバーブ" \
  --note "カフェでの雑談。3人のうち1人は店員"
```

---

## 環境変数に設定する

**キーはファイルに書かず、環境変数で渡します。**

### Mac

`~/.bash_profile`（zsh なら `~/.zshrc`）に追記します。

```bash
export HF_TOKEN=hf_xxxxxxxx
export DEEPGRAM_API_KEY=xxxxxxxx
export ASSEMBLYAI_API_KEY=xxxxxxxx
export GEMINI_API_KEY=xxxxxxxx
```

保存したら、新しいターミナルを開くか次を実行します。

```bash
source ~/.bash_profile
```

### Windows

一度だけ実行すれば、以降のウィンドウで有効になります。

```
setx HF_TOKEN hf_xxxxxxxx
setx DEEPGRAM_API_KEY xxxxxxxx
setx ASSEMBLYAI_API_KEY xxxxxxxx
```

**設定した後は、コマンドプロンプトを開き直してください。** 既に開いているウィンドウには反映されません。

`Traccia 文字起こし.bat` は `HF_TOKEN` を環境変数から読むので、これだけで動きます。

---

## 取れているか確認する

### 設定されているか

```bash
# Mac
env | grep -E "HF_TOKEN|DEEPGRAM|ASSEMBLYAI" | sed 's/=.\{6\}.*/=（設定あり）/'
```

```
:: Windows
echo %HF_TOKEN%
```

### 実際に通るか

```bash
cd /path/to/traccia

# HuggingFace（話者分離のモデルが取得できるか）
.venv/bin/python -c "
import os
from huggingface_hub import HfApi
try:
    HfApi().model_info('pyannote/speaker-diarization-3.1', token=os.environ['HF_TOKEN'])
    print('HF_TOKEN: OK（gated モデルにアクセスできます）')
except Exception as e:
    print('HF_TOKEN: NG', e)
"
```

残りの 3 つは、音声を送らずにキーの有効性だけ確かめられます。
**HTTP 200 が返ればそのキーは生きています。**

```bash
# Deepgram
curl -s -o /dev/null -w "Deepgram   : HTTP %{http_code}\n" \
  -H "Authorization: Token $DEEPGRAM_API_KEY" \
  https://api.deepgram.com/v1/projects

# AssemblyAI（Bearer は付けない。キーをそのまま渡す）
curl -s -o /dev/null -w "AssemblyAI : HTTP %{http_code}\n" \
  -H "Authorization: $ASSEMBLYAI_API_KEY" \
  https://api.assemblyai.com/v2/transcript

# Gemini（使えるモデルの一覧が返る）
curl -s -o /dev/null -w "Gemini     : HTTP %{http_code}\n" \
  "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY"
```

`401` / `403` は通っていません。**環境変数が空のときも同じ応答になる**ので、
まず上の「設定されているか」で変数自体を確認してください。

---

## 使った分と請求を確認する

見る場所が 2 つある。**手元の記録**と**各社のコンソール（正本）**。

### 手元の記録

検証用スクリプト（リポジトリには含まれません）で、保存済みの結果から
どのクリップにいくらかかったかを出したもの。

```bash
.venv/bin/python bench/usage.py --detail
```

```
princi_62s_45s  (45 秒)
  gemini（用語あり）  $0.02516  実測  入力 1,346 / 出力 3,086
      出力内訳    本文 856 + 思考 2,230
      1 時間換算  $2.01
```

`実測` は API 応答に入っていたトークン数から計算したもの。
`概算` は音声長からの当て推量（Deepgram / AssemblyAI は実測が取れない）。

**これは請求書ではない。** 手元の記録なので、単価が変わっていれば実費とずれる。

### Gemini の実費

課金は Cloud 請求システムを通る。**AI Studio ではなく Google Cloud の請求画面**を見る。

1. <https://console.cloud.google.com/billing> を開く
2. API キーを作ったプロジェクトに紐づく**請求先アカウント**を選ぶ
3. **「レポート」**を開き、サービスで **`Generative Language API`** を絞り込む

[AI Studio の usage ページ](https://aistudio.google.com/usage)はリクエスト数とトークン数
（＝レート制限の消化状況）が中心。金額は Cloud 側が正本。

**注意すべき点が 3 つある。**

- **反映が遅れる** — 課金データは即時ではなく、通常 1 日程度遅れて確定する。
  直後に見ても出ていない
- **少額だと見えない** — $0.13 程度だとレポート上 $0.00 に丸められることがある。
  金額で追えないときはトークン数で照合するほうが確実（それが `bench/usage.py`）
- **無料枠かどうかは応答で分かる** — `usageMetadata.serviceTier` が `standard` なら
  従量課金、`free` なら無料枠。`bench/usage.py --detail` の「区分」に出る

### Deepgram / AssemblyAI の実費

どちらも前払いのクレジット制なので、**残高の減りが実費**。

| | URL |
|---|---|
| Deepgram | <https://console.deepgram.com/usage> |
| AssemblyAI | <https://www.assemblyai.com/app/usage> |

### 予算アラートを張る

実費を後から見るより、先に上限を張るほうが安全。
Cloud 請求画面の**「予算とアラート」**で通知を設定できる。

Gemini は $2.01/時なので、**1 時間の動画を 5 本で $10** に達する。
思考トークンの量で振れるため、事前の見積もりだけに頼らないほうがよい水準。

---

## 安全に扱う

> 扱いの全体は [SECURITY.md](../SECURITY.md) にまとめてあります。ここでは要点だけ。

**キーは絶対にコミットしないこと。** 環境変数だけで渡す運用を崩さないでください。

`.gitignore` で以下を除外していますが、**除外設定は事故で外れます。** 最後の砦にしないこと。

```
mac_command.txt
win_実行コマンド.txt
.env
*.token
hf_token*
```

> `mac_command.txt` / `win_実行コマンド.txt` は、**起動コマンドを控えておくための手元用のメモ**です
> （リポジトリには含まれていません）。`--hf-token hf_xxx` を含むコマンドをそのまま貼りがちなので、
> 最初から `.gitignore` に入れてあります。同じ用途のメモを別の名前で作るときは、
> **その名前も忘れずに除外してください。**

### 漏れたときは失効させる

漏れた可能性がある時点で、そのキーは使えないものとして扱います。

| | 失効させる場所 |
|---|---|
| HuggingFace | <https://huggingface.co/settings/tokens> → 対象を Delete → 作り直す |
| Deepgram | コンソール → API Keys → 対象を削除 → 作り直す |
| AssemblyAI | ダッシュボード → キーのローテーション |

Deepgram / AssemblyAI は**使った分だけ課金される**ので、放置すると請求につながります。
迷ったら消して作り直してください。数十秒で済みます。

---

## うまくいかないとき

| 症状 | 原因と対処 |
|---|---|
| `Cannot access gated repo for pyannote/speaker-diarization-3.1` | 手順 2 の同意をしていない。両方のページで同意する |
| 同意済みなのに `401` / `403` | Fine-grained トークンで `Read access to contents of all public gated repos` を入れ忘れている。Read タイプで作り直すのが早い |
| `use_auth_token got an unexpected keyword argument` | トークンではなくライブラリの問題。`huggingface_hub<1.0` に固定が必要（[`transcribe.md`](transcribe.md) 参照）|
| `DEEPGRAM_API_KEY が設定されていません` | `export` したのと同じシェルで実行しているか。Windows は `setx` 後にウィンドウを開き直す |
| Gemini が `HTTP 404: no longer available` | モデル名が古い。`bench/gemini_models.py` で使えるものを確認する |
| Deepgram が `HTTP 400` | そのモデルが `language=ja` と `diarize` の両方に対応しているか。`--dg-model nova-2` も試す |
| AssemblyAI で「本文は取れているが話者が付かない」 | 日本語で `speaker_labels` が使えない可能性。`bench/out/raw_assemblyai.json` の `utterances` を確認 |
| トークンを控え忘れた | 再表示はできません。削除して作り直してください |

---

## 参考

- [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
- [HuggingFace — User access tokens](https://huggingface.co/docs/hub/en/security-tokens)
- [Deepgram Console](https://console.deepgram.com/)
- [AssemblyAI Dashboard](https://www.assemblyai.com/dashboard/)
