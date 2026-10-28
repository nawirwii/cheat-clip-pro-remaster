# PyInstaller spec for the Windows backend sidecar.
# Cheat Clip Pro v0.0.2 — fixed: no frontend dist/ collision, no torch bundle.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).resolve().parent.parent

datas = []
binaries = []
hiddenimports = [
    "backend",
    "backend.main",
    "backend.config",
    "backend.security",
    "backend.video_engine",
    "backend.desktop_entry",
    "backend.routers",
    "backend.routers.analyze",
    "backend.routers.cookies",
    "backend.routers.downloads",
    "backend.routers.media",
    "backend.routers.render",
    "backend.routers.system",
    "backend.services",
    "backend.services.ai_service",
    "backend.services.download_service",
    "backend.services.gdrive_service",
    "backend.services.openai_compatible_service",
    "backend.services.openrouter_service",
    "backend.services.render_service",
    "backend.services.system_service",
    "backend.services.youtube_service",
    "backend.schemas",
    "backend.schemas.analyze",
    "backend.schemas.downloads",
    "backend.schemas.render",
    "backend.utils",
    "backend.utils.heatmap",
    "backend.utils.proxy",
    "backend.utils.sse",
    "backend.utils.text",
]

# Only collect light runtime deps. torch/openai-whisper are lazy-loaded
# (import inside function) and MUST stay out of the bundle — they blow up
# the Windows runner (GBs) and cause step 7 to fail/OOM.
for package in ("uvicorn", "fastapi", "backend", "yt_dlp", "PIL", "cv2"):
    try:
        d, b, h = collect_all(package)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Fonts (overlay rendering)
fonts_dir = root / "backend" / "fonts"
if fonts_dir.exists():
    datas.append((str(fonts_dir), "backend/fonts"))

# Face-detection models
cascades_dir = root / "backend" / "cascades"
if cascades_dir.exists():
    datas.append((str(cascades_dir), "backend/cascades"))
haar_xml = root / "backend" / "haarcascade_frontalface_default.xml"
if haar_xml.exists():
    datas.append((str(haar_xml), "backend"))

excludes = [
    "torch", "torchvision", "torchaudio", "torchgen",
    "tensorflow", "tensorflow_core", "keras",
    "sklearn", "scipy", "matplotlib", "notebook", "IPython",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "Tkinter",
]

a = Analysis(
    [str(root / "backend" / "desktop_entry.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="cheat-clip-pro-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
