"""Check native Qt DLL resolution in a fresh process; never start capture or read account data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


_PROBE = r'''
import ctypes
import json
import os
from pathlib import Path
import sys

root = Path(sys.argv[1]) / "_internal"
directories = [root, root / "PySide6", root / "shiboken6"]
handles = [os.add_dll_directory(str(path)) for path in directories]
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
kernel.SetDllDirectoryW.restype = ctypes.c_int
if not kernel.SetDllDirectoryW(str(root)):
    raise ctypes.WinError(ctypes.get_last_error())
loaded = []
for relative in ("PySide6/Qt6Core.dll", "PySide6/QtCore.pyd", "PySide6/Qt6Gui.dll",
                 "PySide6/QtGui.pyd", "PySide6/Qt6Widgets.dll", "PySide6/QtWidgets.pyd"):
    loaded.append((relative, ctypes.WinDLL(str(root / relative))))
qt = loaded[0][1]
qt.qVersion.restype = ctypes.c_char_p
kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
kernel.GetModuleHandleW.restype = ctypes.c_void_p
kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
kernel.GetModuleFileNameW.restype = ctypes.c_uint
buffer = ctypes.create_unicode_buffer(32768)
icu = kernel.GetModuleHandleW("icuuc.dll")
if not icu or not kernel.GetModuleFileNameW(icu, buffer, len(buffer)):
    raise ctypes.WinError(ctypes.get_last_error())
print(json.dumps(dict(qt_version=qt.qVersion().decode("ascii"), icu_path=buffer.value,
                      native_modules_loaded=[name for name, handle in loaded], capture_started=False)))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, default=Path(__file__).parent / "dist/goutoujunshi-jev-chat-windows")
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    package = Path(os.path.abspath(arguments.package_dir))
    if not package.is_dir():
        raise SystemExit("Package directory does not exist: " + str(package))
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", _PROBE, str(package)],
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        sys.stderr.write(result.stderr or result.stdout)
        raise SystemExit(result.returncode)
    report = json.loads(result.stdout)
    if arguments.report:
        arguments.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
