# =============================================================================
# Homebox One-Liner Installer (Windows)
# =============================================================================
# Usage:
#   irm https://raw.githubusercontent.com/aleontiev/homebox/main/homebox-infra/install.ps1 | iex
#
# Requires: PowerShell 5.1+ and Administrator privileges.
# =============================================================================

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/aleontiev/homebox.git"
$Branch = "main"
$HomeboxDir = "$env:USERPROFILE\homebox"

# ── Banner ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  Homebox - Self-hosted Internal PaaS" -ForegroundColor Cyan
Write-Host ""

# ── Admin check ──────────────────────────────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent() `
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "[WARN]  Re-launching as Administrator..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

# ── Docker ───────────────────────────────────────────────────────────────────
$dockerOk = $false
try {
    $null = docker info 2>$null
    $dockerOk = $true
    Write-Host "[INFO]  Docker is running: $(docker --version)" -ForegroundColor Green
} catch {}

if (-not $dockerOk) {
    $hasDocker = $false
    try { $null = Get-Command docker -ErrorAction Stop; $hasDocker = $true } catch {}

    if ($hasDocker) {
        Write-Host "[FAIL]  Docker is installed but not running. Start Docker Desktop and re-run." -ForegroundColor Red
        exit 1
    }

    Write-Host "[INFO]  Docker not found. Installing Docker Desktop..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
    } else {
        Write-Host "[FAIL]  winget not available. Install Docker Desktop manually:" -ForegroundColor Red
        Write-Host "        https://docker.com/products/docker-desktop" -ForegroundColor Red
        exit 1
    }
    Write-Host "[WARN]  Docker Desktop installed. Start it, then re-run this script:" -ForegroundColor Yellow
    Write-Host "        irm https://raw.githubusercontent.com/aleontiev/homebox/main/homebox-infra/install.ps1 | iex" -ForegroundColor Yellow
    exit 0
}

# ── Git ──────────────────────────────────────────────────────────────────────
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "[INFO]  Installing git..." -ForegroundColor Yellow
    winget install Git.Git --accept-package-agreements --accept-source-agreements
    $env:PATH = "$env:ProgramFiles\Git\cmd;$env:PATH"
}

# ── Download Homebox ─────────────────────────────────────────────────────────
$CloneDir = Join-Path $env:TEMP "homebox-install-$(Get-Random)"
Write-Host "[INFO]  Downloading Homebox..." -ForegroundColor Green
git clone --depth 1 --branch $Branch $RepoUrl $CloneDir 2>$null

# ── Create directories ──────────────────────────────────────────────────────
Write-Host "[INFO]  Creating directories under $HomeboxDir" -ForegroundColor Green
New-Item -ItemType Directory -Force -Path "$HomeboxDir\traefik" | Out-Null
New-Item -ItemType Directory -Force -Path "$HomeboxDir\projects" | Out-Null
New-Item -ItemType Directory -Force -Path "$HomeboxDir\base-infrastructure" | Out-Null

$SrcInfra = Join-Path $CloneDir "homebox-infra\host-provisioner\base-infrastructure"
Copy-Item "$SrcInfra\docker-compose.yml" "$HomeboxDir\base-infrastructure\" -Force
Copy-Item "$SrcInfra\.env.example"       "$HomeboxDir\base-infrastructure\" -Force
Copy-Item "$SrcInfra\dynamic_conf.yml"   "$HomeboxDir\traefik\" -Force

# ── Docker network ──────────────────────────────────────────────────────────
try { docker network inspect traefik-net 2>$null | Out-Null }
catch { docker network create traefik-net | Out-Null }
Write-Host "[INFO]  Docker network traefik-net ready." -ForegroundColor Green

# ── Interactive configuration ────────────────────────────────────────────────
Write-Host ""
Write-Host "── Domain Configuration ──" -ForegroundColor Cyan
$domain = Read-Host "Enter your root domain (e.g. example.com)"
if ([string]::IsNullOrWhiteSpace($domain)) {
    Write-Host "[FAIL]  Domain cannot be empty." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "── Dashboard Authentication ──" -ForegroundColor Cyan
$dashUser = Read-Host "Dashboard username [admin]"
if ([string]::IsNullOrWhiteSpace($dashUser)) { $dashUser = "admin" }

$dashPass = Read-Host "Dashboard password" -AsSecureString
$dashPassPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($dashPass))

Write-Host "[INFO]  Generating credentials..." -ForegroundColor Green
$hash = docker run --rm httpd:2-alpine htpasswd -nb $dashUser $dashPassPlain 2>$null
$hashEscaped = $hash -replace '\$', '$$'

