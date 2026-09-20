<#
.SYNOPSIS
    一次性配置代码验证虚拟机（Ubuntu）的 SSH 免密登录。

.DESCRIPTION
    航天嵌入式代码验证流水线的所有编译 / 运行 / 覆盖率采集都在远端 Linux 上执行，
    Windows 侧只做编排。本脚本负责把这条通道建起来：

      1. 生成专用密钥对（默认 ~/.ssh/id_ed25519_workbuddy_verify），已存在则复用；
      2. ssh-keyscan 把主机公钥钉进 known_hosts（StrictHostKeyChecking=yes，防中间人）；
      3. 用密码把本地公钥装进远端 authorized_keys —— 这是全流程唯一一次用密码；
      4. 关掉远端密码登录后仍能用密钥登入，并打印 gcc / gcov / make 版本用于确认工具链。

    密码只经由环境变量 SSH_ASKPASS 交给 OpenSSH，不写入仓库、不落 .env、不进命令行历史
    （用 -Password 传参会留在 PowerShell 历史里，交互式输入更安全）。
    配置完成后建议修改虚拟机密码：此后系统不再需要它。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup_verify_vm.ps1
    powershell -ExecutionPolicy Bypass -File scripts\setup_verify_vm.ps1 -VerifyHost 192.168.207.128 -VerifyUser lixf
