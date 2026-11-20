# -*- coding: utf-8 -*-
"""Tab 1: 線上影片下載、Colab 雲端直存派送與本機批次佇列控制器。"""

import gc
import os
import re
import socket
import tempfile
import traceback
import urllib.parse
import requests
import streamlit as st

from core.auth import create_temp_cookiefile, run_ytdlp_with_youtube_auth
from core.downloader import check_any_target_file_exists, is_work_paused
from core.ffmpeg_tools import get_ffmpeg_hls_args, get_media_duration
from core.hls import RAMDiskSpaceExceededError
from core.http_client import format_time, normalize_input_url
from core.macos_utils import check_dir_writable, load_download_dir
from parsers import get_media_items
from services import gdrive_service, ramdisk_manager
from ui.components import (
    dispatch_and_monitor_colab_task,
    download_hls_with_st_ui,
    gdrive_precheck_exists,
    mark_and_finish_output_file,
    render_download_summary_report,
    run_ffmpeg_with_st_ui,
    show_error_log_box,
)
from ui.state import (
    get_session_cookies,
    get_yt_auth_kwargs,
    pause_work_callback,
    record_failed_download,
    record_ramdisk_skipped_item,
    record_skipped_existing_item,
    resume_work_callback,
    skip_current_video_callback,
    stop_all_batch_callback,
)


