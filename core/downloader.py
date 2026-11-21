# -*- coding: utf-8 -*-
"""通用媒體下載入口、音訊提取核心、檔案/番號查重與中止控制。"""

import gc
import glob
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse

import requests
import yt_dlp

from core.auth import (
    create_temp_cookiefile,
    normalize_cookies_dict,
    run_ytdlp_with_youtube_auth,
)
from core.ffmpeg_tools import (
    get_audio_stream_info,
    get_ffmpeg_hls_args,
    get_media_duration,
    run_ffmpeg_with_progress,
)
from core.hls import RAMDiskSpaceExceededError, download_fast_parallel_hls
from core.http_client import default_log_info
from core.macos_utils import load_download_dir

# ── 全域下載任務即時取消/中斷信號（防止刷新網頁遺留孤兒下載線程）───────────
_DOWNLOAD_ABORT_EVENT = threading.Event()
_ACTIVE_DOWNLOAD_LOCK = threading.Lock()
_ACTIVE_DOWNLOAD_INFO = {"active": False, "title": "", "time": 0}


def _get_service(name):
    if name in sys.modules:
        return sys.modules[name]
    try:
        return __import__(f"services.{name}", fromlist=[name])
    except ImportError:
        try:
            return __import__(name)
        except ImportError:
            return None


def get_abort_event():
    return _DOWNLOAD_ABORT_EVENT


def get_active_download_info():
    return _ACTIVE_DOWNLOAD_INFO


def get_active_download_lock():
    return _ACTIVE_DOWNLOAD_LOCK


def is_work_paused():
    gs = _get_service("gdrive_service")
    return gs.is_work_paused() if gs else False


def abort_active_download():
    """強制秒級中斷當前所有正在進行的切片下載、轉檔與封裝。"""
    _DOWNLOAD_ABORT_EVENT.set()
    with _ACTIVE_DOWNLOAD_LOCK:
        _ACTIVE_DOWNLOAD_INFO["active"] = False
        _ACTIVE_DOWNLOAD_INFO["title"] = ""

    gs = _get_service("gdrive_service")
    if gs:
        try:
            pause_ev = gs.get_pause_event()
            if not pause_ev.is_set():
                pause_ev.set()
        except Exception:
            pass
        try:
            gs.cancel_all_upload_tasks()
        except Exception:
            pass

    rm = _get_service("ramdisk_manager")
    if rm:
        try:
            rm.clean_scratch_files(preserve_mp4=False)
        except Exception:
            pass


def check_any_target_file_exists(
    filename, title="", download_dir=None, is_gdrive_mode=False
):
    """
    全方位檢查目標儲存位置是否已經存在該檔案 (支援精確匹配與番號智慧匹配)：
    1. 本地下載目標資料夾 (download_dir)
    2. Rclone 掛載目錄 (/Users/ericcheng/GDrive/Download, /Users/ericcheng/GDrive)
    3. Google Drive 雲端目錄 (若使用雲端模式且已認證)
    """
    code_match = re.search(
        r"([A-Za-z0-9]{2,10}-\d{2,6})",
        (filename or "") + " " + (title or ""),
        re.IGNORECASE,
    )
    code = code_match.group(1).upper() if code_match else None

    configured_dir = download_dir or load_download_dir()
    cand_dirs = [
        configured_dir,
        "/Users/ericcheng/GDrive/Download",
        "/Users/ericcheng/GDrive",
        os.path.expanduser("~/Downloads"),
    ]
    seen_dirs = set()
    unique_cand_dirs = []
    for d in cand_dirs:
        if d and d not in seen_dirs:
            seen_dirs.add(d)
            unique_cand_dirs.append(d)

    for d in unique_cand_dirs:
        if not (d and os.path.exists(d) and os.path.isdir(d)):
            continue
        try:
            for f in os.listdir(d):
                if f.startswith("."):
                    continue
                f_path = os.path.join(d, f)
                if f == filename or f.replace("_", " ") == filename.replace("_", " "):
                    if os.path.isfile(f_path) and os.path.getsize(f_path) > 1024 * 1024:
                        return True, f"目錄 `{d}`", f_path
                if (
                    code
                    and f.upper().startswith(code)
                    and any(
                        f.lower().endswith(ext)
                        for ext in [".mp4", ".mkv", ".mov", ".ts"]
                    )
                ):
                    if os.path.isfile(f_path):
                        sz = os.path.getsize(f_path)
                        if sz > 50 * 1024 * 1024:
                            return (
                                True,
                                f"目錄 `{d}` (番號 `{code}` 吻合，大小 {sz/1024/1024/1024:.2f}GB)",
                                f_path,
                            )
        except Exception:
            pass

    if is_gdrive_mode:
        gs = _get_service("gdrive_service")
        if gs and gs.is_authenticated():
            try:
                folder_id = gs.resolve_target_folder_id()
                existing = gs.find_existing_file(filename, folder_id)
                if existing:
                    _, label = gs.get_target_folder()
                    link = existing.get("webViewLink", "")
                    sz_bytes = int(existing.get("size", 0))
                    sz_str = f" ({sz_bytes/1024/1024/1024:.2f}GB)" if sz_bytes > 0 else ""
                    return True, f"Google Drive 雲端 `{label}`{sz_str}", link
            except Exception:
                pass

    return False, "", ""


