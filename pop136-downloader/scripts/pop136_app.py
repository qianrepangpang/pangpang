from __future__ import annotations

import ctypes
import gc
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from pop136_core import (
    APP_VERSION,
    BrowserSessionRecycle,
    Pop136Engine,
    cdp_ready,
    login_browser_launch_args,
    make_diagnostic_bundle,
    reactivate_timed_out_files,
    retry_delay_seconds,
    is_verification_error,
)


CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "POP136Downloader"
CONFIG_PATH = CONFIG_DIR / "config.json"
UNATTENDED_VERIFICATION_SHUTDOWN_SECONDS = 10 * 60


class LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def windows_last_input_tick() -> int:
    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return int(info.dwTime)
    return 0


def requires_manual_verification(text: str) -> bool:
    return is_verification_error(text) or any(
        marker in text for marker in ("登录或验证状态已失效", "请完成登录或验证码")
    )


def download_progress_clears_verification(text: str) -> bool:
    return any(
        marker in text
        for marker in ("：downloaded", "：browser_downloaded", "页处理", "进入第")
    )


def should_resume_from_args(args: list[str]) -> bool:
    return "--resume" in args[1:]


class VerificationShutdownGuard:
    def __init__(self) -> None:
        self.deadline: float | None = None
        self.last_input_tick = 0

    def arm(self, now: float, input_tick: int) -> None:
        if self.deadline is None:
            self.deadline = now + UNATTENDED_VERIFICATION_SHUTDOWN_SECONDS
            self.last_input_tick = input_tick

    def clear(self) -> None:
        self.deadline = None
        self.last_input_tick = 0

    def check(self, now: float, input_tick: int) -> bool:
        if self.deadline is None:
            return False
        if input_tick != self.last_input_tick:
            self.last_input_tick = input_tick
            self.deadline = now + UNATTENDED_VERIFICATION_SHUTDOWN_SECONDS
            return False
        return now >= self.deadline


def process_exists(pid: int) -> bool:
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def restore_login_window(pid: int | None = None) -> bool:
    """Restore only the Chrome window started for manual login and move it on-screen."""
    found = False
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _param):
        nonlocal found
        window_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if pid is not None and window_pid.value != pid:
            return True
        class_name = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        if not class_name.value.startswith("Chrome_WidgetWin"):
            return True
        if pid is None:
            title = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
            user32.GetWindowTextW(hwnd, title, len(title))
            if not any(marker in title.value.upper() for marker in ("POP", "YUNTU", "图案", "云图")):
                return True
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetWindowPos(hwnd, 0, 60, 60, 1200, 900, 0x0040)  # SWP_SHOWWINDOW
        user32.SetForegroundWindow(hwnd)
        found = True
        return False

    user32.EnumWindows(callback_type(visit), 0)
    return found


