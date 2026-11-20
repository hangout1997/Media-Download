# -*- coding: utf-8 -*-
"""純 Python 核心下載引擎層 (零 Streamlit 依賴)。"""

from core.http_client import (
    GLOBAL_HEADERS,
    check_m3u8_accessible,
    cn_to_an,
    default_log_info,
    default_log_toast,
    format_time,
    get_global_session,
    is_valid_m3u8_content,
    normalize_input_url,
)
from core.auth import (
    create_temp_cookiefile,
    get_available_yt_browsers,
    normalize_cookies_dict,
    run_ytdlp_with_youtube_auth,
)
from core.ffmpeg_tools import (
    get_audio_stream_info,
    get_ffmpeg_hls_args,
    get_media_duration,
    run_ffmpeg_with_progress,
)
from core.hls import (
    RAMDiskSpaceExceededError,
    download_fast_parallel_hls,
    filter_and_clean_m3u8_ads,
)
from core.downloader import (
    abort_active_download,
    check_any_target_file_exists,
    download_media_to_path,
    extract_audio_to_path,
    get_abort_event,
    get_active_download_info,
    is_work_paused,
)

__all__ = [
    "GLOBAL_HEADERS",
    "RAMDiskSpaceExceededError",
    "abort_active_download",
    "check_any_target_file_exists",
    "check_m3u8_accessible",
    "cn_to_an",
    "create_temp_cookiefile",
    "default_log_info",
    "default_log_toast",
    "download_fast_parallel_hls",
    "download_media_to_path",
    "extract_audio_to_path",
    "filter_and_clean_m3u8_ads",
    "format_time",
    "get_abort_event",
    "get_active_download_info",
    "get_audio_stream_info",
    "get_available_yt_browsers",
    "get_ffmpeg_hls_args",
    "get_global_session",
    "get_media_duration",
    "is_valid_m3u8_content",
    "is_work_paused",
    "normalize_cookies_dict",
    "normalize_input_url",
    "run_ffmpeg_with_progress",
    "run_ytdlp_with_youtube_auth",
]
