"""Offline, single-writer Codex handoff. Python 3.12+, standard library only."""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone

VERSION = "1.2.0"
DBS = {"state_5.sqlite", "thread_history_1.sqlite"}
DIRS = {"sessions", "archived_sessions", "generated_images", "visualizations"}
SINGLE = {"session_index.jsonl", ".codex-global-state.json"}
GLOBAL_KEYS = {
    "local-projects", "projectless-thread-ids", "thread-workspace-root-hints",
    "thread-projectless-output-directories", "sidebar-project-thread-orders",
    "pinned-thread-ids", "thread-titles", "electron-thread-read-state-v1",
}
EXCLUDED_DIRS = {"node_modules", ".venv", "__pycache__"}
DB_PATTERN = re.compile(r"(?:state|thread_history)_\d+\.sqlite$")
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
ID_PATTERN = re.compile(r"[a-f0-9]{32}")
MANIFEST_FORMAT = 1
# A manifest is metadata, not a file listing of unbounded size. These limits keep a
# damaged or hostile snapshot from exhausting memory before anything is staged.
MAX_MANIFEST_FILES = 200_000
MAX_MANIFEST_BYTES = 1 << 40  # 1 TiB
MAX_MANIFEST_ROOTS = 1_000
MAX_ABSOLUTE_PATH = 32_000  # Below the Windows extended-length path ceiling.
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$",
                    *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def windows_component_problem(part):
    """Return a reason when a path component is unsafe on Windows, else None."""
    if not part:
        return "空路径片段"
    if any(ord(ch) < 32 for ch in part):
        return "包含控制字符"
    if part.endswith((" ", ".")):
        return "以空格或句点结尾"
    if part.casefold().split(".", 1)[0] in WINDOWS_RESERVED:
        return "使用了 Windows 保留名称"
    if len(part) > 255:
        return "单个路径片段过长"
    return None


class HandoffError(Exception):
    pass


def read_json(path, limit=64 * 1024 * 1024):
    p = Path(path)
    try:
        # Read one byte past the limit instead of trusting stat(): a file can
        # grow between the size check and the read inside a sync folder.
        with p.open("rb") as f:
            raw = f.read(limit + 1)
        if len(raw) > limit:
            raise HandoffError(f"JSON 文件过大，已停止读取：{p.name}")
        return json.loads(raw.decode("utf-8-sig"))
    except HandoffError:
        raise
    except FileNotFoundError as exc:
        raise HandoffError(f"找不到文件：{p.name}") from exc
    except UnicodeDecodeError as exc:
        raise HandoffError(f"文件不是 UTF-8 文本：{p.name}") from exc
    except json.JSONDecodeError as exc:
        raise HandoffError(f"文件不是有效 JSON：{p.name}（第 {exc.lineno} 行第 {exc.colno} 列）") from exc
    except RecursionError as exc:
        raise HandoffError(f"JSON 嵌套层级过深，已停止读取：{p.name}") from exc
    except OSError as exc:
        raise HandoffError(f"无法读取文件：{p.name}（{exc.strerror or exc}）") from exc


def atomic_json(path, data):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HandoffError(f"无法创建目录：{path.parent}") from exc
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            # Make the rename durable on POSIX test/support environments.
            try:
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
    except (TypeError, ValueError) as exc:
        raise HandoffError(f"无法序列化数据：{path.name}") from exc
    except OSError as exc:
        raise HandoffError(f"无法写入文件：{path.name}（{exc.strerror or exc}）") from exc
    finally:
        tmp.unlink(missing_ok=True)


def digest(path):
    """Return a streaming SHA-256. hashlib.file_digest keeps the hot loop in C."""
    p = Path(path)
    try:
        with p.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except OSError as exc:
        raise HandoffError(f"无法读取文件：{p.name}（{exc.strerror or exc}）") from exc


def inside(a, b):
    return Path(a).resolve().is_relative_to(Path(b).resolve())


def ensure_free_space(path, needed, label="目标目录"):
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if needed + 64 * 1024 * 1024 > free:
        raise HandoffError(f"{label}可用空间不足：至少需要 {needed / (1024 ** 3):.2f} GiB。")


def no_link(path):
    p = Path(path)
    # Windows junctions must also be refused, not followed into another tree.
    if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
        raise HandoffError(f"发现链接或目录联接，请单独处理后重试：{p}")


def no_link_path(path):
    """Reject a path if it or any existing parent is a link/junction."""
    p = Path(path)
    for candidate in [p, *p.parents]:
        if candidate.exists() or candidate.is_symlink():
            no_link(candidate)


def safe_file(root, relative):
    parts = relative_parts(relative)
    root = Path(root).resolve()
    dest = root.joinpath(*parts)
    if len(str(dest)) > MAX_ABSOLUTE_PATH:
        raise HandoffError("迁移路径过长，已停止。")
    if not inside(dest, root):
        raise HandoffError("迁移路径超出目标目录。")
    for part in [root, *dest.relative_to(root).parents, dest]:
        candidate = part if part.is_absolute() else root / part
        if candidate.exists() or candidate.is_symlink():
            no_link(candidate)
    return dest


_PROCESS_API = None


def _process_api():
    global _PROCESS_API
    if _PROCESS_API is None:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        _PROCESS_API = (ctypes, kernel)
    return _PROCESS_API


def process_image_path(pid):
    """Read the actual executable; None means the process has already exited."""
    ctypes, kernel = _process_api()
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error in {87, 1168}:  # Exited between tasklist and OpenProcess.
            return None
        raise ctypes.WinError(error)
    try:
        size = 32768
        while True:
            buffer = ctypes.create_unicode_buffer(size)
            length = ctypes.c_ulong(size)
            if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return plain_windows_path(buffer.value)
            error = ctypes.get_last_error()
            if error == 122 and size < 32768 * 4:  # ERROR_INSUFFICIENT_BUFFER
                size *= 2
                continue
            raise ctypes.WinError(error)
    finally:
        kernel.CloseHandle(handle)


def chatgpt_product(path):
    """Codex also ships ChatGPT.exe. Only allow positively identified ChatGPT installs."""
    parts = [p.casefold() for p in PureWindowsPath(path).parts[:-1]]
    if any(p == "codex" or p.startswith("openai.codex_") for p in parts):
        return "codex"
    if any(p == "chatgpt" or p.startswith(("openai.chatgpt_", "openai.chatgpt-desktop_"))
           for p in parts):
        return "chatgpt"
    return "unknown"


