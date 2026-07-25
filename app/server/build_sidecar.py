"""Build the PyInstaller onefile sidecar into src-tauri/binaries.

Usage: uv run python build_sidecar.py
Output: ../src-tauri/binaries/filmpaw-server-x86_64-pc-windows-msvc.exe
(the target-triple suffix is what Tauri's externalBin expects).
"""

import platform
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
BINARIES = HERE.parent / "src-tauri" / "binaries"

TRIPLES = {
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
}


def main() -> int:
    triple = TRIPLES.get((platform.system(), platform.machine()))
    if triple is None:
        print(f"unsupported platform: {platform.system()} {platform.machine()}")
        return 1

    entry = HERE / "sidecar_entry.py"
    entry.write_text(
        "from filmpaw_server.main import main\n\nif __name__ == '__main__':\n    main()\n",
        encoding="utf-8",
    )

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--noconfirm",
        "--clean",
        "--name",
        "filmpaw-server",
        "--collect-submodules",
        "uvicorn",
        "--collect-data",
        "zhconv",
        str(entry),
    ]
    print(" ".join(cmd))
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        return r.returncode

    BINARIES.mkdir(parents=True, exist_ok=True)
    src = HERE / "dist" / "filmpaw-server.exe"
    dst = BINARIES / f"filmpaw-server-{triple}.exe"
    shutil.copy2(src, dst)
    print(f"sidecar -> {dst} ({dst.stat().st_size // 1_048_576} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
