"""Production entry point used by the Windows PyInstaller bundle."""
import os
import uvicorn

from backend.main import app


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
        access_log=False,
    )