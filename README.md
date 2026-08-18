# PACHI TOOL

スマスロのハイエナ判定、店舗傾向分析、狙い台検索をまとめたアプリです。Windowsソフト版、ローカルブラウザ版、iPhone PWA版で同じコードを利用します。

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
