$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path .venv\Scripts\python.exe)) {
  py -3 -m venv .venv
}
& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install -r backend\requirements.txt pyinstaller
& .venv\Scripts\python.exe -m PyInstaller --clean --noconfirm packaging\backend.spec
New-Item -ItemType Directory -Force src-tauri\binaries | Out-Null
Copy-Item dist\cheat-clip-pro-backend.exe src-tauri\binaries\cheat-clip-pro-backend-x86_64-pc-windows-msvc.exe -Force
New-Item -ItemType Directory -Force src-tauri\resources | Out-Null
if (Test-Path backend\fonts) { Copy-Item -Recurse -Force backend\fonts src-tauri\resources\fonts }

npm ci
npm run build
npx tauri build --bundles nsis

$exe = Get-ChildItem src-tauri\target\release\bundle\nsis\*.exe | Select-Object -First 1
if (-not $exe) { throw "Tauri installer was not produced" }
Write-Host "Produced $($exe.FullName)"