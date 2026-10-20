param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$taskKey = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\work\access\deploy_ed25519'))
$taskHosts = Join-Path $PSScriptRoot 'deployment-known-hosts'
$sshExe = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
$secureKey = $null
$plainKey = $null
$payload = $null
$keyPointer = [IntPtr]::Zero
$sshProcess = $null
$sshStarted = $false
try {
    if (-not (Test-Path -LiteralPath $taskKey) -or -not (Test-Path -LiteralPath $taskHosts)) {
        throw 'missing_access_files'
    }
    if (-not $CheckOnly) {
        Write-Host 'DeepSeek API 安全配置' -ForegroundColor Cyan
        Write-Host '请填写 DeepSeek 官方 API Key，不要填写 PocketBase 密码或网关管理令牌。'
        Write-Host 'Key 将经 SSH 发送到你的 VPS，由 VPS 向 DeepSeek 官方接口验证。'
        Write-Host '输入不显示明文；可用 Ctrl+V、Ctrl+Shift+V 或右键粘贴，再按回车。'
        Write-Host '本步骤不会发起模型生成请求。按 Ctrl+C 可以取消。'
        $secureKey = Read-Host '粘贴 API Key（隐藏输入）' -AsSecureString
        $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
        if ($plainKey -notmatch '\A[A-Za-z0-9._~+/:=@!%\-]{8,4096}\z') {
            throw 'invalid_key_format'
        }
        $payload = @{key = $plainKey} | ConvertTo-Json -Compress
    }
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $sshExe
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.Arguments = '-T -i "{0}" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile={1}" -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 root@103.146.124.175 /opt/goutou/current/venv/bin/python /usr/local/sbin/goutou-set-model-key' -f $taskKey, $taskHosts
    if ($CheckOnly) { $start.Arguments += ' --check' }
    $sshProcess = New-Object Diagnostics.Process
    $sshProcess.StartInfo = $start
    [void]$sshProcess.Start()
    $sshStarted = $true
    $stdoutTask = $sshProcess.StandardOutput.ReadToEndAsync()
    $stderrTask = $sshProcess.StandardError.ReadToEndAsync()
    if (-not $CheckOnly) {
        $sshProcess.StandardInput.WriteLine($payload)
        Write-Host '正在加密提交、验证并启用，请稍候……'
    }
    $sshProcess.StandardInput.Close()
    $plainKey = $null
    $payload = $null
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
        $keyPointer = [IntPtr]::Zero
    }
    if (-not $sshProcess.WaitForExit(120000)) {
        $sshProcess.Kill()
        throw 'result_unknown_check_server'
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    [void]$stderrTask.GetAwaiter().GetResult()
    if ([string]::IsNullOrWhiteSpace($stdout)) { throw 'ssh_connection_failed' }
    $result = $stdout | ConvertFrom-Json
    if ($result.status -eq 'ready_for_key' -and $CheckOnly -and $sshProcess.ExitCode -eq 0) {
        Write-Host ('安全输入通道已就绪；模型 Key 已配置：{0}' -f [bool]$result.configured)
    } elseif ($result.status -eq 'configured' -and $sshProcess.ExitCode -eq 0) {
        Write-Host '配置成功：官方认证通过，Key 已保存，网关已启用。' -ForegroundColor Green
        Write-Host '未执行付费生成测试。请返回聊天回复“已完成”，我会检查网关状态。'
    } else {
        $safeMessages = @{
            provider_auth_rejected = 'DeepSeek 官方认证未通过；请确认复制的是官方 API Key。原配置未更改。'
            provider_check_unavailable = '服务器暂时无法完成官方接口检查；原配置未更改，可稍后重试。'
            provider_check_failed = '官方接口检查失败；原配置未更改。'
            configured_model_not_available = 'Key 可访问的模型与当前预设不符；请返回聊天说明。'
            maintenance_busy = '服务器正在备份或部署，请稍后重试。'
            invalid_input = 'Key 格式无效，请仅粘贴 Key 本身，不要带引号、空格或 Bearer 前缀。'
            activation_failed_previous_config_restored = '网关启用失败，已恢复旧配置，请返回聊天说明。'
            configuration_restored_service_needs_attention = '旧配置已恢复，但服务需要检查，请返回聊天说明。'
        }
        if ($safeMessages.ContainsKey([string]$result.code)) {
            Write-Host $safeMessages[[string]$result.code] -ForegroundColor Yellow
        } else {
            Write-Host '配置未完成，请返回聊天说明；不需要发送 Key。' -ForegroundColor Yellow
        }
        exit 1
    }
} catch {
    if ($CheckOnly) { Write-Host ('检查失败：{0}' -f $_.Exception.Message) }
    Write-Host '未取得配置成功结果。请确认 Key 格式、网络和服务器状态；不要把 Key 发到聊天。' -ForegroundColor Yellow
    exit 1
} finally {
    $plainKey = $null
    $payload = $null
    if ($keyPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer) }
    if ($null -ne $secureKey) { $secureKey.Dispose() }
    if ($null -ne $sshProcess) {
        if ($sshStarted -and -not $sshProcess.HasExited) { $sshProcess.Kill() }
        $sshProcess.Dispose()
    }
    if (-not $CheckOnly) { [void](Read-Host '按回车关闭窗口') }
}