def download_media(media_item, force_audio=False):
    """在 Streamlit UI 環境下執行單一媒體項目的查重、下載、封裝與分發。"""
    title = media_item["title"]
    media_url = media_item["url"]
    media_type = media_item["type"]

    ext = "mp3" if force_audio else media_item["ext"]
    filename = f"{title}.{ext}"
    is_gdrive = st.session_state.get("storage_destination") == "gdrive_cloud"
    configured_dir = st.session_state.get("download_dir", load_download_dir())

    # 0. 第一時間全覆蓋檢查目標儲存位置是否已有同名或同番號檔案
    exists, where_desc, matched_loc = check_any_target_file_exists(
        filename, title=title, download_dir=configured_dir, is_gdrive_mode=is_gdrive
    )
    if exists:
        link_md = (
            f" [🔗 開啟連結]({matched_loc})"
            if (matched_loc and matched_loc.startswith("http"))
            else ""
        )
        st.success(
            f"⏭️ **目標位置已存在同名/同番號檔案，自動跳過下載**: `{filename}`\n\n*(存在於: {where_desc})*{link_md}"
        )
        curr_proc_url = st.session_state.get("current_processing_url")
        record_skipped_existing_item(
            curr_proc_url or media_item.get("webpage_url") or media_url,
            filename,
            where_desc=where_desc,
            matched_loc=matched_loc,
        )
        if curr_proc_url:
            st.session_state[f"stage_{curr_proc_url}"] = "COMPLETED"
        return

    # 1. 優先檢查是否有活躍的背景上傳任務
    active_task = gdrive_service.get_active_upload_task(filename)
    if active_task and active_task.file_path and os.path.exists(active_task.file_path):
        mark_and_finish_output_file(active_task.file_path, filename)
        return

    # 2. 檢查 session_state 中是否已記錄此檔案已完成封裝就緒
    ready_session_path = st.session_state.get(f"ready_file_{filename}")
    if (
        ready_session_path
        and os.path.exists(ready_session_path)
        and os.path.getsize(ready_session_path) > 1024 * 1024
    ):
        mark_and_finish_output_file(ready_session_path, filename)
        return

    # 3. 跨 RAM Disk 與系統暫存區查重
    clean_sub_filename = re.sub(r'[\/\\:\*\?\"<>\|]', "_", filename)
    clean_tmp_name = f"gdrive_tmp_{clean_sub_filename}"

    cand_ready_paths = []
    if ramdisk_manager.is_ramdisk_mounted():
        cand_ready_paths.append(
            os.path.join(ramdisk_manager.MOUNT_POINT, clean_tmp_name)
        )
    cand_ready_paths.append(os.path.join(tempfile.gettempdir(), clean_tmp_name))

    for p in cand_ready_paths:
        if os.path.exists(p) and os.path.getsize(p) > 1024 * 1024:
            where_desc = (
                "RAM Disk 記憶體" if "/Volumes/MediaRAMDisk" in p else "本機暫存區"
            )
            st.success(
                f"⚡ **檔案已在 {where_desc} 封裝就緒** ({os.path.getsize(p)/1024/1024:.1f} MB)，直接進入雲端/本地分發流程！"
            )
            mark_and_finish_output_file(p, filename)
            return

    st.info(f"📍 正在處理媒體: **{title}** ({media_type})")

    total_dur = 0
    if is_gdrive:
        if gdrive_precheck_exists(filename):
            return

        extra_headers = media_item.get("headers", {})
        total_dur = (
            get_media_duration(media_url, headers=extra_headers)
            if ("m3u8" in media_url or not force_audio)
            else 0
        )
        pref_q = st.session_state.get("pref_quality", "720p")
        is_720_target = (pref_q == "720p") or any(
            k in media_url for k in ["720", "842"]
        )
        est_rate_kb = 220 if is_720_target else 420
        est_size_bytes = (
            int(total_dur * est_rate_kb * 1024)
            if total_dur and total_dur > 0
            else 0
        )
        required_free = (
            int(est_size_bytes * 2.1 + 300 * 1024 * 1024)
            if est_size_bytes > 0
            else (1024 * 1024 * 1024)
        )

        if ramdisk_manager.is_ramdisk_mounted():
            _, _, ram_free, _ = ramdisk_manager.get_ramdisk_usage()
            if est_size_bytes > 0 and ram_free < required_free:
                fallback_to_720 = False
                for b_url in media_item.get("backup_urls", []):
                    if any(k in b_url for k in ["720", "842"]):
                        est_720_bytes = int(total_dur * 220 * 1024)
                        req_720 = int(est_720_bytes * 2.1 + 300 * 1024 * 1024)
                        if ram_free >= req_720:
                            media_url = b_url
                            media_item["url"] = b_url
                            est_size_bytes = est_720_bytes
                            required_free = req_720
                            st.info(
                                f"💡 1080p 預估需 {est_size_bytes/(1024**3):.1f}GB 超出剩餘空間，已自動為您切換至 720p 高畫質串流 ({est_720_bytes/(1024**3):.1f}GB，零溢位安全下載)！"
                            )
                            fallback_to_720 = True
                            break
                if not fallback_to_720:
                    web_url = media_item.get("webpage_url") or media_url
                    dur_text = format_time(total_dur) if total_dur else "未知"
                    reason_desc = f"預估需 {required_free/(1024**3):.2f} GB 記憶體空間 (TS+MP4)，超過目前 RAM Disk 剩餘可用容量 ({ram_free/(1024**3):.2f} GB)"
                    record_ramdisk_skipped_item(
                        title=title,
                        url=web_url,
                        needed_bytes=required_free,
                        free_bytes=ram_free,
                        duration=total_dur,
                        reason=reason_desc,
                    )
                    st.warning(
                        f"⏭️ 影片「{title}」（片長約 {dur_text}）{reason_desc}。為避免 SSD 寫入損耗，已自動跳過此部。"
                    )
                    return

        temp_dir = ramdisk_manager.get_scratch_dir(min_free_bytes=required_free)
        out_path = os.path.join(temp_dir, clean_tmp_name)

        _, _tgt_label = gdrive_service.get_target_folder()
        is_ram = (
            ramdisk_manager.is_ramdisk_mounted()
            and temp_dir == ramdisk_manager.MOUNT_POINT
        )
        storage_note = "🚀 RAM Disk 純記憶體封裝" if is_ram else "本機暫存封裝"
        st.info(
            f"☁️ 檔案將在{storage_note}後，**直接直送至 Google Drive `{_tgt_label}{filename}`**（不保留於本機）"
        )
    else:
        downloads_dir = configured_dir
        is_writable, write_err = check_dir_writable(downloads_dir)
        if not is_writable:
            show_error_log_box(
                "❌ 無法下載：目標儲存目錄為唯讀或無寫入權限！",
                f"目標路徑: {downloads_dir}\n原因: {write_err}\n\n💡 建議：若為外接硬碟 (如 Windows NTFS)，macOS 預設為唯讀模式無法直接寫入。\n請至上方將儲存路徑切換為 Mac 本機資料夾 (如 Downloads)，或使用支援 macOS 寫入的磁碟格式 (如 APFS、exFAT)。",
                title="目錄寫入權限錯誤 (Read-only)",
            )
            record_failed_download(
                st.session_state.get("current_processing_url") or media_url,
                title=title,
                reason=f"目標目錄無寫入權限 ({downloads_dir}): {write_err}",
            )
            return
        os.makedirs(downloads_dir, exist_ok=True)
        out_path = os.path.join(downloads_dir, filename)

        if os.path.exists(out_path):
            st.success(f"⏭️ 檔案已存在，自動跳過: `{filename}`")
            record_skipped_existing_item(
                st.session_state.get("current_processing_url") or media_url,
                filename,
                where_desc=f"目錄 `{downloads_dir}`",
                matched_loc=out_path,
            )
            return
        st.info(f"📍 檔案將以 {ext.upper()} 格式儲存至本機: `{out_path}`")

    try:
        if media_type == "image":
            with st.spinner("⏳ 正在下載圖片 (RAM 處理中)..."):
                response = requests.get(media_url, timeout=30)
                response.raise_for_status()
                with open(out_path, "wb") as f:
                    f.write(response.content)
            mark_and_finish_output_file(out_path, filename)
            return

        extra_headers = dict(media_item.get("headers") or {})

        if "m3u8" in media_url and not force_audio:
            cand_m3u8s = [media_url] + [
                u for u in media_item.get("backup_urls", []) if u != media_url
            ]
            m3u8_content = media_item.get("m3u8_content")
            for c_idx, c_url in enumerate(cand_m3u8s):
                try:
                    if c_idx > 0:
                        st.info(
                            f"🔄 嘗試切換至備用串流線路 ({c_idx+1}/{len(cand_m3u8s)}): `{c_url}`"
                        )
                    final_out_path = download_hls_with_st_ui(
                        c_url,
                        out_path=out_path,
                        extra_headers=extra_headers,
                        max_workers=16,
                        label=title,
                        m3u8_content=m3u8_content if c_idx == 0 else None,
                    )
                    if not (
                        final_out_path
                        and os.path.exists(final_out_path)
                        and os.path.getsize(final_out_path) > 0
                    ):
                        raise ValueError("封裝後輸出檔案不存在或為空")
                    mark_and_finish_output_file(final_out_path, filename)
                    return
                except RAMDiskSpaceExceededError as ram_err:
                    if out_path and os.path.exists(out_path):
                        try:
                            os.remove(out_path)
                        except Exception:
                            pass
                    ramdisk_manager.clean_scratch_files(preserve_mp4=False)
                    web_url = media_item.get("webpage_url") or media_url
                    record_ramdisk_skipped_item(
                        title=title,
                        url=web_url,
                        needed_bytes=ram_err.needed_bytes,
                        free_bytes=ram_err.free_bytes,
                        duration=total_dur,
                        reason=ram_err.reason,
                    )
                    st.warning(
                        f"⏭️ 影片「{title}」因 RAM Disk 空間不足已安全跳過（{ram_err.reason}）。絕不降級使用 SSD 暫存，貫徹 100% 零硬碟磨損。"
                    )
                    return
                except InterruptedError:
                    if out_path and os.path.exists(out_path):
                        try:
                            os.remove(out_path)
                        except Exception:
                            pass
                    ramdisk_manager.clean_scratch_files(preserve_mp4=False)
                    return
                except Exception as hls_err:
                    if out_path and os.path.exists(out_path):
                        try:
                            os.remove(out_path)
                        except Exception:
                            pass
                    ramdisk_manager.clean_scratch_files()
                    if c_idx == len(cand_m3u8s) - 1:
                        st.warning(
                            f"⚠️ 多線程下載失敗 ({hls_err})，降級使用標準 FFmpeg 串流處理..."
                        )

        webpage_url = media_item.get("webpage_url", "")
        is_direct_stream = "m3u8" in media_url or any(
            media_url.lower().split("?")[0].endswith(e)
            for e in [".mp4", ".m4a", ".mp3", ".flv", ".ts", ".webm", ".mkv"]
        )
        is_ytdlp_site = media_item.get("is_ytdlp") or any(
            k in media_url or k in webpage_url
            for k in [
                "youtube.com",
                "youtu.be",
                "googlevideo.com",
                "bilibili.com",
                "b23.tv",
                "twitter.com",
                "x.com",
                "instagram.com",
                "tiktok.com",
                "facebook.com",
                "fb.com",
                "fb.watch",
            ]
        )

        if (is_ytdlp_site or not is_direct_stream) and not force_audio:
            target_url = webpage_url or media_url
            temp_cookie = None
            platform_name = (
                "Bilibili"
                if any(d in target_url for d in ["bilibili", "b23.tv"])
                else ("YouTube" if "youtu" in target_url else "線上")
            )
            try:
                out_base = os.path.splitext(out_path)[0]
                bili_cookie_str = st.session_state.get("bili_cookie")
                fb_cookie_str = st.session_state.get("fb_cookie")
                if any(d in target_url for d in ["bilibili.com", "b23.tv"]) and bili_cookie_str:
                    temp_cookie = create_temp_cookiefile(
                        bili_cookie_str, domain=".bilibili.com"
                    )
                elif (
                    any(d in target_url for d in ["facebook.com", "fb.com", "fb.watch"])
                    and fb_cookie_str
                ):
                    temp_cookie = create_temp_cookiefile(
                        fb_cookie_str, domain=".facebook.com"
                    )

                pref_q = st.session_state.get("pref_quality", "720p")
                max_h = 720 if pref_q == "720p" else 1080
                ydl_opts = {
                    "outtmpl": f"{out_base}.%(ext)s",
                    "quiet": True,
                    "overwrites": True,
                    "nocheckcertificate": True,
                    "legacy_server_connect": True,
                    "format": (
                        f"bestvideo[height<={max_h}][ext=mp4]+bestaudio[ext=m4a]/"
                        f"bestvideo[height<={max_h}]+bestaudio/best[height<={max_h}]/best"
                    ),
                    "merge_output_format": "mp4",
                    "http_headers": {
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                        ),
                    },
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["android", "web", "ios"],
                        }
                    },
                }
                if temp_cookie:
                    ydl_opts["cookiefile"] = temp_cookie
                if any(d in target_url for d in ["bilibili.com", "b23.tv"]):
                    ydl_opts["http_headers"]["Referer"] = "https://www.bilibili.com/"

                yt_kw = get_yt_auth_kwargs()
                with st.spinner(
                    f"⏳ 正在透過 yt-dlp 下載 {platform_name} {max_h}p 高清影片..."
                ):
                    run_ytdlp_with_youtube_auth(
                        target_url,
                        ydl_opts,
                        lambda ydl: ydl.download([target_url]),
                        auth_mode=yt_kw["yt_auth_mode"],
                        yt_cookie_str=yt_kw["yt_cookie"],
                        cached_cfb=yt_kw["cached_cfb"],
                        on_cfb_detected=yt_kw["on_cfb_detected"],
                        on_cfb_invalid=yt_kw["on_cfb_invalid"],
                    )

                actual_out = None
                if os.path.exists(out_path):
                    actual_out = out_path
                else:
                    for e in ["mp4", "mkv", "webm"]:
                        candidate = f"{out_base}.{e}"
                        if os.path.exists(candidate):
                            actual_out = candidate
                            break

                if actual_out:
                    actual_filename = (
                        os.path.basename(actual_out) if not is_gdrive else filename
                    )
                    mark_and_finish_output_file(actual_out, actual_filename)
                    return
                else:
                    show_error_log_box(
                        "❌ 下載失敗！無法產生影片檔案。",
                        f"Target Output Path: {out_path}",
                        title=f"{platform_name} 下載失敗",
                        url=target_url,
                    )
                    record_failed_download(
                        st.session_state.get("current_processing_url") or target_url,
                        title=title,
                        reason=f"{platform_name} 下載失敗：無法產生影片檔案 ({out_path})",
                    )
                    return
            except Exception as yt_err:
                err_str = str(yt_err)
                if any(
                    kw in err_str
                    for kw in [
                        "412",
                        "Precondition Failed",
                        "啥都木有",
                        "KeyError('result')",
                    ]
                ):
                    friendly_msg = "❌ Bilibili 存取受阻：觸發嗶哩嗶哩安全風控策略 (HTTP 412) 或該番劇需登入/大會員。\n\n💡 **原因與建議**：\n1. 該影片/番劇可能已下架、地區限制或需要大會員登入。\n2. 請於上方填入 **Bilibili Cookie (SESSDATA)** 以通過身分驗證及風控限制。"
                    show_error_log_box(
                        friendly_msg,
                        traceback.format_exc(),
                        title="Bilibili 412 風控限制",
                        url=target_url,
                    )
                else:
                    show_error_log_box(
                        f"❌ {platform_name} 下載失敗: {yt_err}",
                        traceback.format_exc(),
                        title=f"{platform_name} 下載詳細錯誤日誌",
                        url=target_url,
                    )
                record_failed_download(
                    st.session_state.get("current_processing_url") or target_url,
                    title=title,
                    reason=f"{platform_name} 下載失敗: {err_str}",
                )
                return
            finally:
                if temp_cookie and os.path.exists(temp_cookie):
                    try:
                        os.remove(temp_cookie)
                    except Exception:
                        pass

        is_valid_url = isinstance(media_url, str) and (
            media_url.startswith("http://") or media_url.startswith("https://")
        )
        is_valid_file = isinstance(media_url, str) and os.path.exists(media_url)

        if not is_valid_url and not is_valid_file:
            show_error_log_box(
                "❌ 無法開始下載！媒體網址或檔案路徑無效。",
                f"錯誤傳入的 Input: '{media_url}'\n說明: 該輸入非有效的 HTTP/HTTPS 網址，且本機找不到此檔案。請確認輸入的網址格式（需包含 http:// 或 https://）或本機檔案是否存在。",
                url=media_url,
            )
            record_failed_download(
                st.session_state.get("current_processing_url") or media_url,
                title=title,
                reason=f"無效的媒體網址或檔案路徑: {media_url}",
            )
            return

        if is_valid_url:
            parsed_host = urllib.parse.urlparse(media_url).hostname
            if parsed_host:
                try:
                    socket.getaddrinfo(
                        parsed_host,
                        443 if media_url.startswith("https") else 80,
                        proto=socket.IPPROTO_TCP,
                    )
                except Exception:
                    friendly_msg = f"❌ 影片伺服器網域失效：主機名稱 `{parsed_host}` 在網際網路上不存在 (DNS 解析失敗)。\n\n💡 **原因與建議**：該影片來源伺服器已被官方下線或 CDN 網域已過期失效。\n若您是在 Movieffm / Gimy 等影音網站觀看，請回到該網頁**切換其他播放線路**（如 FLV 2、FLV 3、線路 B、海外線路等）再進行下載。"
                    show_error_log_box(
                        friendly_msg,
                        f"Target Host: {parsed_host}\nTarget URL: {media_url}\nError: [Errno 8] nodename nor servname provided, or not known",
                        title="DNS 無效與伺服器已下線診斷",
                        url=media_url,
                    )
                    record_failed_download(
                        st.session_state.get("current_processing_url") or media_url,
                        title=title,
                        reason=f"DNS 解析失敗: {parsed_host}",
                    )
                    return

        headers_arg = []
        if extra_headers:
            headers_str = "".join(f"{k}: {v}\r\n" for k, v in extra_headers.items())
            headers_arg = ["-headers", headers_str]

        if "m3u8" in media_url:
            headers_arg += get_ffmpeg_hls_args()

        if force_audio:
            ffmpeg_cmd = (
                ["ffmpeg", "-y"]
                + headers_arg
                + ["-i", media_url, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", out_path]
            )
            label_text = "音訊轉碼"
        else:
            reconnect_args = [
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
                "-multiple_requests",
                "1",
            ]
            if "m3u8" in media_url:
                reconnect_args += ["-http_persistent", "1"]

            ffmpeg_cmd = (
                ["ffmpeg", "-y"]
                + reconnect_args
                + headers_arg
                + [
                    "-i",
                    media_url,
                    "-c",
                    "copy",
                    "-bsf:a",
                    "aac_adtstoasc",
                    "-movflags",
                    "+faststart",
                    out_path,
                ]
            )
            label_text = "影片下載"

        ff_total_dur = get_media_duration(media_url, headers=extra_headers)
        returncode, stderr_log = run_ffmpeg_with_st_ui(
            ffmpeg_cmd, total_duration=ff_total_dur, label=label_text
        )

        if returncode != 0 and "Unrecognized option" in stderr_log:
            simplified_cmd = ["ffmpeg", "-y"]
            if extra_headers:
                headers_str = "".join(
                    f"{k}: {v}\r\n" for k, v in extra_headers.items()
                )
                simplified_cmd += ["-headers", headers_str]
            if force_audio:
                simplified_cmd += [
                    "-i",
                    media_url,
                    "-vn",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "192k",
                    out_path,
                ]
            else:
                simplified_cmd += ["-i", media_url, "-c", "copy", out_path]
            returncode, stderr_log = run_ffmpeg_with_st_ui(
                simplified_cmd, total_duration=ff_total_dur, label=label_text
            )

        if returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            mark_and_finish_output_file(out_path, filename)
        else:
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except Exception:
                    pass
            ramdisk_manager.clean_scratch_files()

            if "No space left on device" in stderr_log or "error code: -28" in stderr_log:
                friendly_msg = "❌ 下載失敗：儲存或暫存磁碟空間耗盡 (No space left on device, Error 28)！\n\n💡 **原因與建議**：\n1. 目前影片容量超出暫存磁碟（RAM Disk 或本機磁區）剩餘空間。\n2. 若您有開啟 RAM Disk，長篇高畫質影片建議點擊下方控制列卸載後「改掛載 8GB / 12GB RAM Disk」，或暫時切換至系統本機硬碟暫存。"
                show_error_log_box(
                    friendly_msg,
                    stderr_log,
                    title="磁碟空間滿溢錯誤 (No space left on device)",
                    url=media_url,
                )
            elif any(
                k in stderr_log
                for k in [
                    "Failed to resolve hostname",
                    "nodename nor servname provided",
                    "Name or service not known",
                    "Could not resolve host",
                    "Temporary failure in name resolution",
                ]
            ):
                parsed_host = urllib.parse.urlparse(media_url).hostname or "串流伺服器"
                friendly_msg = f"❌ 影片伺服器連線失敗：串流主機網域 `{parsed_host}` 無法解析或已失效。\n\n💡 **原因與建議**：該影片來源伺服器已下線或 CDN 網址已過期。若是在 Movieffm / Gimy 等影音網站觀看，請嘗試切換至其他播放線路 (如 FLV 2, FLV 3...)。"
                show_error_log_box(
                    friendly_msg,
                    stderr_log,
                    title="伺服器連線與 DNS 錯誤日誌",
                    url=media_url,
                )
            elif "Read-only file system" in stderr_log or "read-only" in stderr_log.lower():
                friendly_msg = "❌ 下載失敗：儲存目標目錄為「唯讀 (Read-only)」！\n\n💡 **原因與建議**：您的儲存目錄位於唯讀磁碟（例如 Windows NTFS 格式外接硬碟在 macOS 預設無法寫入）。請更換儲存位置至 Mac 本機硬碟（如「下載」資料夾）或使用支援寫入的磁碟格式 (如 APFS、exFAT)。"
                show_error_log_box(
                    friendly_msg,
                    stderr_log,
                    title="唯讀磁碟寫入錯誤 (Read-only file system)",
                    url=media_url,
                )
            else:
                show_error_log_box("❌ FFmpeg 下載失敗！", stderr_log, url=media_url)
            record_failed_download(
                st.session_state.get("current_processing_url") or media_url,
                title=title,
                reason=f"FFmpeg 下載失敗 (code={returncode}): {stderr_log[-300:] if stderr_log else ''}",
            )

        gc.collect()
    except InterruptedError:
        if out_path and os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        ramdisk_manager.clean_scratch_files()
    except Exception as e:
        err_str = str(e)
        if any(
            k in err_str
            for k in [
                "Failed to resolve hostname",
                "nodename nor servname provided",
                "Name or service not known",
                "Could not resolve host",
            ]
        ):
            parsed_host = urllib.parse.urlparse(media_url).hostname or "串流伺服器"
            friendly_msg = f"❌ 影片伺服器連線失敗：串流主機網域 `{parsed_host}` 無法解析或已失效。\n\n💡 **原因與建議**：該影片來源伺服器已下線或 CDN 網址已過期。請嘗試切換其他播放線路。"
            show_error_log_box(
                friendly_msg,
                traceback.format_exc(),
                title="DNS 解析與連線錯誤日誌",
                url=media_url,
            )
        else:
            show_error_log_box(
                f"❌ 發生例外錯誤: {e}",
                traceback.format_exc(),
                title="Exception 堆疊追蹤資訊",
                url=media_url,
            )
        record_failed_download(
            st.session_state.get("current_processing_url") or media_url,
            title=title,
            reason=f"例外錯誤: {err_str}",
        )
        st.caption(
            "Note: 如果看到找不到指令的錯誤，請確認系統已安裝 FFmpeg (`brew install ffmpeg`)。"
        )
    finally:
        if is_gdrive and out_path and os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        ramdisk_manager.clean_scratch_files()


def render_download_tab():
    """渲染 Tab 1：🌐 線上影片下載。"""
    st.markdown(
        "將 Movieffm, Gimymax, MissAV, Bilibili, X, YouTube, Facebook, IG, TikTok 等影片網址直接下載。"
    )

    col_in1, col_in2 = st.columns([2.2, 1.3])
    with col_in1:
        target_urls = st.text_area(
            "🔗 請輸入影片網址 (每行一個):",
            placeholder="https://missav.ai/ipzz-850-uncensored-leak \nhttps://www.youtube.com/watch?v=... \nhttps://www.bilibili.com/video/BV... \n...",
            height=130,
        )
    with col_in2:
        st.markdown("<div style='margin-top: 4px;'></div>", unsafe_allow_html=True)
        quality_options = [
            "🟢 標準高畫質 720p (推薦：體積 1.5~2.5GB，經典不超 3G)",
            "🔵 最高畫質 1080p (體積 3.5~5.5GB，需充裕記憶體)",
            "⚡ 自動智慧適配 (優先 1080p，空間不足自動降級 720p)",
        ]
        saved_q_label = st.session_state.get("pref_quality_label", quality_options[0])
        selected_q_label = st.selectbox(
            "🎞️ 下載畫質設定",
            quality_options,
            index=quality_options.index(saved_q_label)
            if saved_q_label in quality_options
            else 0,
            help=(
                "• 🟢 720p (預設推薦)：經典輕量體積 (約 1.5~2.5GB，不超過 3G)，下載速度快一倍，在 6GB/10GB RAM Disk 零溢位極速封裝。\n"
                "• 🔵 1080p：最高解析度細節，長片體積約 4~5.5GB。\n"
                "• ⚡ 自動智慧適配：優先嘗試下載 1080p，若檢測到 RAM Disk 剩餘量不足以封裝，自動無縫降級為 720p，確保 100% 成功下載且零 SSD 磨損。"
            ),
        )
        st.session_state.pref_quality_label = selected_q_label
        if "720p" in selected_q_label:
            st.session_state.pref_quality = "720p"
        elif "自動" in selected_q_label:
            st.session_state.pref_quality = "auto"
        else:
            st.session_state.pref_quality = "1080p"

    c_cookie1, c_cookie2 = st.columns(2)
    with c_cookie1:
        fb_cookie_str = st.text_input(
            "🔑 Facebook Cookie (選填):",
            type="password",
            placeholder="c_user=xxxx; xs=xxxx; ...",
            help="若要下載私密社團、好友貼文或無法下載相片時填入。",
        )
        st.session_state.fb_cookie = fb_cookie_str
    with c_cookie2:
        bili_cookie_str = st.text_input(
            "🔑 Bilibili Cookie (選填):",
            type="password",
            placeholder="SESSDATA=xxxx; buvid3=xxxx; ...",
            help="若下載 Bilibili 遇 412 風控限制、番劇或需大會員/高畫質時填入。",
        )
        st.session_state.bili_cookie = bili_cookie_str

    yt_auth_labels = {
        "auto": "🤖 自動 (遇 Bot 驗證時自動讀取本機瀏覽器)",
        "comet": "☄️ 本機 Comet 登入狀態",
        "chrome": "🌐 本機 Chrome 登入狀態 (推薦)",
        "safari": "🧭 本機 Safari 登入狀態",
        "edge": "🔷 本機 Edge 登入狀態",
        "firefox": "🦊 本機 Firefox 登入狀態",
        "brave": "🦁 本機 Brave 登入狀態",
        "cookie": "📝 手動貼上 YouTube Cookie / cookies.txt",
    }
    yt_auth_keys = list(yt_auth_labels.keys())
    c_yt1, c_yt2 = st.columns([1.2, 1.8])
    with c_yt1:
        cur_yt_mode = st.session_state.get("yt_auth_mode", "auto")
        cur_yt_idx = (
            yt_auth_keys.index(cur_yt_mode) if cur_yt_mode in yt_auth_keys else 0
        )
        sel_yt_mode = st.selectbox(
            "🔑 YouTube 驗證 (解決 Bot 阻擋):",
            options=yt_auth_keys,
            index=cur_yt_idx,
            format_func=lambda k: yt_auth_labels[k],
            help="當 YouTube 回報 'Sign in to confirm you're not a bot' 時，自動或指定讀取您本機瀏覽器已登入的 YouTube Cookie。",
        )
        st.session_state.yt_auth_mode = sel_yt_mode
    with c_yt2:
        if sel_yt_mode == "cookie":
            yt_cookie_input = st.text_area(
                "📋 貼上 YouTube Cookie (支援瀏覽器匯出之 Netscape cookies.txt 或 Cookie 字串):",
                value=st.session_state.get("yt_cookie", ""),
                height=68,
                placeholder="# Netscape HTTP Cookie File ... 或 SAPISID=...; SID=...",
            )
            st.session_state.yt_cookie = yt_cookie_input
        else:
            st.caption(
                "💡 若選擇自動或本機瀏覽器，遇 YouTube Bot 驗證時會直接沿用您瀏覽器已登入的帳號（首次讀取 Chrome 可能跳出 macOS 鑰匙圈授權，請點「允許」）。"
            )

    current_urls = [url.strip() for url in target_urls.split("\n") if url.strip()]
    if len(current_urls) > 0:
        st.caption(f"ℹ️ 已輸入 **{len(current_urls)}** 個網址。")

    is_batch_active = st.session_state.get("is_video_batch_running", False)
    btn_video = st.button(
        "⬇️ 開始批次下載影片",
        type="primary",
        use_container_width=True,
        disabled=is_batch_active,
    )

    if btn_video:
        raw_urls = [url.strip() for url in target_urls.split("\n") if url.strip()]
        urls = [normalize_input_url(u) for u in raw_urls]

        if not urls:
            st.warning("⚠️ 請先輸入網址！")
        elif (
            st.session_state.get("storage_destination") in ("gdrive_cloud", "colab_cloud")
            and not gdrive_service.is_authenticated()
        ):
            st.error(
                "❌ **尚未連接 Google Drive**：請先在上方完成授權，或切換回「📁 本機硬碟資料夾」模式。"
            )
        elif (
            st.session_state.get("storage_destination") == "local"
            and not check_dir_writable(
                st.session_state.get("download_dir", load_download_dir())
            )[0]
        ):
            target_check_dir = st.session_state.get("download_dir", load_download_dir())
            _, reason = check_dir_writable(target_check_dir)
            st.error(
                f"❌ **無法開始下載：目標儲存目錄為唯讀或無寫入權限！**\n\n📍 路徑：`{target_check_dir}`\n⚠️ 原因：{reason}\n\n💡 請切換至本機可寫入的資料夾（如 Downloads）後再下載。"
            )
        elif st.session_state.get("storage_destination") == "colab_cloud":
            dispatch_and_monitor_colab_task(urls, only_audio=False)
        else:
            st.session_state.video_batch_queue = urls
            st.session_state.video_batch_idx = 0
            st.session_state.is_video_batch_running = True
            st.session_state.skipped_ramdisk_files = []
            st.session_state.successful_downloads = []
            st.session_state.failed_downloads = []
            st.session_state.skipped_existing_files = []
            st.session_state.last_batch_summary = None
            st.rerun()

    # ── 佇列下載控制器 ─────────────────────────────────────────────────────
    if st.session_state.get("is_video_batch_running", False):
        queue = st.session_state.get("video_batch_queue", [])
        idx = st.session_state.get("video_batch_idx", 0)

        if idx >= len(queue):
            skipped = st.session_state.get("skipped_ramdisk_files", [])
            success = st.session_state.get("successful_downloads", [])
            failed = st.session_state.get("failed_downloads", [])
            skipped_existing = st.session_state.get("skipped_existing_files", [])
            st.session_state.last_batch_summary = {
                "total_count": len(queue),
                "skipped_items": list(skipped),
                "success_items": list(success),
                "failed_items": list(failed),
                "skipped_existing_items": list(skipped_existing),
                "stopped_early": False,
            }
            st.session_state.is_video_batch_running = False
            st.session_state.video_batch_queue = []
            st.session_state.video_batch_idx = 0
            if not skipped and not failed and len(success) > 0:
                st.balloons()
            st.rerun()
        else:
            current_num = idx + 1
            url = queue[idx]

            st.markdown("<div style='margin-top: 15px;'></div>", unsafe_allow_html=True)
            ctrl_box = st.container()
            with ctrl_box:
                c_title, c_pause, c_skip, c_stop = st.columns([2.6, 1.3, 1.2, 1.0])
                with c_title:
                    st.markdown(
                        f"### 📍 批次進度：正在處理第 **{current_num} / {len(queue)}** 部"
                    )
                    st.caption(f"🔗 來源網址: `{url}`")
                with c_pause:
                    st.markdown(
                        "<div style='margin-top: 10px;'></div>", unsafe_allow_html=True
                    )
                    if is_work_paused():
                        st.button(
                            "▶️ 恢復工作",
                            key=f"resume_btn_{idx}",
                            type="primary",
                            on_click=resume_work_callback,
                            use_container_width=True,
                            help="立即喚醒下載與雲端上傳，繼續進行任務",
                        )
                    else:
                        st.button(
                            "⏸️ 暫停工作",
                            key=f"pause_btn_{idx}",
                            type="secondary",
                            on_click=pause_work_callback,
                            use_container_width=True,
                            help="立即暫停目前下載與雲端上傳，秒級釋出完整網路頻寬供線上會議等使用",
                        )
                with c_skip:
                    st.markdown(
                        "<div style='margin-top: 10px;'></div>", unsafe_allow_html=True
                    )
                    st.button(
                        "⏭️ 中斷並跳過此部",
                        key=f"skip_btn_{idx}",
                        type="secondary",
                        on_click=skip_current_video_callback,
                        args=(idx + 1,),
                        use_container_width=True,
                        help="立即中斷目前影片的下載與轉檔，直接開始清單中的下一部影片",
                    )
                with c_stop:
                    st.markdown(
                        "<div style='margin-top: 10px;'></div>", unsafe_allow_html=True
                    )
                    st.button(
                        "⏹️ 終止全部",
                        key=f"stop_btn_{idx}",
                        type="primary",
                        on_click=stop_all_batch_callback,
                        use_container_width=True,
                        help="立即終止目前的下載並取消剩餘的所有任務",
                    )

                batch_pct = idx / len(queue)
                st.progress(
                    batch_pct,
                    text=f"批次總進度: {idx}/{len(queue)} 部完成 ({batch_pct*100:.0f}%)",
                )

            st.divider()

            is_fb_group_home = False
            if "facebook.com/groups/" in url or "fb.com/groups/" in url:
                clean_url = url.split("?")[0].rstrip("/")
                parts = clean_url.split("/groups/")
                if len(parts) == 2 and "/" not in parts[1]:
                    is_fb_group_home = True

            if is_fb_group_home:
                st.warning(
                    "⚠️ 偵測到您輸入的是 **社團首頁** 的網址，而非個別貼文！\n\n👉 **正確做法**：請在社團中找到該貼文，點擊貼文下方的 **「發佈時間」**（例如：3小時前、昨天下午 5:00），進入個別貼文頁面後，再複製網址貼到下載器中。"
                )
                st.session_state.video_batch_idx = idx + 1
                st.rerun()

            st.session_state["current_processing_url"] = url
            curr_stage = st.session_state.get(f"stage_{url}")

            if curr_stage == "UPLOADING":
                saved_out = st.session_state.get(f"stage_file_{url}")
                saved_name = st.session_state.get(f"stage_name_{url}")
                if saved_out and saved_name:
                    mark_and_finish_output_file(saved_out, saved_name)
                st.session_state[f"stage_{url}"] = "COMPLETED"
                st.session_state.video_batch_idx = idx + 1
                st.rerun()
            else:
                try:
                    st.text(f"正在擷取網頁資訊: {url}")
                    with st.spinner("🔍 尋找影片串流中..."):
                        media_items = get_media_items(
                            url,
                            cookies=get_session_cookies(),
                            pref_quality=st.session_state.get("pref_quality", "720p"),
                            log_cb=st.info,
                            **get_yt_auth_kwargs(),
                        )

                    if not media_items:
                        st.error(f"❌ 找不到有效的影片串流網址: {url}")
                        record_failed_download(url, reason="找不到有效的影片串流網址")
                    else:
                        for item in media_items:
                            download_media(item)

                except Exception as e:
                    show_error_log_box(
                        f"❌ 處理 {url} 時發生錯誤: {e}",
                        traceback.format_exc(),
                        title="Exception 堆疊追蹤資訊",
                        url=url,
                    )
                    record_failed_download(url, reason=str(e))

                st.session_state[f"stage_{url}"] = "COMPLETED"
                st.session_state.video_batch_idx = idx + 1
                st.rerun()

    if not st.session_state.get("is_video_batch_running", False):
        render_download_summary_report()