#>
[CmdletBinding()]
param(
    [string]$VerifyHost,
    [string]$VerifyUser,
    [int]$Port = 22,
    [string]$KeyPath,
    [string]$Workdir = "wb_verify",
    [string]$Password
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

# OpenSSH 把横幅、进度等信息写到 stderr；在 ErrorActionPreference=Stop 下 PowerShell
# 会把原生命令的 stderr 当成终止性错误抛出。这里统一经 Invoke-Native 调用：
# 临时降级为 Continue，把 stdout/stderr 一并收回，退出码交给调用方判断。
function Invoke-Native {
    # 参数名不能用 $Args：那是 PowerShell 的自动变量，遮蔽后 splat 会拿到空数组
    param([string]$Exe, [string[]]$NativeArgs)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @NativeArgs 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    return [pscustomobject]@{ Output = @($out); ExitCode = $code }
}

# ---- 缺省值：先看 .env（与运行时读的是同一份配置），再退回内置默认 ----
function Read-EnvFile([string]$path) {
    $map = @{}
    if (Test-Path $path) {
        foreach ($line in Get-Content $path) {
            if ($line -match '^\s*#') { continue }
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
                $map[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
            }
        }
    }
    return $map
}

$envMap = Read-EnvFile (Join-Path $repo ".env")
if (-not $VerifyHost) { $VerifyHost = $envMap["VERIFY_HOST"] }
if (-not $VerifyUser) { $VerifyUser = $envMap["VERIFY_USER"] }
if (-not $KeyPath)    { $KeyPath    = $envMap["VERIFY_KEY"] }
if (-not $VerifyHost) { $VerifyHost = "192.168.207.128" }
if (-not $VerifyUser) { $VerifyUser = $env.CurrentUser }
if (-not $VerifyUser) { $VerifyUser = "lixf" }
if (-not $KeyPath)    { $KeyPath    = Join-Path $env:USERPROFILE ".ssh\id_ed25519_workbuddy_verify" }
if ($envMap["VERIFY_PORT"]) { $Port = [int]$envMap["VERIFY_PORT"] }
if ($envMap["VERIFY_WORKDIR"]) { $Workdir = $envMap["VERIFY_WORKDIR"] }

Write-Host "验证机：$VerifyUser@$VerifyHost`:$Port" -ForegroundColor Cyan
Write-Host "密钥　：$KeyPath"
Write-Host "工作区：~/$Workdir"
Write-Host ""

# ---- 1. 密钥对 ----
$sshDir = Split-Path -Parent $KeyPath
if (-not (Test-Path $sshDir)) { New-Item -ItemType Directory -Path $sshDir -Force | Out-Null }
if (Test-Path $KeyPath) {
    Write-Host "[1/4] 密钥已存在，复用" -ForegroundColor Green
} else {
    # -N '""' 经 PowerShell 传到原生命令后是空字符串，即无口令密钥
    $r = Invoke-Native "ssh-keygen" @("-t", "ed25519", "-N", '""', "-C", "workbuddy-verify", "-f", $KeyPath)
    if ($r.ExitCode -ne 0) { throw "ssh-keygen 失败（退出码 $($r.ExitCode)）：$($r.Output -join ' ')" }
    Write-Host "[1/4] 已生成 ed25519 密钥" -ForegroundColor Green
}
$pubPath = "$KeyPath.pub"
if (-not (Test-Path $pubPath)) { throw "找不到公钥 $pubPath" }
$pubKey = (Get-Content $pubPath -Raw).Trim()

# ---- 2. 钉住主机公钥 ----
$knownHosts = Join-Path $sshDir "known_hosts"
$bracket = "[$VerifyHost]:$Port"
$target = if ($Port -eq 22) { $VerifyHost } else { $bracket }
Invoke-Native "ssh-keygen" @("-R", $target, "-f", $knownHosts) | Out-Null
$scan = Invoke-Native "ssh-keyscan" @("-p", "$Port", "-T", "8", $VerifyHost)
# keyscan 的横幅注释行走 stderr，只取真正的公钥行（host keytype base64）
$scanned = @($scan.Output | Where-Object { $_ -match '^\S+ (ssh-rsa|ssh-ed25519|ssh-dss|ecdsa-[\w-]+) ' })
if (-not $scanned) { throw "ssh-keyscan 拿不到 $VerifyHost 的主机公钥，请确认网络与端口" }
$scanned | ForEach-Object {
    if ($Port -ne 22) { $_ -replace "^$([regex]::Escape($VerifyHost)) ", "$bracket " } else { $_ }
} | Add-Content -Path $knownHosts -Encoding ascii
Write-Host "[2/4] 主机公钥已写入 known_hosts（$($scanned.Count) 条）" -ForegroundColor Green

# ---- 3. 用密码装公钥（唯一一次用密码）----
$sshBase = @(
    "-p", "$Port",
    "-i", $KeyPath,
    "-o", "UserKnownHostsFile=$knownHosts",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ConnectTimeout=10"
)

function Test-KeyAuth {
    $r = Invoke-Native "ssh" ($sshBase + @("-o", "BatchMode=yes", "-o", "NumberOfPasswordPrompts=0",
        "$VerifyUser@$VerifyHost", "echo WB_KEY_OK"))
    return ($r.ExitCode -eq 0 -and ($r.Output -join "`n") -match "WB_KEY_OK")
}

if (Test-KeyAuth) {
    Write-Host "[3/4] 密钥登录已可用，跳过密码安装" -ForegroundColor Green
} else {
    if (-not $Password) {
        $sec = Read-Host "请输入 $VerifyUser@$VerifyHost 的密码（仅本次安装公钥使用，不会保存）" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try { $Password = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }

    # Windows OpenSSH 没有 sshpass，用 SSH_ASKPASS + SSH_ASKPASS_REQUIRE=force 喂密码：
    # ssh 在无终端可交互时会转而 spawn 这个程序取密码。必须是 .cmd/.exe，
    # .vbs 会被 CreateProcessW 拒绝（error 193）。
    $askpass = Join-Path $env:TEMP "wb_askpass_$PID.cmd"
    Set-Content -Path $askpass -Value "@echo off`r`necho $Password" -Encoding ascii

    # 远端用 sh 执行：装公钥（幂等）+ 建工作区。
    # 注意 ssh 会把所有命令参数用空格拼成一串交给登录 shell，额外传的实参会变成
    # 命令尾部文本而不是 $1，所以公钥必须直接嵌进命令串（base64 里不含单引号，安全）。
    $remoteCmd = 'umask 077; mkdir -p ~/.ssh; chmod 700 ~/.ssh; touch ~/.ssh/authorized_keys; ' +
                 'K=' + "'" + $pubKey + "'" + '; ' +
                 'grep -qxF "$K" ~/.ssh/authorized_keys || echo "$K" >> ~/.ssh/authorized_keys; ' +
                 'chmod 600 ~/.ssh/authorized_keys; ' +
                 'mkdir -p ~/' + $Workdir + '; echo WB_INSTALL_OK'

    $envOld = @{}
    foreach ($k in @("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "DISPLAY")) {
        $envOld[$k] = [Environment]::GetEnvironmentVariable($k)
    }
    try {
        $env:SSH_ASKPASS = $askpass
        $env:SSH_ASKPASS_REQUIRE = "force"
        $env:DISPLAY = "localhost:0"
        $r = Invoke-Native "ssh" ($sshBase + @("-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1",
            "$VerifyUser@$VerifyHost", $remoteCmd))
        $out = $r.Output
    } finally {
        foreach ($k in $envOld.Keys) {
            if ($null -eq $envOld[$k]) { Remove-Item "Env:\$k" -ErrorAction SilentlyContinue }
            else { Set-Item "Env:\$k" $envOld[$k] }
        }
        Remove-Item $askpass -Force -ErrorAction SilentlyContinue
    }
    $Password = $null

    if (-not (($out -join "`n") -match "WB_INSTALL_OK")) {
        Write-Host ($out -join "`n") -ForegroundColor Red
        throw "公钥安装失败，请检查密码或改用 -Password 参数重试"
    }
    Write-Host "[3/4] 公钥已安装到远端 authorized_keys" -ForegroundColor Green
}

# ---- 4. 校验密钥登录 + 工具链 ----
if (-not (Test-KeyAuth)) { throw "密钥登录校验失败" }
$probeCmd = 'echo "uname=$(uname -srm)"; echo "cores=$(nproc)"; ' +
    'echo "gcc=$(gcc --version 2>/dev/null | head -1)"; ' +
    'echo "gcov=$(gcov --version 2>/dev/null | head -1)"; ' +
    'echo "make=$(make --version 2>/dev/null | head -1)"; ' +
    'echo "tar=$(tar --version 2>/dev/null | head -1)"; ' +
    'echo "workdir=$HOME/' + $Workdir + '"'
$probe = Invoke-Native "ssh" ($sshBase + @("-o", "BatchMode=yes", "$VerifyUser@$VerifyHost", $probeCmd))
Write-Host "[4/4] 免密登录校验通过，工具链：" -ForegroundColor Green
$probe.Output | ForEach-Object { Write-Host "      $_" }

Write-Host ""
Write-Host "把下面几行写进 .env（密钥路径用正斜杠或双反斜杠）：" -ForegroundColor Yellow
Write-Host "  VERIFY_HOST=$VerifyHost"
Write-Host "  VERIFY_USER=$VerifyUser"
Write-Host "  VERIFY_PORT=$Port"
Write-Host "  VERIFY_KEY=$($KeyPath -replace '\\','/')"
Write-Host "  VERIFY_WORKDIR=$Workdir"
Write-Host ""
Write-Host "建议现在修改虚拟机密码：本系统此后只用密钥登录。" -ForegroundColor Yellow
