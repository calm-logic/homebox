# =============================================================================
# Homebox One-Liner Installer (Windows)
# =============================================================================
# Usage (any PowerShell prompt, no Administrator required):
#   powershell -ExecutionPolicy Bypass -c "irm https://homebox.sh/install.ps1 | iex"
#   # (mirror: irm https://raw.githubusercontent.com/calm-logic/homebox/master/host/install.ps1 | iex)
#
# Uninstall - parameters cannot cross an `irm | iex` pipe, so invoke the
# downloaded script text as a scriptblock (or download the file and run it):
#   powershell -ExecutionPolicy Bypass -c "& ([scriptblock]::Create((irm https://homebox.sh/install.ps1))) -Uninstall"
#   powershell -ExecutionPolicy Bypass -c "& ([scriptblock]::Create((irm https://homebox.sh/install.ps1))) -Uninstall -Yes -Purge"
#   .\install.ps1 -Uninstall [-Yes] [-Purge]
#
#   -Uninstall  remove Homebox from the WSL distro instead of installing
#   -Yes        skip the confirmation prompt (required for unattended runs)
#   -Purge      also delete docker volumes (databases!), ~/.homebox secrets,
#               and Homebox docker images inside WSL
#
# Homebox runs inside WSL2 using Docker Desktop's WSL integration. This is a
# thin bootstrapper that:
#   1. Checks Windows 10/11
#   2. Checks Docker Desktop is installed and running
#   3. Checks a WSL2 distro exists and Docker integration is enabled in it
#   4. Runs the canonical installer (https://homebox.sh/install.sh) inside
#      your default WSL distro (its output streams here, including the
#      first-run admin password)
#   5. Opens the admin UI (http://localhost:7765) from Windows
#
# Safe to re-run at any time.
# =============================================================================
param(
    [switch]$Uninstall,
    [switch]$Yes,
    [switch]$Purge
)

$ErrorActionPreference = "Stop"
$env:WSL_UTF8 = "1"  # make wsl.exe emit UTF-8 so its output parses cleanly

function Write-Info([string]$msg) { Write-Host "[INFO]  $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function Write-Fail([string]$msg) { Write-Host "[FAIL]  $msg" -ForegroundColor Red }
function Write-Step([string]$msg) { Write-Host ""; Write-Host "-- $msg --" -ForegroundColor Cyan }

function Install-Homebox {
    $script:ExitCode = 1

    Write-Host ""
    Write-Host "  Homebox - Self-hosted Internal PaaS" -ForegroundColor Cyan
    Write-Host ""

    # -- Step 1/4: Windows preflight ------------------------------------------
    Write-Step "Step 1/4: Windows preflight"
    $os = [Environment]::OSVersion.Version
    if ($os.Major -lt 10) {
        Write-Fail "Windows 10 or 11 is required (detected version $os)."
        return
    }
    Write-Info "Windows $($os.Major) (build $($os.Build)) detected."

    # -- Step 2/4: Docker Desktop ----------------------------------------------
    Write-Step "Step 2/4: Docker Desktop"
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Fail "Docker Desktop is not installed."
        Write-Host ""
        Write-Host "  Install it (with the WSL 2 backend, the default) from:"
        Write-Host "      https://www.docker.com/products/docker-desktop/" -ForegroundColor White
        Write-Host "  or:"
        Write-Host "      winget install Docker.DockerDesktop" -ForegroundColor White
        Write-Host ""
        Write-Host "  Then start Docker Desktop and re-run this installer:"
        Write-Host '      powershell -ExecutionPolicy Bypass -c "irm https://homebox.sh/install.ps1 | iex"' -ForegroundColor White
        return
    }
    cmd /c "docker info >NUL 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Docker is installed but not running."
        Write-Host "  Start Docker Desktop, wait until it says 'Engine running', then re-run this installer."
        return
    }
    Write-Info "Docker is running: $(docker --version)"

    # -- Step 3/4: WSL2 ----------------------------------------------------------
    Write-Step "Step 3/4: WSL2"
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        Write-Fail "WSL (Windows Subsystem for Linux) is not available."
        Write-Host ""
        Write-Host "  Install it with this one command (a reboot may be required):"
        Write-Host ""
        Write-Host "      wsl --install -d Ubuntu" -ForegroundColor White
        Write-Host ""
        Write-Host "  After it finishes (create your Linux username/password when asked),"
        Write-Host "  re-run this installer."
        return
    }

    $distros = @()
    try {
        $distros = @(cmd /c "wsl.exe -l -q 2>NUL" | ForEach-Object { ($_ -replace "`0", "").Trim() } | Where-Object { $_ })
    } catch {}
    if ($distros.Count -eq 0) {
        Write-Fail "No WSL distro is installed."
        Write-Host ""
        Write-Host "  Install Ubuntu with this one command (a reboot may be required):"
        Write-Host ""
        Write-Host "      wsl --install -d Ubuntu" -ForegroundColor White
        Write-Host ""
        Write-Host "  After it finishes (create your Linux username/password when asked),"
        Write-Host "  re-run this installer."
        return
    }
    Write-Info "WSL distro(s) found: $($distros -join ', ')"

    # Docker integration: Windows docker works (checked above) - verify docker
    # is also reachable inside the default WSL distro.
    cmd /c "wsl.exe -e docker info >NUL 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Docker works on Windows but is not available inside your default WSL distro."
        Write-Host ""
        Write-Host "  Fix: Docker Desktop -> Settings -> Resources -> WSL integration ->"
        Write-Host "  turn ON 'Enable integration with my default WSL distro' (and the"
        Write-Host "  toggle for your distro), click 'Apply & restart', then re-run this"
        Write-Host "  installer."
        Write-Host ""
        Write-Host "  If your distro is WSL1, convert it to WSL2 first:"
        Write-Host "      wsl --set-version <distro-name> 2" -ForegroundColor White
        return
    }
    Write-Info "Docker Desktop WSL integration is enabled."

    # -- Step 4/4: Install inside WSL --------------------------------------------
    Write-Step "Step 4/4: Installing Homebox inside WSL"
    Write-Info "Running the canonical installer in your default WSL distro."
    Write-Warn "You may be asked for your WSL (Linux) sudo password."
    Write-Host ""

    # HOMEBOX_NO_BROWSER=1: the in-WSL browser open is skipped; we open the
    # admin from Windows below (WSL2 forwards localhost automatically).
    wsl.exe -e bash -lc "curl -fsSL https://homebox.sh/install.sh | HOMEBOX_NO_BROWSER=1 bash"
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Fail "The installer exited with code $LASTEXITCODE (see output above)."
        Write-Host "  Fix the reported problem and re-run this installer - it is safe to run repeatedly."
        return
    }

    # -- Open the admin UI from Windows ------------------------------------------
    Write-Host ""
    Write-Info "Opening the Homebox admin: http://localhost:7765"
    try {
        Start-Process "http://localhost:7765"
    } catch {
        Write-Warn "Could not open a browser automatically - open http://localhost:7765 yourself."
    }
    Write-Info "Log in with the first-run password printed above."

    $script:ExitCode = 0
}