def offline_guard():
    if os.name != "nt":
        raise HandoffError("此工具只支持 Windows。")
    try:
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], check=True,
                                capture_output=True, creationflags=0x08000000, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HandoffError("无法检查运行中的程序，请稍后重试。") from exc
    busy, unknown = [], []
    candidates = {"codex.exe", "chatgpt.exe", "codex-cli.exe", "codex-app.exe",
                  "codex-desktop.exe", "codex-executor.exe", "codex-tui.exe"}
    try:
        rows = csv.reader(io.StringIO(result.stdout.decode("mbcs", errors="replace")), strict=True)
        for row in rows:
            if not row:
                continue
            name = row[0].strip().lstrip("\ufeff")
            if name.casefold() not in candidates:
                continue
            if len(row) < 2 or not row[1].strip().isdigit():
                raise HandoffError("进程检查结果无效，请稍后重试。")
            pid = int(row[1])
            try:
                path = process_image_path(pid)
            except OSError:
                path = "（无法读取程序路径）"
            if path is None:
                continue
            product = chatgpt_product(path) if name.casefold() == "chatgpt.exe" else "codex"
            if product == "chatgpt":
                continue
            detail = f"{name}（PID {pid}）\n程序路径：{path}"
            (unknown if product == "unknown" else busy).append(detail)
    except csv.Error as exc:
        raise HandoffError("进程检查结果无法解析，请稍后重试。") from exc
    if busy:
        raise HandoffError("检测到 Codex 仍在运行（可能开机自启或驻留后台）。\n"
                           "请结束任务后，从托盘或菜单完全退出 Codex 和 CLI，再重试。\n"
                           "Codex 的进程名也可能是 ChatGPT.exe。\n" + "\n".join(busy + unknown))
    if unknown:
        raise HandoffError("无法确认以下进程是否属于 Codex，请查看操作详情中的程序路径，"
                           "退出对应程序后重试。\n" + "\n".join(unknown))


def sqlite_open(path):
    try:
        return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise HandoffError(f"无法打开数据库：{Path(path).name}") from exc


def database_check(path):
    try:
        with contextlib.closing(sqlite_open(path)) as c:
            if c.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise HandoffError(f"数据库完整性检查失败：{Path(path).name}")
    except sqlite3.Error as exc:
        raise HandoffError(f"数据库完整性检查失败：{Path(path).name}") from exc


def schema(path):
    try:
        with contextlib.closing(sqlite_open(path)) as c:
            return c.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                             "ORDER BY type,name").fetchall()
    except sqlite3.Error as exc:
        raise HandoffError(f"无法读取数据库结构：{Path(path).name}") from exc


def database_signature(path):
    """Schema plus the version fields an application may use during migration."""
    try:
        with contextlib.closing(sqlite_open(path)) as c:
            return _database_signature(c)
    except sqlite3.Error as exc:
        raise HandoffError(f"无法读取数据库结构：{Path(path).name}") from exc


