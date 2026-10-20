param(
    [switch]$CheckOnly,
    [switch]$ImportCsv,
    [string]$KeyPath = '',
    [string]$KnownHostsPath = '',
    [string]$Server = 'root@103.146.124.175'
)
$ErrorActionPreference = 'Stop'
$sshProcess = $null
$sshStarted = $false
$inputCancelled = $false
$plainId = $null
$plainSecret = $null
$plainPassword = $null
$plainConfirmation = $null
$payload = $null
$exitCode = 1

function Convert-HiddenValue([Security.SecureString]$Value) {
    $pointer = [IntPtr]::Zero
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    }
}

function Read-ValidatedHiddenInput(
    [ValidateSet('id', 'secret', 'password', 'confirmation')][string]$Kind,
    [string]$Compare = '',
    [string[]]$Disallowed = @()
) {
    $prompts = @{
        id = 'RAM AccessKey ID（只粘贴一次字段值，隐藏输入）'
        secret = 'RAM AccessKey Secret（只粘贴一次字段值，隐藏输入）'
        password = '你已自行安全保存的独立备份密码（隐藏输入）'
        confirmation = '再次输入同一个备份密码（隐藏输入）'
    }
    while ($true) {
        $hidden = $null
        $plain = $null
        try {
            $hidden = Read-Host $prompts[$Kind] -AsSecureString
            $plain = Convert-HiddenValue $hidden
            if ($Kind -eq 'id' -or $Kind -eq 'secret') {
                # Tolerate surrounding whitespace copied with a single field value.
                # Never trim or otherwise normalize the independent backup password.
                $plain = $plain.Trim()
            }
            $message = $null
            switch ($Kind) {
                id {
                    if ($plain -notmatch '\A[A-Za-z0-9]{8,128}\z') {
                        $message = 'AccessKey ID 格式无效。只复制 ID 字段的值，不要复制 CSV 标题、引号或整行；只粘贴一次后按回车。请重填 ID。'
                    }
                }
                secret {
                    if ($plain -notmatch '\A[A-Za-z0-9+/=_\-]{16,256}\z') {
                        $message = 'AccessKey Secret 格式无效。只复制 Secret 字段的值，不要复制 CSV 标题、引号或整行；只粘贴一次后按回车。请重填 Secret。'
                    }
                }
                password {
                    if ($plain.Length -lt 16 -or $plain.Length -gt 4096 -or $plain -match '[\r\n\x00]') {
                        $message = '备份密码需要 16 至 4096 个字符，不能包含换行或空字符。请重填备份密码。'
                    } else {
                        foreach ($value in $Disallowed) {
                            if ([string]::Equals($plain, $value, [StringComparison]::Ordinal)) {
                                $message = '备份密码必须独立于 AccessKey ID 和 Secret，请重新设置并安全保存。'
                            }
                        }
                    }
                }
                confirmation {
                    if (-not [string]::Equals($plain, $Compare, [StringComparison]::Ordinal)) {
                        $message = '两次备份密码不一致，请只重新输入确认密码。前面已通过的内容无需重填。'
                    }
                }
            }
            if ($null -eq $message) { return $plain }
            Write-Host $message -ForegroundColor Yellow
        } finally {
            $plain = $null
            if ($null -ne $hidden) { $hidden.Dispose() }
        }
    }
}

function ConvertFrom-AccessKeyCsv([string]$Text) {
    $rows = $null
    $pair = $null
    try {
        if ([Text.Encoding]::UTF8.GetByteCount($Text) -gt 65536) { throw 'csv_too_large' }
        $rows = @($Text.TrimStart([char]0xfeff) | ConvertFrom-Csv -Delimiter ',' -ErrorAction Stop -WarningAction SilentlyContinue)
        if ($rows.Count -ne 1) { throw 'csv_invalid' }
        $pair = @{}
        foreach ($property in $rows[0].PSObject.Properties) {
            $normalized = [regex]::Replace($property.Name, '[\s_\-\uFEFF]', '').ToLowerInvariant()
            $field = $null
            if ($normalized -eq 'accesskeyid') { $field = 'access_key_id' }
            if ($normalized -eq 'accesskeysecret') { $field = 'access_key_secret' }
            if ($null -ne $field) {
                if ($pair.ContainsKey($field)) { throw 'csv_invalid' }
                $pair[$field] = ([string]$property.Value).Trim()
            }
        }
        if ($pair.Count -ne 2 -or $pair.access_key_id -notmatch '\A[A-Za-z0-9]{8,128}\z' -or $pair.access_key_secret -notmatch '\A[A-Za-z0-9+/=_\-]{16,256}\z') {
            throw 'csv_invalid'
        }
        return $pair
    } catch {
        if ($_.Exception.Message -eq 'csv_too_large') { throw 'csv_too_large' }
        throw 'csv_invalid'
    } finally {
        $Text = $null
        $rows = $null
        $pair = $null
    }
}