def download_media_to_path(
    media_item,
    out_path,
    force_audio=False,
    cookies=None,
    progress_cb=None,
    status_cb=None,
    toast_cb=None,
    log_cb=None,
    ffmpeg_progress_cb=None,
    scratch_dir=None,
    pref_quality="720p",
    yt_auth_mode="auto",
    yt_cookie="",
    cached_cfb=None,
    on_cfb_detected=None,
    on_cfb_invalid=None,
    abort_event=None,
    pause_event=None,
    strict_ramdisk=False,
    track_active=False,
):
    """
    通用媒體下載入口 (支援 HLS 多線程切片、yt-dlp、直接串流與圖片下載)。
    """
    title = media_item.get("title", "media")
    media_url = media_item.get("url", "")
    media_type = media_item.get("type", "video")
    norm_cookies = normalize_cookies_dict(cookies)

    def _log(msg):
        if log_cb:
            log_cb(msg)
        else:
            default_log_info(msg)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    # 1. 圖片處理
    if media_type == "image":
        response = requests.get(media_url, timeout=30)
        response.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(response.content)
        return out_path

    extra_headers = dict(media_item.get("headers") or {})

    # 2. HLS / m3u8 多線程切片下載
    last_hls_err = None
    hls_error_summary = []
    if "m3u8" in media_url and not force_audio:
        cand_m3u8s = [media_url] + [
            u for u in media_item.get("backup_urls", []) if u != media_url
        ]
        m3u8_content = media_item.get("m3u8_content")
        for c_idx, c_url in enumerate(cand_m3u8s):
            try:
                if c_idx > 0:
                    _log(f"🔄 嘗試切換至備用串流線路 ({c_idx+1}/{len(cand_m3u8s)}): `{c_url}`")
                final_out = download_fast_parallel_hls(
                    c_url,
                    out_path=out_path,
                    extra_headers=extra_headers,
                    max_workers=16,
                    label=title,
                    progress_cb=progress_cb,
                    status_cb=status_cb,
                    toast_cb=toast_cb,
                    scratch_dir=scratch_dir,
                    m3u8_content=m3u8_content if c_idx == 0 else None,
                    pref_quality=pref_quality,
                    abort_event=abort_event,
                    pause_event=pause_event,
                    strict_ramdisk=strict_ramdisk,
                    active_info=_ACTIVE_DOWNLOAD_INFO if track_active else None,
                    active_lock=_ACTIVE_DOWNLOAD_LOCK if track_active else None,
                )
                if (
                    final_out
                    and os.path.exists(final_out)
                    and os.path.getsize(final_out) > 0
                ):
                    return final_out
            except (RAMDiskSpaceExceededError, InterruptedError):
                if out_path and os.path.exists(out_path):
                    try:
                        os.remove(out_path)
                    except Exception:
                        pass
                raise
            except Exception as hls_err:
                if out_path and os.path.exists(out_path):
                    try:
                        os.remove(out_path)
                    except Exception:
                        pass
                last_hls_err = hls_err
                host_label = urllib.parse.urlparse(c_url).netloc or c_url
                hls_error_summary.append(f"[{host_label}] {hls_err}")
                if c_idx == len(cand_m3u8s) - 1:
                    _log(
                        f"多線程下載備援耗盡 ({len(hls_error_summary)} 條線路): {hls_error_summary[0]}"
                    )

        is_cf_protected = (
            any(k in media_url for k in ["surrit", "ppqrrs", "play-cdn", "bfikun"])
            or "missav" in extra_headers.get("Referer", "")
            or "missav" in extra_headers.get("Origin", "")
        )
        if is_cf_protected and last_hls_err:
            raise RuntimeError(
                f"HLS 多線程下載失敗 (來源受 Cloudflare 防護，FFmpeg CLI 無法直連): {last_hls_err}"
            )

    # 3. yt-dlp 支援平台 (YouTube, Bilibili, FB, IG 等)
    webpage_url = media_item.get("webpage_url", "")
    is_direct_stream = "m3u8" in media_url or any(
        media_url.lower().split("?")[0].endswith(ext)
        for ext in [".mp4", ".m4a", ".mp3", ".flv", ".ts", ".webm", ".mkv"]
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
        out_base = os.path.splitext(out_path)[0]

        if any(d in target_url for d in ["bilibili.com", "b23.tv"]) and norm_cookies["bili_cookie"]:
            temp_cookie = create_temp_cookiefile(
                norm_cookies["bili_cookie"], domain=".bilibili.com"
            )
        elif (
            any(d in target_url for d in ["facebook.com", "fb.com", "fb.watch"])
            and norm_cookies["fb_cookie"]
        ):
            temp_cookie = create_temp_cookiefile(
                norm_cookies["fb_cookie"], domain=".facebook.com"
            )

        max_h = 720 if pref_quality == "720p" else 1080
        try:
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

            run_ytdlp_with_youtube_auth(
                target_url,
                ydl_opts,
                lambda ydl: ydl.download([target_url]),
                auth_mode=yt_auth_mode,
                yt_cookie_str=yt_cookie or norm_cookies["yt_cookie"],
                cached_cfb=cached_cfb,
                on_cfb_detected=on_cfb_detected,
                on_cfb_invalid=on_cfb_invalid,
            )

            if os.path.exists(out_path):
                return out_path
            for e in ["mp4", "mkv", "webm"]:
                cand = f"{out_base}.{e}"
                if os.path.exists(cand):
                    if cand != out_path:
                        shutil.move(cand, out_path)
                    return out_path
        finally:
            if temp_cookie and os.path.exists(temp_cookie):
                try:
                    os.remove(temp_cookie)
                except Exception:
                    pass

    # 4. 標準 FFmpeg 直接串流下載
    if "surrit" in media_url or "missav" in extra_headers.get("Referer", ""):
        extra_headers["Origin"] = "https://missav.ai"
        if "Referer" not in extra_headers:
            extra_headers["Referer"] = "https://missav.ai/"

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

    total_dur = get_media_duration(media_url, headers=extra_headers)
    ret, stderr = run_ffmpeg_with_progress(
        ffmpeg_cmd,
        total_duration=total_dur,
        label=label_text,
        progress_cb=ffmpeg_progress_cb,
        abort_event=abort_event,
    )
    if ret == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path

    # 若因 FFmpeg 版本過舊出現參數不識別 (Unrecognized option)，安全剔除進階參數重試
    if ret != 0 and "Unrecognized option" in stderr:
        simplified_cmd = ["ffmpeg", "-y"]
        if extra_headers:
            headers_str = "".join(f"{k}: {v}\r\n" for k, v in extra_headers.items())
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
        ret, stderr = run_ffmpeg_with_progress(
            simplified_cmd,
            total_duration=total_dur,
            label=label_text,
            progress_cb=ffmpeg_progress_cb,
            abort_event=abort_event,
        )
        if ret == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path

    err_lines = [
        l.strip()
        for l in stderr.splitlines()
        if l.strip()
        and not any(
            l.strip().startswith(k)
            for k in [
                "ffmpeg version",
                "configuration:",
                "built with",
                "libav",
                "libsw",
                "libpost",
            ]
        )
    ]
    actual_err = " | ".join(err_lines[-4:]) if err_lines else stderr[-300:]
    if hls_error_summary:
        summary_text = " | ".join(hls_error_summary[:3])
        if len(hls_error_summary) > 3:
            summary_text += f" ...（共 {len(hls_error_summary)} 條線路皆失敗）"
        err_msg = (
            f"多線程 HLS 下載失敗 ({summary_text})，且 FFmpeg 備援亦失敗: {actual_err}"
        )
        if "403" in summary_text or "403" in actual_err:
            err_msg += (
                "\n💡 提示：來源串流 CDN 回傳 403 Forbidden（常見於封鎖 Google Colab 雲端機房 IP）。"
                "建議將儲存目標切換為『☁️ Google Drive 雲端直送（本機下載自動同步）』或『📁 本機硬碟資料夾』以家用 IP 下載。"
            )
        raise RuntimeError(err_msg)
    if last_hls_err:
        raise RuntimeError(
            f"多線程 HLS 下載失敗 ({last_hls_err})，且 FFmpeg 備援亦失敗: {actual_err}"
        )
    raise RuntimeError(f"FFmpeg 下載失敗: {actual_err}")


def extract_audio_to_path(
    video_path,
    audio_format,
    out_dir,
    title=None,
    headers=None,
    cookies=None,
    yt_auth_mode="auto",
    yt_cookie="",
    cached_cfb=None,
    on_cfb_detected=None,
    on_cfb_invalid=None,
    ffmpeg_progress_cb=None,
    abort_event=None,
    warning_cb=None,
):
    """
    純 Python 音訊提取核心（支援線上網址 yt-dlp 與本地/m3u8 FFmpeg 轉碼）。
    回傳 dict: {"status": "success"|"exists"|"failed", "out_path": str, "error": str, "traceback": str}
    """
    base_name = title if title else os.path.splitext(os.path.basename(video_path))[0]
    norm_cookies = normalize_cookies_dict(cookies)

    is_valid_url = isinstance(video_path, str) and (
        video_path.startswith("http://") or video_path.startswith("https://")
    )
    is_youtube_or_online = is_valid_url and ("m3u8" not in video_path.lower())
    is_mono_96k = "96kbps" in audio_format or "單聲道" in audio_format

    if is_youtube_or_online:
        target_ext = (
            "mp3"
            if audio_format == "MP3"
            else ("m4a" if audio_format == "M4A" else "mp4")
        )
        expected_out_path = os.path.join(out_dir, f"{base_name}.{target_ext}")
        out_base = os.path.splitext(expected_out_path)[0]
        postprocessors = []
        if audio_format == "MP3":
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            })
        elif audio_format == "M4A":
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "192",
            })
        elif is_mono_96k:
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "96",
            })
        elif audio_format in ("MP4 (AAC音訊 - 相容AI轉錄)", "MP4"):
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "192",
            })
        else:
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            })

        temp_cookie = None
        if any(d in video_path for d in ["bilibili.com", "b23.tv"]) and norm_cookies["bili_cookie"]:
            temp_cookie = create_temp_cookiefile(
                norm_cookies["bili_cookie"], domain=".bilibili.com"
            )
        elif (
            any(d in video_path for d in ["facebook.com", "fb.com", "fb.watch"])
            and norm_cookies["fb_cookie"]
        ):
            temp_cookie = create_temp_cookiefile(
                norm_cookies["fb_cookie"], domain=".facebook.com"
            )

        try:
            ydl_opts = {
                "outtmpl": f"{out_base}.%(ext)s",
                "quiet": True,
                "overwrites": True,
                "nocheckcertificate": True,
                "legacy_server_connect": True,
                "format": "bestaudio/best",
                "postprocessors": postprocessors,
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
            if any(d in video_path for d in ["bilibili.com", "b23.tv"]):
                ydl_opts["http_headers"]["Referer"] = "https://www.bilibili.com/"

            run_ytdlp_with_youtube_auth(
                video_path,
                ydl_opts,
                lambda ydl: ydl.download([video_path]),
                auth_mode=yt_auth_mode,
                yt_cookie_str=yt_cookie or norm_cookies["yt_cookie"],
                cached_cfb=cached_cfb,
                on_cfb_detected=on_cfb_detected,
                on_cfb_invalid=on_cfb_invalid,
            )

            matched_file = None
            if os.path.exists(expected_out_path):
                matched_file = expected_out_path
            else:
                candidates = glob.glob(f"{out_base}.*") + glob.glob(
                    os.path.join(out_dir, f"{base_name[:30]}*")
                )
                if candidates:
                    matched_file = candidates[0]

            if not matched_file:
                raise RuntimeError(f"無法產出音訊檔 (Expected: {expected_out_path})")

            actual_ext = os.path.splitext(matched_file)[1].lower().lstrip(".")
            if target_ext == "mp4":
                final_mp4_path = os.path.join(out_dir, f"{base_name}.mp4")
                if is_mono_96k:
                    orig_codec, orig_br_kbps, orig_channels = get_audio_stream_info(
                        matched_file
                    )
                    if orig_br_kbps and orig_br_kbps < 96:
                        if orig_codec == "aac" and orig_channels <= 1:
                            mono_cmd = [
                                "ffmpeg", "-y", "-i", matched_file, "-vn",
                                "-c:a", "copy", "-bsf:a", "aac_adtstoasc", final_mp4_path,
                            ]
                        else:
                            mono_cmd = [
                                "ffmpeg", "-y", "-i", matched_file, "-vn",
                                "-c:a", "aac", "-ac", "1", "-b:a", f"{orig_br_kbps}k",
                                final_mp4_path,
                            ]
                    else:
                        mono_cmd = [
                            "ffmpeg", "-y", "-i", matched_file, "-vn",
                            "-c:a", "aac", "-ac", "1", "-b:a", "96k", final_mp4_path,
                        ]
                    try:
                        subprocess.run(
                            mono_cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=True,
                        )
                        if os.path.exists(matched_file) and matched_file != final_mp4_path:
                            os.remove(matched_file)
                        matched_file = final_mp4_path
                    except Exception:
                        pass
                elif actual_ext in ["m4a", "aac"] and matched_file != final_mp4_path:
                    try:
                        subprocess.run(
                            [
                                "ffmpeg", "-y", "-i", matched_file, "-vn",
                                "-c:a", "copy", "-bsf:a", "aac_adtstoasc", final_mp4_path,
                            ],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=True,
                        )
                        if os.path.exists(matched_file) and matched_file != final_mp4_path:
                            os.remove(matched_file)
                        matched_file = final_mp4_path
                    except Exception:
                        if os.path.exists(matched_file):
                            os.rename(matched_file, final_mp4_path)
                            matched_file = final_mp4_path

            gc.collect()
            return matched_file
        finally:
            if temp_cookie and os.path.exists(temp_cookie):
                try:
                    os.remove(temp_cookie)
                except Exception:
                    pass

    # 本地檔案或 m3u8 串流音訊提取
    headers_arg = []
    if headers:
        headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        headers_arg = ["-headers", headers_str]
    if "m3u8" in video_path:
        headers_arg += get_ffmpeg_hls_args()

    if is_mono_96k:
        orig_codec, orig_br_kbps, orig_channels = get_audio_stream_info(
            video_path, headers=headers
        )
        ext = "mp4"
        out_path = os.path.join(out_dir, f"{base_name}.{ext}")
        if orig_br_kbps and orig_br_kbps < 96:
            if orig_codec == "aac" and orig_channels <= 1:
                ffmpeg_cmd = (
                    ["ffmpeg", "-y"]
                    + headers_arg
                    + ["-i", video_path, "-vn", "-c:a", "copy", "-bsf:a", "aac_adtstoasc", out_path]
                )
            else:
                ffmpeg_cmd = (
                    ["ffmpeg", "-y"]
                    + headers_arg
                    + ["-i", video_path, "-vn", "-c:a", "aac", "-ac", "1", "-b:a", f"{orig_br_kbps}k", out_path]
                )
        else:
            ffmpeg_cmd = (
                ["ffmpeg", "-y"]
                + headers_arg
                + ["-i", video_path, "-vn", "-c:a", "aac", "-ac", "1", "-b:a", "96k", out_path]
            )
    elif audio_format == "MP3":
        ext = "mp3"
        out_path = os.path.join(out_dir, f"{base_name}.{ext}")
        ffmpeg_cmd = (
            ["ffmpeg", "-y"]
            + headers_arg
            + ["-i", video_path, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", out_path]
        )
    elif audio_format == "M4A":
        ext = "m4a"
        out_path = os.path.join(out_dir, f"{base_name}.{ext}")
        ffmpeg_cmd = (
            ["ffmpeg", "-y"]
            + headers_arg
            + ["-i", video_path, "-vn", "-c:a", "aac", "-b:a", "192k", out_path]
        )
    elif audio_format in ("MP4 (AAC音訊 - 相容AI轉錄)", "MP4"):
        ext = "mp4"
        out_path = os.path.join(out_dir, f"{base_name}.{ext}")
        ffmpeg_cmd = (
            ["ffmpeg", "-y"]
            + headers_arg
            + ["-i", video_path, "-vn", "-c:a", "aac", "-b:a", "192k", out_path]
        )
    else:
        try:
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
            if codec == "aac":
                ext = "mp4"
                out_path = os.path.join(out_dir, f"{base_name}.{ext}")
                ffmpeg_cmd = (
                    ["ffmpeg", "-y"]
                    + headers_arg
                    + ["-i", video_path, "-vn", "-c:a", "copy", "-bsf:a", "aac_adtstoasc", out_path]
                )
            elif codec in ["mp3", "opus"]:
                ext = codec
                out_path = os.path.join(out_dir, f"{base_name}.{ext}")
                ffmpeg_cmd = (
                    ["ffmpeg", "-y"]
                    + headers_arg
                    + ["-i", video_path, "-vn", "-c:a", "copy", out_path]
                )
            else:
                ext = "mp4"
                out_path = os.path.join(out_dir, f"{base_name}.{ext}")
                ffmpeg_cmd = (
                    ["ffmpeg", "-y"]
                    + headers_arg
                    + ["-i", video_path, "-vn", "-c:a", "aac", "-b:a", "192k", out_path]
                )
        except Exception:
            if warning_cb:
                warning_cb(f"⚠️ `{base_name}` 無法解析原始音訊格式，將預設轉換為 MP4 (AAC 音訊)。")
            ext = "mp4"
            out_path = os.path.join(out_dir, f"{base_name}.{ext}")
            ffmpeg_cmd = (
                ["ffmpeg", "-y"]
                + headers_arg
                + ["-i", video_path, "-vn", "-c:a", "aac", "-b:a", "192k", out_path]
            )

    total_dur = get_media_duration(video_path, headers=headers)
    returncode, stderr_log = run_ffmpeg_with_progress(
        ffmpeg_cmd,
        total_duration=total_dur,
        label="音訊提取",
        progress_cb=ffmpeg_progress_cb,
        abort_event=abort_event,
    )
    if returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        gc.collect()
        return out_path
    raise RuntimeError(stderr_log or f"FFmpeg 提取失敗 (code={returncode})")
