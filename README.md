# PACHI TOOL

スマスロのハイエナ判定、店舗傾向分析、狙い台検索をまとめたアプリです。Windowsソフト版、ローカルブラウザ版、iPhone PWA版で同じコードを利用します。

## 会議・開発を引き継ぐ

まず [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) で現状を確認してください。
決定事項・モデルの検討案は [DECISIONS.md](DECISIONS.md)、次の会議・作業は [TODO.md](TODO.md) にまとめています。
ローカルの未コミット変更やDBが、GitHub・別PC・公開版にも反映済みとは限りません。

## 別のWindows PCで開発を始める

### 1. GitHubから取得

GitHub Desktopの場合は `File` → `Clone repository` → `URL` を開き、次を指定します。

```text
https://github.com/Yoshirou911/pachi-tool.git
```

ターミナルの場合は次を実行します。

```powershell
git clone https://github.com/Yoshirou911/pachi-tool.git
cd pachi-tool
```

### 2. 開発環境を自動準備

Python 3.12をインストール後、プロジェクトフォルダで実行します。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup-dev.ps1
```

仮想環境の作成と必要パッケージの導入が自動で行われます。

### 3. 起動

```powershell
.\.venv\Scripts\python.exe -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```

ブラウザで <http://127.0.0.1:8000/> を開きます。

## AI接続・根拠付き回答と費用管理（v3.48）

AIはPCまたは公開サーバー側から接続します。APIキーをスマホ画面、ブラウザ保存、SQLite、GitHubへ入れないでください。接続先は `PACHI_AI_PROVIDER` で1社だけ明示し、その会社のキーを環境変数へ設定します。

| 接続先 | `PACHI_AI_PROVIDER` | APIキーの環境変数 | 初期モデル |
|---|---|---|---|
| Groq | `groq` | `GROQ_API_KEY` | `qwen/qwen3.6-27b` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-flash` |
| Qwen | `qwen` | `DASHSCOPE_API_KEY` | `qwen3.5-plus` |
| Kimi | `kimi` | `MOONSHOT_API_KEY` | `kimi-k2.6` |
| Claude | `claude` | `ANTHROPIC_API_KEY` | `claude-sonnet-5` |

PowerShellでKimiを選ぶ例です。`ここにキー`を実際のキーへ置き換え、同じ画面からアプリを起動します。