def _database_signature(connection):
    objects = connection.execute("SELECT type,name,sql FROM sqlite_master "
                                 "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    encoding = connection.execute("PRAGMA encoding").fetchone()[0]
    return objects, user_version, application_id, encoding


def _remap_database_value(value, column, mappings):
    mapped = map_path(value, mappings)
    if isinstance(value, str) and column in {"payload", "metadata"}:
        try:
            parsed = json.loads(value)
            remapped = map_tree(parsed, mappings)
            if remapped != parsed:
                mapped = json.dumps(remapped, ensure_ascii=False)
            else:
                mapped = value
        except (ValueError, TypeError):
            pass
    return mapped


def remap_database(path, mappings):
    """Rewrite only known path-bearing columns, in one transaction.

    Transcript bytes and projection offsets are deliberately untouched. Rows
    are streamed in batches so a large attachment table does not have to be
    materialized in memory.
    """
    tables = {
        "threads": ["cwd", "rollout_path"],
        "project_roots": ["path"],
        "thread_attachments": ["identity_key", "payload"],
        "projects": ["metadata"],
    }
    try:
        with contextlib.closing(sqlite3.connect(path)) as c:
            c.execute("BEGIN IMMEDIATE")
            for table, columns in tables.items():
                available = {x[1] for x in c.execute(f'PRAGMA table_info("{table}")')}
                for col in columns:
                    if col not in available:
                        continue
                    try:
                        with contextlib.closing(sqlite_open(path)) as reader:
                            cursor = reader.execute(f'SELECT rowid,"{col}" FROM "{table}"')
                            while True:
                                rows = cursor.fetchmany(1000)
                                if not rows:
                                    break
                                updates = []
                                for rid, value in rows:
                                    mapped = _remap_database_value(value, col, mappings)
                                    if mapped != value:
                                        updates.append((mapped, rid))
                                if updates:
                                    c.executemany(f'UPDATE "{table}" SET "{col}"=? WHERE rowid=?', updates)
                    except sqlite3.OperationalError as exc:
                        raise HandoffError(f"无法读取数据库表 {table}.{col}，可能版本不兼容。") from exc
            c.commit()
    except sqlite3.Error as exc:
        raise HandoffError(f"更新数据库路径失败：{Path(path).name}") from exc
    database_check(path)


def read_global_state(path):
    data = read_json(path)
    if not isinstance(data, dict):
        raise HandoffError("Codex 桌面状态文件格式无效。")
    return data


def merge_global_state(incoming_path, target_path, mappings):
    incoming = read_global_state(incoming_path)
    forbidden = sorted(k for k in incoming if k not in GLOBAL_KEYS)
    if forbidden:
        raise HandoffError("迁移包包含不允许导入的桌面设置：" + "、".join(forbidden[:5]))
    if target_path.exists() or target_path.is_symlink():
        no_link(target_path)
        if not target_path.is_file():
            raise HandoffError("本机 Codex 桌面状态不是普通文件。")
        data = read_global_state(target_path)
    else:
        data = {}
    for k in GLOBAL_KEYS:
        data.pop(k, None)
    data.update(map_tree(incoming, mappings))
    atomic_json(incoming_path, data)


def db_copy(src, dst, sanitize=False):
    try:
        with contextlib.closing(sqlite_open(src)) as c, contextlib.closing(sqlite3.connect(dst)) as d:
            c.backup(d)
            if sanitize:
                tables = {x[0] for x in d.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "remote_control_enrollments" in tables:
                    d.execute("PRAGMA secure_delete=ON")
                    d.execute("DELETE FROM remote_control_enrollments")
                    d.commit()
                    if d.execute("SELECT COUNT(*) FROM remote_control_enrollments").fetchone()[0]:
                        raise HandoffError("远程控制凭据未能从备份中清除，已停止。")
                    d.execute("VACUUM")
            d.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error as exc:
        raise HandoffError(f"复制数据库失败：{Path(src).name}") from exc
    database_check(dst)


def prune_empty_dirs(root):
    """Remove empty directories left behind by a snapshot deletion pass.

    The snapshot format tracks files, not empty directories. Keeping a
    directory that no longer exists on the source would make the two machines
    drift apart and could confuse tools that enumerate project folders.
    """
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return
    for base, _dirs, _files in os.walk(root, topdown=False, followlinks=False):
        p = Path(base)
        if p == root or p.is_symlink():
            continue
        try:
            p.rmdir()
        except OSError:
            pass


def walk_files(root, project=False):
    root = Path(root)
    no_link_path(root)
    if not root.exists():
        return
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs.sort(key=str.casefold)
        files.sort(key=str.casefold)
        for name in list(dirs):
            if project and name in EXCLUDED_DIRS:
                dirs.remove(name)
                continue
            no_link(Path(base) / name)
        for name in files:
            p = Path(base) / name
            no_link(p)
            if project and name == ".git":
                raise HandoffError(f"暂不支持 Git worktree 或子模块指针：{p}\n请使用独立仓库副本，或单独迁移该项目。")
            yield p


def inventory(home):
    home = Path(home)
    no_link(home)
    for path in home.iterdir() if home.exists() else []:
        if DB_PATTERN.fullmatch(path.name) and path.name not in DBS:
            raise HandoffError(f"尚未验证这个 Codex 数据库版本：{path.name}。已停止，不会覆盖。")
    files = []
    for name in sorted(DIRS):
        files.extend(walk_files(home / name))
    for name in sorted(SINGLE | DBS):
        p = home / name
        if p.exists():
            no_link(p)
            if not p.is_file():
                raise HandoffError(f"Codex 数据路径不是普通文件：{name}")
            files.append(p)
    files.sort(key=lambda p: p.relative_to(home).as_posix().casefold())
    return files


def managed(relative):
    p = PurePosixPath(relative)
    return relative in SINGLE | DBS or (len(p.parts) > 1 and p.parts[0] in DIRS)


def has_exportable_data(home):
    home = Path(home)
    if not home.is_dir():
        return False
    if any((home / name).is_file() for name in DBS | SINGLE):
        return True
    for name in DIRS:
        root = home / name
        if root.is_dir() and next(iter(walk_files(root)), None) is not None:
            return True
    return False


def validate_recovery(record):
    if not isinstance(record, dict):
        raise HandoffError("恢复标记损坏，请保留工具数据目录并人工检查。")
    if not isinstance(record.get("home"), str) or not record["home"]:
        raise HandoffError("恢复标记缺少 Codex 目录。")
    try:
        validate_ref(record.get("ref"))
    except HandoffError as exc:
        raise HandoffError("恢复标记中的快照校验值无效。") from exc
    state = record.get("state")
    if state:
        try:
            validate_state(state)
        except HandoffError as exc:
            raise HandoffError("恢复标记中的本机状态无效。") from exc
    return record


def validate_state(state):
    if not isinstance(state, dict):
        raise HandoffError("本机交接状态无效，请从备份恢复或删除工具数据目录后重试。")
    if not state:
        return state
    store_id, head = state.get("store_id"), state.get("head")
    if not isinstance(store_id, str) or not ID_PATTERN.fullmatch(store_id):
        raise HandoffError("本机交接状态缺少有效的中转目录编号。")
    if not isinstance(head, str) or not ID_PATTERN.fullmatch(head):
        raise HandoffError("本机交接状态缺少有效的快照编号。")
    projects = state.get("projects")
    if not isinstance(projects, list) or any(not isinstance(p, str) for p in projects):
        raise HandoffError("本机交接状态中的项目列表无效。")
    fingerprint = state.get("fingerprint")
    if fingerprint is not None and not isinstance(fingerprint, str):
        raise HandoffError("本机交接状态中的校验值无效。")
    return state


def relative_parts(relative):
    """Validate a manifest path before it is joined to any root."""
    if not isinstance(relative, str):
        raise HandoffError("迁移包包含非文本路径。")
    if (not relative or "\\" in relative or ":" in relative
            or any(x in {"..", ".", ""} for x in relative.split("/"))):
        raise HandoffError("迁移包包含非法路径。")
    p = PurePosixPath(relative)
    if p.is_absolute():
        raise HandoffError("迁移包包含绝对路径。")
    for part in p.parts:
        problem = windows_component_problem(part)
        if problem:
            raise HandoffError(f"迁移包路径不安全（{problem}）：{relative}")
    return p.parts


def validate_ref(ref):
    if not isinstance(ref, dict):
        raise HandoffError("最新快照标记无效。")
    if not isinstance(ref.get("id"), str) or not ID_PATTERN.fullmatch(ref["id"]):
        raise HandoffError("最新快照标记无效。")
    if not isinstance(ref.get("sha256"), str) or not SHA256_PATTERN.fullmatch(ref["sha256"]):
        raise HandoffError("最新快照校验值无效。")
    return ref


def validate_manifest(manifest, expected_id=None):
    """Reject malformed metadata before it is used to touch the filesystem.

    A snapshot travels through a sync folder, so it must be treated as untrusted
    input even when it was produced by this tool on another computer.
    """
    if not isinstance(manifest, dict):
        raise HandoffError("快照格式无效：清单不是对象。")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise HandoffError("快照格式无效或版本过新。")
    snapshot_id = manifest.get("id")
    if not isinstance(snapshot_id, str) or not ID_PATTERN.fullmatch(snapshot_id):
        raise HandoffError("快照编号无效。")
    if expected_id is not None and snapshot_id != expected_id:
        raise HandoffError("快照编号与最新标记不一致。")
    parent = manifest.get("parent")
    if parent is not None and (not isinstance(parent, str) or not ID_PATTERN.fullmatch(parent)):
        raise HandoffError("快照的上一版本编号无效。")
    if parent is not None and parent == snapshot_id:
        raise HandoffError("快照的上一版本编号不能指向自身。")
    for key in ("tool", "created", "computer", "profile"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise HandoffError(f"快照缺少有效的 {key} 字段。")
    try:
        datetime.fromisoformat(manifest["created"])
    except (TypeError, ValueError) as exc:
        raise HandoffError("快照的导出时间无效。") from exc
    raw_backup = manifest.get("raw_backup", False)
    if not isinstance(raw_backup, bool):
        raise HandoffError("快照的 raw_backup 字段无效。")
    if not PureWindowsPath(manifest["profile"]).is_absolute():
        raise HandoffError("快照中的用户目录不是绝对路径。")
    roots = manifest.get("roots")
    if (not isinstance(roots, dict) or not isinstance(roots.get("codex"), str)
            or not roots["codex"] or not PureWindowsPath(roots["codex"]).is_absolute()):
        raise HandoffError("快照缺少 Codex 目录。")
    if len(roots) > MAX_MANIFEST_ROOTS + 1:
        raise HandoffError("快照包含的项目数量超过限制。")
    project_roots = set()
    for key, value in roots.items():
        if key == "codex":
            continue
        if (not isinstance(key, str) or not re.fullmatch(r"p\d+", key)
                or not isinstance(value, str) or not value
                or not PureWindowsPath(value).is_absolute()):
            raise HandoffError("快照的项目目录格式无效。")
        identity = str(value).casefold()
        if identity in project_roots:
            raise HandoffError("快照包含重复的项目目录。")
        project_roots.add(identity)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise HandoffError("快照文件清单无效。")
    if len(files) > MAX_MANIFEST_FILES:
        raise HandoffError("快照文件数量超过限制，请检查中转目录是否被篡改。")
    seen = set()
    total = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise HandoffError("快照文件条目格式无效。")
        key, rel = entry.get("root"), entry.get("path")
        if not isinstance(key, str) or key not in roots:
            raise HandoffError("快照文件指向未知目录。")
        parts = relative_parts(rel)
        if key == "codex" and not managed(rel):
            raise HandoffError("快照包含不允许恢复的文件：" + rel)
        sha = entry.get("sha256")
        if not isinstance(sha, str) or not SHA256_PATTERN.fullmatch(sha):
            raise HandoffError("快照文件校验值无效：" + rel)
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise HandoffError("快照文件大小无效：" + rel)
        total += size
        if total > MAX_MANIFEST_BYTES:
            raise HandoffError("快照总大小超过限制，请检查中转目录是否被篡改。")
        identity = (key, rel.casefold())
        if identity in seen:
            raise HandoffError("快照包含重复文件：" + rel)
        seen.add(identity)
        if len(parts) > 64 or len(rel) > 4096:
            raise HandoffError("快照路径过深或过长：" + rel)
    return manifest


def check_project_location(path, home, data):
    try:
        no_link_path(path)
        p = Path(path).resolve()
    except OSError as exc:
        raise HandoffError(f"无法解析项目目录：{path}") from exc
    no_link_path(p)
    protected = [home, data, Path.home()]
    protected.extend(Path(os.environ[k]) for k in ["WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA", "APPDATA"] if os.environ.get(k))
    if p.parent == p or any(inside(q, p) for q in protected):
        raise HandoffError("请只选择具体项目文件夹，不能选择磁盘、用户主目录或系统目录。")
    if any(inside(p, q) for q in [home, data] + protected[3:]):
        raise HandoffError("项目文件夹不能位于 Codex、工具数据或系统目录内。")


def plain_windows_path(value):
    normalized = str(value).replace("/", "\\")
    if normalized[:8].casefold() == "\\\\?\\unc\\".casefold():
        return "\\\\" + normalized[8:]
    if normalized.startswith("\\\\?\\"):
        return normalized[4:]
    return normalized


def _mapping_key(value):
    text = plain_windows_path(value)
    if re.fullmatch(r"[A-Za-z]:\\?", text):
        return text[0].upper() + ":\\"
    if text.startswith("\\\\"):
        return text.rstrip("\\")
    return text.rstrip("\\") if len(text) > 3 else text


def _mapped_target(new, suffix, exact):
    base = plain_windows_path(new)
    if exact:
        if re.fullmatch(r"[A-Za-z]:\\?", base):
            return base[0].upper() + ":\\"
        return base.rstrip("\\") or base
    base = base.rstrip("\\")
    if re.fullmatch(r"[A-Za-z]:", base):
        return base + "\\" + suffix
    if suffix.startswith("\\"):
        return base + suffix
    return base + "\\" + suffix


def map_path(value, mappings):
    if not isinstance(value, str):
        return value
    normalized = plain_windows_path(value)
    key_text = os.path.normcase(normalized)
    ordered = []
    for old, new in mappings:
        old_key = _mapping_key(old)
        ordered.append((os.path.normcase(old_key), old_key, new))
    ordered.sort(key=lambda item: -len(item[0]))
    for old_key, display_old, new in ordered:
        if key_text == old_key:
            return _mapped_target(new, "", True)
        if key_text.startswith(old_key + "\\"):
            return _mapped_target(new, normalized[len(old_key):], False)
        if old_key.endswith("\\") and key_text.startswith(old_key):
            return _mapped_target(new, normalized[len(old_key):], False)
    return value


def map_tree(value, mappings, depth=0):
    if depth > 200:
        raise HandoffError("JSON 路径数据嵌套层级过深，已停止处理。")
    if isinstance(value, dict):
        return {map_path(k, mappings): map_tree(v, mappings, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [map_tree(v, mappings, depth + 1) for v in value]
    return map_path(value, mappings)


class Store:
    def __init__(self, root):
        raw = Path(os.path.abspath(os.fspath(root)))
        # Check the path exactly as selected: resolving first would silently
        # follow a junction or symlink and defeat the link policy below.
        if raw.exists() or raw.is_symlink():
            no_link_path(raw)
        self.root = raw.resolve()
        self.meta = self.root / "store.json"

    def initialize(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HandoffError(f"无法创建中转目录：{self.root}") from exc
        no_link_path(self.root)
        if self.meta.exists() or self.meta.is_symlink():
            no_link(self.meta)
            if not self.meta.is_file():
                raise HandoffError("中转目录标记不是普通文件。")
        else:
            self._create_meta()
        no_link(self.meta)
        if not self.meta.is_file():
            raise HandoffError("中转目录标记不是普通文件。")
        info = read_json(self.meta)
        if (not isinstance(info, dict) or info.get("format") != 1
                or not isinstance(info.get("id"), str) or not ID_PATTERN.fullmatch(info["id"])):
            raise HandoffError("中转目录格式不受支持。")
        return info["id"]

    def _create_meta(self):
        """Create store.json without replacing a concurrently created identity."""
        payload = {"format": 1, "id": uuid.uuid4().hex}
        tmp = self.meta.with_name(self.meta.name + "." + uuid.uuid4().hex + ".new")
        try:
            atomic_json(tmp, payload)
            try:
                os.link(tmp, self.meta)
            except FileExistsError:
                pass
            except OSError:
                # Some network shares do not implement hard links. An exclusive
                # create still prevents two computers from silently replacing
                # each other's store identity.
                try:
                    with self.meta.open("x", encoding="utf-8", newline="\n") as f:
                        json.dump(payload, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                except FileExistsError:
                    pass
        finally:
            tmp.unlink(missing_ok=True)

    def head(self):
        if self.root.exists():
            no_link(self.root)
        p = self.root / "latest.json"
        if not p.exists():
            return None
        no_link(p)
        if not p.is_file():
            raise HandoffError("最新快照标记不是普通文件。")
        return validate_ref(read_json(p))

    def ensure(self, expected):
        validate_ref(expected)
        current = self.head()
        if current is None or current["id"] != expected["id"] or current["sha256"] != expected["sha256"]:
            raise HandoffError("中转目录在操作期间被另一台电脑更新，请重新接收。")

    def object(self, sha):
        if not isinstance(sha, str) or not SHA256_PATTERN.fullmatch(sha):
            raise HandoffError("文件校验值无效。")
        return safe_file(self.root, "objects/" + sha[:2] + "/" + sha)

    def put(self, source):
        source = Path(source)
        if not source.is_file():
            raise HandoffError(f"待复制的内容不是普通文件：{source.name}")
        no_link_path(source)
        sha = digest(source)
        dest = self.object(sha)
        if dest.exists():
            if not dest.is_file():
                raise HandoffError("中转目录有损坏的文件，请从完好副本恢复。")
            if digest(dest) != sha:
                raise HandoffError("中转目录有损坏的文件，请从完好副本恢复。")
        else:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HandoffError(f"无法创建中转目录结构：{dest.parent}") from exc
            tmp = dest.with_name(dest.name + "." + uuid.uuid4().hex + ".part")
            try:
                shutil.copyfile(source, tmp)
                if digest(tmp) != sha:
                    raise HandoffError("复制时源文件发生变化，请关闭相关程序后重试。")
                os.replace(tmp, dest)
            except OSError as exc:
                raise HandoffError(f"复制到中转目录失败：{source.name}（{exc.strerror or exc}）") from exc
            finally:
                tmp.unlink(missing_ok=True)
        return sha

    def load(self, ref=None):
        if ref is None:
            ref = self.head()
            if ref is None:
                raise HandoffError("中转目录还没有导出的快照。")
        ref = validate_ref(ref)
        obj = self.object(ref["sha256"])
        if not obj.is_file() or digest(obj) != ref["sha256"]:
            raise HandoffError("快照尚未同步完整，或校验失败。请等同步结束后重试。")
        manifest = read_json(obj)
        validate_manifest(manifest, ref["id"])
        return manifest

    def publish(self, manifest, expected):
        validate_manifest(manifest)
        if expected is not None:
            validate_ref(expected)
            if manifest.get("parent") != expected["id"]:
                raise HandoffError("快照的上一版本与中转目录不一致，已停止发布。")
        elif manifest.get("parent") is not None:
            raise HandoffError("中转目录没有上一版本，但快照声称有父版本，已停止发布。")
        if self.head() != expected:
            raise HandoffError("中转目录已被另一台电脑更新。请先接收最新快照。")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "manifest.json"
            atomic_json(p, manifest)
            sha = self.put(p)
        ref = {"id": manifest["id"], "sha256": sha}
        snapshot = safe_file(self.root, "snapshots/" + manifest["id"] + ".json")
        if snapshot.exists():
            if read_json(snapshot) != ref:
                raise HandoffError("快照编号已被另一份内容占用，已停止发布。")
        else:
            atomic_json(snapshot, ref)
        atomic_json(self.root / "latest.json", ref)
        return ref


class Engine:
    def __init__(self, home=None, data=None, profile=None, guard=offline_guard, log=print, progress=None):
        self.home = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
        self.data = Path(data or Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "CodexHandoff").resolve()
        self.profile = str(Path(profile or Path.home()).resolve())
        self.guard, self.log = guard, log
        self.progress = progress or (lambda done, total: None)
        self.state_path = self.data / (hashlib.sha256(str(self.home).casefold().encode()).hexdigest()[:16] + ".json")

    def report_progress(self, done, total):
        try:
            self.progress(done, total)
        except Exception:
            pass

    def state(self):
        return validate_state(read_json(self.state_path)) if self.state_path.exists() else {}

    @contextlib.contextmanager
    def lock(self, recovery_allowed=False):
        self.data.mkdir(parents=True, exist_ok=True)
        p = self.data / "operation.lock"
        f = p.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                if not f.read(1):
                    f.write(b"0")
                    f.flush()
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise HandoffError("另一个交接操作尚未结束。") from exc
            if not recovery_allowed and (self.data / "recovery.json").exists():
                raise HandoffError("上次操作被中断。请先点击“恢复备份”。")
            yield
        finally:
            f.close()

    def validate_projects(self, projects, store):
        if projects is None:
            projects = ()
        if isinstance(projects, (str, bytes)):
            raise HandoffError("项目列表格式无效，请重新选择项目目录。")
        result = []
        identities = set()
        for p in projects:
            no_link_path(p)
            p = Path(p).resolve()
            check_project_location(p, self.home, self.data)
            if not p.is_dir() or p.parent == p or inside(self.home, p) or inside(p, self.home):
                raise HandoffError(f"项目目录无效，或与 Codex 数据目录重叠：{p}")
            if inside(store.root, p) or inside(p, store.root) or inside(self.data, p) or inside(p, self.data):
                raise HandoffError("项目目录不能与中转目录或工具备份目录重叠。")
            identity = os.path.normcase(str(p))
            if identity in identities or any(inside(p, q) or inside(q, p) for q in result):
                raise HandoffError("请勿重复选择项目目录及其子目录。")
            identities.add(identity)
            no_link(p)
            result.append(p)
        return result

    def fingerprint(self, projects, cache=None):
        cache = {} if cache is None else cache

        def file_sha(path):
            st = Path(path).stat()
            key = (str(path), st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            if key not in cache:
                cache[key] = digest(path)
            return cache[key]

        h = hashlib.sha256()
        for p in sorted(inventory(self.home)):
            rel = p.relative_to(self.home).as_posix()
            if p.name == ".codex-global-state.json":
                value = {k: v for k, v in read_global_state(p).items() if k in GLOBAL_KEYS}
                h.update(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())
            elif p.name in DBS:
                with contextlib.closing(sqlite_open(p)) as c:
                    objects, user_version, application_id, encoding = _database_signature(c)
                    h.update((rel + ":schema").encode())
                    h.update(json.dumps([objects, user_version, application_id, encoding],
                                        ensure_ascii=False, default=str).encode())
                    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    selected = (["threads", "thread_sections", "projects", "project_roots", "thread_attachments"]
                                if p.name.startswith("state") else ["thread_turns", "thread_items", "thread_realtime_items"])
                    for table in selected:
                        if table not in tables:
                            continue
                        cols = [x[1] for x in c.execute(f'PRAGMA table_info("{table}")')]
                        if table == "threads":
                            cols = [v for v in ["id", "cwd", "title", "name", "archived", "is_pinned",
                                               "thread_section_id", "project_id", "updated_at", "updated_at_ms"] if v in cols]
                        quoted = ",".join('"' + v + '"' for v in cols)
                        h.update((rel + ":" + table).encode())
                        for row in c.execute(f'SELECT {quoted} FROM "{table}" ORDER BY {quoted}'):
                            h.update(json.dumps(row, ensure_ascii=False, default=str).encode())
            else:
                h.update((rel + ":" + file_sha(p)).encode())
        for root in projects:
            for p in sorted(walk_files(root, project=True)):
                h.update((str(root) + ":" + p.relative_to(root).as_posix() + ":" + file_sha(p)).encode())
        return h.hexdigest()

    def capture(self, store, projects, parent=None, raw=False):
        roots = {"codex": str(self.home)}
        roots.update({f"p{i}": str(p) for i, p in enumerate(projects)})
        files = []
        with tempfile.TemporaryDirectory(prefix="codex-handoff-") as temp:
            plan = {}
            for key, root in roots.items():
                root = Path(root)
                plan[key] = inventory(root) if key == "codex" else list(walk_files(root, project=True))
            planned = sum(len(v) for v in plan.values())
            if planned > MAX_MANIFEST_FILES:
                raise HandoffError("待导出的文件数量超过限制，请缩小项目范围后重试。")
            self.log("计划：" + str(planned) + " 个文件")
            done = 0
            total = planned
            total_bytes = 0
            for key, paths in plan.items():
                root = Path(roots[key])
                for p in paths:
                    done += 1
                    self.report_progress(done, total)
                    rel = p.relative_to(root).as_posix()
                    self.log("读取：" + (rel if key == "codex" else root.name + "/" + rel))
                    source = p
                    if key == "codex" and p.name in DBS:
                        source = Path(temp) / p.name
                        db_copy(p, source, sanitize=not raw)
                    elif key == "codex" and p.name == ".codex-global-state.json" and not raw:
                        source = Path(temp) / p.name
                        atomic_json(source, {k: v for k, v in read_global_state(p).items() if k in GLOBAL_KEYS})
                    size = source.stat().st_size
                    total_bytes += size
                    if total_bytes > MAX_MANIFEST_BYTES:
                        raise HandoffError("待导出的文件总大小超过限制，请缩小项目范围后重试。")
                    sha = store.put(source)
                    if source.stat().st_size != size:
                        raise HandoffError(f"导出期间文件大小发生变化，已停止：{source.name}")
                    files.append({"root": key, "path": rel, "sha256": sha, "size": size})
            if not raw and not any(e["root"] == "codex" and e["path"] == ".codex-global-state.json" for e in files):
                source = Path(temp) / ".codex-global-state.json"
                atomic_json(source, {})
                files.append({"root": "codex", "path": source.name, "sha256": store.put(source), "size": source.stat().st_size})
        # Manifest order is part of the snapshot identity. Sort explicitly so
        # directory enumeration order can never leak into the published JSON.
        files.sort(key=lambda e: (0 if e["root"] == "codex" else int(e["root"][1:]) + 1,
                                  e["path"].casefold()))
        return {"format": 1, "tool": VERSION, "id": uuid.uuid4().hex, "parent": parent,
                "created": datetime.now(timezone.utc).isoformat(), "computer": socket.gethostname(),
                "profile": self.profile, "roots": roots, "files": files, "raw_backup": raw}

    def export(self, store_path, projects=()):
        with self.lock():
            self.guard()
            if not self.home.is_dir():
                raise HandoffError(f"找不到 Codex 数据目录：{self.home}")
            if not has_exportable_data(self.home):
                raise HandoffError("Codex 数据目录中没有可导出的聊天记录，已停止。")
            store = Store(store_path)
            if inside(store.root, self.home) or inside(self.home, store.root):
                raise HandoffError("中转目录不能与 Codex 数据目录重叠。")
            if inside(store.root, self.data) or inside(self.data, store.root):
                raise HandoffError("中转目录不能与工具备份目录重叠。")
            store_id = store.initialize()
            projects = self.validate_projects(projects, store)
            old = store.head()
            if old:
                store.load(old)
            state = self.state()
            if old and (state.get("store_id") != store_id or state.get("head") != old["id"]):
                raise HandoffError("另一台电脑有更新，或此电脑尚未加入这套交接记录。请先接收，不能直接导出覆盖。")
            cache = {}
            before = self.fingerprint(projects, cache)
            # Avoid a redundant snapshot when the user clicks export twice
            # without changing anything. Reusing the head also avoids forcing
            # the other computer to download an identical manifest.
            if (old and state.get("head") == old["id"] and state.get("fingerprint") == before
                    and isinstance(state.get("projects"), list)):
                saved = sorted(os.path.normcase(os.path.abspath(str(p))) for p in state["projects"])
                current = sorted(os.path.normcase(os.path.abspath(str(p))) for p in projects)
                if saved == current:
                    self.log("内容没有变化，复用最新快照。")
                    return store.load(old)
            manifest = self.capture(store, projects, old["id"] if old else None)
            self.guard()
            # Capture must not make the post-check reuse hashes of files that
            # could have changed without a visible timestamp change.
            cache.clear()
            if before != self.fingerprint(projects, cache):
                raise HandoffError("导出期间有文件变化，未发布快照。请关闭相关程序后重试。")
            ref = store.publish(manifest, old)
            atomic_json(self.state_path, {"store_id": store_id, "head": ref["id"], "fingerprint": before,
                                          "projects": [str(p) for p in projects], "store": str(store.root)})
            return manifest

    def mappings(self, manifest):
        return [(manifest["roots"]["codex"], str(self.home)), (manifest["profile"], self.profile)]

    def destinations(self, manifest):
        roots = manifest.get("roots", {})
        if "codex" not in roots or not isinstance(roots["codex"], str):
            raise HandoffError("快照缺少 Codex 目录。")
        out = {"codex": self.home}
        identities = {os.path.normcase(str(self.home))}
        for key, value in roots.items():
            if key == "codex":
                continue
            if not re.fullmatch(r"p\d+", key) or not isinstance(value, str):
                raise HandoffError("项目目录格式无效。")
            p = Path(map_path(value, self.mappings(manifest)))
            if not p.is_absolute():
                raise HandoffError("项目目录必须为绝对路径。")
            p = p.resolve()
            check_project_location(p, self.home, self.data)
            identity = os.path.normcase(str(p))
            if identity in identities:
                raise HandoffError("迁移包的不同项目目录映射到了同一位置。")
            identities.add(identity)
            if p.parent == p or inside(self.home, p) or inside(p, self.home) or inside(self.data, p) or inside(p, self.data):
                raise HandoffError("项目目标目录越界或与工具数据重叠。")
            if any(inside(p, q) or inside(q, p) for k, q in out.items() if k != "codex"):
                raise HandoffError("迁移包的项目目录互相重叠。")
            no_link(p)
            out[key] = p
        return out

    def prepare(self, store, manifest, stage, raw=False):
        validate_manifest(manifest)
        dests = self.destinations(manifest)
        ensure_free_space(stage, sum(e["size"] for e in manifest["files"]), "临时目录")
        total = len(manifest["files"])
        seen = set()
        for index, entry in enumerate(manifest["files"], 1):
            self.report_progress(index, total)
            key, rel = entry["root"], entry["path"]
            identity = (key, rel.casefold())
            if identity in seen or key not in dests or (key == "codex" and not managed(rel)):
                raise HandoffError("快照包含重复文件或不允许恢复的文件。")
            seen.add(identity)
            safe_file(dests[key], rel)
            src = store.object(entry["sha256"])
            if not src.is_file() or src.stat().st_size != entry["size"] or digest(src) != entry["sha256"]:
                raise HandoffError("迁移包未同步完整或文件损坏：" + rel)
            dst = safe_file(stage / key, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            if digest(dst) != entry["sha256"]:
                raise HandoffError("复制后的文件校验失败：" + rel)
            if key == "codex" and rel in DBS:
                database_check(dst)
                current = self.home / rel
                if not raw and current.exists() and database_signature(current) != database_signature(dst):
                    raise HandoffError("两台电脑数据库结构不一致，请使用相同版本 Codex 后重试。")
        if not raw:
            p = stage / "codex" / ".codex-global-state.json"
            if p.exists():
                merge_global_state(p, self.home / p.name, self.mappings(manifest))
            p = stage / "codex" / "state_5.sqlite"
            if p.exists():
                # Keep transcript bytes and projection offsets unchanged. Only active paths are remapped.
                remap_database(p, self.mappings(manifest))
        return dests

    def apply(self, manifest, stage, dests):
        validate_manifest(manifest)
        entries = {(e["root"], e["path"]): e for e in manifest["files"]}
        incoming = set(entries)
        existing = set()
        for key, root in dests.items():
            paths = inventory(root) if key == "codex" else walk_files(root, project=True)
            existing.update((key, p.relative_to(root).as_posix()) for p in paths)
        # Only files that actually need replacing require temporary space.
        # Counting unchanged files would reject valid incremental receives on a
        # nearly full disk.
        needed = {key: 0 for key in dests}
        unchanged = set()
        for key, rel in sorted(incoming):
            src = safe_file(stage / key, rel)
            size = src.stat().st_size
            dst = safe_file(dests[key], rel)
            if dst.is_file() and dst.stat().st_size == size and digest(dst) == digest(src):
                unchanged.add((key, rel))
            else:
                needed[key] += size
        for key, root in dests.items():
            if needed[key]:
                ensure_free_space(root, needed[key], f"{key} 目标目录")
        # Remove stale SQLite sidecars before replacing a database, then copy new
        # files before deleting extras. A crash still leaves a recovery marker.
        for name in DBS:
            for suffix in ["-wal", "-shm", "-journal"]:
                safe_file(self.home, name + suffix).unlink(missing_ok=True)
        total = len(incoming)
        for index, (key, rel) in enumerate(sorted(incoming), 1):
            self.report_progress(index, total)
            if (key, rel) in unchanged:
                continue
            src = safe_file(stage / key, rel)
            dst = safe_file(dests[key], rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".handoff-" + uuid.uuid4().hex)
            try:
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
            finally:
                tmp.unlink(missing_ok=True)
        for key, rel in sorted(existing - incoming):
            safe_file(dests[key], rel).unlink()
        for key, root in dests.items():
            bases = [root / name for name in DIRS] if key == "codex" else [root]
            for base in bases:
                prune_empty_dirs(base)

    def make_backup(self, projects):
        store = Store(self.data / "backups")
        store.initialize()
        old = store.head()
        m = self.capture(store, projects, parent=old["id"] if old else None, raw=True)
        ref = store.publish(m, old)
        atomic_json(self.data / "recovery.json", {"ref": ref, "home": str(self.home), "state": self.state()})
        return ref

    def restore_ref(self, ref):
        store = Store(self.data / "backups")
        manifest = store.load(ref)
        with tempfile.TemporaryDirectory(prefix="codex-restore-") as tmp:
            stage = Path(tmp)
            dests = self.prepare(store, manifest, stage, raw=True)
            self.guard()
            self.apply(manifest, stage, dests)

    def receive(self, store_path, allow_initial_replace=False):
        with self.lock():
            self.guard()
            store = Store(store_path)
            store_id = store.initialize()
            manifest = store.load()
            ref = store.head()
            if ref is None or manifest["id"] != ref["id"]:
                raise HandoffError("最新快照发生变化，请重试。")
            store.ensure(ref)
            dests = self.destinations(manifest)
            projects = [p for k, p in dests.items() if k != "codex"]
            for p in projects:
                if inside(store.root, p) or inside(p, store.root):
                    raise HandoffError("项目目标目录与中转目录重叠。")
            state = self.state()
            joined = state.get("store_id") == store_id
            cache = {}
            if joined:
                if self.fingerprint([Path(p) for p in state.get("projects", [])], cache) != state.get("fingerprint"):
                    raise HandoffError("本机在上次交接后有改动。请先导出；如果另一台也已导出，需保留双方副本后人工处理。")
                if state.get("head") == manifest["id"]:
                    return "already", manifest
            elif not allow_initial_replace:
                raise HandoffError("首次接收需要确认：本机聊天及所选项目将先备份，再由中转快照替换。")
            with tempfile.TemporaryDirectory(prefix="codex-receive-") as tmp:
                stage = Path(tmp)
                dests = self.prepare(store, manifest, stage)
                store.ensure(ref)
                self.guard()
                if joined and self.fingerprint([Path(p) for p in state.get("projects", [])], cache) != state.get("fingerprint"):
                    raise HandoffError("检查期间本机文件发生变化，请关闭相关程序后重试。")
                backup = self.make_backup(projects)
                try:
                    store.ensure(ref)
                    self.guard()
                except Exception:
                    # Nothing has been replaced yet; the backup is complete but
                    # there is no interrupted mutation to recover.
                    (self.data / "recovery.json").unlink(missing_ok=True)
                    raise
                try:
                    self.apply(manifest, stage, dests)
                    cache.clear()
                    fingerprint = self.fingerprint(projects, cache)
                    atomic_json(self.state_path, {"store_id": store_id, "head": manifest["id"], "fingerprint": fingerprint,
                                                  "projects": [str(p) for p in projects], "store": str(store.root)})
                except Exception:
                    self.log("导入失败，正在恢复操作前备份……")
                    self.restore_ref(backup)
                    if state:
                        atomic_json(self.state_path, state)
                    else:
                        self.state_path.unlink(missing_ok=True)
                    (self.data / "recovery.json").unlink(missing_ok=True)
                    raise
                (self.data / "recovery.json").unlink(missing_ok=True)
            return "received", manifest

    def restore(self):
        with self.lock(recovery_allowed=True):
            self._restore_locked()

    def _restore_locked(self):
        self.guard()
        recovery = self.data / "recovery.json"
        if recovery.exists():
            record = validate_recovery(read_json(recovery))
            if record["home"] != str(self.home):
                raise HandoffError("未完成的恢复记录属于另一个 Codex 目录。")
            self.restore_ref(record["ref"])
            if record["state"]:
                atomic_json(self.state_path, record["state"])
            else:
                self.state_path.unlink(missing_ok=True)
            recovery.unlink()
        else:
            backup = Store(self.data / "backups")
            ref = backup.head()
            if not ref:
                raise HandoffError("还没有本机备份。")
            # Preserve the current state before a voluntary rollback, permitting undo.
            manifest = backup.load(ref)
            projects = [p for k, p in self.destinations(manifest).items() if k != "codex"]
            current = self.make_backup(projects)
            try:
                self.restore_ref(ref)
                self.state_path.unlink(missing_ok=True)
            except Exception:
                self.restore_ref(current)
                recovery.unlink(missing_ok=True)
                raise
            recovery.unlink(missing_ok=True)


def verify_store(store_path):
    """Check every object referenced by the current snapshot."""
    store = Store(store_path)
    ref = store.head()
    if ref is None:
        raise HandoffError("中转目录还没有导出的快照。")
    manifest = store.load(ref)
    total = 0
    for entry in manifest["files"]:
        obj = store.object(entry["sha256"])
        if not obj.is_file() or obj.stat().st_size != entry["size"] or digest(obj) != entry["sha256"]:
            raise HandoffError("快照校验失败，文件缺失或损坏：" + entry["path"])
        total += entry["size"]
    return {"id": ref["id"], "files": len(manifest["files"]), "bytes": total}


def diagnose(home=None, data=None):
    engine = Engine(home=home, data=data, guard=lambda: None, log=lambda _: None)
    state_status = "empty"
    state_error = None
    try:
        if engine.state():
            state_status = "ok"
    except Exception as exc:
        state_status, state_error = "error", str(exc)
    result = {
        "version": VERSION,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "codex_home": str(engine.home),
        "codex_home_exists": engine.home.is_dir(),
        "data": str(engine.data),
        "state": state_status,
        "recovery_pending": (engine.data / "recovery.json").exists(),
    }
    if state_error:
        result["state_error"] = state_error
    return result


def _emit_json(data, target):
    if target in (None, "-"):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        atomic_json(Path(target), data)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="CodexHandoff", description="Codex 交接助手")
    parser.add_argument("--version", action="store_true", help="显示版本号")
    parser.add_argument("--self-test", nargs="?", const="-", metavar="OUTPUT",
                        help="输出 Python 与 SQLite 版本（默认输出到屏幕）")
    parser.add_argument("--diagnose", nargs="?", const="-", metavar="OUTPUT",
                        help="输出本机路径与交接状态诊断（默认输出到屏幕）")
    parser.add_argument("--verify-store", metavar="STORE",
                        help="逐文件校验中转目录中的最新快照")
    parser.add_argument("--ui-smoke-test", metavar="OUTPUT",
                        help="启动一次隐藏界面并把布局报告写入 JSON")
    args = parser.parse_args(argv)
    if args.version:
        print(VERSION)
        return 0
    if args.self_test:
        _emit_json({"version": VERSION, "python": platform.python_version(),
                    "sqlite": sqlite3.sqlite_version}, args.self_test)
        return 0
    if args.diagnose:
        _emit_json(diagnose(), args.diagnose)
        return 0
    if args.verify_store:
        try:
            result = verify_store(args.verify_store)
        except HandoffError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.ui_smoke_test:
        gui(Path(args.ui_smoke_test))
        return 0
    gui()
    return 0


def gui(smoke_path=None):
    import tkinter as tk
    from handoff_ui import HandoffApp, enable_high_dpi
    enable_high_dpi()
    root = tk.Tk()
    if smoke_path:
        root.withdraw()
    app = HandoffApp(root, sys.modules[__name__])
    if smoke_path:
        atomic_json(smoke_path, app.report())
        app.close()
        return
    root.mainloop()


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
