# PyInstaller spec for the Windows backend sidecar.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent.parent
datas = [(str(root / "dist"), "dist")]
fonts_dir = root / "backend" / "fonts"
if fonts_dir.exists():
    datas.append((str(fonts_dir), "backend/fonts"))
binaries = []
hiddenimports = []
for package in ("uvicorn", "fastapi", "backend", "yt_dlp", "cv2", "PIL", "torch"):
    try:
        d, b, h = collect_all(package)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis([str(root / "backend" / "desktop_entry.py")],
             pathex=[str(root)], datas=datas, binaries=binaries,
             hiddenimports=hiddenimports, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name="cheat-clip-pro-backend", console=True)