```powershell
$env:PACHI_AI_PROVIDER = "kimi"
$env:MOONSHOT_API_KEY = "ここにキー"
$env:PACHI_AI_ALLOW_EXTERNAL = "true" # 外部送信を許可。接続先の利用料金が発生し得ます
$env:PACHI_AI_KIMI_INPUT_USD_PER_MTOK = "契約中の正式な入力単価"
$env:PACHI_AI_KIMI_OUTPUT_USD_PER_MTOK = "契約中の正式な出力単価"
$env:PACHI_AI_KIMI_MONTHLY_LIMIT_USD = "自分で決めた月上限"
.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

- `PACHI_AI_MODEL`: 初期モデル以外を明示するときだけ設定
- `PACHI_AI_TIMEOUT_SECONDS`: 5～120秒、未設定は45秒
- `PACHI_AI_ALLOW_EXTERNAL`: 未設定・`false`では外部送信を停止。`true`を明示した場合だけ有効
- `PACHI_AI_<AI名>_INPUT_USD_PER_MTOK` / `OUTPUT_USD_PER_MTOK`: 契約中の正式な100万トークン単価。未設定なら有料呼出しを停止
- `PACHI_AI_<AI名>_MONTHLY_LIMIT_USD`: AI別の月上限。共通値は`PACHI_AI_MONTHLY_LIMIT_USD`。未設定・超過なら停止
- `PACHI_AI_MAX_CALLS_PER_REQUEST`: 1要求の最大呼出回数。未設定は40回
- `PACHI_AI_SEND_PERSONAL_DATA`: 3.48では値にかかわらず無効。個人の実戦履歴・収支・会話履歴は外部送信しない

キー・外部許可・正式単価・月上限を設定しても自動では呼び出しません。店舗質問は「外部AIをこの1回だけ使う」と送信内容確認の両方を選んだ1回だけ、比較・評価も専用確認後だけ送信します。未設定、個人情報らしい文字列、上限超過、通信失敗、認証失敗、応答形式不正では停止または統計説明へ戻り、AIが予測順位や着席判定を変更することはありません。

利用記録は`DATA_DIR/ai_usage.db`へ追記し、AI名・モデル・用途・入力ハッシュ・予約額・報告トークン・概算費用・失敗区分だけを保存します。質問本文、根拠本文、生回答、APIキーは保存しません。同じ実行IDは再送せず、複数AI比較は選択した全社が費用・個人情報点検に通った場合だけ開始します。詳細は [RELEASE_3.48.md](RELEASE_3.48.md)。

AI回答の「根拠・出典を見る」から対象日、集計期間、出典、取得時刻を確認できます。AIは根拠IDを選び、文章と数字はサーバーが固定文で表示します。自由な分析文・新しい判定は生成しません。出典や取得時刻が不足する場合は補完せず未記録と表示します。一般チャットで全店舗を選んだ場合は対象日より前の30日分、記録日数順の最大30機種区分を参照し、1店舗を選んだ場合は3.42の店舗別処理を使います。将来の勝率ではありません。既存の店舗カルテとイベント分析にも同じ照合を適用します。根拠契約の詳細は [RELEASE_3.41.md](RELEASE_3.41.md)。

## AI機能の審査結果（v3.49）

スマホは「設定 → AI接続」、PCは「AI分析」の **AI機能の審査結果** を開きます。機能ごとの採用・補助限定・保留・不採用、外部AIの未測定事項と費用設定をいつでも確認できます。閲覧による外部AI通信・課金はありません。接続設定済みでも品質が審査済みという意味ではありません。

関連ソースや資料を変更した場合、以前の検証記録は認定に使わず再点検と表示します。開発者は以下で、隔離したデータ領域と模擬AIを使って全体を検証し、成功時だけ新しい記録を生成できます。

```powershell
.\.venv\Scripts\python.exe scripts/verify_ai_release.py
```

記録は `data/ai_review/verification_3.49.json`、詳細は [RELEASE_3.49.md](RELEASE_3.49.md)。実AI品質・実戦勝率・実iPhoneの操作を認定するものではありません。外部AIを止める場合は画面の外部利用チェックを外すか、サーバーで `PACHI_AI_ALLOW_EXTERNAL=false` にして再起動します。再送の受付状況が不明な場合は新しい実行を作らず同じIDで確認します。

この版はローカル実装です。GitHub公開PWA／APIの配信、EXEの再ビルドは別途必要です。配布EXEは照合用ソースが不足すると認定保留になります。

## 店舗について質問する（v3.42）

v3.42では「狙い台捜索 → 店舗×強い機種／傾向分析」の下に店舗質問欄を追加しました。上で店舗・行く日・期間を選び、「強い機種」「強い曜日」「イベント」「台番号の履歴」「島・配置」を押すか質問を入力します。機種を限定する場合は正式名を入力してください。根拠と比較相手、取得時刻、不足情報を展開できます。過去の公開実績の説明で、確定設定や将来の勝率・着席推薦ではありません。他店舗の公平な比較は根拠未整備と表示します。詳細は [RELEASE_3.42.md](RELEASE_3.42.md)。

## 複数AIを同じ条件で比較する（v3.43）

同じ画面の「複数AIを同じ条件で比較」で会社と固定問題を選びます。「外部通信なしで準備を確認」はAPI料金が発生しません。各社の契約適合率・固定期待根拠との一致・応答時間・報告トークン・費用を実測する場合だけ、サーバーで`PACHI_AI_ALLOW_EXTERNAL=true`と`PACHI_AI_ALLOW_COMPARISON_EXTERNAL=true`を設定し、画面の料金確認後に実行します。会社別APIキーがない接続先は呼ばれません。

費用単価と月上限は必須の管理者設定です。接続先が使用量を返さない場合、実額は未計測のままですが、送信前に予約した上限額は月枠から引いたままにします。比較結果はAIの勝者、予測精度、勝率、着席判定ではなく、自動採用もしません。詳細は [RELEASE_3.43.md](RELEASE_3.43.md) と [RELEASE_3.48.md](RELEASE_3.48.md)。

## AI回答の自動評価（v3.44）

「店舗×強い機種 → AI回答の自動評価」で方式と反復回数を選び、「評価を開始して保存」を押します。11問の固定された架空データで、根拠の選択・不要な根拠・回答を控える判断・反復時の一致を点検します。上部の店舗選択には依存しません。初期値のローカル参考方式はAI未使用で無料です。外部AIの実測には3.43と同じ接続設定・比較許可と画面の実行確認が必要です。

進捗と問題別の結果、直近10件の保存履歴を開けます。履歴全体は`DATA_DIR/ai_evaluations.db`に蓄積し、同じ実行IDで再確認しても重複実行しません。DBはGit公開対象外で、既存の店舗データ移行ZIPには含まれません。検証問題の点数と実店舗の予測成績は別です。詳細は [RELEASE_3.44.md](RELEASE_3.44.md)。

## 画像を読み取る（v3.45）

「狙い台捜索 → 店内」の「画像を読み取る」で、データランプ・店舗資料・ホールマップを選びます。端末内で抽出した候補を元画像と照合して訂正し、確認欄へチェックしてから保存します。データランプの確認値はハイエナ判定欄へ、ホールマップの台番号は未確認の編集下書きへ反映できます。画像だけから島・隣接・座標・確認済み配置を決めません。

サーバーに保存するのは確認済み抽出値、画像のSHA-256、画像形式・寸法だけです。元画像は自動送信せず、任意で選んだ場合もそのブラウザの端末内保存だけです。ブラウザデータを消すと元画像は消え、別端末には同期されません。ブラウザに文字検出機能がない場合、7セグ数字以外は手入力で確認します。この版は外部画像AIを呼ばず、料金も発生しません。詳細は [RELEASE_3.45.md](RELEASE_3.45.md)。

## 店舗分析データも別PCへ移す

SQLiteデータベースはGitHubへ公開しません。元のPCで次を実行します。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\backup-dev-data.ps1
```