def restore_app_window() -> bool:
    found = False
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _param):
        nonlocal found
        title = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        if not title.value.startswith("POP136 自动下载器"):
            return True
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        found = True
        return False

    user32.EnumWindows(callback_type(visit), 0)
    return found


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"POP136 自动下载器 {APP_VERSION}")
        self.geometry("900x720")
        self.minsize(760, 620)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.login_process: subprocess.Popen | None = None
        self.manual_pause = False
        self.file_statuses: dict[str, str] = {}
        self.verification_shutdown = VerificationShutdownGuard()
        self._build_ui()
        self._load_config()
        self.after(150, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.status_var.set("请先打开网页登录并完成验证，然后点击“2. 开始 / 续传”。")

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Hint.TLabel", foreground="#5f6368")
        outer = ttk.Frame(self, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="POP136 自动下载器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="下载账号当前可用的非 PSD/EPS 文件；不会购买、开通会员或绕过权限。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(4, 18))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        self.target_var = tk.StringVar(value=r"H:\POP136原图")
        self.profile_var = tk.StringVar(value=r"H:\POP136浏览器登录状态")
        self._path_row(form, 0, "下载目录", self.target_var)
        self._path_row(form, 1, "登录状态目录", self.profile_var)
        ttk.Label(form, text="下载范围").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Label(form, text="全部年份（从新到旧连续下载）").grid(row=2, column=1, sticky="w", pady=6)
        form.columnconfigure(1, weight=1)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(16, 12))
        self.login_button = ttk.Button(buttons, text="1. 打开网页登录", command=self._open_login)
        self.login_button.pack(side="left")
        self.start_button = ttk.Button(buttons, text="2. 开始 / 续传", command=self._start)
        self.start_button.pack(side="left", padx=8)
        self.pause_button = ttk.Button(buttons, text="暂停", command=self._pause, state="disabled")
        self.pause_button.pack(side="left")
        ttk.Button(buttons, text="打开下载目录", command=self._open_target).pack(side="right")
        ttk.Button(buttons, text="生成诊断包", command=self._diagnostics).pack(side="right", padx=8)

        self.status_var = tk.StringVar(value="就绪。临时网络故障会自动重试，无需手动重启。")
        ttk.Label(outer, textvariable=self.status_var).pack(anchor="w", pady=(0, 8))
        progress_row = ttk.Frame(outer)
        progress_row.pack(fill="x", pady=(0, 10))
        self.progress_bar = ttk.Progressbar(progress_row, mode="determinate", maximum=1, value=0)
        self.progress_bar.pack(side="left", fill="x", expand=True)
        self.progress_text_var = tk.StringVar(value="当前批次：未开始")
        ttk.Label(progress_row, textvariable=self.progress_text_var, width=30, anchor="e").pack(side="left", padx=(10, 0))
        self.current_file_var = tk.StringVar(value="当前文件：等待下载")
        ttk.Label(outer, textvariable=self.current_file_var).pack(anchor="w")
        self.file_progress_bar = ttk.Progressbar(outer, mode="determinate", maximum=100, value=0)
        self.file_progress_bar.pack(fill="x", pady=(4, 10))
        self.file_progress_text_var = tk.StringVar(value="0% · 0 B")
        ttk.Label(outer, textvariable=self.file_progress_text_var, style="Hint.TLabel").pack(anchor="e", pady=(0, 8))
        columns = ("file", "status")
        self.file_tree = ttk.Treeview(outer, columns=columns, show="headings", height=6)
        self.file_tree.heading("file", text="文件")
        self.file_tree.heading("status", text="状态")
        self.file_tree.column("file", width=620, anchor="w")
        self.file_tree.column("status", width=180, anchor="center")
        self.file_tree.pack(fill="x", pady=(0, 10))
        self.log_text = tk.Text(
            outer, height=18, wrap="word", state="disabled", font=("Consolas", 10),
            background="#111827", foreground="#e5e7eb", insertbackground="white",
        )
        self.log_text.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="暂停会保留 .part 断点；再次开始自动续传。诊断包不包含登录 Cookie。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(8, 0))

    def _path_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(12, 8), pady=6)
        ttk.Button(parent, text="选择…", command=lambda: self._choose_folder(variable)).grid(row=row, column=2, pady=6)

    def _choose_folder(self, variable: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=variable.get() or None)
        if chosen:
            variable.set(chosen)

    def _load_config(self) -> None:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.target_var.set(data.get("target", self.target_var.get()))
            self.profile_var.set(data.get("profile", self.profile_var.get()))
        except (OSError, json.JSONDecodeError):
            pass

    def _save_config(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(
                {"target": self.target_var.get(), "profile": self.profile_var.get(), "scope": "all_years"},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _validate(self) -> tuple[Path, Path] | None:
        target = Path(self.target_var.get().strip())
        profile = Path(self.profile_var.get().strip())
        if not target.drive or not profile.drive:
            messagebox.showerror("设置错误", "请选择完整的 Windows 目录。")
            return None
        return target, profile

    def _open_login(self) -> None:
        values = self._validate()
        if not values:
            return
        _, profile = values
        profile.mkdir(parents=True, exist_ok=True)
        try:
            self.manual_pause = True
            self.stop_event.set()
            deadline = time.monotonic() + 5
            while self.worker and self.worker.is_alive() and time.monotonic() < deadline:
                self.update_idletasks()
                time.sleep(0.1)
            if cdp_ready():
                restore_login_window()
            else:
                browser = Pop136Engine.browser_path()
                self.login_process = subprocess.Popen(
                    [str(browser), *login_browser_launch_args(profile)],
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
                self._show_login_window(self.login_process.pid)
            self.status_var.set("请完成登录或验证码，然后直接点击“开始 / 续传”。")
        except Exception as error:
            messagebox.showerror("无法打开浏览器", str(error))

    def _show_login_window(self, pid: int, attempts: int = 20) -> None:
        if restore_login_window(pid):
            return
        if attempts > 0:
            self.after(250, self._show_login_window, pid, attempts - 1)

    def _active_script_runner(self, target: Path) -> bool:
        pid_file = target / "_runner.pid"
        try:
            active = process_exists(int(pid_file.read_text(encoding="ascii").strip()))
            if not active:
                pid_file.unlink(missing_ok=True)
            return active
        except (OSError, ValueError):
            return False

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        values = self._validate()
        if not values:
            return
        target, profile = values
        if self._active_script_runner(target):
            messagebox.showwarning("已有任务在运行", "当前命令行下载任务仍在运行。请等待它结束后再启动桌面 App，避免状态文件冲突。")
            return
        self.login_process = None
        self.manual_pause = False
        self.verification_shutdown.clear()
        retry_count = reactivate_timed_out_files(target / "_download_state.json")
        (target / "_download_complete.flag").unlink(missing_ok=True)
        self._save_config()
        self.stop_event = threading.Event()
        self.start_button.configure(state="disabled")
        self.login_button.configure(state="normal")
        self.pause_button.configure(state="normal")
        self.progress_bar.configure(maximum=1, value=0)
        self.progress_text_var.set(
            f"当前批次：准备中；重新尝试 {retry_count} 个上次超时文件"
            if retry_count else "当前批次：准备中"
        )
        self.worker = threading.Thread(target=self._run_engine, args=(target, profile), daemon=True)
        self.worker.start()

    def _run_engine(self, target: Path, profile: Path) -> None:
        attempt = 0
        while not self.stop_event.is_set():
            try:
                engine = Pop136Engine(
                    target,
                    profile,
                    "all",
                    lambda text: self.events.put(("log", text)),
                    self.stop_event,
                    lambda done, total, label: self.events.put(("progress", (done, total, label))),
                    lambda name, done, total: self.events.put(("file_progress", (name, done, total))),
                )
                engine.run()
                self.events.put(("done", "任务已暂停" if self.stop_event.is_set() else "全部年份下载完成"))
                return
            except BrowserSessionRecycle:
                gc.collect()
                attempt = 0
                continue
            except Exception as error:
                attempt += 1
                delay = retry_delay_seconds(attempt)
                text = f"遇到临时故障：{error}；{delay} 秒后自动续传（无需操作）"
                try:
                    target.mkdir(parents=True, exist_ok=True)
                    with (target / "_download_log.txt").open("a", encoding="utf-8") as output:
                        output.write(f"[桌面 App 自动恢复] {text}\n")
                except OSError:
                    pass
                self.events.put(("log", text))
                if self.stop_event.wait(delay):
                    break
        self.events.put(("done", "任务已暂停"))

    def _pause(self) -> None:
        self.manual_pause = True
        self.verification_shutdown.clear()
        self.stop_event.set()
        self.status_var.set("正在安全暂停；当前网络请求结束后保留断点…")

    def _update_file_status(self, text: str) -> None:
        if "：" not in text:
            return
        name, status = (part.strip() for part in text.split("：", 1))
        if status.startswith("下载失败") or status.startswith("Chrome 下载失败"):
            status = "failed"
        elif status.startswith("下载超时，已跳过"):
            status = "skipped_timeout"
        if not name or status not in {"下载中", "downloaded", "browser_downloaded", "skipped_exists", "skipped_timeout", "failed"}:
            return
        display = {
            "下载中": "下载中",
            "downloaded": "完成",
            "browser_downloaded": "完成（浏览器）",
            "skipped_exists": "已存在",
            "skipped_timeout": "超时，本轮跳过",
            "failed": "失败，自动重试",
        }[status]
        self.file_statuses[name] = display
        if len(self.file_statuses) > 12:
            self.file_statuses.pop(next(iter(self.file_statuses)))
        rows = self.file_tree.get_children()
        if rows:
            self.file_tree.delete(*rows)
        for file_name, file_status in self.file_statuses.items():
            self.file_tree.insert("", "end", values=(file_name, file_status))

    @staticmethod
    def _format_bytes(value: int) -> str:
        units = ("B", "KB", "MB", "GB")
        amount = float(max(0, value))
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
            amount /= 1024
        return "0 B"

    def _update_file_progress(self, name: str, done: int, total: int | None) -> None:
        if total and total > 0:
            percent = min(100, int(done * 100 / total))
            progress_text = f"{percent}% · {self._format_bytes(done)} / {self._format_bytes(total)}"
            status = f"下载中 {percent}%"
        else:
            percent = 0
            progress_text = f"已下载 {self._format_bytes(done)}"
            status = "下载中"
        self.current_file_var.set(f"当前文件：{name}")
        self.file_progress_bar.configure(maximum=100, value=percent)
        self.file_progress_text_var.set(progress_text)
        self.file_statuses[name] = status
        if len(self.file_statuses) > 12:
            self.file_statuses.pop(next(iter(self.file_statuses)))
        rows = self.file_tree.get_children()
        if rows:
            self.file_tree.delete(*rows)
        for file_name, file_status in self.file_statuses.items():
            self.file_tree.insert("", "end", values=(file_name, file_status))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    text = str(payload)
                    if requires_manual_verification(text):
                        self.verification_shutdown.arm(time.monotonic(), windows_last_input_tick())
                    elif download_progress_clears_verification(text):
                        self.verification_shutdown.clear()
                    self.status_var.set(text)
                    self._update_file_status(text)
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", text + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "progress":
                    done, total, label = payload
                    self.progress_bar.configure(maximum=max(1, int(total)), value=int(done))
                    self.progress_text_var.set(f"当前批次：{int(done)}/{int(total)}  {label}")
                elif kind == "file_progress":
                    name, done, total = payload
                    self._update_file_progress(str(name), int(done), int(total) if total is not None else None)
                elif kind in {"done", "error"}:
                    self.start_button.configure(state="normal")
                    self.login_button.configure(state="normal")
                    self.pause_button.configure(state="disabled")
                    text = str(payload)
                    self.status_var.set(text)
                    if kind == "done":
                        self.progress_text_var.set("当前批次：已结束")
                    if kind == "error":
                        messagebox.showerror("任务已停止", text + "\n\n可点击“生成诊断包”交给 Codex 排查。")
        except queue.Empty:
            pass
        if self.verification_shutdown.check(time.monotonic(), windows_last_input_tick()):
            self.verification_shutdown.clear()
            self.status_var.set("人工验证等待 10 分钟且电脑无人操作，正在自动关机…")
            subprocess.Popen(
                ["shutdown.exe", "/s", "/t", "0"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        self.after(150, self._poll_events)

    def _open_target(self) -> None:
        target = Path(self.target_var.get())
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def _diagnostics(self) -> None:
        try:
            archive = make_diagnostic_bundle(Path(self.target_var.get()))
            prompt = f"请检查 POP136 自动下载器问题，诊断包路径：{archive}"
            self.clipboard_clear()
            self.clipboard_append(prompt)
            subprocess.Popen(["explorer.exe", "/select,", str(archive)])
            messagebox.showinfo("诊断包已生成", f"已生成：\n{archive}\n\n排障提示已复制，可粘贴给 Codex。")
        except Exception as error:
            messagebox.showerror("生成失败", str(error))

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("任务运行中", "退出会请求安全暂停并保留断点。确定退出吗？"):
                return
            self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    # 2026-09-23 v1.7.7：单实例互斥。
    # 双实例并行写同一 _download_state.json 会数据竞争（当日守护重置窗口实测两实例并行）。
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\POP136Downloader_2")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        restore_app_window()
        sys.exit(0)
    app = App()
    if should_resume_from_args(sys.argv):
        app.after(500, app._start)
    app.mainloop()
