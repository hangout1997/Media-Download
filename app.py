# -*- coding: utf-8 -*-
"""
Media Downloader & Converter — Streamlit Web Application Entrypoint
主程式僅負責頁面組裝、熱重載與向後相容符號匯出；核心邏輯與 UI 模組分別位於 `core/`、`parsers/`、`services/` 與 `ui/`。
"""

import importlib
import sys

import streamlit as st

import gdrive_bridge  # noqa: F401
import gdrive_service
import media_converter  # noqa: F401
import ramdisk_manager

# 自動熱重載自訂子模組，確保前端每次 Rerun / 重新整理時永遠套用最新程式碼
for _mod in (gdrive_service, ramdisk_manager):
    if _mod.__name__ in sys.modules:
        try:
            importlib.reload(_mod)
        except Exception:
            pass

from core.auth import create_temp_cookiefile  # noqa: E402, F401
from core.downloader import (  # noqa: E402, F401
    abort_active_download,
    download_media_to_path,
    extract_audio_to_path,
    is_work_paused,
)
from core.ffmpeg_tools import (  # noqa: E402, F401
    get_audio_stream_info,
    get_ffmpeg_hls_args,
    get_media_duration,
    run_ffmpeg_with_progress,
)
from core.hls import (  # noqa: E402, F401
    RAMDiskSpaceExceededError,
    download_fast_parallel_hls,
    filter_and_clean_m3u8_ads,
)
from core.http_client import (  # noqa: E402, F401
    check_m3u8_accessible,
    cn_to_an,
    format_time,
    is_valid_m3u8_content,
    normalize_input_url,
)
from parsers import (  # noqa: E402, F401
    extract_packer_blocks,
    get_media_items,
    resolve_gimy_stream,
    unpack_dean_packer,
)
from ui.header_controls import render_header_controls  # noqa: E402
from ui.state import init_session_state  # noqa: E402
from ui.tab_audio import extract_local_audio, render_audio_tab  # noqa: E402, F401
from ui.tab_converter import render_converter_tab  # noqa: E402
from ui.tab_download import download_media, render_download_tab  # noqa: E402, F401


def main():
    """組裝並渲染 Streamlit 應用程式主畫面。"""
    st.set_page_config(
        page_title="Gimymax Media Downloader", page_icon="🎬", layout="centered"
    )

    init_session_state()
    render_header_controls()

    tab1, tab2, tab3 = st.tabs(
        ["🌐 線上影片下載", "📁 影片音訊提取", "🔄 媒體格式轉換"]
    )
    with tab1:
        render_download_tab()
    with tab2:
        render_audio_tab()
    with tab3:
        render_converter_tab()


if __name__ == "__main__" or st.runtime.exists():
    main()
