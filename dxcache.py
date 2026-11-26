# -*- coding: utf-8 -*-
"""
NVIDIA DXCache 迁移工具（GUI 版）
====================================
将 C 盘上的 DXCache 文件夹迁移到其他分区，并在原位置创建 NTFS 联接（Junction），
让显卡驱动仍然以为缓存在 C 盘，实际读写被系统静默转发到目标分区。

功能：
  1. 查看当前 DXCache 状态（是否已迁移、体积、文件数、实际存储位置）
  2. 迁移到其他 NTFS 分区（自动停止可能占用文件的 NVIDIA 进程）
  3. 一键还原（解除联接并把数据搬回 C 盘）

要求：
  - Windows 10 / 11
  - Python 3.8+（tkinter 标准库，无第三方依赖）
  - 管理员权限
"""

import os
import sys
import time
import ctypes
import shutil
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from pathlib import Path

# ------------------------------------------------------------
# 常量与 Windows API
# ------------------------------------------------------------
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

NVIDIA_PROCESS_NAMES = [
    "NVIDIA Share.exe",
    "NVIDIA GeForce Experience.exe",
    "NVIDIA Overlay.exe",
    "NVIDIA Web Helper.exe",
    "nvcontainer.exe",
    "NVIDIA App.exe",
    "nvsphelper64.exe",
]

DEFAULT_FONT = ("Microsoft YaHei UI", 9)


# ------------------------------------------------------------
# 提权相关
# ------------------------------------------------------------
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_as_admin():
    """用管理员身份重新启动自身，成功返回 True。"""
    try:
        script = os.path.abspath(sys.argv[0])
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
        # 用 pythonw 时提权后没有控制台，但 GUI 正常；用 python 时同理
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            f'"{script}" {params}'.strip(), None, 1)
        return ret > 32
    except Exception:
        return False


# ------------------------------------------------------------
# 文件系统探测
# ------------------------------------------------------------
def get_fs_type(root):
    """获取分区文件系统类型（NTFS / FAT32 / exFAT ...）。"""
    fs_name = ctypes.create_unicode_buffer(261)
    vol_name = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_ulong()
    max_len = ctypes.c_ulong()
    flags = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), vol_name, 261,
        ctypes.byref(serial), ctypes.byref(max_len), ctypes.byref(flags),
        fs_name, 261)
    return fs_name.value if ok else "未知"


def is_reparse_point(path):
    """判断路径是否为重解析点（Junction / SymbolicLink）。"""
    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    if attrs == INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def _clean_target(t):
    if not t:
        return t
    if t.startswith("\\\\?\\"):
        return t[4:]
    if t.startswith("\\??\\"):
        return t[4:]
    return t


def get_junction_target(path):
    """获取 Junction 目标。优先 os.readlink，失败回退 PowerShell。"""
    try:
        return _clean_target(os.readlink(str(path)))
    except OSError:
        pass
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item -LiteralPath '{path}' -Force).Target"],
            text=True, encoding="utf-8", errors="ignore", timeout=10)
        return _clean_target(out.strip()) or None
    except Exception:
        return None


def get_dir_stats(path):
    """递归统计目录体积与文件数（不跟随子目录联接，避免循环）。"""
    total = 0
    count = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat().st_size
                            count += 1
                    except OSError:
                        pass
        except OSError:
            pass
    return total, count


def format_size(size):
    s = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if s < 1024:
            return f"{s:.2f} {unit}"
        s /= 1024
    return f"{s:.2f} PB"


def get_available_drives():
    drives = []
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        fs = get_fs_type(root)
        drives.append({
            "letter": letter, "root": root,
            "free": usage.free, "total": usage.total, "fs": fs,
        })
    return drives


# ------------------------------------------------------------
# 系统操作封装
# ------------------------------------------------------------
def stop_nvidia_processes():
    """停止可能占用缓存文件的 NVIDIA 进程，返回已停止的进程名列表。"""
    stopped = []
    for name in NVIDIA_PROCESS_NAMES:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", name],
            capture_output=True, text=True, encoding="gbk", errors="ignore")
        if result.returncode == 0:
            stopped.append(name)
    return stopped