function Read-BoundedAccessKeyCsv([string]$Path) {
    $stream = $null
    $buffer = New-Object byte[] 65537
    $text = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $count = 0
        while ($count -lt $buffer.Length) {
            $read = $stream.Read($buffer, $count, $buffer.Length - $count)
            if ($read -eq 0) { break }
            $count += $read
        }
        if ($count -gt 65536) { throw 'csv_too_large' }
        $encoding = New-Object Text.UTF8Encoding($false, $true)
        $text = $encoding.GetString($buffer, 0, $count)
        return ConvertFrom-AccessKeyCsv $text
    } catch {
        if ($_.Exception.Message -eq 'csv_too_large') { throw 'csv_too_large' }
        throw 'csv_invalid'
    } finally {
        $text = $null
        [Array]::Clear($buffer, 0, $buffer.Length)
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function Select-LocalAccessKeyCsv {
    $dialog = $null
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $dialog = New-Object Windows.Forms.OpenFileDialog
        $dialog.Title = '选择之前下载的 RAM AccessKey CSV（仅在本机读取）'
        $dialog.Filter = 'CSV 文件 (*.csv)|*.csv'
        $dialog.Multiselect = $false
        $dialog.CheckFileExists = $true
        $dialog.CheckPathExists = $true
        $dialog.RestoreDirectory = $true
        if ($dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { throw 'input_cancelled' }
        if ([IO.Path]::GetExtension($dialog.FileName) -ine '.csv') { throw 'csv_invalid' }
        return Read-BoundedAccessKeyCsv $dialog.FileName
    } catch {
        $code = [string]$_.Exception.Message
        if ($code -in @('input_cancelled', 'csv_invalid', 'csv_too_large')) { throw $code }
        throw 'csv_dialog_unavailable'
    } finally {
        if ($null -ne $dialog) { $dialog.Dispose() }
    }
}

function Find-AccessFile([string]$Explicit, [string]$Name) {
    if (-not [string]::IsNullOrWhiteSpace($Explicit)) {
        $resolved = [IO.Path]::GetFullPath($Explicit)
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) { throw 'missing_access_files' }
        return $resolved
    }
    # Delivery lives in outer outputs/; source invocation also finds outer work/access/.
    $directory = [IO.DirectoryInfo]$PSScriptRoot
    while ($null -ne $directory) {
        $candidate = Join-Path $directory.FullName ('work\access\' + $Name)
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return [IO.Path]::GetFullPath($candidate) }
        $directory = $directory.Parent
    }
    throw 'missing_access_files'
}

function New-RedirectedProcessStartInfo([string]$Executable) {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $Executable
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $start.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    return $start
}