function Uninstall-Homebox {
    $script:ExitCode = 1

    Write-Host ""
    Write-Host "  Homebox - Self-hosted Internal PaaS (uninstall)" -ForegroundColor Cyan
    Write-Host ""

    # -- Step 1/2: preflight (lighter than install: docker + WSL just need to
    # be present; the canonical uninstaller degrades gracefully inside WSL) ----
    Write-Step "Step 1/2: Preflight"
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        Write-Fail "WSL is not available - nothing to uninstall (Homebox runs inside WSL)."
        return
    }
    $distros = @()
    try {
        $distros = @(cmd /c "wsl.exe -l -q 2>NUL" | ForEach-Object { ($_ -replace "`0", "").Trim() } | Where-Object { $_ })
    } catch {}
    if ($distros.Count -eq 0) {
        Write-Fail "No WSL distro is installed - nothing to uninstall."
        return
    }
    Write-Info "WSL distro(s) found: $($distros -join ', ')"
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Warn "Docker Desktop not found - container cleanup inside WSL will be skipped."
    } else {
        cmd /c "docker info >NUL 2>&1"
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Docker is installed but not running - start Docker Desktop first for a"
            Write-Warn "complete cleanup (containers/volumes are otherwise left in place)."
        }
    }

    # -- Step 2/2: run the canonical uninstaller inside WSL -----------------------
    Write-Step "Step 2/2: Uninstalling Homebox inside WSL"
    $flags = "--uninstall"
    if ($Yes)   { $flags = "$flags --yes" }
    if ($Purge) { $flags = "$flags --purge" }
    if (-not $Yes) {
        Write-Info "You will be asked to confirm inside WSL (pass -Yes to skip)."
    }
    Write-Warn "You may be asked for your WSL (Linux) sudo password."
    Write-Host ""

    wsl.exe -e bash -lc "curl -fsSL https://homebox.sh/install.sh | bash -s -- $flags"
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Fail "The uninstaller exited with code $LASTEXITCODE (see output above)."
        return
    }

    Write-Host ""
    Write-Info "Homebox has been removed from your WSL distro."
    Write-Info "Docker Desktop itself and the WSL distro were left untouched."
    if (-not $Purge) {
        Write-Info "Data volumes and ~/.homebox secrets were kept (re-run with -Purge to delete them)."
    }

    $script:ExitCode = 0
}

$script:ExitCode = 0
if ($Uninstall) {
    Uninstall-Homebox
} else {
    Install-Homebox
}
# Only call `exit` when running as a script file; under `irm | iex` an exit
# would close the user's interactive shell.
if ($PSCommandPath -and $script:ExitCode -ne 0) {
    exit $script:ExitCode
}
