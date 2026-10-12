<#
  Plex Sonic Doctor installer for Windows.

  Run from the downloaded folder:
      powershell -ExecutionPolicy Bypass -File install\install-windows.ps1

  It checks for Python 3.10+ and ffmpeg (offering to install them with winget), installs the app
  into its own private Python environment, and adds the `sonicdoctor` command to your PATH.
  Nothing outside %LOCALAPPDATA%\PlexSonicDoctor is changed, apart from the PATH entry.
#>
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\PlexSonicDoctor\app",
    [switch]$NoPath,      # don't add the command to PATH
    [switch]$NoShortcut,  # don't add a Start-menu shortcut
    [switch]$Yes          # answer yes to every question
)
$ErrorActionPreference = "Stop"
$Source = Split-Path -Parent $PSScriptRoot

function Ask($question) {
    if ($Yes) { return $true }
    $a = Read-Host "$question [Y/n]"
    return ($a -eq "" -or $a -match "^[Yy]")
}

function Find-Python {
    foreach ($cmd in @("py -3", "python", "python3")) {
        $parts = $cmd.Split(" ")
        try {
            $v = & $parts[0] $parts[1..9] -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.10") { return $parts }
        } catch {}
    }
    return $null
}

Write-Host "`nPlex Sonic Doctor installer`n"

$py = Find-Python
if (-not $py) {
    Write-Host "Python 3.10 or newer isn't installed."
    if (Ask "Install Python 3.12 with winget now?") {
        winget install --id Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        $py = Find-Python
    }
    if (-not $py) { throw "Python 3.10+ is needed. Install it from https://www.python.org and run this again." }
}
Write-Host "Python: OK"

$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    Write-Host "ffmpeg isn't installed. It's needed to fingerprint the audio."
    if (Ask "Install ffmpeg (Gyan full build, includes Chromaprint) with winget now?") {
        winget install --id Gyan.FFmpeg --exact --accept-package-agreements --accept-source-agreements
        Write-Host "ffmpeg installed. Open a new terminal afterwards so Windows finds it."
    } else {
        Write-Host "Skipping. You can install it later:  winget install Gyan.FFmpeg"
    }
} else {
    $muxers = & ffmpeg -hide_banner -muxers 2>$null | Out-String
    if ($muxers -notmatch "chromaprint") {
        Write-Host "Your ffmpeg has no Chromaprint. Either install the full build (winget install Gyan.FFmpeg)"
        Write-Host "or Chromaprint's fpcalc tool (winget install AcoustID.Chromaprint)."
    } else { Write-Host "ffmpeg with Chromaprint: OK" }
}

Write-Host "Installing into $InstallDir ..."
& $py[0] $py[1..9] -m venv "$InstallDir"
& "$InstallDir\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$InstallDir\Scripts\python.exe" -m pip install --quiet "$Source"
if ($LASTEXITCODE -ne 0) { throw "Installing the app failed." }

$scripts = "$InstallDir\Scripts"
if (-not $NoPath) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($userPath -split ";") -notcontains $scripts) {
        [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $scripts), "User")
        Write-Host "Added the sonicdoctor command to your PATH (open a new terminal to use it)."
    }
}
& "$scripts\sonicdoctor.exe" --version

if (-not $NoShortcut) {
    # A Start-menu shortcut that opens the app in the web browser (no typing needed).
    $lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "Plex Sonic Doctor.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = "$scripts\sonicdoctor.exe"
    $s.Arguments = "gui"
    $s.WorkingDirectory = $InstallDir
    $s.Description = "Find duplicate, different-version and broken tracks in Plex"
    $s.Save()
    Write-Host "Added 'Plex Sonic Doctor' to the Start menu."
}
Write-Host "`nInstalled. Open 'Plex Sonic Doctor' from the Start menu (or run  sonicdoctor gui  in a new terminal)."
Write-Host "To uninstall: delete $InstallDir and the Start-menu shortcut, and remove it from your PATH (Settings > System > About > Advanced > Environment Variables)."
