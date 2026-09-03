# PyInstaller build for the Windows download.
#
# The audience is a club officer who has never installed Python. Everyone else
# uses pip, which is universal on Linux and macOS and needs none of this.
#
# Two things have to be right or the executable starts and then fails in ways
# that are hard to read:
#
#   * The static files and schema.sql must be bundled under `courseops/`, so
#     that `resources.package_file` finds them at the same relative path it uses
#     when running from source. Without them the app serves pages with no
#     stylesheet and cannot create its database.
#
#   * uvicorn loads its protocol and lifespan implementations by string name at
#     runtime, so PyInstaller's import analysis cannot see them. They are listed
#     explicitly below; miss one and the server raises on the first request
#     rather than at startup.
#
# Console, not windowed: the banner tells you the setup URL, and a windowed
# build would hide it.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
PACKAGE = ROOT / "src" / "courseops"

datas = [
    (str(PACKAGE / "static"), "courseops/static"),
    (str(PACKAGE / "schema.sql"), "courseops"),
]

hiddenimports = [
    *collect_submodules("uvicorn"),
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # aprslib parses four incompatible position encodings and reaches for its
    # submodules dynamically.
    *collect_submodules("aprslib"),
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CourseOps",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX compression is a reliable way to be flagged by AV
    console=True,
    icon=str(PACKAGE / "static" / "favicon-48.png")
    if (PACKAGE / "static" / "favicon-48.png").exists() else None,
)
