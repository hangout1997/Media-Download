# -*- coding: utf-8 -*-
"""
Core Media Downloader Engine (Facade)
跨平台核心下載引擎 (無 Streamlit 依賴，相容 本地端 與 Google Colab 雲端執行)。
實際實作已模組化拆分為 `core/` 與 `parsers/`，本檔案保留完整向後相容對外介面。
"""

import os
import sys


def _ensure_colab_packages():
    """在 Google Colab 等僅單檔下載 core_downloader.py 的環境中，自動補齊 core/ 與 parsers/ 套件。"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)

    missing_local = (
        not os.path.isfile(os.path.join(base_dir, "core", "__init__.py"))
        or not os.path.isfile(os.path.join(base_dir, "parsers", "__init__.py"))
    )
    in_colab = os.path.abspath(base_dir) == "/content" and "google.colab" in sys.modules
    if not missing_local and not in_colab:
        return

    import time
    import urllib.request

    ts = int(time.time())
    rel_files = [
        "core/__init__.py",
        "core/http_client.py",
        "core/auth.py",
        "core/ffmpeg_tools.py",
        "core/hls.py",
        "core/macos_utils.py",
        "core/downloader.py",
        "parsers/__init__.py",
        "parsers/movieffm.py",
        "parsers/missav.py",
        "parsers/facebook.py",
        "parsers/ytdlp_parser.py",
        "parsers/gimy.py",
    ]
    base_raw = "https://raw.githubusercontent.com/hangout1997/Media-Download/main"
    updated = False
    for rel in rel_files:
        dest = os.path.join(base_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            urllib.request.urlretrieve(f"{base_raw}/{rel}?ts={ts}", dest)
            updated = True
        except Exception as e:
            if not os.path.exists(dest):
                print(f"⚠️ 自動同步 {rel} 失敗: {e}")

    if in_colab and updated:
        import importlib

        for mod_name in sorted(list(sys.modules.keys())):
            if mod_name == "core" or mod_name.startswith("core.") or mod_name == "parsers" or mod_name.startswith("parsers."):
                try:
                    importlib.reload(sys.modules[mod_name])
                except Exception:
                    pass


_ensure_colab_packages()

from core.auth import create_temp_cookiefile  # noqa: E402
from core.downloader import (  # noqa: E402
    download_media_to_path,
    extract_audio_to_path,
)
from core.ffmpeg_tools import (  # noqa: E402
    get_audio_stream_info,
    get_ffmpeg_hls_args,
    get_media_duration,
    run_ffmpeg_with_progress,
)
from core.hls import (  # noqa: E402
    RAMDiskSpaceExceededError,
    download_fast_parallel_hls,
    filter_and_clean_m3u8_ads,
)
from core.http_client import (  # noqa: E402
    _GLOBAL_HEADERS,
    _global_session,
    check_m3u8_accessible,
    cn_to_an,
    format_time,
    is_valid_m3u8_content,
    normalize_input_url,
)
from parsers import (  # noqa: E402
    _decode_movieffm_url,
    ensure_m3u8_prefetched,
    extract_packer_blocks,
    get_media_items,
    resolve_gimy_stream,
    unpack_dean_packer,
)


def _log_info(msg):
    """若在作用中的 Streamlit Context 下則優先呼叫 st.info，否則直接 print。"""
    try:
        import streamlit as _st

        if _st.runtime.exists():
            _st.info(msg)
            return
    except Exception:
        pass
    print(f"ℹ️ {msg}")


def _log_toast(msg, icon="ℹ️"):
    """若在作用中的 Streamlit Context 下則優先呼叫 st.toast，否則直接 print。"""
    try:
        import streamlit as _st

        if _st.runtime.exists():
            _st.toast(msg, icon=icon)
            return
    except Exception:
        pass
    print(f"{icon} {msg}")


__all__ = [
    "_GLOBAL_HEADERS",
    "_decode_movieffm_url",
    "_global_session",
    "_log_info",
    "_log_toast",
    "RAMDiskSpaceExceededError",
    "check_m3u8_accessible",
    "cn_to_an",
    "create_temp_cookiefile",
    "download_fast_parallel_hls",
    "download_media_to_path",
    "ensure_m3u8_prefetched",
    "extract_audio_to_path",
    "extract_packer_blocks",
    "filter_and_clean_m3u8_ads",
    "format_time",
    "get_audio_stream_info",
    "get_ffmpeg_hls_args",
    "get_media_duration",
    "get_media_items",
    "is_valid_m3u8_content",
    "normalize_input_url",
    "resolve_gimy_stream",
    "run_ffmpeg_with_progress",
    "unpack_dean_packer",
]
