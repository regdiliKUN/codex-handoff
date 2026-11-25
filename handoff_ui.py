"""Three-step desktop interface. The migration engine is supplied by the launcher."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
import queue
import socket
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

BG = "#F3F5FA"
WHITE = "#FFFFFF"
INK = "#17243B"
MUTED = "#66758E"
BLUE = "#285CE6"
PALE = "#EDF3FF"
BORDER = "#DDE4EF"
GREEN = "#137853"
AMBER = "#A85E0B"
RED = "#B43A47"
FONT = "Microsoft YaHei UI"


def h_inside(a, b):
    try:
        Path(a).resolve().relative_to(Path(b).resolve())
        return True
    except (OSError, ValueError):
        return False


def normalize_store_path(text):
    text = str(text).strip().strip('"').strip("'").strip()
    text = os.path.expandvars(os.path.expanduser(text))
    if not text:
        raise ValueError("请先选择中转文件夹。")
    try:
        p = Path(text)
    except (OSError, ValueError) as exc:
        raise ValueError("中转文件夹路径格式无效。") from exc
    if not p.is_absolute():
        raise ValueError("请填写完整的中转文件夹路径，例如 D:\\同步\\Codex交接数据。")
    if p.is_symlink():
        raise ValueError("中转文件夹不能是符号链接或目录联接，请选择真实文件夹。")
    if p.exists() and not p.is_dir():
        raise ValueError("中转位置不是文件夹。")
    try:
        return str(p.resolve())
    except OSError as exc:
        raise ValueError("无法解析中转文件夹路径，请检查磁盘是否可用。") from exc


def enable_high_dpi():
    """Ask Windows for crisp text before Tk creates any window."""
    if os.name != "nt":
        return False
    try:
        import ctypes
    except ImportError:
        return False
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(1) == 0:
            return True
    except Exception:
        pass
    try:
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except Exception:
        return False


def human_size(value):
    value = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return (f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}")
        value /= 1024
    return f"{value:.1f} TiB"


def path_preview(values, limit=8):
    paths = [str(v) for v in values]
    if len(paths) <= limit:
        return "\n".join(paths)
    return "\n".join(paths[:limit]) + f"\n……另有 {len(paths) - limit} 个目标目录"


class HandoffApp:
    def __init__(self, root, api, engine=None):
        self.root, self.api = root, api
        self.engine = engine or api.Engine()
        self.events = queue.Queue()
        self.engine.log = lambda text: self.events.put(("log", text))
        self.engine.progress = lambda done, total: self.events.put(("progress", (done, total)))
        self.settings_path = self.engine.data / "settings.json"
        self.settings_error = None
        self.state_warning = None
        try:
            settings = api.read_json(self.settings_path) if self.settings_path.exists() else {}
            if not isinstance(settings, dict):
                raise ValueError("settings is not an object")
        except Exception as exc:
            settings = {}
            self.settings_error = str(exc)
        mode = settings.get("mode")
        if mode not in {"export", "receive"}:
            mode = "export"
        self.mode = tk.StringVar(root, value=mode)
        store = settings.get("store", "")
        self.store = tk.StringVar(root, value=store if isinstance(store, str) else "")
        try:
            state_projects = self.engine.state().get("projects", [])
        except Exception as exc:
            state_projects = None
            self.state_warning = str(exc)
        projects = state_projects if isinstance(state_projects, list) else settings.get("projects", [])
        self.projects = [p for p in projects if isinstance(p, str)] if isinstance(projects, list) else []
        self.busy = False
        self.count = 0
        self.project_open = False
        self.details_open = False
        self.controls = []
        self.cards = {}
        self.closed = False
        self.root.title("Codex 交接助手 · " + api.VERSION)
        self.root.configure(bg=BG)
        width = min(1040, root.winfo_screenwidth() - 80)
        height = min(850, root.winfo_screenheight() - 100)
        root.geometry(f"{width}x{height}+{max(0, (root.winfo_screenwidth()-width)//2)}+{max(0, (root.winfo_screenheight()-height)//2-25)}")
        root.minsize(min(900, width), min(680, height))
        root.option_add("*Font", (FONT, 10))
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "resources/handoff.ico"
        if icon.exists():
            root.iconbitmap(str(icon))
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Handoff.Horizontal.TProgressbar", troughcolor="#E7ECF5", background=BLUE,
                        bordercolor="#E7ECF5", lightcolor=BLUE, darkcolor=BLUE, thickness=6)
        self._header()
        self._footer()
        self._body()
        self.mode.trace_add("write", lambda *_: self.update_mode())
        self.store.trace_add("write", lambda *_: self.folder_changed())
        self.update_mode()
        if self.settings_error:
            self.append_log("已忽略损坏的设置：" + self.settings_error)
            self.toggle_details()
        elif self.state_warning:
            self.append_log("本机交接状态暂时不可读，已改用上次保存的项目列表：" + self.state_warning)
            self.toggle_details()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Return>", self.keyboard_start)
        self.root.bind("<Control-o>", lambda _: self.choose_store())
        self.root.bind_all("<Alt-Key-1>", lambda _: self.set_mode("export"))
        self.root.bind_all("<Alt-Key-2>", lambda _: self.set_mode("receive"))
        self.root.bind("<F1>", lambda _: self.toggle_details())
        self.poll_id = self.root.after(80, self.poll)

    def set_mode(self, mode):
        if not self.busy and mode in {"export", "receive"}:
            self.mode.set(mode)

    def keyboard_start(self, event=None):
        if self.busy:
            return "break"
        focus = self.root.focus_get()
        # Buttons already invoke themselves on Return. Handling the key twice
        # could open two confirmation dialogs before the busy flag is set.
        if isinstance(focus, (tk.Text, tk.Listbox, tk.Button)):
            return None
        self.start_selected()
        return "break"

    def label(self, parent, text="", size=14, bold=False, fg=INK, bg=WHITE, **kw):
        return tk.Label(parent, text=text, font=(FONT, -size, "bold" if bold else "normal"),
                        fg=fg, bg=bg, anchor="w", **kw)

    def button(self, parent, text, command, primary=False, **kw):
        pad_y = kw.pop("pady", 10)
        button = tk.Button(parent, text=text, command=command,
                           font=(FONT, -15, "bold" if primary else "normal"),
                           bg=BLUE if primary else WHITE, fg=WHITE if primary else INK,
                           activebackground="#1946C5" if primary else PALE,
                           activeforeground=WHITE if primary else BLUE,
                           relief="flat", bd=0, highlightthickness=1,
                           highlightbackground=BLUE if primary else BORDER,
                           highlightcolor=BLUE, cursor="hand2", padx=16, pady=pad_y, **kw)
        self.controls.append(button)
        return button

    def _header(self):
        header = tk.Frame(self.root, bg=WHITE, padx=28, pady=19)
        header.pack(side="top", fill="x")
        mark = tk.Canvas(header, width=46, height=46, bg=WHITE, highlightthickness=0)
        mark.pack(side="left", padx=(0, 14))
        self.draw_computer(mark, 4, 5, BLUE, right=True)
        title = tk.Frame(header, bg=WHITE)
        title.pack(side="left")
        self.label(title, "Codex 交接助手", 23, True).pack(anchor="w")
        self.label(title, "换一台电脑，接着上次的聊天。", 13, fg=MUTED).pack(anchor="w", pady=(3, 0))
        self.label(header, "本机  " + socket.gethostname(), 12, fg=MUTED, padx=10, pady=7).pack(side="right")
        tk.Frame(self.root, bg=BORDER, height=1).pack(side="top", fill="x")

    @staticmethod
    def draw_computer(canvas, x, y, color, right):
        canvas.create_rectangle(x, y, x+30, y+23, outline=color, width=2)
        canvas.create_line(x-3, y+29, x+33, y+29, fill=color, width=3)
        canvas.create_line(x+13, y+23, x+13, y+28, fill=color, width=2)
        a, b = ((x+14, x+35) if right else (x+35, x+14))
        canvas.create_line(a, y+13, b, y+13, fill=color, width=3, arrow="last")

    def _footer(self):
        footer = tk.Frame(self.root, bg=WHITE, padx=28, pady=16)
        footer.pack(side="bottom", fill="x")
        self.status_title = self.label(footer, "准备开始", 16, True)
        self.status_title.pack(anchor="w")
        self.status_detail = self.label(footer, "先选择中转文件夹，开始前请退出 Codex。", 13, fg=MUTED,
                                        justify="left", wraplength=870)
        self.status_detail.pack(fill="x", pady=(3, 10))
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=100,
                                       style="Handoff.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(0, 12))
        self.start_button = self.button(footer, "开始导出聊天  →", self.start_selected, primary=True)
        self.start_button.configure(font=(FONT, -20, "bold"), pady=14)
        self.start_button.pack(fill="x")
        links = tk.Frame(footer, bg=WHITE)
        links.pack(fill="x", pady=(9, 0))
        self.next_hint = self.label(links, "导出后：等待同步完成 → 到另一台电脑接收", 12, fg=MUTED)
        self.next_hint.pack(side="left")
        self.restore_button = tk.Button(links, text="恢复上次备份", command=lambda: self.start("restore"),
                                        font=(FONT, -12), fg=MUTED, bg=WHITE, activeforeground=BLUE,
                                        activebackground=WHITE, relief="flat", cursor="hand2", bd=0)
        self.restore_button.pack(side="right")
        self.controls.append(self.restore_button)
        self.root.bind("<Configure>", lambda e: self.status_detail.configure(
            wraplength=max(300, self.root.winfo_width()-80)) if e.widget is self.root else None)

    def _body(self):
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(shell, bg=BG, bd=0, highlightthickness=0)
        self.scroll = ttk.Scrollbar(shell, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=BG, padx=28, pady=17)
        self.canvas_body = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.canvas_body, width=e.width))
        self.root.bind("<MouseWheel>", self.mousewheel)
        trail = tk.Frame(self.body, bg=BG)
        trail.pack(fill="x", pady=(0, 16))
        for i, text in enumerate(["选择交接方向", "选择中转文件夹", "点击开始"]):
            self.label(trail, f"0{i+1}  {text}", 13, bold=i == 0, fg=BLUE if i == 0 else MUTED, bg=BG).pack(side="left")
            if i < 2:
                self.label(trail, "    ────    ", 12, fg="#B5C0D3", bg=BG).pack(side="left")
        self.label(self.body, "1  你现在要做什么？", 16, True, bg=BG).pack(anchor="w", pady=(0, 9))
        directions = tk.Frame(self.body, bg=BG)
        directions.pack(fill="x")
        directions.grid_columnconfigure((0, 1), weight=1, uniform="direction")
        for col, mode, title, subtitle in [
            (0, "export", "从这台电脑带走", "导出聊天，到另一台电脑继续"),
            (1, "receive", "接到这台电脑", "接收聊天，在这里接着做"),
        ]:
            border = tk.Frame(directions, bg=BLUE if col == 0 else BORDER, padx=2, pady=2)
            border.grid(row=0, column=col, sticky="nsew", padx=(0, 7) if col == 0 else (7, 0))
            card = tk.Frame(border, bg=WHITE, padx=17, pady=14, cursor="hand2")
            card.pack(fill="both", expand=True)
            icon = tk.Canvas(card, width=43, height=38, bg=WHITE, highlightthickness=0)
            icon.pack(side="left", padx=(0, 13))
            self.draw_computer(icon, 4, 2, BLUE, right=mode == "export")
            words = tk.Frame(card, bg=WHITE)
            words.pack(side="left", fill="both", expand=True)
            radio = tk.Radiobutton(words, text=title, variable=self.mode, value=mode,
                                   font=(FONT, -17, "bold"), bg=WHITE, fg=INK, selectcolor=WHITE,
                                   activebackground=PALE, activeforeground=BLUE, cursor="hand2",
                                   anchor="w", bd=0, highlightthickness=0, padx=0)
            radio.pack(anchor="w")
            desc = self.label(words, subtitle, 12, fg=MUTED)
            desc.pack(anchor="w", padx=(24, 0), pady=(3, 0))
            for widget in [card, icon, words, desc]:
                widget.bind("<Button-1>", lambda _, v=mode: self.mode.set(v) if not self.busy else None)
            self.cards[mode] = (border, card, icon, words, radio, desc)
            self.controls.append(radio)
        folder = tk.Frame(self.body, bg=WHITE, highlightthickness=1, highlightbackground=BORDER, padx=18, pady=16)
        folder.pack(fill="x", pady=(16, 0))
        self.label(folder, "2  选择中转文件夹", 16, True).pack(anchor="w")
        self.label(folder, "选择你已有的网盘同步目录、NAS 或移动硬盘文件夹。", 12, fg=MUTED).pack(anchor="w", pady=(4, 10))
        row = tk.Frame(folder, bg=WHITE)
        row.pack(fill="x")
        self.store_entry = tk.Entry(row, textvariable=self.store, font=(FONT, -14), fg=INK,
                                    bg="#F8FAFD", relief="flat", highlightthickness=1,
                                    highlightbackground=BORDER, highlightcolor=BLUE)
        self.store_entry.pack(side="left", fill="x", expand=True, ipady=12)
        self.controls.append(self.store_entry)
        self.browse_button = self.button(row, "选择文件夹…", self.choose_store)
        self.browse_button.pack(side="left", padx=(10, 0))
        self.folder_hint = self.label(folder, "两台电脑选择同一份同步文件夹；各自盘符可以不同。", 12, fg=MUTED)
        self.folder_hint.pack(anchor="w", pady=(8, 0))
        options = tk.Frame(self.body, bg=WHITE, highlightthickness=1, highlightbackground=BORDER, padx=18, pady=11)
        options.pack(fill="x", pady=(12, 0))
        self.project_toggle = tk.Button(options, text="▸ 同时携带项目文件（可选）", command=self.toggle_projects,
                                        font=(FONT, -14, "bold"), fg=INK, bg=WHITE, relief="flat",
                                        bd=0, anchor="w", cursor="hand2", activebackground=WHITE)
        self.project_toggle.pack(fill="x")
        self.controls.append(self.project_toggle)
        self.project_summary = self.label(options, "", 12, fg=MUTED)
        self.project_summary.pack(anchor="w", pady=(4, 0))
        self.project_frame = tk.Frame(options, bg=WHITE)
        self.project_list = tk.Listbox(self.project_frame, height=3, bg="#F8FAFD", fg=INK,
                                       selectbackground=PALE, selectforeground=BLUE,
                                       relief="flat", highlightthickness=1, highlightbackground=BORDER,
                                       font=(FONT, -12), activestyle="none")
        self.project_list.pack(fill="x", pady=(9, 6))
        self.controls.append(self.project_list)
        actions = tk.Frame(self.project_frame, bg=WHITE)
        actions.pack(fill="x")
        self.add_button = self.button(actions, "+ 添加项目", self.add_project, pady=5)
        self.add_button.pack(side="left")
        self.remove_button = self.button(actions, "移除选中", self.remove_project, pady=5)
        self.remove_button.pack(side="left", padx=8)
        self.label(self.project_frame, "接收时会先备份，再按快照恢复项目，包含文件删除。", 12, fg=AMBER).pack(anchor="w", pady=(7, 0))
        self.refresh_projects()
        details = tk.Frame(self.body, bg=BG)
        details.pack(fill="x", pady=(12, 0))
        self.details_toggle = tk.Button(details, text="▸ 查看操作详情与本机路径", command=self.toggle_details,
                                        font=(FONT, -12), fg=MUTED, bg=BG, relief="flat", bd=0,
                                        anchor="w", cursor="hand2", activebackground=BG)
        self.details_toggle.pack(fill="x")
        self.details_frame = tk.Frame(details, bg=BG)
        self.label(self.details_frame, "聊天目录：" + str(self.engine.home), 12, fg=MUTED, bg=BG, wraplength=800).pack(anchor="w", pady=(6, 4))
        self.label(self.details_frame, "账号、插件和工具环境在两台电脑分别配置。", 12, fg=MUTED, bg=BG).pack(anchor="w", pady=(0, 6))
        self.log = tk.Text(self.details_frame, height=5, state="disabled", wrap="word", bg="#EAF0F8",
                           fg=MUTED, font=(FONT, -12), relief="flat", padx=10, pady=8)
        self.log.pack(fill="x")
        log_actions = tk.Frame(self.details_frame, bg=BG)
        log_actions.pack(fill="x", pady=(6, 0))
        self.copy_button = self.button(log_actions, "复制日志", self.copy_log, pady=4)
        self.copy_button.pack(side="left")
        self.copy_hint = self.label(log_actions, "", 12, fg=GREEN, bg=BG)
        self.copy_hint.pack(side="left", padx=(8, 0))

    def mousewheel(self, event):
        # Scroll only the middle area; the primary action is fixed and always visible.
        if self.canvas.winfo_rooty() <= event.y_root < self.canvas.winfo_rooty() + self.canvas.winfo_height():
            self.canvas.yview_scroll(-int(event.delta/120), "units")

    def update_mode(self):
        for mode, widgets in self.cards.items():
            selected = self.mode.get() == mode
            color = PALE if selected else WHITE
            widgets[0].configure(bg=BLUE if selected else BORDER)
            for widget in widgets[1:]:
                widget.configure(bg=color)
            widgets[4].configure(selectcolor=color)
        exporting = self.mode.get() == "export"
        self.start_button.configure(text="开始导出聊天  →" if exporting else "开始接收聊天  →")
        self.next_hint.configure(text="导出后：等待同步完成 → 到另一台电脑接收" if exporting else "接收后：打开 Codex → 在原聊天继续")
        self.refresh_projects()
        self.update_controls()
        if not self.busy:
            self.set_status("准备导出" if exporting else "准备接收",
                            "选择中转文件夹。开始前请先完全退出 Codex 和 CLI（包括托盘后台）。")

    def set_status(self, title, detail, color=INK):
        self.status_title.configure(text=title, fg=color)
        self.status_detail.configure(text=detail)

    def folder_changed(self):
        self.store_entry.configure(highlightbackground=BORDER)
        if not self.busy:
            self.folder_hint.configure(text="已填写。请确认两台电脑指向同一份同步文件夹。" if self.store.get().strip()
                                       else "两台电脑选择同一份同步文件夹；各自盘符可以不同。", fg=MUTED)

    def choose_store(self):
        folder = filedialog.askdirectory(parent=self.root, title="选择中转文件夹")
        if folder:
            self.store.set(folder)

    def refresh_projects(self):
        self.project_list.delete(0, "end")
        for p in self.projects:
            self.project_list.insert("end", p)
        if self.mode.get() == "receive":
            text = "接收时自动使用迁移包中的项目，无需在此添加。"
        else:
            text = f"已添加 {len(self.projects)} 个项目目录" if self.projects else "当前仅携带聊天记录与生成图片；项目可自行同步。"
        self.project_summary.configure(text=text)

    def toggle_projects(self):
        self.project_open = not self.project_open
        if self.project_open:
            self.project_frame.pack(fill="x")
        else:
            self.project_frame.pack_forget()
        self.project_toggle.configure(text=("▾" if self.project_open else "▸") + " 同时携带项目文件（可选）")

    def toggle_details(self):
        self.details_open = not self.details_open
        if self.details_open:
            self.details_frame.pack(fill="x")
        else:
            self.details_frame.pack_forget()
        self.details_toggle.configure(text=("▾" if self.details_open else "▸") + " 查看操作详情与本机路径")

    def add_project(self):
        folder = filedialog.askdirectory(parent=self.root, title="选择具体项目目录")
        if not folder:
            return
        try:
            candidate = Path(folder).resolve()
            if not candidate.is_dir():
                raise ValueError("所选路径不是文件夹。")
            if candidate == self.engine.home or h_inside(candidate, self.engine.home) or h_inside(self.engine.home, candidate):
                raise ValueError("项目不能是 Codex 数据目录或它的上级目录。")
            store = None
            if self.store.get().strip():
                try:
                    store = Path(normalize_store_path(self.store.get()))
                except ValueError:
                    # A malformed store path is reported when starting; it must
                    # not prevent the user from editing the project list.
                    store = None
            if store and (h_inside(candidate, store) or h_inside(store, candidate)):
                raise ValueError("项目不能与中转文件夹重叠。")
            key = os.path.normcase(str(candidate))
            for existing in self.projects:
                old = Path(existing).resolve()
                if key == os.path.normcase(str(old)) or h_inside(candidate, old) or h_inside(old, candidate):
                    raise ValueError("请勿添加重复项目，或项目与其子目录。")
        except (OSError, ValueError) as exc:
            self.project_summary.configure(text=str(exc), fg=RED)
            self.append_log("无法添加项目：" + str(exc))
            if not self.project_open:
                self.toggle_projects()
            return
        self.projects.append(str(candidate))
        self.refresh_projects()
        self.project_summary.configure(fg=MUTED)

    def remove_project(self):
        for i in reversed(self.project_list.curselection()):
            self.projects.pop(i)
        self.refresh_projects()

    def update_controls(self):
        for widget in self.controls:
            widget.configure(state="disabled" if self.busy else "normal")
        if not self.busy and self.mode.get() == "receive":
            for widget in [self.add_button, self.remove_button, self.project_list]:
                widget.configure(state="disabled")

    def start_selected(self):
        self.start(self.mode.get())

    def start(self, action):
        if self.busy:
            return
        try:
            raw = self.store.get().strip()
            if action != "restore" and not raw:
                self.store_entry.configure(highlightbackground=RED)
                self.folder_hint.configure(text="请点击“选择文件夹”，先选好中转位置。", fg=RED)
                self.set_status("还差一步：选择中转文件夹", "选好位置后，点击下方大按钮开始。", AMBER)
                self.canvas.yview_moveto(0)
                self.store_entry.focus_set()
                return
            path = normalize_store_path(raw) if action != "restore" else raw
            if action != "restore" and path != self.store.get():
                self.store.set(path)
            self.engine.guard()
            allow = False
            if action == "receive":
                store = self.api.Store(path)
                manifest = store.load()
                destinations = self.engine.destinations(manifest)
                preview = path_preview(destinations.values())
                created = manifest.get("created", "")
                try:
                    created = datetime.fromisoformat(created).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    pass
                projects = [k for k in manifest.get("roots", {}) if k != "codex"]
                allow = messagebox.askyesno(
                    "确认接收聊天",
                    f"来源电脑：{manifest['computer']}\n"
                    f"导出时间：{created}\n"
                    f"快照文件：{len(manifest['files'])} 个 · 项目：{len(projects)} 个\n\n"
                    f"工具将先备份，再替换以下范围内的聊天和项目文件：\n{preview}\n\n"
                    "项目中快照没有的文件也会被删除。\n"
                    "确认两台 Codex 版本相同、文件已同步完成，现在接收？",
                    parent=self.root)
                if not allow:
                    self.set_status("已取消，文件没有改动", "准备好后可再次点击“开始接收聊天”。")
                    return
            if action == "restore" and not messagebox.askyesno("恢复上次备份", "恢复最近一次操作前的聊天和项目文件？当前内容会先备份（中断恢复除外）。", parent=self.root):
                return
        except Exception as exc:
            self.append_log(str(exc))
            self.set_status("暂时无法开始", str(exc), AMBER)
            if not self.details_open:
                self.toggle_details()
            return
        try:
            self.api.atomic_json(self.settings_path, {"store": path, "projects": self.projects,
                                                      "mode": self.mode.get()})
        except Exception as exc:
            self.append_log("设置未能保存，但不影响本次交接：" + str(exc))
        self.busy = True
        self.count = 0
        self.update_controls()
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(10)
        title = {"export": "正在导出聊天…", "receive": "正在备份并接收…", "restore": "正在恢复备份…"}[action]
        self.start_button.configure(text=title)
        self.set_status(title, "请保持工具运行，暂时不要启动 Codex 或修改项目文件。", BLUE)
        projects = list(self.projects)

        def work():
            try:
                if action == "export":
                    result = self.engine.export(path, projects)
                    size = sum(e.get("size", 0) for e in result.get("files", []) if isinstance(e, dict))
                    result_message = ("导出完成", f"共 {len(result['files'])} 个文件，约 {human_size(size)}。"
                                                 "等待中转目录同步完成，再到另一台电脑选择“接收”。")
                elif action == "receive":
                    state, _ = self.engine.receive(path, allow_initial_replace=allow)
                    result_message = (("已经是最新快照", "无需重复接收，可打开 Codex 继续聊天。") if state == "already"
                                      else ("聊天已接收", "现在可以打开 Codex，检查旧聊天和项目文件，再在原聊天里继续。"))
                    try:
                        refreshed = self.engine.state().get("projects", [])
                    except Exception as exc:
                        self.events.put(("log", "聊天已接收，但读取项目列表失败：" + str(exc)))
                    else:
                        self.events.put(("projects", refreshed))
                else:
                    self.engine.restore()
                    result_message = ("备份已恢复", "现在可以打开 Codex，检查恢复后的聊天和项目。")
                self.events.put(("done", result_message))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def append_log(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 1500:
            self.log.delete("1.0", "501.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def copy_log(self):
        text = self.log.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self.copy_hint.configure(text="已复制到剪贴板")

    def poll(self):
        if self.closed:
            return
        for _ in range(100):
            try:
                kind, message = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(message)
                if message.startswith("读取："):
                    self.count += 1
                    self.status_detail.configure(text=f"已读取 {self.count} 个文件 · 请勿启动 Codex 或修改项目文件。")
            elif kind == "projects":
                self.projects[:] = message
                self.project_list.configure(state="normal")
                self.refresh_projects()
            elif kind == "progress":
                done, total = message
                if self.busy:
                    percent = round(done * 100 / total) if total else 0
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=max(1, total), value=done)
                    self.status_detail.configure(
                        text=f"已处理 {done} / {total} 个文件（{percent}%）· 请勿启动 Codex 或修改项目文件。")
            else:
                self.busy = False
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=100, value=100 if kind == "done" else 0)
                self.update_mode()
                if kind == "done":
                    self.set_status(message[0] + "  ✓", message[1], GREEN)
                    self.append_log(message[0] + "：" + message[1])
                else:
                    self.set_status("交接未完成", message, RED)
                    self.append_log(message)
                    if not self.details_open:
                        self.toggle_details()
        self.poll_id = self.root.after(80, self.poll)

    def report(self):
        self.root.update_idletasks()
        return {"title": self.root.title(), "version": self.api.VERSION, "mode": self.mode.get(),
                "requested_width": self.root.winfo_reqwidth(),
                "requested_height": self.root.winfo_reqheight(), "primary_action": self.start_button.cget("text"),
                "primary_color": self.start_button.cget("bg"), "directions": list(self.cards),
                "primary_action_pinned_to_footer": True}

    def close(self):
        if self.busy:
            messagebox.showinfo("交接尚未完成", "请等操作结束再关闭窗口。", parent=self.root)
            return
        self.closed = True
        self.root.after_cancel(self.poll_id)
        self.root.destroy()
