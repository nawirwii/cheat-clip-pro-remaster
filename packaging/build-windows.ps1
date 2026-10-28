$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path .venv\Scripts\python.exe)) {
  py -3 -m venv .venv
}
& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install -r backend\requirements.txt pyinstaller
& .venv\Scripts\python.exe -m PyInstaller --clean --noconfirm packaging\backend.spec --distpath sidecar-dist --workpath build-sidecar
if (-not (Test-Path sidecar-dist\cheat-clip-pro-backend.exe)) { throw "sidecar exe was not produced in sidecar-dist/" }
Remove-Item -Recurse -Force src-tauri\binaries -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force src-tauri\binaries | Out-Null
Copy-Item sidecar-dist\cheat-clip-pro-backend.exe src-tauri\binaries\cheat-clip-pro-backend-x86_64-pc-windows-msvc.exe -Force
Remove-Item -Recurse -Force src-tauri\resources -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force src-tauri\resources | Out-Null
if (Test-Path backend\fonts) { Copy-Item -Recurse -Force backend\fonts src-tauri\resources\fonts }
if (Test-Path backend\cascades) { Copy-Item -Recurse -Force backend\cascades src-tauri\resources\cascades }
if (Test-Path backend\haarcascade_frontalface_default.xml) { Copy-Item -Force backend\haarcascade_frontalface_default.xml src-tauri\resources\haarcascade_frontalface_default.xml }

npm ci
npm run build
npx tauri build --bundles nsis

$exe = Get-ChildItem src-tauri\target\release\bundle\nsis\*.exe | Select-Object -First 1
if (-not $exe) { throw "Tauri installer was not produced" }
Write-Host "Produced $($exe.FullName)"
