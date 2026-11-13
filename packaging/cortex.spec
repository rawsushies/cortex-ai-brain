# PyInstaller recipe for Cortex.exe.  Build:  pyinstaller packaging/cortex.spec
#
# Choices that matter for antivirus false positives:
#  - onedir (a folder with Cortex.exe + its files), NOT onefile: one-file exes unpack
#    themselves to a temp folder at startup, the same trick malware packers use.
#  - upx=False: UPX-compressed executables are a classic heuristic red flag.
#  - Windows version resource (product/company/description) instead of a blank exe.
import re
from pathlib import Path

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "cortex" / "__init__.py").read_text()).group(1)

version_file = None
if __import__("sys").platform == "win32":
    nums = tuple(int(x) for x in (VERSION.split(".") + ["0", "0", "0"])[:4])
    vf = Path(SPECPATH) / "build_version_info.txt"
    vf.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Cortex (open source)'),
      StringStruct('FileDescription', 'Cortex - one second brain shared by all your AIs'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'Cortex'),
      StringStruct('LegalCopyright', 'Open source, see https://github.com/rawsushies/cortex-ai-brain'),
      StringStruct('OriginalFilename', 'Cortex.exe'),
      StringStruct('ProductName', 'Cortex'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""", encoding="utf-8")
    version_file = str(vf)

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "cortex" / "ui"), "cortex/ui")],
    hiddenimports=["cortex.web", "cortex.mcp_server", "cortex.semantic", "cortex.importers",
                   "cortex.research", "cortex.cloud", "cortex.archive", "cortex.organize", "cortex.dates",
                   "cortex.graph", "cortex.notify", "cortex_gateway", "cortex_gateway.cli", "cortex_gateway.queue",
                   "cortex_gateway.schema", "cortex_gateway.adapters", "cortex_gateway.adapters.claude_code",
                   "cortex_gateway.adapters.file_drop", "cortex_gateway.adapters.local_apps",
                   "cortex_gateway.adapters.exports", "cortex_gateway.adapters.base", "cortex_gateway.rules"],
    excludes=["tkinter", "unittest", "pydoc", "test", "lib2to3", "idlelib"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Cortex",
    console=True,          # needed: AI apps talk to Cortex over stdin/stdout (MCP)
    upx=False,
    version=version_file,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Cortex")
