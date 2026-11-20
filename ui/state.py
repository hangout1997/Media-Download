# -*- coding: utf-8 -*-
"""Streamlit Session State 初始化、Callbacks 與批次任務紀錄管理。"""

import os
import shutil
import time
import streamlit as st

from core.downloader import abort_active_download
from core.http_client import format_time
from core.macos_utils import (
    choose_folder_dialog,
    load_download_dir,
    open_in_finder,
    open_media_player,
    save_download_dir,
)
from services import gdrive_service


DEST_LOCAL = "📁 本機硬碟資料夾 (Local Disk)"
DEST_GDRIVE = "☁️ Google Drive 雲端直送 (不存本地硬碟)"
DEST_COLAB = "🤖 Google Colab 雲端直存 (0 本地頻寬)"


def init_session_state():
    """初始化 Streamlit Session State 預設值並顯示待辦 Toast。"""
    if "download_dir" not in st.session_state:
        st.session_state.download_dir = load_download_dir()
    if "main_dir_input" not in st.session_state:
        st.session_state.main_dir_input = st.session_state.download_dir
    if "storage_destination" not in st.session_state:
        st.session_state.storage_destination = gdrive_service.load_config().get(
            "storage_destination", "local"
        )
    if "gdrive_skip_dup" not in st.session_state:
        st.session_state.gdrive_skip_dup = gdrive_service.load_config().get(
            "gdrive_skip_duplicates", True
        )
    if "gdrive_save_local_copy" not in st.session_state:
        st.session_state.gdrive_save_local_copy = gdrive_service.load_config().get(
            "gdrive_save_local_copy", False
        )

    if "dest_radio" not in st.session_state:
        if st.session_state.storage_destination == "colab_cloud":
            st.session_state.dest_radio = DEST_COLAB
        elif st.session_state.storage_destination == "gdrive_cloud":
            st.session_state.dest_radio = DEST_GDRIVE
        else:
            st.session_state.dest_radio = DEST_LOCAL

    if "pending_toast" in st.session_state:
        t_msg, t_icon = st.session_state.pop("pending_toast")
        st.toast(t_msg, icon=t_icon)


def get_session_cookies():
    """取得目前使用者填入的 Cookie 字典（同時相容本機與 Colab 鍵名）。"""
    fb_c = st.session_state.get("fb_cookie", "") or ""
    bili_c = st.session_state.get("bili_cookie", "") or ""
    yt_c = st.session_state.get("yt_cookie", "") or ""
    return {
        "fb_cookie": fb_c,
        "bili_cookie": bili_c,
        "yt_cookie": yt_c,
        "facebook": fb_c,
        "bilibili": bili_c,
        "youtube": yt_c,
    }


def get_yt_auth_kwargs():
    """組裝傳給 parsers / core.auth 的 YouTube 驗證參數與 callbacks。"""
    return {
        "yt_auth_mode": st.session_state.get("yt_auth_mode", "auto"),
        "yt_cookie": (st.session_state.get("yt_cookie") or "").strip(),
        "cached_cfb": st.session_state.get("yt_auto_detected_cfb"),
        "on_cfb_detected": lambda cfb: st.session_state.__setitem__(
            "yt_auto_detected_cfb", cfb
        ),
        "on_cfb_invalid": lambda: st.session_state.pop("yt_auto_detected_cfb", None),
    }


# ── UI 互動 Callbacks ────────────────────────────────────────────────────────
def pause_work_callback():
    gdrive_service.pause_all_work()
    st.session_state.is_batch_paused = True


def resume_work_callback():
    gdrive_service.resume_all_work()
    st.session_state.is_batch_paused = False


def skip_current_video_callback(next_idx):
    abort_active_download()
    gdrive_service.resume_all_work()
    st.session_state.is_batch_paused = False

    curr_url = st.session_state.get("current_processing_url")
    if curr_url:
        st.session_state[f"stage_{curr_url}"] = "SKIPPED"
        curr_file = st.session_state.get(f"stage_file_{curr_url}")
        curr_name = st.session_state.get(f"stage_name_{curr_url}")
        if curr_name:
            gdrive_service.cancel_upload_task(curr_name)
        local_saved = st.session_state.get(f"saved_local_{curr_name}", False)
        if not local_saved and curr_file and os.path.exists(curr_file):
            try:
                os.remove(curr_file)
            except Exception:
                pass

    gdrive_service.cancel_all_upload_tasks()
    st.session_state.video_batch_idx = next_idx


def stop_all_batch_callback():
    abort_active_download()
    gdrive_service.resume_all_work()
    st.session_state.is_batch_paused = False
    gdrive_service.cancel_all_upload_tasks()

    curr_url = st.session_state.get("current_processing_url")
    if curr_url:
        st.session_state[f"stage_{curr_url}"] = "STOPPED"
        curr_file = st.session_state.get(f"stage_file_{curr_url}")
        curr_name = st.session_state.get(f"stage_name_{curr_url}")
        local_saved = st.session_state.get(f"saved_local_{curr_name}", False)
        if not local_saved and curr_file and os.path.exists(curr_file):
            try:
                os.remove(curr_file)
            except Exception:
                pass

    skipped = st.session_state.get("skipped_ramdisk_files", [])
    success = st.session_state.get("successful_downloads", [])
    failed = st.session_state.get("failed_downloads", [])
    skipped_existing = st.session_state.get("skipped_existing_files", [])
    queue = st.session_state.get("video_batch_queue", [])
    total_tasks = (
        len(queue)
        if queue
        else (len(skipped) + len(success) + len(failed) + len(skipped_existing))
    )
    if skipped or success or failed or skipped_existing:
        st.session_state.last_batch_summary = {
            "total_count": total_tasks,
            "skipped_items": list(skipped),
            "success_items": list(success),
            "failed_items": list(failed),
            "skipped_existing_items": list(skipped_existing),
            "stopped_early": True,
        }
    st.session_state.is_video_batch_running = False
    st.session_state.video_batch_queue = []
    st.session_state.video_batch_idx = 0