`data/dev-backups/` にZIPが作成されます。ZIPをOneDrive、USB、または自分だけがアクセスできるストレージで別PCへ移し、Clone後のフォルダで実行します。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup-dev.ps1 -DataBackup "C:\path\pachi-tool-dev-data-YYYYMMDD-HHMMSS.zip"
```

バックアップには公開情報から収集した `hall_reports.db` だけが含まれます。以下の個人データは含まれません。

- 実戦収支とセッション (`sessions.db`)
- 候補台と資金設定 (`opportunities.db`)
- APIキーなどの環境変数

バックアップZIPを公開GitHubへコミットしないでください。復元時はハッシュ、SQLite整合性、必須テーブルを検証し、既存DBを `.bak-日時` へ退避します。アプリを終了してから復元してください。

## 複数PCでのGit運用

作業開始前に毎回更新します。

```powershell
git pull --rebase origin main
```

同じファイルを2台で同時編集しないのが安全です。作業後はテストしてからコミット・プッシュします。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node tests/mobile_core_test.mjs
node tests/mobile_ui_contract_test.mjs
git add .
git commit -m "変更内容"
git push origin main
```

## 公開版

- iPhone PWA: <https://yoshirou911.github.io/pachi-tool/>
- API: <https://pachi-tool.fly.dev/api/version>
