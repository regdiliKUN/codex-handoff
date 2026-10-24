"""Generate or verify SHA256SUMS.txt for the delivered files."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
FILES = [
    "CodexHandoff.exe",
    "handoff.py",
    "handoff_ui.py",
    "build_exe.py",
    "checksums.py",
    "resources/handoff.ico",
    "tests/test_handoff.py",
    "tests/test_handoff_ui.py",
    "tests/test_build.py",
    "使用说明.md",
    "验证记录.md",
    "迭代记录.md",
]


def sha256_file(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def compute(root=ROOT):
    result = []
    for relative in FILES:
        path = Path(root) / relative
        if path.is_file():
            result.append((relative, sha256_file(path)))
    return result


def render(root=ROOT):
    return "".join(f"{value}  {relative}\n" for relative, value in compute(root))


def write(root=ROOT, output=None):
    root = Path(root)
    output = Path(output) if output else root / "SHA256SUMS.txt"
    output.write_text(render(root), encoding="utf-8", newline="\n")
    return output


def verify(root=ROOT, output=None):
    root = Path(root)
    output = Path(output) if output else root / "SHA256SUMS.txt"
    expected = {}
    for line in output.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value, relative = line.split("  ", 1)
            expected[relative] = value
    actual = dict(compute(root))
    problems = []
    for relative, value in expected.items():
        if relative not in actual:
            problems.append(f"缺少文件：{relative}")
        elif actual[relative] != value:
            problems.append(f"校验不符：{relative}")
    for relative in actual:
        if relative not in expected:
            problems.append(f"未记录文件：{relative}")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成或校验交付文件哈希")
    parser.add_argument("--check", action="store_true", help="只校验，不写入")
    args = parser.parse_args(argv)
    if args.check:
        problems = verify()
        if problems:
            print("\n".join(problems), file=sys.stderr)
            return 1
        print("SHA256SUMS.txt 校验通过。")
        return 0
    output = write()
    print(f"已写入：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
