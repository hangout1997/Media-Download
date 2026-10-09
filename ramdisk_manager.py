import os
import sys
import shutil
import atexit
import tempfile
import platform
import subprocess

VOLUME_NAME = "MediaRAMDisk"
MOUNT_POINT = f"/Volumes/{VOLUME_NAME}"
DEFAULT_SIZE_GB = 4

# 模組內部紀錄關聯的 ramdisk 裝置 (例如 /dev/disk4)
_associated_device = None


def is_macos():
    return platform.system() == "Darwin"


def is_ramdisk_mounted():
    """檢查 MediaRAMDisk 是否已成功掛載且具備寫入權限。"""
    if not is_macos():
        return False
    if not os.path.ismount(MOUNT_POINT):
        return False
    # 測試是否可寫入
    test_path = os.path.join(MOUNT_POINT, ".write_test")
    try:
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
        return True
    except Exception:
        return False


def get_ramdisk_device():
    """嘗試取得 MediaRAMDisk 掛載點對應的底層裝置路徑 (例如 /dev/disk4)。"""
    global _associated_device
    if _associated_device and os.path.exists(_associated_device):
        return _associated_device
    if not os.path.ismount(MOUNT_POINT):
        return None
    try:
        out = subprocess.check_output(["df", MOUNT_POINT], text=True)
        lines = out.strip().splitlines()
        if len(lines) >= 2:
            dev = lines[1].split()[0]
            # 轉換 /dev/disk4s1 為 base 裝置 /dev/disk4
            import re
            m = re.match(r"(/dev/disk\d+)", dev)
            if m:
                _associated_device = m.group(1)
                return _associated_device
    except Exception:
        pass
    return None


def mount_ramdisk(size_gb=DEFAULT_SIZE_GB):
    """
    在 macOS 記憶體中劃分指定大小的 APFS RAM Disk 並掛載於 /Volumes/MediaRAMDisk。
    回傳 (success: bool, message: str)
    """
    global _associated_device
    if not is_macos():
        return False, "RAM Disk 僅支援 macOS (Darwin) 作業系統"

    if is_ramdisk_mounted():
        return True, f"RAM Disk 已存在於 {MOUNT_POINT}，可直接使用。"

    # 清理舊的殘留空目錄（若有）
    if os.path.exists(MOUNT_POINT) and not os.path.ismount(MOUNT_POINT):
        try:
            os.rmdir(MOUNT_POINT)
        except Exception:
            pass

    # 扇區數計算：每扇區 512 bytes
    sectors = int(size_gb * 1024 * 1024 * 1024 / 512)

    try:
        # 1. 劃分 RAM 裝置
        attach_cmd = ["hdiutil", "attach", "-nomount", f"ram://{sectors}"]
        dev_bytes = subprocess.check_output(attach_cmd, stderr=subprocess.PIPE)
        raw_dev = dev_bytes.decode("utf-8", errors="ignore").strip()
        # 提取 /dev/diskX
        import re
        m = re.search(r"(/dev/disk\d+)", raw_dev)
        if not m:
            return False, f"建立 RAM 裝置失敗，輸出非預期：{raw_dev}"
        dev = m.group(1)
        _associated_device = dev

        # 2. 格式化為 APFS 並命名為 MediaRAMDisk
        erase_cmd = ["diskutil", "eraseVolume", "APFS", VOLUME_NAME, dev]
        res = subprocess.run(erase_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            # 嘗試回退至 HFS+ 格式化
            erase_hfs = ["diskutil", "eraseVolume", "HFS+", VOLUME_NAME, dev]
            res_hfs = subprocess.run(erase_hfs, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res_hfs.returncode != 0:
                # 釋放裝置
                subprocess.run(["hdiutil", "detach", dev], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _associated_device = None
                return False, f"格式化 RAM Disk 失敗: {res.stderr or res_hfs.stderr}"

        if not is_ramdisk_mounted():
            return False, "RAM Disk 格式化完成但掛載未就緒"

        return True, f"✅ 成功掛載 {size_gb}GB APFS RAM Disk 於 {MOUNT_POINT}"

    except Exception as e:
        return False, f"掛載 RAM Disk 時發生例外: {e}"


def unmount_ramdisk():
    """
    安全卸載並完全釋放 RAM Disk 佔用的記憶體空間。
    回傳 (success: bool, message: str)
    """
    global _associated_device
    if not is_macos():
        return True, "非 macOS 系統"

    dev = get_ramdisk_device()
    unmounted = False

    if os.path.exists(MOUNT_POINT) or os.path.ismount(MOUNT_POINT):
        try:
            # 先嘗試正常卸載
            res = subprocess.run(["diskutil", "unmount", "force", MOUNT_POINT], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0:
                unmounted = True
        except Exception:
            pass

    if dev:
        try:
            # 釋放 ram:// 裝置
            subprocess.run(["diskutil", "eject", dev], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["hdiutil", "detach", dev, "-force"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _associated_device = None
            unmounted = True
        except Exception:
            pass

    if not is_ramdisk_mounted():
        return True, f"✅ 已成功卸載 RAM Disk 並將記憶體全數歸還系統。"
    return False, "⚠️ 卸載 RAM Disk 時可能仍有檔案開啟，請稍後重試。"


def clean_scratch_files():
    """清理 RAM Disk 內的所有暫存檔案，保留磁區結構。"""
    if not is_ramdisk_mounted():
        return 0
    cleaned = 0
    try:
        for fname in os.listdir(MOUNT_POINT):
            if fname.startswith("."):
                continue
            fpath = os.path.join(MOUNT_POINT, fname)
            try:
                if os.path.isfile(fpath) or os.path.islink(fpath):
                    os.remove(fpath)
                    cleaned += 1
                elif os.path.isdir(fpath):
                    shutil.rmtree(fpath, ignore_errors=True)
                    cleaned += 1
            except Exception:
                pass
    except Exception:
        pass
    return cleaned


def get_ramdisk_usage():
    """
    取得 RAM Disk 使用狀況。
    回傳 (total_bytes, used_bytes, free_bytes, usage_pct)
    若未掛載則回傳 (0, 0, 0, 0.0)
    """
    if not is_ramdisk_mounted():
        return 0, 0, 0, 0.0
    try:
        stat = os.statvfs(MOUNT_POINT)
        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bavail
        used = total - free
        pct = (used / total) if total > 0 else 0.0
        return total, used, free, pct
    except Exception:
        return 0, 0, 0, 0.0


def get_scratch_dir(min_free_bytes=500 * 1024 * 1024):
    """
    取得建議的暫存處理目錄：
    - 若 RAM Disk 已掛載且剩餘空間大於 min_free_bytes (預設 500MB)，優先使用 RAM Disk（100% 零 SSD 磨損）
    - 若未啟用或空間不足，自動安全降級為系統暫存目錄 (tempfile.gettempdir())
    """
    if is_ramdisk_mounted():
        _, _, free, _ = get_ramdisk_usage()
        if free >= min_free_bytes:
            return MOUNT_POINT
    return tempfile.gettempdir()


# 於行程結束時嘗試自動卸載
def _auto_cleanup_at_exit():
    try:
        if is_ramdisk_mounted():
            unmount_ramdisk()
    except Exception:
        pass

atexit.register(_auto_cleanup_at_exit)