def move_with_robocopy(src, dst):
    """用 robocopy /MOVE 移动目录内容。返回 (成功, 输出)。"""
    result = subprocess.run(
        ["robocopy", str(src), str(dst), "/E", "/MOVE", "/R:1", "/W:1",
         "/NFL", "/NDL", "/NJH", "/NJS"],
        capture_output=True, text=True, encoding="gbk", errors="ignore")
    # robocopy 退出码：0~7 表示成功，8 及以上为失败
    return result.returncode < 8, (result.stdout or "") + (result.stderr or "")


def create_junction(link, target):
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", f'"{link}"', f'"{target}"'],
        capture_output=True, text=True, encoding="gbk", errors="ignore")
    return result.returncode == 0, (result.stdout or "") + (result.stderr or "")


def remove_junction(path):
    """移除 Junction（不会删除目标内容）。"""
    try:
        os.rmdir(str(path))
        return True, ""
    except OSError as e:
        result = subprocess.run(
            ["cmd", "/c", "rmdir", f'"{path}"'],
            capture_output=True, text=True, encoding="gbk", errors="ignore")
        return result.returncode == 0, (result.stdout or "") + (result.stderr or "")


# ------------------------------------------------------------
# 目标分区选择对话框
# ------------------------------------------------------------
class DriveSelectDialog(tk.Toplevel):
    def __init__(self, parent, drives):
        super().__init__(parent)
        self.title("选择目标分区")
        self.result = None
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)

        tk.Label(self, text="请选择用于存储 DXCache 的目标分区（仅支持 NTFS）：",
                 font=DEFAULT_FONT, anchor="w",
                 padx=12, pady=10).pack(fill="x")

        self.var = tk.StringVar()
        frame = tk.Frame(self, padx=12, pady=0)
        frame.pack(fill="both", expand=True)

        first_ntfs = None
        for d in drives:
            free_gb = d["free"] / (1024 ** 3)
            total_gb = d["total"] / (1024 ** 3)
            fs_note = "" if d["fs"].upper() == "NTFS" else "  ← 非 NTFS，不可用"
            text = (f"{d['letter']}:\\   [{d['fs']}]   "
                    f"可用 {free_gb:.1f} GB / 共 {total_gb:.1f} GB{fs_note}")
            rb = tk.Radiobutton(frame, text=text, value=d["letter"],
                                variable=self.var, font=DEFAULT_FONT, anchor="w")
            rb.pack(fill="x", pady=2)
            if d["fs"].upper() == "NTFS" and first_ntfs is None:
                first_ntfs = d["letter"]

        if first_ntfs:
            self.var.set(first_ntfs)
        else:
            tk.Label(frame, text="没有检测到任何 NTFS 分区，无法迁移。",
                     fg="red", font=DEFAULT_FONT).pack(anchor="w", pady=8)

        btn_frame = tk.Frame(self, padx=12, pady=12)
        btn_frame.pack(fill="x")
        tk.Button(btn_frame, text="取消", width=10, font=DEFAULT_FONT,
                  command=self.destroy).pack(side="right", padx=(6, 0))
        tk.Button(btn_frame, text="确定", width=10, font=DEFAULT_FONT,
                  command=self._on_ok).pack(side="right")

        # 居中到父窗口
        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw = parent.winfo_width()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + 80}")

    def _on_ok(self):
        letter = self.var.get()
        if not letter:
            messagebox.showwarning("提示", "请选择一个分区。", parent=self)
            return
        fs = get_fs_type(f"{letter}:\\")
        if fs.upper() != "NTFS":
            messagebox.showerror(
                "错误",
                f"{letter}: 的文件系统是 {fs}，不是 NTFS。\n"
                "NTFS 联接（Junction）需要目标分区为 NTFS。",
                parent=self)
            return
        self.result = letter
        self.destroy()


