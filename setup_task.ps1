# pachi-tool 自動スクレイプ タスクスケジューラ登録
# 管理者権限で実行してください

$TaskName = "PachiToolScrape"
$PythonPath = (Get-Command python).Source
$ScriptPath = "$PSScriptRoot\sync_scrape.py"
$WorkDir = $PSScriptRoot

# 毎日 02:00 (PCが起動していれば) に実行
$Trigger = New-ScheduledTaskTrigger -Daily -At "02:00"

# PC がスリープ中でも起動させる設定
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $WorkDir

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

# 既存タスクがあれば削除
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Trigger $Trigger `
    -Action $Action `
    -Settings $Settings `
    -Principal $Principal `
    -Description "pachi-tool: 毎日02:00にホールデータをスクレイプしてサーバーへ同期"

Write-Host "`n✅ タスク登録完了: $TaskName (毎日 02:00 実行)" -ForegroundColor Green
Write-Host "   確認: タスクスケジューラ → タスクスケジューラライブラリ → $TaskName`n"
