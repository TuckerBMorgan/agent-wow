# Wait for the game to attach the camera before acknowledging a watch request.
$ErrorActionPreference = 'Stop'
$aws = 'C:\Program Files\Amazon\AWSCLIV2\aws.exe'
$s = Get-Content C:\viewer\settings.json | ConvertFrom-Json
$prefix = "s3://$($s.bucket)/viewer/$($s.instance)"
$gamePath = (Get-Content C:\viewer\game-path.txt).Trim()
$screenshots = Join-Path $gamePath 'Screenshots'
$shell = New-Object -ComObject WScript.Shell

function Save-WatchStatus($Request, $State, $ErrorMessage = '') {
    @{request_id=$Request.id; character=$Request.character; state=$State; error=$ErrorMessage;
      updated_at=(Get-Date).ToUniversalTime().ToString('o')} |
        ConvertTo-Json | Set-Content C:\viewer\watch-status.json
    & $aws s3 cp C:\viewer\watch-status.json "$prefix/watch-status.json" --only-show-errors
}

function Wait-Watch($Game, $Character) {
    $started = [DateTime]::UtcNow
    $deadline = $started.AddMinutes(3)
    $retryAt = $started
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Game.HasExited) { return $false }
        $proof = Get-ChildItem $screenshots -Filter 'WoWScrnShot_*.tga' -File -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTimeUtc -gt $started -and $_.Length -gt 0 } |
            Select-Object -First 1
        if ($proof) {
            Remove-Item -LiteralPath $proof.FullName -ErrorAction SilentlyContinue
            return $true
        }
        if ([DateTime]::UtcNow -ge $retryAt) {
            if ($shell.AppActivate($Game.Id)) {
                if ($retryAt -eq $started) {
                    $shell.SendKeys('{ESC}')
                    Start-Sleep -Milliseconds 400
                    $shell.SendKeys('{ESC}')
                    Start-Sleep -Milliseconds 600
                }
                $shell.SendKeys('{ENTER}')
                Start-Sleep -Milliseconds 300
                $shell.SendKeys('/agentwowwatch ' + $Character)
                Start-Sleep -Milliseconds 300
                $shell.SendKeys('{ENTER}')
            }
            $retryAt = [DateTime]::UtcNow.AddSeconds(8)
        }
        Start-Sleep 1
    }
    throw "WoW did not confirm the camera was following $Character within three minutes."
}

$current = ''
$target = $null
$cameraEntry = $null
while ($true) {
    $game = Get-Process Wow -ErrorAction SilentlyContinue | Select-Object -First 1
    $entry = Get-Item C:\viewer\character-entry.flag -ErrorAction SilentlyContinue
    if (!$game -or !$entry -or $entry.LastWriteTimeUtc -gt [DateTime]::UtcNow.AddSeconds(-30) -or
        (Get-Content $entry.FullName -ErrorAction SilentlyContinue) -ne [string]$game.Id) {
        Start-Sleep 5
        continue
    }

    try {
        $ErrorActionPreference = 'Continue'
        & $aws s3 cp "$prefix/watch-request.json" C:\viewer\watch-request.tmp --only-show-errors 2>$null
        $request = if ($LASTEXITCODE -eq 0) { Get-Content C:\viewer\watch-request.tmp | ConvertFrom-Json }
    } finally { $ErrorActionPreference = 'Stop' }
    if ($request -and $request.id -ne $current) {
        $current = $request.id
        if ($request.character -cnotmatch '^[A-Za-z]{2,12}$' -or
            !$request.expires_at -or [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt $request.expires_at) {
            Save-WatchStatus $request 'failed' 'The camera request is invalid or expired.'
            continue
        }
        $target = $request
        $cameraEntry = $null
    }
    if ($target -and $cameraEntry -ne $entry.LastWriteTimeUtc) {
        # Retry the selected character after the game client reconnects.
        $cameraEntry = $entry.LastWriteTimeUtc
        Save-WatchStatus $target 'waiting'
        try {
            if (Wait-Watch $game $target.character) { Save-WatchStatus $target 'watching' }
        } catch { Save-WatchStatus $target 'failed' $_.Exception.Message }
    }
    Start-Sleep 5
}