# ------------------------------------------------------------
# 主窗口
# ------------------------------------------------------------
class DXCacheApp:
    def __init__(self, root):
        self.root = root
        root.title("NVIDIA DXCache 迁移工具（GUI 版）")
        root.geometry("510x340")
        root.minsize(510, 300)

        localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        self.source = Path(localappdata) / "NVIDIA" / "DXCache"
        self.busy = False

        self._build_ui()

        self.log("=" * 60)
        self.log(f"DXCache 源路径：{self.source}")
        self.log("提示：迁移前建议关闭所有游戏和 NVIDIA 相关软件。")
        self.refresh_status_async()

    # ---------- UI ----------
    def _build_ui(self):
        # 顶部状态区
        status_frame = ttk.LabelFrame(self.root, text="当前状态", padding=10)
        status_frame.pack(fill="x", padx=12, pady=(12, 6))

        self.lbl_path = tk.Label(status_frame, text="路径：-", anchor="w", font=DEFAULT_FONT)
        self.lbl_path.pack(fill="x")
        self.lbl_state = tk.Label(status_frame, text="状态：-", anchor="w", font=DEFAULT_FONT)
        self.lbl_state.pack(fill="x")
        self.lbl_target = tk.Label(status_frame, text="实际存储：-", anchor="w", font=DEFAULT_FONT)
        self.lbl_target.pack(fill="x")
        self.lbl_stats = tk.Label(status_frame, text="体积 / 文件数：-", anchor="w", font=DEFAULT_FONT)
        self.lbl_stats.pack(fill="x")

        # 按钮区
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill="x", padx=12, pady=6)

        self.btn_refresh = tk.Button(btn_frame, text="刷新状态", width=12,
                                     font=DEFAULT_FONT, command=self.refresh_status_async)
        self.btn_refresh.pack(side="left")

        self.btn_migrate = tk.Button(btn_frame, text="迁移到其他分区...", width=18,
                                     font=DEFAULT_FONT, command=self.on_migrate)
        self.btn_migrate.pack(side="left", padx=8)

        self.btn_rollback = tk.Button(btn_frame, text="一键还原到 C 盘", width=16,
                                      font=DEFAULT_FONT, command=self.on_rollback)
        self.btn_rollback.pack(side="left")

        self.btn_open = tk.Button(btn_frame, text="打开缓存目录", width=14,
                                  font=DEFAULT_FONT, command=self.on_open_dir)
        self.btn_open.pack(side="right")

        # 日志区
        log_frame = ttk.LabelFrame(self.root, text="日志", padding=6)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(6, 12))

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=16, state="disabled", wrap="word",
            font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    # ---------- 日志 ----------
    def log(self, msg):
        self.root.after(0, self._append_log, msg)

    def _append_log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    # ---------- 忙碌状态 ----------
    def set_busy(self, busy):
        self.busy = busy
        self.btn_refresh.config(state="disabled" if busy else "normal")
        if busy:
            self.btn_migrate.config(state="disabled")
            self.btn_rollback.config(state="disabled")

    # ---------- 状态刷新 ----------
    def refresh_status_async(self):
        if self.busy:
            return
        self.set_busy(True)
        self.lbl_state.config(text="状态：正在检测...", fg="gray")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        info = {"path": str(self.source), "exists": self.source.exists(),
                "is_junction": False, "target": None, "size": 0, "count": 0}
        if info["exists"]:
            info["is_junction"] = is_reparse_point(self.source)
            if info["is_junction"]:
                info["target"] = get_junction_target(self.source)
            scan_path = info["target"] if (info["target"] and os.path.exists(info["target"])) else info["path"]
            try:
                info["size"], info["count"] = get_dir_stats(scan_path)
            except Exception:
                pass
        self.root.after(0, self._update_status_ui, info)

    def _update_status_ui(self, info):
        self.lbl_path.config(text=f"路径：{info['path']}")

        if not info["exists"]:
            self.lbl_state.config(
                text="状态：缓存目录不存在（请先运行一次游戏或图形程序）", fg="#c07000")
            self.lbl_target.config(text="实际存储：-")
            self.lbl_stats.config(text="体积 / 文件数：-")
            self.btn_migrate.config(state="disabled")
            self.btn_rollback.config(state="disabled")
        elif info["is_junction"]:
            self.lbl_state.config(text="状态：已迁移（NTFS 联接）", fg="#1a7f37")
            self.lbl_target.config(text=f"实际存储：{info['target'] or '（无法读取）'}")
            self.lbl_stats.config(
                text=f"体积 / 文件数：{format_size(info['size'])} / {info['count']:,} 个文件")
            self.btn_migrate.config(state="disabled")
            self.btn_rollback.config(state="normal")
        else:
            self.lbl_state.config(
                text="状态：未迁移（普通文件夹，占用 C 盘空间）", fg="#c07000")
            self.lbl_target.config(text="实际存储：-")
            self.lbl_stats.config(
                text=f"体积 / 文件数：{format_size(info['size'])} / {info['count']:,} 个文件")
            self.btn_migrate.config(state="normal")
            self.btn_rollback.config(state="disabled")

        self.set_busy(False)

    # ---------- 打开目录 ----------
    def on_open_dir(self):
        try:
            target = self.source
            if self.source.exists() and is_reparse_point(self.source):
                t = get_junction_target(self.source)
                if t and os.path.exists(t):
                    target = Path(t)
            if not target.exists():
                messagebox.showinfo("提示", "缓存目录不存在。")
                return
            os.startfile(str(target))
        except Exception as e:
            messagebox.showerror("错误", f"无法打开目录：{e}")

    # ---------- 迁移 ----------
    def on_migrate(self):
        if self.busy:
            return
        if not self.source.exists():
            messagebox.showwarning(
                "提示", "缓存目录不存在。\n请先运行一次游戏或图形程序，让驱动生成缓存后再迁移。")
            return
        if is_reparse_point(self.source):
            messagebox.showinfo("提示", "DXCache 已经迁移过了。\n如需更换目标，请先执行“一键还原”。")
            return

        drives = get_available_drives()
        if not drives:
            messagebox.showerror("错误", "没有检测到任何非 C 盘分区。")
            return

        dlg = DriveSelectDialog(self.root, drives)
        self.root.wait_window(dlg)
        if not dlg.result:
            return

        letter = dlg.result
        target = Path(f"{letter}:\\NVIDIA\\DXCache")

        if not messagebox.askyesno(
                "确认迁移",
                f"将把 DXCache 迁移到：\n{target}\n\n"
                "C 盘原路径会保留一个 NTFS 联接（Junction），\n"
                "显卡驱动读写不受影响。\n\n"
                "迁移前请关闭所有游戏和 NVIDIA 相关软件。\n"
                "是否继续？"):
            return

        self.set_busy(True)
        self.log("=" * 60)
        self.log(f"开始迁移：\n  源：{self.source}\n  目标：{target}")
        threading.Thread(target=self._migrate_worker, args=(target,), daemon=True).start()

    def _migrate_worker(self, target):
        try:
            # 1) 停止可能占用文件的 NVIDIA 进程
            self.log("正在尝试释放文件占用（停止 NVIDIA 相关进程）...")
            stopped = stop_nvidia_processes()
            if stopped:
                self.log(f"已停止进程：{', '.join(stopped)}")
                time.sleep(2)
            else:
                self.log("未发现可停止的 NVIDIA 进程，继续。")

            # 2) 准备目标目录
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and any(target.iterdir()):
                    self.log(f"目标目录已存在且非空：{target}")
                    self.log("将把源内容合并到该目录（同名文件会被覆盖）。")
                target.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self.log(f"无法创建目标目录：{e}")
                self.root.after(0, self._migrate_done, False)
                return

            # 3) robocopy /MOVE
            self.log("正在复制并移动缓存文件（robocopy /MOVE）...")
            ok, out = move_with_robocopy(self.source, target)
            if not ok:
                self.log("移动失败！robocopy 输出末尾：")
                for line in out.strip().splitlines()[-12:]:
                    self.log("  " + line)
                self.log("常见原因：文件仍被占用。")
                self.log("建议：关闭所有游戏 / NVIDIA 软件后重试；或在安全模式下运行本工具。")
                self.root.after(0, self._migrate_done, False)
                return

            # 4) 清理残留源目录
            if self.source.exists():
                try:
                    self.source.rmdir()
                    self.log("已删除 C 盘上的空源目录。")
                except OSError:
                    # 仍有文件占用时重命名，为创建 junction 让路
                    try:
                        backup = str(self.source) + ".old"
                        if os.path.exists(backup):
                            shutil.rmtree(backup, ignore_errors=True)
                        self.source.rename(backup)
                        self.log(f"源目录仍被占用，已重命名为：{backup}（可稍后手动删除）")
                    except OSError as e2:
                        self.log(f"无法清理源目录：{e2}")
                        self.root.after(0, self._migrate_done, False)
                        return

            # 5) 创建 Junction
            self.log("正在创建 NTFS 联接（Junction）...")
            ok, out = create_junction(self.source, target)
            if not ok:
                self.log(f"创建联接失败：{out.strip()}")
                self.log(f"数据已移动到：{target}")
                self.log(f"可手动执行：mklink /J \"{self.source}\" \"{target}\"")
                self.root.after(0, self._migrate_done, False)
                return

            self.log("✔ 迁移完成！")
            self.log(f"  C 盘路径：{self.source}")
            self.log(f"  实际存储：{target}")
            self.root.after(0, self._migrate_done, True)
        except Exception as e:
            self.log(f"发生异常：{e}")
            self.root.after(0, self._migrate_done, False)

    def _migrate_done(self, success):
        if success:
            messagebox.showinfo("完成", "DXCache 迁移完成！\n后续读写会自动转发到目标分区。")
        else:
            messagebox.showerror("失败", "迁移未完成，请查看日志中的原因。")
        self.refresh_status_async()

    # ---------- 还原 ----------
    def on_rollback(self):
        if self.busy:
            return
        if not self.source.exists() or not is_reparse_point(self.source):
            messagebox.showinfo("提示", "当前不是联接状态，无需还原。")
            return
        target = get_junction_target(self.source)
        if not target:
            messagebox.showerror("错误", "无法读取联接目标路径。")
            return

        if not messagebox.askyesno(
                "确认还原",
                f"将把缓存数据从：\n{target}\n搬回：\n{self.source}\n\n"
                "并解除 NTFS 联接。\n"
                "建议先关闭所有游戏和 NVIDIA 相关软件。\n"
                "是否继续？"):
            return

        self.set_busy(True)
        self.log("=" * 60)
        self.log(f"开始还原：\n  源：{target}\n  目标：{self.source}")
        threading.Thread(target=self._rollback_worker, args=(target,), daemon=True).start()

    def _rollback_worker(self, target):
        try:
            self.log("正在停止可能占用文件的进程...")
            stopped = stop_nvidia_processes()
            if stopped:
                self.log(f"已停止进程：{', '.join(stopped)}")
                time.sleep(2)

            # 1) 移除 junction（仅删除联接本身，目标数据不动）
            self.log("正在解除 NTFS 联接...")
            ok, out = remove_junction(self.source)
            if not ok:
                self.log(f"解除联接失败：{out.strip()}")
                self.log("请关闭所有游戏 / NVIDIA 软件后重试。")
                self.root.after(0, self._rollback_done, False)
                return

            # 2) 搬回数据
            self.log("正在把缓存数据搬回 C 盘...")
            ok, out = move_with_robocopy(target, self.source)
            if not ok:
                self.log("搬回失败！robocopy 输出末尾：")
                for line in out.strip().splitlines()[-12:]:
                    self.log("  " + line)
                self.log(f"数据仍在：{target}，可手动复制回 {self.source}")
                self.root.after(0, self._rollback_done, False)
                return

            self.log("✔ 还原完成。C 盘缓存路径已恢复为普通文件夹。")
            self.root.after(0, self._rollback_done, True)
        except Exception as e:
            self.log(f"发生异常：{e}")
            self.root.after(0, self._rollback_done, False)

    def _rollback_done(self, success):
        if success:
            messagebox.showinfo("完成", "DXCache 已还原到 C 盘。")
        else:
            messagebox.showerror("失败", "还原未完成，请查看日志中的原因。")
        self.refresh_status_async()


# ------------------------------------------------------------
# 入口
# ------------------------------------------------------------
def main():
    if not is_admin():
        if run_as_admin():
            sys.exit(0)
        # 提权失败，允许用户手动确认以受限方式继续（绝大多数操作会失败，但至少能看界面）
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "需要管理员权限",
                "本工具需要管理员权限才能创建 NTFS 联接。\n"
                "请右键选择“以管理员身份运行”，或通过管理员终端启动本脚本。")
            root.destroy()
        except Exception:
            pass
        sys.exit(1)

    root = tk.Tk()
    DXCacheApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()