def cb_choose_folder():
    chosen = choose_folder_dialog(st.session_state.download_dir)
    if chosen:
        saved = save_download_dir(chosen)
        st.session_state.download_dir = saved
        st.session_state.main_dir_input = saved
        st.session_state.pending_toast = (
            f"✅ 已成功將儲存位置更改為：\n{saved}",
            "📁",
        )
    else:
        st.session_state.pending_toast = ("ℹ️ 未選擇新資料夾或取消選擇", "💡")


def cb_set_folder(target_path, name):
    saved = save_download_dir(target_path)
    st.session_state.download_dir = saved
    st.session_state.main_dir_input = saved
    st.session_state.pending_toast = (f"✅ 已切換至 {name}", "📁")


def cb_dir_input_change():
    val = st.session_state.main_dir_input
    if val:
        st.session_state.download_dir = save_download_dir(val)


def cb_dest_change():
    if st.session_state.dest_radio == DEST_COLAB:
        mode = "colab_cloud"
    elif st.session_state.dest_radio == DEST_GDRIVE:
        mode = "gdrive_cloud"
    else:
        mode = "local"
    st.session_state.storage_destination = mode
    gdrive_service.update_config(storage_destination=mode)


def cb_save_local(src_path, dest_path, key):
    try:
        if src_path and os.path.exists(src_path):
            shutil.copy2(src_path, dest_path)
            st.session_state[key] = True
    except Exception as e:
        st.session_state[f"err_{key}"] = str(e)


def cb_open_finder(path):
    open_in_finder(path)


def cb_play_video(path):
    open_media_player(path)


# ── 任務報告紀錄輔助函式 ────────────────────────────────────────────────────
def record_ramdisk_skipped_item(
    title, url, needed_bytes=0, free_bytes=0, duration=0, reason=""
):
    """記錄因 RAM Disk 空間不足而跳過的檔案資訊，以便在任務結束時彙整報告。"""
    if "skipped_ramdisk_files" not in st.session_state:
        st.session_state.skipped_ramdisk_files = []

    for item in st.session_state.skipped_ramdisk_files:
        if (url and item.get("url") == url) or (title and item.get("title") == title):
            return

    try:
        dur_str = format_time(duration) if duration and duration > 0 else "未知"
    except Exception:
        dur_str = f"{int(duration)} 秒" if duration and duration > 0 else "未知"

    needed_str = f"{needed_bytes / (1024**3):.2f} GB" if needed_bytes > 0 else "超過上限"
    free_str = f"{free_bytes / (1024**3):.2f} GB" if free_bytes > 0 else "0 GB"

    st.session_state.skipped_ramdisk_files.append({
        "title": title or "未知標題",
        "url": url or "",
        "duration": dur_str,
        "needed_size": needed_str,
        "free_size": free_str,
        "reason": reason,
        "time": time.strftime("%H:%M:%S"),
    })


def record_successful_download(title, filename, out_path, is_gdrive=False):
    """記錄成功下載與封裝的檔案資訊。"""
    if "successful_downloads" not in st.session_state:
        st.session_state.successful_downloads = []

    for item in st.session_state.successful_downloads:
        if item.get("filename") == filename:
            return

    file_sz = os.path.getsize(out_path) if (out_path and os.path.exists(out_path)) else 0
    if file_sz >= 1024 ** 3:
        sz_str = f"{file_sz / (1024**3):.2f} GB"
    elif file_sz > 0:
        sz_str = f"{file_sz / (1024**2):.1f} MB"
    else:
        sz_str = "未知"

    dest_desc = "Google Drive (雲端直送)" if is_gdrive else f"本機硬碟 ({out_path})"

    st.session_state.successful_downloads.append({
        "title": title or filename,
        "filename": filename,
        "size_str": sz_str,
        "destination": dest_desc,
        "time": time.strftime("%H:%M:%S"),
    })


def record_failed_download(url, title="", reason=""):
    """記錄下載失敗的網址與錯誤原因，避免被 st.rerun() 洗掉後誤報全數成功。"""
    if "failed_downloads" not in st.session_state:
        st.session_state.failed_downloads = []

    for item in st.session_state.failed_downloads:
        if url and item.get("url") == url and item.get("reason") == reason:
            return

    st.session_state.failed_downloads.append({
        "title": title or url or "未知項目",
        "url": url or "",
        "reason": reason or "未知錯誤",
        "time": time.strftime("%H:%M:%S"),
    })


def record_skipped_existing_item(url, filename, where_desc="", matched_loc=""):
    """記錄因目標位置或跨資料夾已存在同名檔案而自動跳過的項目。"""
    if "skipped_existing_files" not in st.session_state:
        st.session_state.skipped_existing_files = []

    for item in st.session_state.skipped_existing_files:
        if item.get("filename") == filename:
            return

    st.session_state.skipped_existing_files.append({
        "filename": filename,
        "url": url or "",
        "where_desc": where_desc or "目標資料夾",
        "matched_loc": matched_loc or "",
        "time": time.strftime("%H:%M:%S"),
    })
