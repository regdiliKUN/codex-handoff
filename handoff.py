"""Offline, single-writer Codex handoff. Python 3.12+, standard library only."""
from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone

VERSION = "1.1.2"
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


class HandoffError(Exception):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def inside(a, b):
    return Path(a).resolve().is_relative_to(Path(b).resolve())


def no_link(path):
    p = Path(path)
    # Windows junctions must also be refused, not followed into another tree.
    if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
        raise HandoffError(f"发现链接或目录联接，请单独处理后重试：{p}")


def safe_file(root, relative):
    p = PurePosixPath(relative)
    if (not relative or "\\" in relative or ":" in relative or p.is_absolute()
            or any(x in {"..", ".", ""} for x in relative.split("/"))):
        raise HandoffError("迁移包包含非法路径。")
    root = Path(root).resolve()
    dest = root.joinpath(*p.parts)
    if not inside(dest, root):
        raise HandoffError("迁移路径超出目标目录。")
    for part in [root, *dest.relative_to(root).parents, dest]:
        candidate = part if part.is_absolute() else root / part
        if candidate.exists() or candidate.is_symlink():
            no_link(candidate)
    return dest


def process_image_path(pid):
    """Read the actual executable; None means the process has already exited."""
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
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error in {87, 1168}:  # Exited between tasklist and OpenProcess.
            return None
        raise ctypes.WinError(error)
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value
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
                  "codex-desktop.exe", "codex-executor.exe"}
    for row in csv.reader(io.StringIO(result.stdout.decode("mbcs", errors="replace"))):
        if not row or row[0].casefold() not in candidates:
            continue
        name = row[0]
        if len(row) < 2 or not row[1].isdigit():
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
    if busy:
        raise HandoffError("检测到 Codex 仍在运行（可能开机自启或驻留后台）。\n"
                           "请结束任务后，从托盘或菜单完全退出 Codex 和 CLI，再重试。\n"
                           "Codex 的进程名也可能是 ChatGPT.exe。\n" + "\n".join(busy + unknown))
    if unknown:
        raise HandoffError("无法确认以下进程是否属于 Codex，请查看操作详情中的程序路径，"
                           "退出对应程序后重试。\n" + "\n".join(unknown))


def sqlite_open(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def database_check(path):
    with contextlib.closing(sqlite_open(path)) as c:
        if c.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise HandoffError(f"数据库完整性检查失败：{Path(path).name}")


def schema(path):
    with contextlib.closing(sqlite_open(path)) as c:
        return c.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                         "ORDER BY type,name").fetchall()