function Write-Utf8ProcessInput([Diagnostics.Process]$Process, [string]$Value) {
    # .NET Framework used by Windows PowerShell 5.1 has no StandardInputEncoding.
    # Write the pipe bytes directly so Unicode passwords survive without a BOM.
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    try {
        $Process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
        $Process.StandardInput.BaseStream.Flush()
    } finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

try {
    $taskKey = Find-AccessFile $KeyPath 'deploy_ed25519'
    $taskHosts = Find-AccessFile $KnownHostsPath 'known_hosts'
    if ($taskKey -match '["\r\n\x00]' -or $taskHosts -match '["\r\n\x00]' -or $Server -notmatch '\A[a-z_][a-z0-9_-]*@[A-Za-z0-9.-]+\z') {
        throw 'invalid_connection_settings'
    }
    $sshExe = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
    if (-not (Test-Path -LiteralPath $sshExe -PathType Leaf)) { throw 'ssh_unavailable' }
    if (-not $CheckOnly) {
        Write-Host 'OSS 加密异地备份安全配置' -ForegroundColor Cyan
        Write-Host '目标：zovoeh-backup-20261021 / goutou-v1（北京地域）'
        Write-Host '先在你自己的密码管理器或离线安全位置生成并保存一个独立的备份密码（至少 16 个字符）。'
        Write-Host '灾难恢复时需要这个密码；请确保换电脑后仍能取回。不要把 AccessKey Secret 当作备份密码。'
        Write-Host '密钥和密码均隐藏；仅经已验证主机身份的 SSH 发送到服务器。不要把密钥或密码发到聊天。'
        if (-not $ImportCsv) {
            Write-Host 'AccessKey 每次只复制对应字段的值，不要复制 CSV 标题、引号或整行；只粘贴一次后按回车。格式错误会只重填当前字段。'
        }
        Write-Host '按 Ctrl+C 可取消。'
        if ($ImportCsv) {
            Write-Host '请选择之前下载的 RAM CSV。文件只在本机读取，密钥会自动填入并隐藏；不会显示文件路径或账号信息。'
            $imported = $null
            try {
                $imported = Select-LocalAccessKeyCsv
                $plainId = $imported.access_key_id
                $plainSecret = $imported.access_key_secret
                Write-Host '已导入一组 AccessKey，接下来只需填写独立备份密码。'
            } finally {
                if ($null -ne $imported) { $imported.Clear() }
                $imported = $null
            }
        } else {
            $plainId = Read-ValidatedHiddenInput -Kind id
            $plainSecret = Read-ValidatedHiddenInput -Kind secret
        }
        $plainPassword = Read-ValidatedHiddenInput -Kind password -Disallowed @($plainId, $plainSecret)
        $plainConfirmation = Read-ValidatedHiddenInput -Kind confirmation -Compare $plainPassword
        $plainConfirmation = $null
        $payload = @{ access_key_id = $plainId; access_key_secret = $plainSecret; password = $plainPassword } | ConvertTo-Json -Compress
        if ([Text.Encoding]::UTF8.GetByteCount($payload) -gt 16384) { throw 'invalid_input' }
    }

    $start = New-RedirectedProcessStartInfo $sshExe
    $start.Arguments = '-T -i "{0}" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile={1}" -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 {2} /usr/local/sbin/goutou-configure-offsite' -f $taskKey, $taskHosts, $Server
    if ($CheckOnly) { $start.Arguments += ' --check' }
    $sshProcess = New-Object Diagnostics.Process
    $sshProcess.StartInfo = $start
    [void]$sshProcess.Start()
    $sshStarted = $true
    $stdoutTask = $sshProcess.StandardOutput.ReadToEndAsync()
    $stderrTask = $sshProcess.StandardError.ReadToEndAsync()
    if (-not $CheckOnly) {
        Write-Utf8ProcessInput $sshProcess $payload
        Write-Host '正在验证 OSS 权限和加密仓库，请稍候……'
    }
    $sshProcess.StandardInput.Close()
    $plainId = $null
    $plainSecret = $null
    $plainPassword = $null
    $plainConfirmation = $null
    $payload = $null
    if (-not $sshProcess.WaitForExit(120000)) {
        $sshProcess.Kill()
        throw 'result_unknown_check_server'
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    [void]$stderrTask.GetAwaiter().GetResult()
    if ([string]::IsNullOrWhiteSpace($stdout) -or $stdout.Length -gt 16384) { throw 'ssh_connection_failed' }
    $result = $stdout | ConvertFrom-Json
    if ($CheckOnly -and $result.status -eq 'ready_for_credentials' -and $sshProcess.ExitCode -eq 0) {
        Write-Host ('安全输入通道已就绪；凭据已配置：{0}；备份工具已安装：{1}' -f [bool]$result.configured, [bool]$result.tools_available)
        if (-not $result.tools_available) { throw 'tool_unavailable' }
        $exitCode = 0
    } elseif (-not $CheckOnly -and $result.status -eq 'configured' -and $sshProcess.ExitCode -eq 0) {
        Write-Host '配置成功：OSS 访问和仓库密码验证通过，服务器已安全保存配置。' -ForegroundColor Green
        Write-Host '请妥善保存你的独立备份密码。返回聊天回复“已完成”即可，无需发送密钥或密码。'
        $exitCode = 0
    } else {
        $safeCodes = @('invalid_input', 'requires_root', 'root_required', 'busy', 'maintenance_busy', 'tool_unavailable', 'tool_timeout', 'cloud_probe_failed', 'repository_open_failed', 'repository_init_failed', 'repository_missing', 'password_change_forbidden', 'config_invalid', 'config_missing', 'unsafe_config', 'configuration_failed')
        if ($safeCodes -contains [string]$result.code) { throw [string]$result.code }
        throw 'configuration_failed'
    }
} catch [Management.Automation.PipelineStoppedException] {
    $inputCancelled = $true
} catch {
    $safeMessages = @{
        missing_access_files = '缺少已验证的 SSH 密钥或 known_hosts 文件，请返回聊天说明。'
        invalid_connection_settings = 'SSH 连接设置无效，请返回聊天说明。'
        ssh_unavailable = 'Windows OpenSSH 未安装，请返回聊天说明。'
        invalid_input = 'AccessKey 格式无效，请仅输入密钥本身。'
        invalid_password = '备份密码必须至少 16 个字符，且不能包含换行或空字符。'
        password_mismatch = '两次备份密码不一致，本次未提交。请重新运行。'
        independent_password_required = '备份密码必须独立于 AccessKey，请重新设置并安全保存。'
        tool_unavailable = '服务器备份工具尚未就绪，请返回聊天说明。'
        tool_timeout = '验证超时；结果需要检查。请返回聊天说明。'
        cloud_probe_failed = 'OSS 访问验证失败；本地原配置未更改。请检查 RAM 权限与 AccessKey。'
        repository_open_failed = '已有仓库无法用该密码打开；原配置未更改。请使用原备份密码。'
        repository_init_failed = '加密仓库初始化未取得成功结果；原配置未更改。请返回聊天说明。'
        repository_missing = '已配置的远端仓库内容为空；为避免重建覆盖，本次已停止。请返回聊天说明。'
        password_change_forbidden = '已配置仓库必须使用原备份密码；本工具不执行密码替换。'
        busy = '备份或维护正在运行，请稍后重试。'
        maintenance_busy = '备份或维护正在运行，请稍后重试。'
        result_unknown_check_server = '等待超过 120 秒，结果需要检查。请返回聊天说明，勿发送任何密钥或密码。'
        ssh_connection_failed = '未取得服务器验证结果，请返回聊天说明。'
        csv_invalid = '所选 CSV 无法导入。请使用包含 AccessKey ID 和 AccessKey Secret 的原始下载文件，并确保只有一个账号的数据行；文件内容不会显示。'
        csv_too_large = '所选 CSV 超过 64 KiB，已停止导入。请选择原始下载的单账号 AccessKey CSV。'
        csv_dialog_unavailable = '本地文件选择窗口无法打开，请返回聊天说明；无需发送文件或密钥。'
        input_cancelled = '已取消，未提交任何密钥。'
    }
    $safeCode = [string]$_.Exception.Message
    if ($safeCode -eq 'input_cancelled') { $inputCancelled = $true }
    if ($safeMessages.ContainsKey($safeCode)) {
        Write-Host $safeMessages[$safeCode] -ForegroundColor Yellow
    } else {
        Write-Host '配置未完成，请返回聊天说明；无需发送密钥或密码。' -ForegroundColor Yellow
    }
} finally {
    $plainId = $null
    $plainSecret = $null
    $plainPassword = $null
    $plainConfirmation = $null
    $payload = $null
    if ($null -ne $sshProcess) {
        if ($sshStarted -and -not $sshProcess.HasExited) { $sshProcess.Kill() }
        $sshProcess.Dispose()
    }
    if (-not $CheckOnly -and -not $inputCancelled) { [void](Read-Host '按回车关闭窗口') }
}
exit $exitCode
