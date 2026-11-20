# -*- coding: utf-8 -*-
"""Tab 2: 影片音訊提取（支援本機檔案/資料夾與線上播放清單/Colab 提取）。"""

import gc
import os
import socket
import traceback
import urllib.parse
import streamlit as st

from core.auth import run_ytdlp_with_youtube_auth
from core.downloader import extract_audio_to_path, get_abort_event
from core.ffmpeg_tools import get_ffmpeg_hls_args
from core.http_client import format_time, normalize_input_url
from core.macos_utils import check_dir_writable, load_download_dir
from parsers import get_media_items
from services import gdrive_service, ramdisk_manager
from ui.components import (
    dispatch_and_monitor_colab_task,
    finish_output_file,
    gdrive_precheck_exists,
    show_error_log_box,
)
from ui.state import get_session_cookies, get_yt_auth_kwargs


def extract_local_audio(video_path, audio_format, title=None, headers=None):
    """在 Streamlit UI 下執行單支影片或線上串流的音訊提取。"""
    base_name = title if title else os.path.splitext(os.path.basename(video_path))[0]

    is_gdrive = st.session_state.get("storage_destination") == "gdrive_cloud"
    if is_gdrive:
        out_dir = ramdisk_manager.get_scratch_dir()
    else:
        out_dir = st.session_state.get("download_dir", load_download_dir())
        is_writable, write_err = check_dir_writable(out_dir)
        if not is_writable:
            show_error_log_box(
                "❌ 無法提取音訊：目標儲存目錄為唯讀或無寫入權限！",
                f"目標路徑: {out_dir}\n原因: {write_err}\n\n💡 建議：若為外接硬碟 (如 Windows NTFS)，macOS 預設為唯讀模式無法直接寫入。\n請至上方將儲存路徑切換為 Mac 本機資料夾 (如 Downloads)，或使用支援 macOS 寫入的磁碟格式 (如 APFS、exFAT)。",
                title="目錄寫入權限錯誤 (Read-only)",
            )
            return
        os.makedirs(out_dir, exist_ok=True)

    is_valid_url = isinstance(video_path, str) and (
        video_path.startswith("http://") or video_path.startswith("https://")
    )
    is_valid_file = isinstance(video_path, str) and os.path.exists(video_path)

    if not is_valid_url and not is_valid_file:
        show_error_log_box(
            "❌ 無法提取音訊！檔案或網址格式無效",
            f"傳入的路徑或網址: '{video_path}'\n說明: 該輸入非有效的 HTTP/HTTPS 網址，且本機找不到此檔案。請確認網址是否包含 http:// 或 https://，或確認本地檔案路徑正確。",
            url=video_path,
        )
        return

    if is_valid_url:
        parsed_host = urllib.parse.urlparse(video_path).hostname
        if parsed_host:
            try:
                socket.getaddrinfo(
                    parsed_host,
                    443 if video_path.startswith("https") else 80,
                    proto=socket.IPPROTO_TCP,
                )
            except Exception:
                friendly_msg = f"❌ 影片伺服器網域失效：主機名稱 `{parsed_host}` 在網際網路上不存在 (DNS 解析失敗)。\n\n💡 **原因與建議**：該影片來源伺服器已被官方下線或 CDN 網域已過期失效。\n若您是在 Movieffm / Gimy 等影音網站觀看，請回到該網頁**切換其他播放線路**（如 FLV 2、FLV 3、線路 B、海外線路等）再進行音訊提取。"
                show_error_log_box(
                    friendly_msg,
                    f"Target Host: {parsed_host}\nTarget URL: {video_path}\nError: [Errno 8] nodename nor servname provided, or not known",
                    title="DNS 無效與伺服器已下線診斷",
                    url=video_path,
                )
                return

    is_youtube_or_online = is_valid_url and ("m3u8" not in video_path.lower())
    is_mono_96k = "96kbps" in audio_format or "單聲道" in audio_format

    # 預先判斷預期輸出檔名以執行存在跳過檢查
    if is_youtube_or_online:
        target_ext = (
            "mp3"
            if audio_format == "MP3"
            else ("m4a" if audio_format == "M4A" else "mp4")
        )
        expected_filename = f"{base_name}.{target_ext}"
        expected_out_path = os.path.join(out_dir, expected_filename)
        if not is_gdrive and os.path.exists(expected_out_path):
            st.success(f"⏭️ 檔案已存在: `{expected_out_path}`")
            return
        if is_gdrive and gdrive_precheck_exists(expected_filename):
            return
    else:
        if is_mono_96k or audio_format in ("MP4 (AAC音訊 - 相容AI轉錄)", "MP4"):
            expected_ext = "mp4"
        elif audio_format == "MP3":
            expected_ext = "mp3"
        elif audio_format == "M4A":
            expected_ext = "m4a"
        else:
            # 預設原始格式依實際音軌決定，快速探測避免重複轉碼
            expected_ext = "mp4"
            try:
                import subprocess

                headers_arg = []
                if headers:
                    headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                    headers_arg = ["-headers", headers_str]
                if "m3u8" in video_path:
                    headers_arg += get_ffmpeg_hls_args()
                probe_cmd = (
                    ["ffprobe", "-v", "error"]
                    + headers_arg
                    + [
                        "-select_streams",
                        "a:0",
                        "-show_entries",
                        "stream=codec_name",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        video_path,
                    ]
                )
                codec = (
                    subprocess.check_output(
                        probe_cmd,
                        text=True,
                        stdin=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                    .strip()
                    .lower()
                )
                if codec in ["mp3", "opus"]:
                    expected_ext = codec
            except Exception:
                pass
        expected_out_path = os.path.join(out_dir, f"{base_name}.{expected_ext}")
        if not is_gdrive and os.path.exists(expected_out_path):
            st.success(f"⏭️ 檔案已存在: `{expected_out_path}`")
            return
        if is_gdrive and gdrive_precheck_exists(os.path.basename(expected_out_path)):
            return

    cookies = get_session_cookies()
    yt_kw = get_yt_auth_kwargs()

    if is_youtube_or_online:
        try:
            with st.spinner("⏳ 正在透過 yt-dlp 從線上網址提取高品質音訊..."):
                matched_file = extract_audio_to_path(
                    video_path,
                    audio_format=audio_format,
                    out_dir=out_dir,
                    title=title,
                    headers=headers,
                    cookies=cookies,
                    **yt_kw,
                )
            if is_gdrive:
                finish_output_file(matched_file, os.path.basename(matched_file))
            else:
                st.success(f"✅ 提取完成！音訊已儲存至: `{matched_file}`")
            gc.collect()
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
                friendly_msg = "❌ Bilibili 存取受阻：觸發嗶哩嗶哩安全風控策略 (HTTP 412) 或該番劇需登入/大會員。\n\n💡 **原因與建議**：\n1. 該影片/番劇可能已下架、地區限制或需要大會員登入。\n2. 請於「線上影片下載」分頁填入 **Bilibili Cookie (SESSDATA)** 以通過身分驗證及風控限制。"
                show_error_log_box(
                    friendly_msg,
                    traceback.format_exc(),
                    title="Bilibili 412 風控限制",
                    url=video_path,
                )
            else:
                show_error_log_box(
                    f"❌ 線上網址音訊提取失敗: {yt_err}",
                    traceback.format_exc(),
                    title="線上網址音訊提取詳細錯誤日誌",
                    url=video_path,
                )
        return

    # 本地檔案或 m3u8 音訊提取（使用 Streamlit 進度條）
    progress_bar = st.progress(0.0)
    status_text = st.empty()

    def _ff_prog(pct, current_sec, total_duration, speed):
        if total_duration > 0:
            pct_num = pct * 100
            progress_bar.progress(pct)
            status_text.markdown(
                f"⏳ **音訊提取進度**: `{pct_num:.1f}%` ({format_time(current_sec)} / {format_time(total_duration)}) | 速度: `{speed}`"
            )
        else:
            status_text.markdown(
                f"⏳ **音訊提取處理中...** 已完成 `{format_time(current_sec)}` | 速度: `{speed}`"
            )

    try:
        out_path = extract_audio_to_path(
            video_path,
            audio_format=audio_format,
            out_dir=out_dir,
            title=title,
            headers=headers,
            cookies=cookies,
            ffmpeg_progress_cb=_ff_prog,
            abort_event=get_abort_event(),
            warning_cb=st.warning,
            **yt_kw,
        )
        progress_bar.empty()
        status_text.empty()
        if is_gdrive:
            finish_output_file(out_path, os.path.basename(out_path))
        else:
            st.success(f"✅ 提取完成！音訊已儲存至: `{out_path}`")
        gc.collect()
    except Exception as e:
        progress_bar.empty()
        status_text.empty()
        err_str = str(e)
        if any(
            k in err_str
            for k in [
                "Failed to resolve hostname",
                "nodename nor servname provided",
                "Name or service not known",
                "Could not resolve host",
                "Temporary failure in name resolution",
            ]
        ):
            parsed_host = urllib.parse.urlparse(video_path).hostname or "串流伺服器"
            friendly_msg = f"❌ 影片伺服器連線失敗：主機網域 `{parsed_host}` 無法解析或已失效。\n\n💡 **原因與建議**：該影片來源伺服器已被下線或 CDN 網域已過期。若是在 Movieffm / Gimy 等影音網站，請嘗試切換至其他播放線路 (如 FLV 2, FLV 3...)。"
            show_error_log_box(
                friendly_msg,
                err_str if "FFmpeg" not in err_str else traceback.format_exc(),
                title="伺服器連線與 DNS 錯誤日誌",
                url=video_path,
            )
        elif "Read-only file system" in err_str or "read-only" in err_str.lower():
            friendly_msg = "❌ 提取音訊失敗：儲存目標目錄為「唯讀 (Read-only)」！\n\n💡 **原因與建議**：您的儲存目錄位於唯讀磁碟（例如 Windows NTFS 格式外接硬碟在 macOS 預設無法寫入）。請更換儲存位置至 Mac 本機硬碟（如「下載」資料夾）或使用支援寫入的磁碟格式 (如 APFS、exFAT)。"
            show_error_log_box(
                friendly_msg,
                err_str,
                title="唯讀磁碟寫入錯誤 (Read-only file system)",
                url=video_path,
            )
        else:
            show_error_log_box(
                f"❌ 提取 `{base_name}` 失敗: {e}",
                traceback.format_exc(),
                title="音訊提取錯誤日誌",
                url=video_path,
            )


def render_audio_tab():
    """渲染 Tab 2：📁 影片音訊提取。"""
    st.markdown("從本地檔案或線上播放清單提取出純音訊，完全在記憶體內處理，避免硬碟損耗。")
    local_video_path = st.text_input(
        "📁 請輸入路徑 (本地影片/資料夾，或 YouTube, FB, IG 等網址/播放清單):",
        placeholder="/Users/ericcheng/Movies/ 或 https://youtube.com/playlist?list=... 或 https://www.facebook.com/watch/?v=...",
    )
    audio_format = st.selectbox(
        "🎵 請選擇輸出音訊格式:",
        [
            "預設 (原始格式 - AAC一律存為MP4)",
            "96kbps, 單聲道 (MP4 - AI轉錄推薦)",
            "MP4 (AAC音訊 - 相容AI轉錄)",
            "MP3",
            "M4A",
        ],
    )

    if st.button("▶️ 開始提取音訊", type="primary", use_container_width=True):
        raw_input_path = local_video_path.strip()
        input_path = normalize_input_url(raw_input_path)
        if not input_path:
            st.warning("⚠️ 請先輸入路徑！")
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
                f"❌ **無法開始提取：目標儲存目錄為唯讀或無寫入權限！**\n\n📍 路徑：`{target_check_dir}`\n⚠️ 原因：{reason}\n\n💡 請切換至本機可寫入的資料夾（如 Downloads）後再試。"
            )
        elif input_path.startswith("http://") or input_path.startswith("https://"):
            urls_to_process = []
            ydl_opts = {
                "quiet": True,
                "extract_flat": "in_playlist",
                "nocheckcertificate": True,
                "legacy_server_connect": True,
            }
            yt_kw = get_yt_auth_kwargs()

            try:
                with st.spinner("🔍 正在解析線上網址/播放清單..."):
                    info = run_ytdlp_with_youtube_auth(
                        input_path,
                        ydl_opts,
                        lambda ydl: ydl.extract_info(input_path, download=False),
                        auth_mode=yt_kw["yt_auth_mode"],
                        yt_cookie_str=yt_kw["yt_cookie"],
                        cached_cfb=yt_kw["cached_cfb"],
                        on_cfb_detected=yt_kw["on_cfb_detected"],
                        on_cfb_invalid=yt_kw["on_cfb_invalid"],
                    )
                    if "entries" in info:
                        for entry in info["entries"]:
                            if not entry:
                                continue
                            u = (
                                entry.get("webpage_url")
                                or entry.get("url")
                                or entry.get("original_url")
                            )
                            if u and not u.startswith(("http://", "https://")):
                                if entry.get("id"):
                                    u = f"https://www.youtube.com/watch?v={entry.get('id')}"
                            if u:
                                urls_to_process.append(u)
                    else:
                        urls_to_process.append(input_path)
            except Exception as e:
                st.error(f"❌ 解析網址失敗: {e}")

            urls_to_process = [u for u in urls_to_process if u]

            if urls_to_process:
                if st.session_state.get("storage_destination") == "colab_cloud":
                    dispatch_and_monitor_colab_task(urls_to_process, only_audio=True)
                else:
                    st.info(f"📥 準備處理 {len(urls_to_process)} 個線上影片...")
                    progress_bar = st.progress(0)
                    cookies = get_session_cookies()
                    pref_q = st.session_state.get("pref_quality", "720p")

                    for i, url in enumerate(urls_to_process):
                        st.markdown(
                            f"### 📍 正在處理第 {i+1}/{len(urls_to_process)} 個影片..."
                        )
                        try:
                            with st.spinner("🔍 尋找最佳串流..."):
                                media_items = get_media_items(
                                    url,
                                    cookies=cookies,
                                    pref_quality=pref_q,
                                    log_cb=st.info,
                                    **yt_kw,
                                )

                            if media_items:
                                for item in media_items:
                                    target_media = (
                                        item["url"]
                                        if (
                                            item["url"].startswith("http://")
                                            or item["url"].startswith("https://")
                                        )
                                        else item.get("webpage_url", item["url"])
                                    )
                                    extract_local_audio(
                                        target_media,
                                        audio_format,
                                        title=item["title"],
                                        headers=item.get("headers"),
                                    )
                            else:
                                st.error(f"❌ 無法取得串流: {url}")
                        except Exception as e:
                            show_error_log_box(
                                f"❌ 發生例外錯誤: {e}",
                                traceback.format_exc(),
                                title="Exception 堆疊追蹤資訊",
                            )

                        progress_bar.progress((i + 1) / len(urls_to_process))
                        st.divider()

                    st.balloons()
                    st.success("🎉 所有線上提取任務處理完畢！")

        elif not os.path.exists(input_path):
            st.error("❌ 找不到指定的路徑，請確認路徑正確！")
        else:
            video_files = []
            if os.path.isfile(input_path):
                video_files.append(input_path)
            elif os.path.isdir(input_path):
                valid_exts = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".ts"}
                for root, _, files in os.walk(input_path):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in valid_exts:
                            video_files.append(os.path.join(root, f))

                if not video_files:
                    st.warning("⚠️ 該資料夾內找不到任何支援的影片檔案！")

            if video_files:
                st.info(f"📥 準備處理 {len(video_files)} 個影片檔案...")
                progress_bar = st.progress(0)

                for i, v_path in enumerate(video_files):
                    st.markdown(
                        f"### 📍 正在處理第 {i+1}/{len(video_files)} 個檔案: `{os.path.basename(v_path)}`"
                    )
                    extract_local_audio(v_path, audio_format)
                    progress_bar.progress((i + 1) / len(video_files))
                    st.divider()

                st.balloons()
                st.success("🎉 所有本地提取任務處理完畢！")
