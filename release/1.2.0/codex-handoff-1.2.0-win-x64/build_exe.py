"""Build the standalone CodexHandoff.exe with PyInstaller.

Run from this folder with a Python that has PyInstaller installed:

    py -3.14 build_exe.py

The script keeps the version resource, the application icon, and the bundled
resources in sync with handoff.VERSION so a rebuilt EXE cannot silently claim
an old version.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from handoff import VERSION, read_json


ROOT = Path(__file__).resolve().parent
ICON = ROOT / "resources" / "handoff.ico"
EXE_NAME = "CodexHandoff"


def version_tuple(version=VERSION):
    parts = [int(p) for p in re.findall(r"\d+", version)][:4]
    return tuple((parts + [0, 0, 0, 0])[:4])


def version_info_text(version=VERSION):
    major, minor, patch, build = version_tuple(version)
    dotted = ".".join(str(v) for v in (major, minor, patch, build))
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, {build}),
    prodvers=({major}, {minor}, {patch}, {build}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Codex Handoff'),
        StringStruct('FileDescription', 'Codex 交接助手'),
        StringStruct('FileVersion', '{dotted}'),
        StringStruct('InternalName', '{EXE_NAME}'),
        StringStruct('OriginalFilename', '{EXE_NAME}.exe'),
        StringStruct('ProductName', 'Codex 交接助手'),
        StringStruct('ProductVersion', '{dotted}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def build_command(python=sys.executable, *, clean=True, dist=ROOT / "dist",
                  work=ROOT / "build", spec=ROOT / "build", version_file=None):
    command = [str(python), "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
               "--name", EXE_NAME, "--icon", str(ICON),
               "--add-data", f"{ICON.parent}{';' if sys.platform == 'win32' else ':'}resources",
               "--distpath", str(dist), "--workpath", str(work), "--specpath", str(spec)]
    if clean:
        command.append("--clean")
    if version_file:
        command.extend(["--version-file", str(version_file)])
    command.append(str(ROOT / "handoff.py"))
    return command


def verify_exe(exe, python=sys.executable):
    with tempfile.TemporaryDirectory(prefix="codex-build-verify-") as temp:
        result_path = Path(temp) / "self-test.json"
        subprocess.run([str(exe), "--self-test", str(result_path)], check=True, timeout=60)
        result = read_json(result_path)
    smoke_path = None
    with tempfile.TemporaryDirectory(prefix="codex-build-smoke-") as temp:
        smoke_path = Path(temp) / "ui.json"
        subprocess.run([str(exe), "--ui-smoke-test", str(smoke_path)], check=True, timeout=60)
        smoke = read_json(smoke_path)
    if result.get("version") != VERSION:
        raise RuntimeError(f"EXE 版本不符：{result.get('version')} != {VERSION}")
    if smoke.get("version") != VERSION or smoke.get("primary_action_pinned_to_footer") is not True:
        raise RuntimeError("EXE 界面自检未通过。")
    return result, smoke


def build(python=sys.executable, *, clean=True, verify=True):
    if not ICON.is_file():
        raise SystemExit(f"缺少图标：{ICON}")
    version_file = ROOT / "build" / "version_info.txt"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(version_info_text(), encoding="utf-8")
    command = build_command(python, clean=clean, version_file=version_file)
    print("构建命令：" + " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)
    built = ROOT / "dist" / (EXE_NAME + ".exe")
    if not built.is_file():
        raise SystemExit(f"构建完成但没有找到：{built}")
    target = ROOT / (EXE_NAME + ".exe")
    shutil.copy2(built, target)
    print(f"已生成：{target}")
    if verify:
        result, smoke = verify_exe(target)
        print(f"自检通过：Python {result['python']}，界面 {smoke['title']}")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建 CodexHandoff.exe")
    parser.add_argument("--python", default=sys.executable, help="带 PyInstaller 的 Python 解释器")
    parser.add_argument("--no-clean", action="store_true", help="复用 PyInstaller 缓存")
    parser.add_argument("--no-verify", action="store_true", help="跳过生成后的自检")
    args = parser.parse_args(argv)
    try:
        build(args.python, clean=not args.no_clean, verify=not args.no_verify)
    except subprocess.CalledProcessError as exc:
        return exc.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
