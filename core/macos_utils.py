# -*- coding: utf-8 -*-
"""本機系統操作輔助工具：下載目錄管理、寫入測試、資料夾選取器、Finder/播放器與記憶體釋放。"""

import gc
import glob
import json
import os
import subprocess
import sys
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DEFAULT_DOWNLOAD_DIR = "/Users/ericcheng/Downloads"


def _get_service(name):
    if name in sys.modules:
        return sys.modules[name]
    try:
        mod = __import__(f"services.{name}", fromlist=[name])
        return mod
    except ImportError:
        return __import__(name)


def load_download_dir():
    """從 config.json 讀取本機下載儲存目錄。"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                d = data.get("download_dir", "").strip()
                if d:
                    return d
        except Exception:
            pass
    return DEFAULT_DOWNLOAD_DIR


def save_download_dir(path):
    """儲存並建立本機下載儲存目錄至 config.json。"""
    if not path or not path.strip():
        path = DEFAULT_DOWNLOAD_DIR
    path = os.path.abspath(os.path.expanduser(path.strip()))
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    gdrive_service = _get_service("gdrive_service")
    gdrive_service.update_config(download_dir=path)
    return path


def check_dir_writable(path):
    """
    檢查資料夾是否具備寫入權限 (含偵測 macOS 對 Windows NTFS 等唯讀掛載磁碟)
    回傳 (is_writable: bool, reason: str)
    """
    if not path or not str(path).strip():
        return False, "未指定資料夾路徑"
    abs_path = os.path.abspath(os.path.expanduser(str(path).strip()))

    if not os.path.exists(abs_path):
        try:
            os.makedirs(abs_path, exist_ok=True)
        except OSError as e:
            if getattr(e, "errno", None) == 30 or "read-only" in str(e).lower():
                return (
                    False,
                    "此磁碟為唯讀檔案系統 (Read-only file system，如 Windows NTFS 外接硬碟在 macOS 預設無法寫入)。",
                )
            if getattr(e, "errno", None) == 13 or "permission denied" in str(e).lower():
                return False, "權限不足，無法在此路徑建立目錄 (Permission denied)。"
            return False, f"無法建立目錄: {e}"

    test_file = os.path.join(
        abs_path, f".write_test_{os.getpid()}_{int(time.time()*1000)}.tmp"
    )
    try:
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("test")
        try:
            os.remove(test_file)
        except Exception:
            pass
        return True, ""
    except OSError as e:
        if getattr(e, "errno", None) == 30 or "read-only" in str(e).lower():
            return (
                False,
                "此磁碟為唯讀檔案系統 (Read-only file system，如 Windows NTFS 格式外接硬碟在 macOS 預設僅供讀取)。",
            )
        if getattr(e, "errno", None) == 13 or "permission denied" in str(e).lower():
            return False, "權限不足，無法在此目錄建立或寫入檔案 (Permission denied)。"
        return False, f"無法寫入此目錄: {e}"


def choose_folder_dialog(current_dir):
    """開啟作業系統原生資料夾選擇對話框。"""
    if not current_dir or not os.path.exists(current_dir):
        current_dir = DEFAULT_DOWNLOAD_DIR
    try:
        os.makedirs(current_dir, exist_ok=True)
    except Exception:
        current_dir = DEFAULT_DOWNLOAD_DIR

    current_dir = os.path.abspath(current_dir)

    # 1. macOS native Finder folder chooser via osascript
    if sys.platform == "darwin":
        try:
            safe_dir = current_dir.replace('"', '\\"')
            script = (
                f'POSIX path of (choose folder with prompt "請選擇下載儲存資料夾" '
                f'default location POSIX file "{safe_dir}")'
            )
            result = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                res_path = result.stdout.strip()
                if res_path and os.path.exists(res_path):
                    return res_path
        except Exception:
            pass

        try:
            script = 'POSIX path of (choose folder with prompt "請選擇下載儲存資料夾")'
            result = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                res_path = result.stdout.strip()
                if res_path and os.path.exists(res_path):
                    return res_path
        except Exception:
            pass

    # 2. Linux zenity
    if sys.platform.startswith("linux"):
        try:
            cmd = [
                "zenity",
                "--file-selection",
                "--directory",
                f"--filename={current_dir}",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                res_path = result.stdout.strip()
                if res_path and os.path.exists(res_path):
                    return res_path
        except Exception:
            pass

    # 3. PyQt5 / PyQt6 / PySide6 / PySide2 Fallback
    for qt_mod in [
        "PyQt6.QtWidgets",
        "PyQt5.QtWidgets",
        "PySide6.QtWidgets",
        "PySide2.QtWidgets",
    ]:
        try:
            mod = __import__(qt_mod, fromlist=["QApplication", "QFileDialog"])
            QApplication = getattr(mod, "QApplication")
            QFileDialog = getattr(mod, "QFileDialog")
            _ = QApplication.instance() or QApplication([])
            folder = QFileDialog.getExistingDirectory(
                None, "請選擇下載儲存資料夾", current_dir
            )
            if folder and os.path.exists(folder):
                return folder
            break
        except Exception:
            pass

    # 4. Fallback to tkinter
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(
            initialdir=current_dir, title="請選擇下載儲存資料夾"
        )
        root.destroy()
        if folder and os.path.exists(folder):
            return folder
    except Exception:
        pass

    return None


def open_in_finder(path):
    """在 macOS Finder 中定位並高亮選取檔案。"""
    try:
        if path and os.path.exists(path):
            subprocess.Popen(["open", "-R", path])
    except Exception:
        pass


def open_media_player(path):
    """以系統預設播放器開啟媒體檔案。"""
    try:
        if path and os.path.exists(path):
            subprocess.Popen(["open", path])
    except Exception:
        pass


def force_os_memory_trim():
    """強制 macOS 系統底層釋放 Python 進程中已回收但未返還作業系統之虛擬記憶體分頁。"""
    gc.collect()
    try:
        import ctypes

        libc = ctypes.CDLL(None)
        if hasattr(libc, "malloc_zone_pressure_relief"):
            libc.malloc_zone_pressure_relief(None, 0)
    except Exception:
        pass


def release_system_resources(clear_cache_cb=None):
    """執行垃圾回收、清空暫存 Cookie、終止殘留進程並清理 RAM Disk。"""
    released_info = []

    # 1. 垃圾回收與 macOS 記憶體歸還
    collected = gc.collect()
    force_os_memory_trim()
    released_info.append(
        f"🧹 已執行 Python 垃圾回收 (清理 {collected} 個物件) 並歸還系統分頁 (malloc_zone_pressure_relief)。"
    )

    # 2. 清除 UI 快取（由呼叫端注入）
    if clear_cache_cb:
        try:
            clear_cache_cb()
            released_info.append("💾 已清除應用程式快取數據。")
        except Exception as e:
            released_info.append(f"⚠️ 清除快取時發生錯誤: {e}")

    # 3. 刪除暫存 Cookie 檔案
    temp_dir = tempfile.gettempdir()
    cookie_patterns = [
        os.path.join(temp_dir, "fb_cookies_*.txt"),
        os.path.join(temp_dir, "ytdlp_cookies_*.txt"),
        os.path.join(os.getcwd(), "fb_cookies_*.txt"),
        os.path.join(os.getcwd(), "ytdlp_cookies_*.txt"),
    ]

    deleted_files = 0
    for pattern in cookie_patterns:
        for fpath in glob.glob(pattern):
            try:
                os.remove(fpath)
                deleted_files += 1
            except Exception:
                pass

    if deleted_files > 0:
        released_info.append(f"🗑️ 已成功清理 {deleted_files} 個暫存 Cookie 檔案。")
    else:
        released_info.append("✨ 未偵測到殘留的暫存 Cookie 檔案。")

    # 4. 嘗試關閉殘留的 ffmpeg 與 yt-dlp 進程
    try:
        subprocess.run(
            ["pkill", "-f", "ffmpeg"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["pkill", "-f", "yt-dlp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        released_info.append("⚙️ 已嘗試終止背景殘留的 FFmpeg 與 yt-dlp 進程。")
    except Exception as e:
        released_info.append(f"⚠️ 嘗試終止進程時發生錯誤: {e}")

    # 5. 清理 RAM Disk 暫存檔案
    ramdisk_manager = _get_service("ramdisk_manager")
    if ramdisk_manager.is_ramdisk_mounted():
        c_ram = ramdisk_manager.clean_all_ramdisk_files()
        if c_ram > 0:
            released_info.append(f"🚀 已清空 RAM Disk 暫存區中 {c_ram} 個檔案。")
        else:
            released_info.append("🚀 RAM Disk 暫存區已為空。")

    return released_info
