Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LauncherRoot = Join-Path $ScriptRoot ".launcher"
$RegistryPath = Join-Path $LauncherRoot "bots.json"
$InstanceDir = Join-Path $LauncherRoot "instances"
$VenvPython = Join-Path $ScriptRoot "venv\Scripts\python.exe"
$PowerShellExe = (Get-Command powershell).Source

function Write-Title {
    param([string]$Text)
    Write-Host ""
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Write-Utf8NoBomFile {
    param(
        [string]$Path,
        [string]$Content
    )

    $directory = Split-Path -Parent $Path
    if ($directory -and -not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}
function Ensure-LauncherStorage {
    if (-not (Test-Path -LiteralPath $LauncherRoot)) {
        New-Item -ItemType Directory -Path $LauncherRoot | Out-Null
    }
    if (-not (Test-Path -LiteralPath $InstanceDir)) {
        New-Item -ItemType Directory -Path $InstanceDir | Out-Null
    }
    if (-not (Test-Path -LiteralPath $RegistryPath)) {
        @{
            version = 1
            bots = @()
        } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $RegistryPath -Encoding UTF8
    }
}

function Get-Registry {
    Ensure-LauncherStorage
    $raw = Get-Content -LiteralPath $RegistryPath -Raw
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return [pscustomobject]@{
            version = 1
            bots = @()
        }
    }

    $parsed = $raw | ConvertFrom-Json
    if (-not $parsed) {
        return [pscustomobject]@{
            version = 1
            bots = @()
        }
    }
    if (-not ($parsed.PSObject.Properties.Name -contains "bots") -or $null -eq $parsed.bots) {
        $parsed.bots = @()
    }
    return $parsed
}

function Save-Registry {
    param($Registry)
    $Registry | ConvertTo-Json -Depth 8 | ForEach-Object { Write-Utf8NoBomFile -Path $RegistryPath -Content $_ }
}

function Resolve-WorkspacePath {
    param([string]$InputPath)

    if ([string]::IsNullOrWhiteSpace($InputPath)) {
        throw "Workspace path cannot be empty."
    }

    $value = $InputPath.Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    $value = [Environment]::ExpandEnvironmentVariables($value)

    if ($value -eq "~") {
        $value = $HOME
    } elseif ($value.StartsWith("~/") -or $value.StartsWith('~\')) {
        $suffix = $value.Substring(2).Replace("/", '\')
        $value = Join-Path $HOME $suffix
    } elseif ($value -eq '$HOME') {
        $value = $HOME
    } elseif ($value -like '$HOME\*' -or $value -like '$HOME/*') {
        $suffix = $value.Substring(6).Replace("/", '\')
        $value = Join-Path $HOME $suffix
    }

    if (-not [System.IO.Path]::IsPathRooted($value)) {
        $value = Join-Path (Get-Location).Path $value
    }

    return [System.IO.Path]::GetFullPath($value)
}

function Read-Port {
    param([string]$Prompt)

    while ($true) {
        $raw = Read-Host $Prompt
        $port = 0
        if ([int]::TryParse($raw, [ref]$port) -and $port -ge 1 -and $port -le 65535) {
            return $port
        }
        Write-Host "Invalid port. Enter a number from 1 to 65535." -ForegroundColor Yellow
    }
}

function Test-PortFree {
    param([int]$Port)

    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) {
            $listener.Stop()
        }
    }
}

function Quote-PowerShell {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function New-InstanceId {
    return "bot-" + (Get-Date -Format "yyyyMMdd-HHmmss")
}

function Ensure-VenvPython {
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        throw "Missing $VenvPython. Create the venv and install nanobot first."
    }
}

function Sync-BotRuntimeConfig {
    param(
        [string]$ConfigPath,
        [string]$WorkspacePath,
        [int]$WebPort
    )

    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        throw "Config not found: $ConfigPath"
    }

    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    if (-not $config.gateway) {
        $config | Add-Member -MemberType NoteProperty -Name gateway -Value ([pscustomobject]@{})
    }
    if (-not $config.gateway.web) {
        $config.gateway | Add-Member -MemberType NoteProperty -Name web -Value ([pscustomobject]@{})
    }
    if (-not $config.agents) {
        $config | Add-Member -MemberType NoteProperty -Name agents -Value ([pscustomobject]@{})
    }
    if (-not $config.agents.defaults) {
        $config.agents | Add-Member -MemberType NoteProperty -Name defaults -Value ([pscustomobject]@{})
    }

    $config.gateway.web.enabled = $true
    $config.gateway.web.port = $WebPort
    $config.agents.defaults.workspace = $WorkspacePath

    $config | ConvertTo-Json -Depth 20 | ForEach-Object { Write-Utf8NoBomFile -Path $ConfigPath -Content $_ }
}
function Initialize-BotConfig {
    param(
        [string]$ConfigPath,
        [string]$WorkspacePath,
        [int]$WebPort
    )

    Ensure-VenvPython

    & $VenvPython -m nanobot.cli.commands onboard --config $ConfigPath --workspace $WorkspacePath
    if ($LASTEXITCODE -ne 0) {
        throw "nanobot onboard failed."
    }

    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    if (-not $config.gateway) {
        $config | Add-Member -MemberType NoteProperty -Name gateway -Value ([pscustomobject]@{})
    }
    if (-not $config.gateway.web) {
        $config.gateway | Add-Member -MemberType NoteProperty -Name web -Value ([pscustomobject]@{})
    }
    if (-not $config.agents) {
        $config | Add-Member -MemberType NoteProperty -Name agents -Value ([pscustomobject]@{})
    }
    if (-not $config.agents.defaults) {
        $config.agents | Add-Member -MemberType NoteProperty -Name defaults -Value ([pscustomobject]@{})
    }

    $config.gateway.web.enabled = $true
    $config.gateway.web.port = $WebPort
    $config.agents.defaults.workspace = $WorkspacePath

    $config | ConvertTo-Json -Depth 20 | ForEach-Object { Write-Utf8NoBomFile -Path $ConfigPath -Content $_ }
}

function Select-Bot {
    param([array]$Bots)

    if ($Bots.Count -eq 0) {
        Write-Host "No saved bots yet." -ForegroundColor Yellow
        return $null
    }

    Write-Title "Saved bots"
    for ($i = 0; $i -lt $Bots.Count; $i++) {
        $bot = $Bots[$i]
        Write-Host ("[{0}] {1}" -f ($i + 1), $bot.name) -ForegroundColor Green
        Write-Host ("     Workspace: {0}" -f $bot.workspace)
        Write-Host ("     Web port : {0}" -f $bot.web_port)
    }

    while ($true) {
        $choice = Read-Host "Choose bot number"
        $index = 0
        if ([int]::TryParse($choice, [ref]$index) -and $index -ge 1 -and $index -le $Bots.Count) {
            return $Bots[$index - 1]
        }
        Write-Host "Invalid choice." -ForegroundColor Yellow
    }
}

function Start-Bot {
    param(
        $Registry,
        $Bot
    )

    Ensure-VenvPython

    Sync-BotRuntimeConfig -ConfigPath $Bot.config_path -WorkspacePath $Bot.workspace -WebPort ([int]$Bot.web_port)
    $Bot.last_run_at = (Get-Date).ToString("s")
    Save-Registry $Registry

    $command = "& { Set-Location " + (Quote-PowerShell $ScriptRoot) + "; & " + (Quote-PowerShell $VenvPython) + " -m nanobot.cli.commands gateway --web --config " + (Quote-PowerShell $Bot.config_path) + " --workspace " + (Quote-PowerShell $Bot.workspace) + " }"

    Start-Process -FilePath $PowerShellExe -WorkingDirectory $ScriptRoot -ArgumentList @(
        "-NoExit",
        "-Command",
        $command
    ) | Out-Null

    Write-Host ""
    Write-Host ("Started bot '{0}' in a new PowerShell window." -f $Bot.name) -ForegroundColor Green
    Write-Host ("Workspace: {0}" -f $Bot.workspace)
    Write-Host ("Web dashboard: http://127.0.0.1:{0}" -f $Bot.web_port)
}

function Create-NewBot {
    $registry = Get-Registry
    $bots = @($registry.bots)

    Write-Title "Create new bot"
    $workspaceInput = Read-Host 'Workspace path (supports absolute, relative, ~, $HOME)'
    $workspacePath = Resolve-WorkspacePath $workspaceInput
    $defaultName = Split-Path -Leaf $workspacePath
    if ([string]::IsNullOrWhiteSpace($defaultName)) {
        $defaultName = "nanobot"
    }

    foreach ($existing in $bots) {
        if ($existing.workspace -eq $workspacePath) {
            Write-Host "This workspace already exists in the bot list." -ForegroundColor Yellow
            Write-Host ("Existing bot: {0} (web port {1})" -f $existing.name, $existing.web_port)
            return
        }
    }

    $name = Read-Host "Bot name (Enter for '$defaultName')"
    if ([string]::IsNullOrWhiteSpace($name)) {
        $name = $defaultName
    }

    $webPort = Read-Port "Web port"
    if (-not (Test-PortFree $webPort)) {
        Write-Host "That port is already busy or cannot be bound." -ForegroundColor Yellow
        $continue = Read-Host "Continue anyway? (y/N)"
        if ($continue -notin @("y", "Y")) {
            return
        }
    }

    foreach ($existing in $bots) {
        if ([int]$existing.web_port -eq $webPort) {
            Write-Host ("Warning: this web port is already saved for bot '{0}'." -f $existing.name) -ForegroundColor Yellow
            $continue = Read-Host "Continue anyway? (y/N)"
            if ($continue -notin @("y", "Y")) {
                return
            }
            break
        }
    }

    $instanceId = New-InstanceId
    $configDir = Join-Path $InstanceDir $instanceId
    $configPath = Join-Path $configDir "config.json"
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null

    Initialize-BotConfig -ConfigPath $configPath -WorkspacePath $workspacePath -WebPort $webPort

    $record = [pscustomobject]@{
        id = $instanceId
        name = $name
        workspace = $workspacePath
        config_path = $configPath
        web_port = $webPort
        created_at = (Get-Date).ToString("s")
        last_run_at = $null
    }

    $registry.bots = @($bots + $record)
    Save-Registry $registry

    Write-Host ""
    Write-Host "New bot created." -ForegroundColor Green
    Write-Host ("Name      : {0}" -f $name)
    Write-Host ("Workspace : {0}" -f $workspacePath)
    Write-Host ("Config    : {0}" -f $configPath)
    Write-Host ("Web port  : {0}" -f $webPort)

    $startNow = Read-Host "Start this bot now? (Y/n)"
    if ($startNow -notin @("n", "N")) {
        Start-Bot -Registry $registry -Bot $record
    }
}

function Run-ExistingBot {
    $registry = Get-Registry
    $bot = Select-Bot -Bots @($registry.bots)
    if ($null -eq $bot) {
        return
    }
    Start-Bot -Registry $registry -Bot $bot
}

function Show-MainMenu {
    while ($true) {
        Write-Title "Nanobot Launcher"
        Write-Host "Setup hint:" -ForegroundColor DarkGray
        Write-Host "  python -m venv venv" -ForegroundColor DarkGray
        Write-Host "  .\venv\Scripts\Activate.ps1" -ForegroundColor DarkGray
        Write-Host "  pip install -e "".""" -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "[1] Create new bot"
        Write-Host "[2] Run existing bot"
        Write-Host "[3] Exit"

        $choice = Read-Host "Choose action"
        switch ($choice) {
            "1" {
                Create-NewBot
                return
            }
            "2" {
                Run-ExistingBot
                return
            }
            "3" {
                return
            }
            default {
                Write-Host "Invalid choice." -ForegroundColor Yellow
            }
        }
    }
}

try {
    Show-MainMenu
} catch {
    Write-Host ""
    Write-Host ("Error: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}



