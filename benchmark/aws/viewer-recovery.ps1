function Stop-GameTunnel($Port, $Tunnel) {
    if ($Tunnel -and !$Tunnel.process.HasExited) {
        & taskkill.exe /PID $Tunnel.process.Id /T /F 2>$null | Out-Null
    }
    foreach ($listener in (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
        $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        if ($owner.ProcessName -eq 'session-manager-plugin') {
            Stop-Process -Id $owner.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

function Update-GameTunnel($Port, $Instance, $Tunnel) {
    if ($Tunnel -and !$Tunnel.process.HasExited) {
        $listening = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
        if ($listening -or (Get-Date) -lt $Tunnel.startupDeadline) { return $Tunnel }
    }
    Stop-GameTunnel $Port $Tunnel
    $arguments = "ssm start-session --region $($s.region) --target $Instance --document-name agent-wow-game-port --parameters portNumber=$Port,localPortNumber=$Port"
    try {
        $process = Start-Process $aws -ArgumentList $arguments -PassThru -WindowStyle Hidden -RedirectStandardOutput "C:\viewer\tunnel-$Port.log" -RedirectStandardError "C:\viewer\tunnel-$Port.err" -ErrorAction Stop
        return @{process=$process; startupDeadline=(Get-Date).AddSeconds(45)}
    } catch { Write-Warning "Could not start the viewer tunnel for port $Port." }
}

function Update-ViewerClient($DisconnectedSince) {
    $game = Get-Process Wow -ErrorAction SilentlyContinue | Select-Object -First 1
    $connected = $game -and (Get-NetTCPConnection -OwningProcess $game.Id -State Established -RemoteAddress 127.0.0.1 -RemotePort 8085 -ErrorAction SilentlyContinue)
    $client = Get-ScheduledTask -TaskName AgentWowClient
    if ($connected -or $client.State -in @('Running','Queued')) { return }
    if (!$DisconnectedSince) { return (Get-Date) }
    if ((Get-Date) -lt $DisconnectedSince.AddSeconds(30)) { return $DisconnectedSince }
    # Wait for both tunnels before restarting the interactive login task.
    foreach ($port in @(3724,8085)) {
        if (!(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)) {
            return $DisconnectedSince
        }
    }
    if ($game) { Stop-Process -Id $game.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item C:\viewer\character-entry.flag -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName AgentWowClient
}

$job = $null
$tunnels = @{}
$disconnectedSince = $null
while ((Get-Date) -lt $deadline) {
    # Harbor assigns one trial per viewer and the launcher terminates it at the end.
    if (!$job) {
        & $aws s3 cp "s3://$($s.bucket)/viewer/$($s.instance)/job.json" C:\viewer\job.tmp --only-show-errors 2>$null
        if ($LASTEXITCODE -eq 0) {
            Move-Item C:\viewer\job.tmp C:\viewer\job.json -Force
            $job = Get-Content C:\viewer\job.json | ConvertFrom-Json
        }
    }
    if ($job) {
        if (!(Test-Path C:\viewer\account.json)) {
            & $aws s3 cp "s3://$($s.bucket)/viewer/$($job.job_id)/account.json" C:\viewer\account.tmp --only-show-errors 2>$null
            if ($LASTEXITCODE -eq 0) { Move-Item C:\viewer\account.tmp C:\viewer\account.json -Force }
        }
        foreach ($port in @(3724,8085)) {
            $tunnels[$port] = Update-GameTunnel $port $job.game_instance $tunnels[$port]
        }
        if (Test-Path C:\viewer\account.json) {
            $disconnectedSince = Update-ViewerClient $disconnectedSince
        }
    }
    Start-Sleep 15
}