def db_copy(src, dst, sanitize=False):
    with contextlib.closing(sqlite_open(src)) as c, contextlib.closing(sqlite3.connect(dst)) as d:
        c.backup(d)
        if sanitize:
            tables = {x[0] for x in d.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "remote_control_enrollments" in tables:
                d.execute("DELETE FROM remote_control_enrollments")
                d.commit()
                d.execute("VACUUM")
        d.execute("PRAGMA journal_mode=DELETE")
    database_check(dst)


def walk_files(root, project=False):
    root = Path(root)
    no_link(root)
    if not root.exists():
        return
    for base, dirs, files in os.walk(root, followlinks=False):
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
            files.append(p)
    return files


def managed(relative):
    p = PurePosixPath(relative)
    return relative in SINGLE | DBS or (len(p.parts) > 1 and p.parts[0] in DIRS)


def check_project_location(path, home, data):
    p = Path(path).resolve()
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


def map_path(value, mappings):
    if not isinstance(value, str):
        return value
    normalized = plain_windows_path(value)
    for old, new in sorted(mappings, key=lambda x: -len(plain_windows_path(x[0]))):
        old = plain_windows_path(old).rstrip("\\")
        if normalized.casefold() == old.casefold():
            return str(new)
        if normalized.casefold().startswith(old.casefold() + "\\"):
            return str(new).rstrip("\\/") + normalized[len(old):]
    return value


def map_tree(value, mappings):
    if isinstance(value, dict):
        return {map_path(k, mappings): map_tree(v, mappings) for k, v in value.items()}
    if isinstance(value, list):
        return [map_tree(v, mappings) for v in value]
    return map_path(value, mappings)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.meta = self.root / "store.json"

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        no_link(self.root)
        if not self.meta.exists():
            atomic_json(self.meta, {"format": 1, "id": uuid.uuid4().hex})
        info = read_json(self.meta)
        if info.get("format") != 1 or not re.fullmatch(r"[a-f0-9]{32}", info.get("id", "")):
            raise HandoffError("中转目录格式不受支持。")
        return info["id"]

    def head(self):
        p = self.root / "latest.json"
        if not p.exists():
            return None
        h = read_json(p)
        if not re.fullmatch(r"[a-f0-9]{32}", h.get("id", "")):
            raise HandoffError("最新快照标记无效。")
        self.object(h["sha256"])
        return h

    def object(self, sha):
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise HandoffError("文件校验值无效。")
        return safe_file(self.root, "objects/" + sha[:2] + "/" + sha)

    def put(self, source):
        sha = digest(source)
        dest = self.object(sha)
        if dest.exists():
            if digest(dest) != sha:
                raise HandoffError("中转目录有损坏的文件，请从完好副本恢复。")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + "." + uuid.uuid4().hex + ".part")
            try:
                shutil.copyfile(source, tmp)
                if digest(tmp) != sha:
                    raise HandoffError("复制时源文件发生变化，请关闭相关程序后重试。")
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
        return sha

    def load(self, ref=None):
        ref = ref or self.head()
        if not ref:
            raise HandoffError("中转目录还没有导出的快照。")
        obj = self.object(ref["sha256"])
        if not obj.is_file() or digest(obj) != ref["sha256"]:
            raise HandoffError("快照尚未同步完整，或校验失败。请等同步结束后重试。")
        manifest = read_json(obj)
        if manifest.get("format") != 1 or manifest.get("id") != ref["id"]:
            raise HandoffError("快照格式无效。")
        return manifest

    def publish(self, manifest, expected):
        if self.head() != expected:
            raise HandoffError("中转目录已被另一台电脑更新。请先接收最新快照。")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "manifest.json"
            atomic_json(p, manifest)
            sha = self.put(p)
        ref = {"id": manifest["id"], "sha256": sha}
        atomic_json(self.root / "snapshots" / (manifest["id"] + ".json"), ref)
        atomic_json(self.root / "latest.json", ref)
        return ref