# Write .env
$envContent = @"
HOMEBOX_DOMAIN=$domain
TRAEFIK_DASHBOARD_AUTH=$hashEscaped
TRAEFIK_DYNAMIC_CONF_DIR=$HomeboxDir\traefik
"@
$envContent | Set-Content "$HomeboxDir\base-infrastructure\.env" -Encoding UTF8
Write-Host "[INFO]  Environment file written." -ForegroundColor Green

# ── Cloudflare Tunnel (optional) ────────────────────────────────────────────
Write-Host ""
$setupTunnel = Read-Host "Set up a Cloudflare Tunnel? [y/N]"
if ($setupTunnel -match '^[Yy]') {
    if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
        Write-Host "[INFO]  Installing cloudflared..." -ForegroundColor Yellow
        winget install Cloudflare.cloudflared --accept-package-agreements --accept-source-agreements
    }

    Write-Host "[INFO]  Authenticating with Cloudflare (opens browser)..." -ForegroundColor Green
    cloudflared tunnel login

    $tunnelExists = cloudflared tunnel list 2>$null | Select-String "homebox"
    if ($tunnelExists) {
        Write-Host "[INFO]  Tunnel 'homebox' already exists." -ForegroundColor Green
    } else {
        cloudflared tunnel create homebox
    }

    $tunnelId = (cloudflared tunnel list 2>$null | Select-String "homebox" |
        ForEach-Object { ($_ -split '\s+')[0] })

    $cfDir = "$env:USERPROFILE\.cloudflared"
    New-Item -ItemType Directory -Force -Path $cfDir | Out-Null

    @"
tunnel: $tunnelId
credentials-file: $cfDir\$tunnelId.json

ingress:
  - hostname: "*.$domain"
    service: http://localhost:80
  - service: http_status:404
"@ | Set-Content "$cfDir\config.yml" -Encoding UTF8

    Write-Host "[INFO]  Tunnel config written to $cfDir\config.yml" -ForegroundColor Green

    cloudflared tunnel route dns homebox "*.$domain" 2>$null
    cloudflared service install 2>$null
    Write-Host "[DONE]  Cloudflare Tunnel configured." -ForegroundColor Green
}

# ── GitHub Actions Runner (optional) ────────────────────────────────────────
Write-Host ""
$setupRunner = Read-Host "Set up a GitHub Actions self-hosted runner? [y/N]"
if ($setupRunner -match '^[Yy]') {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Host "[INFO]  Installing GitHub CLI..." -ForegroundColor Yellow
        winget install GitHub.cli --accept-package-agreements --accept-source-agreements
    }

    if (-not (gh auth status 2>$null)) {
        gh auth login
    }

    $repoUrl = Read-Host "GitHub repository URL (e.g. https://github.com/owner/repo)"
    $ownerRepo = $repoUrl -replace 'https://github.com/' -replace '\.git$' -replace '/$'

    $token = gh api "repos/$ownerRepo/actions/runners/registration-token" -q '.token' 2>$null

    if ($token) {
        $runnerVersion = (gh api repos/actions/runner/releases/latest -q '.tag_name') -replace '^v'
        $runnerDir = "$HomeboxDir\actions-runner"
        New-Item -ItemType Directory -Force -Path $runnerDir | Out-Null

        $runnerUrl = "https://github.com/actions/runner/releases/download/v$runnerVersion/actions-runner-win-x64-$runnerVersion.zip"
        $runnerZip = "$env:TEMP\actions-runner.zip"

        Write-Host "[INFO]  Downloading runner v$runnerVersion..." -ForegroundColor Green
        Invoke-WebRequest -Uri $runnerUrl -OutFile $runnerZip
        Expand-Archive -Path $runnerZip -DestinationPath $runnerDir -Force

        Push-Location $runnerDir
        .\config.cmd --url $repoUrl --token $token --unattended --name "homebox-$env:COMPUTERNAME" --labels "homebox,self-hosted" --replace
        .\svc.cmd install
        .\svc.cmd start
        Pop-Location

        Write-Host "[DONE]  GitHub Actions runner installed and started." -ForegroundColor Green
    } else {
        Write-Host "[WARN]  Could not get registration token. Set up the runner manually." -ForegroundColor Yellow
    }
}

# ── Start infrastructure ────────────────────────────────────────────────────
Write-Host ""
Write-Host "── Starting Base Infrastructure ──" -ForegroundColor Cyan
Push-Location "$HomeboxDir\base-infrastructure"
docker compose --env-file .env up -d
Pop-Location

# ── Cleanup ──────────────────────────────────────────────────────────────────
Remove-Item -Recurse -Force $CloneDir -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host "  Homebox setup complete!" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "[INFO]  Dashboard: http://dashboard.$domain" -ForegroundColor Green
Write-Host "[INFO]  Install the developer CLI on your workstation:" -ForegroundColor Green
Write-Host "        pip install ./homebox-infra/cli" -ForegroundColor White
Write-Host "        homebox init" -ForegroundColor White
Write-Host ""
