# -*- coding: utf-8 -*-
"""Tab 3: 媒體格式轉換（圖片、影片、音訊單檔上傳與資料夾批次轉檔）。"""

import os
import tempfile
import streamlit as st

from core.macos_utils import check_dir_writable, load_download_dir
from services import media_converter


def render_converter_tab():
    """渲染 Tab 3：🔄 媒體格式轉換。"""
    st.markdown(
        "支援 **圖片 (JPEG/PNG/WEBP)**、**視訊 (MP4/MOV/MKV/GIF)**、**音訊 (MP3/WAV/M4A/FLAC)** 的本地高效格式轉換，支援單檔與整批資料夾批次處理。"
    )

    conv_category = st.radio(
        "📂 選擇轉換類型",
        ["🖼️ 圖片轉換", "🎬 影片轉換", "🎵 音訊轉換"],
        horizontal=True,
    )
    source_mode = st.radio(
        "📥 來源方式",
        ["📤 上傳檔案", "📁 本機路徑 / 資料夾批次"],
        horizontal=True,
    )

    out_dir = st.session_state.get("download_dir", load_download_dir())
    st.caption(f"💾 轉換輸出儲存目錄：`{out_dir}`")

    # 1. 圖片轉換
    if conv_category == "🖼️ 圖片轉換":
        col_img1, col_img2 = st.columns(2)
        with col_img1:
            img_target_fmt = st.selectbox(
                "目標格式", ["JPEG (.jpg)", "PNG (.png)", "WEBP (.webp)"], index=0
            )
            img_fmt_clean = (
                "JPEG"
                if "JPEG" in img_target_fmt
                else ("PNG" if "PNG" in img_target_fmt else "WEBP")
            )
        with col_img2:
            img_quality = st.slider(
                "壓縮品質 (Quality)",
                min_value=10,
                max_value=100,
                value=95,
                step=5,
                help="針對 JPEG / WEBP 格式有效。",
            )

        with st.expander("🛠️ 進階圖片處理選項", expanded=False):
            col_opt1, col_opt2 = st.columns(2)
            with col_opt1:
                bg_option = st.selectbox(
                    "透明背景填補色 (轉 JPEG 時必備)",
                    ["白色 (預設)", "黑色", "自訂透明不補色 (限 PNG/WEBP)"],
                )
                bg_color = (
                    (255, 255, 255)
                    if bg_option.startswith("白色")
                    else ((0, 0, 0) if bg_option.startswith("黑色") else (255, 255, 255))
                )
            with col_opt2:
                resize_opt = st.selectbox(
                    "最大邊長限制 (等比例縮放)",
                    ["維持原尺寸", "1920 px (Full HD)", "1280 px (HD)", "800 px (縮圖)"],
                )
                max_dim_map = {
                    "維持原尺寸": None,
                    "1920 px (Full HD)": 1920,
                    "1280 px (HD)": 1280,
                    "800 px (縮圖)": 800,
                }
                max_dim = max_dim_map.get(resize_opt)

        if source_mode == "📤 上傳檔案":
            uploaded_files = st.file_uploader(
                "選擇或拖曳圖片檔案 (可多選，支援 PNG, WEBP, BMP, HEIC, TIFF, JPG...)",
                type=[
                    "png",
                    "webp",
                    "bmp",
                    "tiff",
                    "tif",
                    "jpg",
                    "jpeg",
                    "heic",
                    "heif",
                    "avif",
                    "gif",
                ],
                accept_multiple_files=True,
            )
            if uploaded_files:
                st.info(f"已選取 {len(uploaded_files)} 個圖片檔案。")
                if st.button("🚀 開始轉換圖片", type="primary", use_container_width=True):
                    is_writable, write_err = check_dir_writable(out_dir)
                    if not is_writable:
                        st.error(f"❌ 目標目錄無寫入權限：{write_err}")
                    else:
                        prog_bar = st.progress(0)
                        success_count = 0
                        for idx, uf in enumerate(uploaded_files):
                            base_n = os.path.splitext(uf.name)[0]
                            ext_clean = (
                                "jpg"
                                if img_fmt_clean == "JPEG"
                                else img_fmt_clean.lower()
                            )
                            dest_path = media_converter.get_safe_output_path(
                                out_dir, base_n, ext_clean
                            )
                            temp_in = os.path.join(tempfile.gettempdir(), uf.name)
                            try:
                                with open(temp_in, "wb") as f_tmp:
                                    f_tmp.write(uf.getbuffer())
                                ok, msg = media_converter.convert_image(
                                    temp_in,
                                    dest_path,
                                    target_format=img_fmt_clean,
                                    quality=img_quality,
                                    bg_color=bg_color,
                                    max_dimension=max_dim,
                                )
                                if ok:
                                    success_count += 1
                                    st.write(
                                        f"✅ `{uf.name}` ➔ `{os.path.basename(dest_path)}`"
                                    )
                                else:
                                    st.error(f"❌ `{uf.name}`: {msg}")
                            finally:
                                if os.path.exists(temp_in):
                                    try:
                                        os.remove(temp_in)
                                    except Exception:
                                        pass
                            prog_bar.progress((idx + 1) / len(uploaded_files))
                        st.success(
                            f"🎉 圖片轉換完成！成功 {success_count}/{len(uploaded_files)} 個檔案，已儲存至：`{out_dir}`"
                        )
        else:
            in_path = st.text_input(
                "📁 請輸入本地圖片檔案或資料夾路徑:",
                placeholder="/Users/ericcheng/Pictures/my_photos",
            )
            is_recursive = st.checkbox("包含子資料夾內的所有圖片", value=True)
            if in_path and os.path.exists(in_path):
                if os.path.isfile(in_path):
                    target_list = [in_path]
                else:
                    target_list = media_converter.scan_files_for_conversion(
                        in_path, category="image", recursive=is_recursive
                    )
                st.caption(f"ℹ️ 偵測到 **{len(target_list)}** 個符合的圖片檔案。")
                if target_list and st.button(
                    "🚀 開始批次轉換圖片", type="primary", use_container_width=True
                ):
                    is_writable, write_err = check_dir_writable(out_dir)
                    if not is_writable:
                        st.error(f"❌ 目標目錄無寫入權限：{write_err}")
                    else:
                        prog_bar = st.progress(0)
                        success_count = 0
                        for idx, f_path in enumerate(target_list):
                            base_n = os.path.splitext(os.path.basename(f_path))[0]
                            ext_clean = (
                                "jpg"
                                if img_fmt_clean == "JPEG"
                                else img_fmt_clean.lower()
                            )
                            dest_path = media_converter.get_safe_output_path(
                                out_dir, base_n, ext_clean
                            )
                            ok, msg = media_converter.convert_image(
                                f_path,
                                dest_path,
                                target_format=img_fmt_clean,
                                quality=img_quality,
                                bg_color=bg_color,
                                max_dimension=max_dim,
                            )
                            if ok:
                                success_count += 1
                                st.write(
                                    f"✅ `{os.path.basename(f_path)}` ➔ `{os.path.basename(dest_path)}`"
                                )
                            else:
                                st.error(f"❌ `{os.path.basename(f_path)}`: {msg}")
                            prog_bar.progress((idx + 1) / len(target_list))
                        st.success(
                            f"🎉 圖片批次轉換完成！成功 {success_count}/{len(target_list)} 個檔案，已儲存至：`{out_dir}`"
                        )
            elif in_path:
                st.warning("⚠️ 輸入的路徑不存在，請確認路徑正確。")

    # 2. 影片轉換
    elif conv_category == "🎬 影片轉換":
        col_v1, col_v2 = st.columns(2)
        with col_v1:
            vid_target_fmt = st.selectbox(
                "目標格式",
                [
                    "MP4 (廣泛相容)",
                    "MOV (Apple / 剪輯)",
                    "MKV (多軌封裝)",
                    "GIF (高品質動圖)",
                    "WEBM (網頁串流)",
                ],
            )
            vid_fmt_clean = vid_target_fmt.split()[0].upper()
        with col_v2:
            vid_mode_opt = st.selectbox(
                "轉檔模式",
                [
                    "⚡ 智慧加速 (相容則無損拷貝，不相容則 VideoToolbox 硬解)",
                    "🚀 純無損封裝 (Remux - 極速零失真)",
                    "🛠️ 重新編碼 (Transcode - 相容性高)",
                ],
            )
            mode_key = (
                "smart"
                if vid_mode_opt.startswith("⚡")
                else ("remux" if vid_mode_opt.startswith("🚀") else "transcode")
            )

        with st.expander("🛠️ 進階影片選項", expanded=False):
            col_vo1, col_vo2 = st.columns(2)
            with col_vo1:
                res_opt = st.selectbox(
                    "解析度調整", ["維持原解析度", "1080p", "720p", "480p"]
                )
                res_key = "original" if res_opt.startswith("維持") else res_opt
            with col_vo2:
                q_opt = st.selectbox(
                    "畫質/位元率配置",
                    ["高品質 (High)", "標準 (Medium)", "輕巧/低碼率 (Low)"],
                )
                q_key = (
                    "high"
                    if "High" in q_opt
                    else ("medium" if "Medium" in q_opt else "low")
                )

        if source_mode == "📤 上傳檔案":
            uploaded_videos = st.file_uploader(
                "選擇或拖曳影片檔案 (支援 MP4, MKV, MOV, AVI, WEBM, FLV, TS...)",
                type=["mp4", "mkv", "mov", "avi", "webm", "flv", "ts", "m4v"],
                accept_multiple_files=True,
            )
            if uploaded_videos:
                st.info(f"已選取 {len(uploaded_videos)} 個影片檔案。")
                if st.button("🚀 開始轉換影片", type="primary", use_container_width=True):
                    is_writable, write_err = check_dir_writable(out_dir)
                    if not is_writable:
                        st.error(f"❌ 目標目錄無寫入權限：{write_err}")
                    else:
                        prog_bar = st.progress(0)
                        for idx, uv in enumerate(uploaded_videos):
                            base_n = os.path.splitext(uv.name)[0]
                            dest_path = media_converter.get_safe_output_path(
                                out_dir, base_n, vid_fmt_clean.lower()
                            )
                            temp_in = os.path.join(tempfile.gettempdir(), uv.name)
                            try:
                                with open(temp_in, "wb") as f_tmp:
                                    f_tmp.write(uv.getbuffer())
                                with st.spinner(f"正在轉換 {uv.name}..."):
                                    ok, msg = media_converter.convert_video(
                                        temp_in,
                                        dest_path,
                                        target_format=vid_fmt_clean,
                                        mode=mode_key,
                                        resolution=res_key,
                                        quality_preset=q_key,
                                    )
                                if ok:
                                    st.write(f"✅ {msg}")
                                else:
                                    st.error(f"❌ `{uv.name}`: {msg}")
                            finally:
                                if os.path.exists(temp_in):
                                    try:
                                        os.remove(temp_in)
                                    except Exception:
                                        pass
                            prog_bar.progress((idx + 1) / len(uploaded_videos))
                        st.success(f"🎉 影片轉換任務完成！儲存目錄：`{out_dir}`")
        else:
            in_vid_path = st.text_input(
                "📁 請輸入本地影片檔案或資料夾路徑:",
                placeholder="/Users/ericcheng/Movies",
            )
            is_vid_recursive = st.checkbox("包含子資料夾內的所有影片", value=True)
            if in_vid_path and os.path.exists(in_vid_path):
                if os.path.isfile(in_vid_path):
                    vid_list = [in_vid_path]
                else:
                    vid_list = media_converter.scan_files_for_conversion(
                        in_vid_path, category="video", recursive=is_vid_recursive
                    )
                st.caption(f"ℹ️ 偵測到 **{len(vid_list)}** 個符合的影片檔案。")
                if vid_list and st.button(
                    "🚀 開始批次轉換影片", type="primary", use_container_width=True
                ):
                    is_writable, write_err = check_dir_writable(out_dir)
                    if not is_writable:
                        st.error(f"❌ 目標目錄無寫入權限：{write_err}")
                    else:
                        prog_bar = st.progress(0)
                        for idx, v_path in enumerate(vid_list):
                            base_n = os.path.splitext(os.path.basename(v_path))[0]
                            dest_path = media_converter.get_safe_output_path(
                                out_dir, base_n, vid_fmt_clean.lower()
                            )
                            with st.spinner(
                                f"正在轉換 `{os.path.basename(v_path)}`..."
                            ):
                                ok, msg = media_converter.convert_video(
                                    v_path,
                                    dest_path,
                                    target_format=vid_fmt_clean,
                                    mode=mode_key,
                                    resolution=res_key,
                                    quality_preset=q_key,
                                )
                            if ok:
                                st.write(f"✅ {msg}")
                            else:
                                st.error(f"❌ `{os.path.basename(v_path)}`: {msg}")
                            prog_bar.progress((idx + 1) / len(vid_list))
                        st.success(f"🎉 影片批次轉換完成！儲存目錄：`{out_dir}`")
            elif in_vid_path:
                st.warning("⚠️ 輸入的路徑不存在，請確認路徑正確。")

    # 3. 音訊轉換
    elif conv_category == "🎵 音訊轉換":
        col_a1, col_a2 = st.columns(2)
        with col_a1:
            aud_target_fmt = st.selectbox(
                "目標格式",
                [
                    "MP3 (泛用通用)",
                    "WAV (無損/剪輯推薦)",
                    "M4A (AAC 高音質)",
                    "FLAC (無損壓縮)",
                    "OGG",
                ],
            )
            aud_fmt_clean = aud_target_fmt.split()[0].upper()
        with col_a2:
            preset_aud = st.selectbox(
                "場景預設",
                [
                    "標準音質 (192 kbps)",
                    "超高音質 (320 kbps)",
                    "輕巧音質 (128 kbps)",
                    "🎙️ 語音辨識/Whisper 專用 (16kHz 單聲道 WAV)",
                ],
            )

        sample_rate = 16000 if "Whisper" in preset_aud else None
        channels = 1 if "Whisper" in preset_aud else None
        if "Whisper" in preset_aud:
            aud_fmt_clean = "WAV"
            aud_br = "128k"
        else:
            aud_br = (
                "320k"
                if "320" in preset_aud
                else ("128k" if "128" in preset_aud else "192k")
            )

        if source_mode == "📤 上傳檔案":
            uploaded_auds = st.file_uploader(
                "選擇或拖曳音訊/影片檔案 (直接提取音訊並轉檔)",
                type=[
                    "mp3",
                    "wav",
                    "m4a",
                    "flac",
                    "aac",
                    "ogg",
                    "mp4",
                    "mkv",
                    "mov",
                    "webm",
                ],
                accept_multiple_files=True,
            )
            if uploaded_auds:
                st.info(f"已選取 {len(uploaded_auds)} 個檔案。")
                if st.button("🚀 開始轉換音訊", type="primary", use_container_width=True):
                    is_writable, write_err = check_dir_writable(out_dir)
                    if not is_writable:
                        st.error(f"❌ 目標目錄無寫入權限：{write_err}")
                    else:
                        prog_bar = st.progress(0)
                        for idx, ua in enumerate(uploaded_auds):
                            base_n = os.path.splitext(ua.name)[0]
                            dest_path = media_converter.get_safe_output_path(
                                out_dir, base_n, aud_fmt_clean.lower()
                            )
                            temp_in = os.path.join(tempfile.gettempdir(), ua.name)
                            try:
                                with open(temp_in, "wb") as f_tmp:
                                    f_tmp.write(ua.getbuffer())
                                ok, msg = media_converter.convert_audio(
                                    temp_in,
                                    dest_path,
                                    target_format=aud_fmt_clean,
                                    audio_bitrate=aud_br,
                                    sample_rate=sample_rate,
                                    channels=channels,
                                )
                                if ok:
                                    st.write(f"✅ {msg}")
                                else:
                                    st.error(f"❌ `{ua.name}`: {msg}")
                            finally:
                                if os.path.exists(temp_in):
                                    try:
                                        os.remove(temp_in)
                                    except Exception:
                                        pass
                            prog_bar.progress((idx + 1) / len(uploaded_auds))
                        st.success(f"🎉 音訊轉換任務完成！儲存目錄：`{out_dir}`")
        else:
            in_aud_path = st.text_input(
                "📁 請輸入本地音訊或影片檔案/資料夾路徑:",
                placeholder="/Users/ericcheng/Music",
            )
            is_aud_recursive = st.checkbox("包含子資料夾內的所有音訊/影片", value=True)
            if in_aud_path and os.path.exists(in_aud_path):
                if os.path.isfile(in_aud_path):
                    aud_list = [in_aud_path]
                else:
                    aud_list = media_converter.scan_files_for_conversion(
                        in_aud_path, category="all", recursive=is_aud_recursive
                    )
                st.caption(f"ℹ️ 偵測到 **{len(aud_list)}** 個可處理檔案。")
                if aud_list and st.button(
                    "🚀 開始批次轉換音訊", type="primary", use_container_width=True
                ):
                    is_writable, write_err = check_dir_writable(out_dir)
                    if not is_writable:
                        st.error(f"❌ 目標目錄無寫入權限：{write_err}")
                    else:
                        prog_bar = st.progress(0)
                        for idx, a_path in enumerate(aud_list):
                            base_n = os.path.splitext(os.path.basename(a_path))[0]
                            dest_path = media_converter.get_safe_output_path(
                                out_dir, base_n, aud_fmt_clean.lower()
                            )
                            ok, msg = media_converter.convert_audio(
                                a_path,
                                dest_path,
                                target_format=aud_fmt_clean,
                                audio_bitrate=aud_br,
                                sample_rate=sample_rate,
                                channels=channels,
                            )
                            if ok:
                                st.write(f"✅ {msg}")
                            else:
                                st.error(f"❌ `{os.path.basename(a_path)}`: {msg}")
                            prog_bar.progress((idx + 1) / len(aud_list))
                        st.success(f"🎉 音訊批次轉換完成！儲存目錄：`{out_dir}`")
            elif in_aud_path:
                st.warning("⚠️ 輸入的路徑不存在，請確認路徑正確。")