class Engine:
    def __init__(self, home=None, data=None, profile=None, guard=offline_guard, log=print):
        self.home = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
        self.data = Path(data or Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "CodexHandoff").resolve()
        self.profile = str(Path(profile or Path.home()).resolve())
        self.guard, self.log = guard, log
        self.state_path = self.data / (hashlib.sha256(str(self.home).casefold().encode()).hexdigest()[:16] + ".json")

    def state(self):
        return read_json(self.state_path) if self.state_path.exists() else {}

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
        result = []
        for p in projects:
            p = Path(p).resolve()
            check_project_location(p, self.home, self.data)
            if not p.is_dir() or p.parent == p or inside(self.home, p) or inside(p, self.home):
                raise HandoffError(f"项目目录无效，或与 Codex 数据目录重叠：{p}")
            if inside(store.root, p) or inside(p, store.root) or inside(self.data, p) or inside(p, self.data):
                raise HandoffError("项目目录不能与中转目录或工具备份目录重叠。")
            if any(inside(p, q) or inside(q, p) for q in result):
                raise HandoffError("请勿重复选择项目目录及其子目录。")
            no_link(p)
            result.append(p)
        return result

    def fingerprint(self, projects):
        h = hashlib.sha256()
        for p in sorted(inventory(self.home)):
            rel = p.relative_to(self.home).as_posix()
            if p.name == ".codex-global-state.json":
                value = {k: v for k, v in read_json(p).items() if k in GLOBAL_KEYS}
                h.update(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())
            elif p.name in DBS:
                with contextlib.closing(sqlite_open(p)) as c:
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
                h.update((rel + ":" + digest(p)).encode())
        for root in projects:
            for p in sorted(walk_files(root, project=True)):
                h.update((str(root) + ":" + p.relative_to(root).as_posix() + ":" + digest(p)).encode())
        return h.hexdigest()

    def capture(self, store, projects, parent=None, raw=False):
        roots = {"codex": str(self.home)}
        roots.update({f"p{i}": str(p) for i, p in enumerate(projects)})
        files = []
        with tempfile.TemporaryDirectory(prefix="codex-handoff-") as temp:
            for key, root in roots.items():
                root = Path(root)
                paths = inventory(root) if key == "codex" else walk_files(root, project=True)
                for p in paths:
                    rel = p.relative_to(root).as_posix()
                    self.log("读取：" + (rel if key == "codex" else root.name + "/" + rel))
                    source = p
                    if key == "codex" and p.name in DBS:
                        source = Path(temp) / p.name
                        db_copy(p, source, sanitize=not raw)
                    elif key == "codex" and p.name == ".codex-global-state.json" and not raw:
                        source = Path(temp) / p.name
                        atomic_json(source, {k: v for k, v in read_json(p).items() if k in GLOBAL_KEYS})
                    files.append({"root": key, "path": rel, "sha256": store.put(source), "size": source.stat().st_size})
            if not raw and not any(e["root"] == "codex" and e["path"] == ".codex-global-state.json" for e in files):
                source = Path(temp) / ".codex-global-state.json"
                atomic_json(source, {})
                files.append({"root": "codex", "path": source.name, "sha256": store.put(source), "size": source.stat().st_size})
        return {"format": 1, "tool": VERSION, "id": uuid.uuid4().hex, "parent": parent,
                "created": datetime.now(timezone.utc).isoformat(), "computer": socket.gethostname(),
                "profile": self.profile, "roots": roots, "files": files, "raw_backup": raw}

    def export(self, store_path, projects=()):
        with self.lock():
            self.guard()
            store = Store(store_path)
            if inside(store.root, self.home) or inside(self.home, store.root):
                raise HandoffError("中转目录不能与 Codex 数据目录重叠。")
            store_id = store.initialize()
            projects = self.validate_projects(projects, store)
            old = store.head()
            if old:
                store.load(old)
            state = self.state()
            if old and (state.get("store_id") != store_id or state.get("head") != old["id"]):
                raise HandoffError("另一台电脑有更新，或此电脑尚未加入这套交接记录。请先接收，不能直接导出覆盖。")
            before = self.fingerprint(projects)
            manifest = self.capture(store, projects, old["id"] if old else None)
            self.guard()
            if before != self.fingerprint(projects):
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
            if p.parent == p or inside(self.home, p) or inside(p, self.home) or inside(self.data, p) or inside(p, self.data):
                raise HandoffError("项目目标目录越界或与工具数据重叠。")
            if any(inside(p, q) or inside(q, p) for k, q in out.items() if k != "codex"):
                raise HandoffError("迁移包的项目目录互相重叠。")
            no_link(p)
            out[key] = p
        return out

    def prepare(self, store, manifest, stage, raw=False):
        dests = self.destinations(manifest)
        seen = set()
        for entry in manifest["files"]:
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
                if not raw and current.exists() and schema(current) != schema(dst):
                    raise HandoffError("两台电脑数据库结构不一致，请使用相同版本 Codex 后重试。")
        if not raw:
            p = stage / "codex" / ".codex-global-state.json"
            if p.exists():
                incoming = read_json(p)
                if any(k not in GLOBAL_KEYS for k in incoming):
                    raise HandoffError("迁移包包含不允许导入的桌面设置。")
                target = self.home / p.name
                data = read_json(target) if target.exists() else {}
                for k in GLOBAL_KEYS:
                    data.pop(k, None)
                data.update(map_tree(incoming, self.mappings(manifest)))
                atomic_json(p, data)
            p = stage / "codex" / "state_5.sqlite"
            if p.exists():
                # Keep transcript bytes and projection offsets unchanged. Only active paths are remapped.
                with contextlib.closing(sqlite3.connect(p)) as c:
                    for table, columns in {"threads": ["cwd", "rollout_path"], "project_roots": ["path"],
                                           "thread_attachments": ["identity_key", "payload"], "projects": ["metadata"]}.items():
                        available = {x[1] for x in c.execute(f'PRAGMA table_info("{table}")')}
                        for col in columns:
                            if col not in available:
                                continue
                            rows = c.execute(f'SELECT rowid,"{col}" FROM "{table}"').fetchall()
                            for rid, value in rows:
                                mapped = map_path(value, self.mappings(manifest))
                                if isinstance(value, str) and col in {"payload", "metadata"}:
                                    try:
                                        mapped = json.dumps(map_tree(json.loads(value), self.mappings(manifest)), ensure_ascii=False)
                                    except (ValueError, TypeError):
                                        pass
                                if mapped != value:
                                    c.execute(f'UPDATE "{table}" SET "{col}"=? WHERE rowid=?', (mapped, rid))
                    c.commit()
                database_check(p)
        return dests

    def apply(self, manifest, stage, dests):
        incoming = {(e["root"], e["path"]) for e in manifest["files"]}
        existing = set()
        for key, root in dests.items():
            paths = inventory(root) if key == "codex" else walk_files(root, project=True)
            existing.update((key, p.relative_to(root).as_posix()) for p in paths)
        # Every target resolves inside its declared root; never recursively delete directories.
        for key, rel in sorted(existing - incoming):
            safe_file(dests[key], rel).unlink()
        for name in DBS:
            for suffix in ["-wal", "-shm", "-journal"]:
                safe_file(self.home, name + suffix).unlink(missing_ok=True)
        for key, rel in sorted(incoming):
            src = safe_file(stage / key, rel)
            dst = safe_file(dests[key], rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".handoff-" + uuid.uuid4().hex)
            try:
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
            finally:
                tmp.unlink(missing_ok=True)

    def make_backup(self, projects):
        store = Store(self.data / "backups")
        store.initialize()
        old = store.head()
        m = self.capture(store, projects, raw=True)
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
            if manifest["id"] != ref["id"]:
                raise HandoffError("最新快照发生变化，请重试。")
            dests = self.destinations(manifest)
            projects = [p for k, p in dests.items() if k != "codex"]
            for p in projects:
                if inside(store.root, p) or inside(p, store.root):
                    raise HandoffError("项目目标目录与中转目录重叠。")
            state = self.state()
            joined = state.get("store_id") == store_id
            if joined:
                if self.fingerprint([Path(p) for p in state.get("projects", [])]) != state.get("fingerprint"):
                    raise HandoffError("本机在上次交接后有改动。请先导出；如果另一台也已导出，需保留双方副本后人工处理。")
                if state.get("head") == manifest["id"]:
                    return "already", manifest
            elif not allow_initial_replace:
                raise HandoffError("首次接收需要确认：本机聊天及所选项目将先备份，再由中转快照替换。")
            with tempfile.TemporaryDirectory(prefix="codex-receive-") as tmp:
                stage = Path(tmp)
                dests = self.prepare(store, manifest, stage)
                self.guard()
                if joined and self.fingerprint([Path(p) for p in state.get("projects", [])]) != state.get("fingerprint"):
                    raise HandoffError("检查期间本机文件发生变化，请关闭相关程序后重试。")
                backup = self.make_backup(projects)
                self.guard()
                try:
                    self.apply(manifest, stage, dests)
                    fingerprint = self.fingerprint(projects)
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
            record = read_json(recovery)
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


def gui(smoke_path=None):
    import sys
    import tkinter as tk
    from handoff_ui import HandoffApp
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
    if "--self-test" in sys.argv:
        import platform
        result = {"version": VERSION, "python": platform.python_version(), "sqlite": sqlite3.sqlite_version}
        i = sys.argv.index("--self-test")
        if len(sys.argv) > i + 1:
            atomic_json(Path(sys.argv[i + 1]), result)
        elif sys.stdout:
            print(json.dumps(result))
    elif "--ui-smoke-test" in sys.argv:
        gui(Path(sys.argv[sys.argv.index("--ui-smoke-test") + 1]))
    else:
        gui()
