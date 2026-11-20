# -*- coding: utf-8 -*-
"""共用 Streamlit UI 組件：錯誤日誌框、任務總結報告、GDrive 上傳卡片與進度條 Adapters。"""

import gc
import os
import shutil
import time
import traceback
import streamlit as st

from core.downloader import (
    check_any_target_file_exists,
    get_abort_event,
    get_active_download_info,
    get_active_download_lock,
)
from core.ffmpeg_tools import run_ffmpeg_with_progress
from core.hls import download_fast_parallel_hls
from core.http_client import default_log_info, format_time, normalize_input_url
from core.macos_utils import load_download_dir
from parsers import ensure_m3u8_prefetched, get_media_items
from services import gdrive_bridge, gdrive_service
from ui.state import (
    cb_open_finder,
    cb_play_video,
    cb_save_local,
    get_session_cookies,
    get_yt_auth_kwargs,
    record_successful_download,
)


def show_error_log_box(error_msg, log_text=None, title="詳細錯誤日誌", url=None):
    """顯示格式化錯誤訊息與一鍵複製除錯日誌摺疊區塊。"""
    st.error(error_msg)
    if log_text or url:
        full_log = ""
        if url:
            full_log += f"🔗 相關網址 (Target URL):\n{url}\n\n"
        if log_text:
            full_log += str(log_text).strip()

        with st.expander(f"📋 {title} (點擊開啟一鍵複製)", expanded=True):
            st.caption(
                "💡 點擊下方框框右上角的 **「📋 複製 (Copy)」** 按鈕，即可快速複製完整日誌（包含目標網址）提供給 AI："
            )
            st.code(full_log.strip(), language="log")


def run_ffmpeg_with_st_ui(ffmpeg_cmd, total_duration=0.0, label="下載"):
    """使用 Streamlit 進度條與狀態列執行 FFmpeg，並注入核心中斷事件。"""
    progress_bar = st.progress(0.0)
    status_text = st.empty()

    def _progress_cb(pct, current_sec, tot_dur, speed):
        if tot_dur > 0:
            pct_num = pct * 100
            progress_bar.progress(pct)
            status_text.markdown(
                f"⏳ **{label}進度**: `{pct_num:.1f}%` ({format_time(current_sec)} / {format_time(tot_dur)}) | 速度: `{speed}`"
            )
        else:
            status_text.markdown(
                f"⏳ **{label}處理中...** 已完成 `{format_time(current_sec)}` | 速度: `{speed}`"
            )

    try:
        ret, stderr_log = run_ffmpeg_with_progress(
            ffmpeg_cmd,
            total_duration=total_duration,
            label=label,
            progress_cb=_progress_cb,
            abort_event=get_abort_event(),
        )
        if total_duration > 0 and ret == 0:
            progress_bar.progress(1.0)
        return ret, stderr_log
    finally:
        status_text.empty()
        progress_bar.empty()


def download_hls_with_st_ui(
    m3u8_url,
    out_path=None,
    extra_headers=None,
    max_workers=None,
    label="影片",
    m3u8_content=None,
):
    """以 Streamlit 進度條、Toast、畫質偏好、嚴格零 SSD 磨損與中斷控制包裝核心 HLS 下載引擎。"""
    progress_bar = st.progress(0.0)
    status_text = st.empty()

    def _progress_cb(pct, completed_dur, total_dur, speed_mb, speed_x):
        progress_bar.progress(pct)

    def _status_cb(md_text):
        status_text.markdown(md_text)

    def _toast_cb(msg, icon="ℹ️"):
        st.toast(msg, icon=icon)

    try:
        return download_fast_parallel_hls(
            m3u8_url,
            out_path=out_path,
            extra_headers=extra_headers,
            max_workers=max_workers,
            label=label,
            progress_cb=_progress_cb,
            status_cb=_status_cb,
            toast_cb=_toast_cb,
            m3u8_content=m3u8_content,
            pref_quality=st.session_state.get("pref_quality", "720p"),
            abort_event=get_abort_event(),
            pause_event=gdrive_service.get_pause_event(),
            strict_ramdisk=True,
            active_info=get_active_download_info(),
            active_lock=get_active_download_lock(),
        )
    finally:
        progress_bar.empty()
        status_text.empty()


def gdrive_precheck_exists(filename, title=""):
    """雲端模式下載前檢查是否已有檔案，支援全覆蓋與番號智慧比對。"""
    is_gdrive = st.session_state.get("storage_destination") == "gdrive_cloud"
    configured_dir = st.session_state.get("download_dir", load_download_dir())
    exists, where_desc, link = check_any_target_file_exists(
        filename, title=title, download_dir=configured_dir, is_gdrive_mode=is_gdrive
    )
    if exists:
        link_md = f" [🔗 開啟連結]({link})" if (link and link.startswith("http")) else ""
        st.success(
            f"⏭️ **檔案已存在於 {where_desc}，自動跳過下載**: `{filename}`{link_md}"
        )
        return True
    return False


def finish_output_file(local_temp_or_final_path, filename):
    """
    完成檔案處理並依據儲存模式分發 (Google Drive 背景非同步上傳互動卡片 / 本機硬碟)。
    """
    st.session_state[f"ready_file_{filename}"] = local_temp_or_final_path
    is_gdrive_mode = st.session_state.get("storage_destination") == "gdrive_cloud"
    record_successful_download(
        title=filename,
        filename=filename,
        out_path=local_temp_or_final_path,
        is_gdrive=is_gdrive_mode,
    )
    if is_gdrive_mode:
        local_dest_dir = st.session_state.get("download_dir", load_download_dir())
        os.makedirs(local_dest_dir, exist_ok=True)
        local_dest_path = os.path.join(local_dest_dir, filename)

        local_saved_key = f"saved_local_{filename}"
        if local_saved_key not in st.session_state:
            st.session_state[local_saved_key] = os.path.exists(local_dest_path)

        # 1. 檢查是否勾選「同步在本地儲存一份」
        if (
            st.session_state.get("gdrive_save_local_copy", False)
            and not st.session_state[local_saved_key]
        ):
            try:
                if os.path.exists(local_temp_or_final_path) and not os.path.exists(
                    local_dest_path
                ):
                    shutil.copy2(local_temp_or_final_path, local_dest_path)
                    st.session_state[local_saved_key] = True
            except Exception as copy_err:
                st.warning(f"⚠️ 本地同步失敗: {copy_err}")

        # 2. 驗證 Google Drive 授權
        if not gdrive_service.is_authenticated():
            st.error(
                "❌ 尚未完成 Google Drive 授權！請先至上方儲存設定區點擊「授權連接 Google Drive」完成登入。"
            )
            return False

        target_folder_id, target_label = gdrive_service.get_target_folder()
        pause_event = gdrive_service.get_pause_event()

        # 3. 取得或啟動背景非同步上傳任務
        task = gdrive_service.get_or_create_upload_task(
            local_temp_or_final_path,
            filename=filename,
            folder_id=target_folder_id,
            skip_if_exists=st.session_state.get("gdrive_skip_dup", True),
            pause_event=pause_event,
        )

        src_to_copy = (
            local_temp_or_final_path
            if (local_temp_or_final_path and os.path.exists(local_temp_or_final_path))
            else getattr(task, "file_path", None)
        )

        # 4. 前端即時互動卡片
        with st.container(border=True):
            st.markdown(f"#### ☁️ Google Drive 上傳進行中：`{filename}`")
            file_sz_mb = task.file_size / 1024 / 1024
            st.caption(
                f"📁 檔案大小: `{file_sz_mb:.1f} MB` ｜ 本地目標資料夾: `{local_dest_dir}`"
            )

            c_btn1, c_btn2, c_btn3 = st.columns([1.6, 1.2, 1.2])

            with c_btn1:
                is_saved = st.session_state.get(local_saved_key, False) or os.path.exists(
                    local_dest_path
                )
                btn_label = "✅ 本地已儲存" if is_saved else "📥 儲存至本地資料夾"
                st.button(
                    btn_label,
                    key=f"btn_save_{filename}",
                    disabled=is_saved,
                    on_click=cb_save_local,
                    args=(src_to_copy, local_dest_path, local_saved_key),
                    use_container_width=True,
                    help=f"立即複製檔案至上一次儲存的資料夾：\n{local_dest_path}",
                )

            with c_btn2:
                target_show = (
                    local_dest_path if os.path.exists(local_dest_path) else src_to_copy
                )
                st.button(
                    "📂 在 Finder 顯示",
                    key=f"btn_finder_{filename}",
                    on_click=cb_open_finder,
                    args=(target_show,),
                    use_container_width=True,
                    help="在 Mac Finder 中定位並高亮選取此影片",
                )

            with c_btn3:
                target_play = (
                    local_dest_path if os.path.exists(local_dest_path) else src_to_copy
                )
                st.button(
                    "🎬 立即播放觀賞",
                    key=f"btn_play_{filename}",
                    on_click=cb_play_video,
                    args=(target_play,),
                    use_container_width=True,
                    help="直接以系統預設播放器 (如 QuickTime / IINA) 立即開啟影片",
                )

            if st.session_state.get(local_saved_key, False) or os.path.exists(
                local_dest_path
            ):
                st.success(
                    f"💾 **本地已妥善保存**：`{local_dest_path}`（您可以立即觀賞，雲端上傳不受影響繼續進行中）"
                )
            elif st.session_state.get(f"err_{local_saved_key}"):
                st.error(
                    f"❌ 儲存至本地失敗: {st.session_state.get(f'err_{local_saved_key}')}"
                )

            prog_bar = st.progress(min(1.0, max(0.0, task.progress)))
            status_text = st.empty()

            while task.status == "uploading":
                pct_val = min(1.0, max(0.0, task.progress))
                prog_bar.progress(pct_val)
                cur_mb = task.uploaded_bytes / 1024 / 1024
                tot_mb = task.file_size / 1024 / 1024
                if not pause_event.is_set():
                    status_text.markdown(
                        f"⏸️ **Google Drive 雲端直送已暫停 (頻寬已釋出)**: `{pct_val*100:.1f}%` ({cur_mb:.1f} MB / {tot_mb:.1f} MB) ｜ 點擊上方「▶️ 恢復工作」即可繼續傳輸"
                    )
                else:
                    eta_str = (
                        f" ｜ 預估剩餘: `{format_time(task.eta_sec)}`"
                        if task.eta_sec > 0
                        else ""
                    )
                    speed_note = (
                        " 🐢 (家用上傳頻寬受限)" if task.speed_mb < 0.8 else ""
                    )
                    status_text.markdown(
                        f"☁️ **Google Drive 雲端直送中**: `{pct_val*100:.1f}%` ({cur_mb:.1f} MB / {tot_mb:.1f} MB) ｜ 上傳速度: `{task.speed_mb:.2f} MB/s`{speed_note}{eta_str}"
                    )
                time.sleep(0.8)

            prog_bar.empty()
            status_text.empty()

            if task.status == "success":
                res = task.res
                link = res.get("webViewLink") if res else None
                folder_link = (
                    gdrive_service.get_folder_link(res["folder_id"])
                    if res and res.get("folder_id")
                    else None
                )
                link_md = f" [🔗 開啟檔案]({link})" if link else ""
                folder_md = f" ｜ [📂 開啟資料夾]({folder_link})" if folder_link else ""
                has_local = st.session_state.get(local_saved_key) or os.path.exists(
                    local_dest_path
                )
                local_note = (
                    f"（已同步保留於本地 `{local_dest_path}`）"
                    if has_local
                    else "（本機無殘留）"
                )
                st.success(
                    f"🎉 **雲端直送成功！** `{target_label}{filename}`{link_md}{folder_md} {local_note}"
                )

                if (
                    os.path.exists(local_temp_or_final_path)
                    and os.path.abspath(local_temp_or_final_path)
                    != os.path.abspath(local_dest_path)
                ):
                    try:
                        os.remove(local_temp_or_final_path)
                    except Exception:
                        pass
                gdrive_service.remove_upload_task(filename)
                st.session_state.pop(f"ready_file_{filename}", None)
                gc.collect()
                return True

            elif task.status == "skipped":
                res = task.res
                link = res.get("webViewLink") if res else None
                folder_link = (
                    gdrive_service.get_folder_link(res["folder_id"])
                    if res and res.get("folder_id")
                    else None
                )
                link_md = f" [🔗 開啟檔案]({link})" if link else ""
                folder_md = f" ｜ [📂 開啟資料夾]({folder_link})" if folder_link else ""
                has_local = st.session_state.get(local_saved_key) or os.path.exists(
                    local_dest_path
                )
                local_note = (
                    f"（已同步保留於本地 `{local_dest_path}`）" if has_local else ""
                )
                st.info(
                    f"⏭️ 雲端 `{target_label}` 已存在同名檔案，已跳過上傳：`{filename}`{link_md}{folder_md} {local_note}"
                )
                if (
                    os.path.exists(local_temp_or_final_path)
                    and os.path.abspath(local_temp_or_final_path)
                    != os.path.abspath(local_dest_path)
                ):
                    try:
                        os.remove(local_temp_or_final_path)
                    except Exception:
                        pass
                gdrive_service.remove_upload_task(filename)
                st.session_state.pop(f"ready_file_{filename}", None)
                gc.collect()
                return True

            elif task.status == "cancelled":
                st.warning(f"⏹️ 已中斷 Google Drive 雲端上傳：`{filename}`")
                if (
                    os.path.exists(local_temp_or_final_path)
                    and os.path.abspath(local_temp_or_final_path)
                    != os.path.abspath(local_dest_path)
                ):
                    has_local = st.session_state.get(local_saved_key) or os.path.exists(
                        local_dest_path
                    )
                    if not has_local:
                        try:
                            os.remove(local_temp_or_final_path)
                        except Exception:
                            pass
                gdrive_service.remove_upload_task(filename)
                st.session_state.pop(f"ready_file_{filename}", None)
                gc.collect()
                return False

            elif task.status == "failed":
                err = task.error
                show_error_log_box(
                    f"❌ Google Drive 雲端直送失敗: {err}",
                    traceback.format_exc(),
                    title="Google Drive API 上傳錯誤",
                )
                gdrive_service.remove_upload_task(filename)
                st.session_state.pop(f"ready_file_{filename}", None)
                gc.collect()
                return False
    else:
        st.success(f"✅ 下載完成！檔案已儲存至本機: `{local_temp_or_final_path}`")
        gc.collect()
        return True


def mark_and_finish_output_file(out_path, filename):
    """標記目前處理中之 URL 為 UPLOADING 階段，防止互動 rerun 重複下載。"""
    curr_proc_url = st.session_state.get("current_processing_url")
    if curr_proc_url:
        st.session_state[f"stage_{curr_proc_url}"] = "UPLOADING"
        st.session_state[f"stage_file_{curr_proc_url}"] = out_path
        st.session_state[f"stage_name_{curr_proc_url}"] = filename
    return finish_output_file(out_path, filename)


def render_download_summary_report(summary_data=None):
    """渲染下載任務結束總結報告（詳細列出成功、失敗、已存在跳過與 RAM Disk 容量不足跳過清單）。"""
    if not summary_data:
        summary_data = st.session_state.get("last_batch_summary")
    if not summary_data:
        return

    skipped_items = summary_data.get("skipped_items", [])
    success_items = summary_data.get("success_items", [])
    failed_items = summary_data.get("failed_items", [])
    skipped_existing_items = summary_data.get("skipped_existing_items", [])
    total_count = summary_data.get(
        "total_count",
        len(skipped_items)
        + len(success_items)
        + len(failed_items)
        + len(skipped_existing_items),
    )
    stopped_early = summary_data.get("stopped_early", False)

    with st.container(border=True):
        st.markdown("### 📋 下載任務結束總結報告")
        if stopped_early:
            st.info(
                f"⏹️ **任務已由使用者手動停止**。共規劃 `{total_count}` 部影片，已處理進度摘要如下："
            )
        elif failed_items:
            st.error(
                f"❌ **下載任務結束，但有 `{len(failed_items)}` 部影片失敗！** 本次共處理 `{total_count}` 部影片："
                f"成功 **{len(success_items)} 部**、失敗 **{len(failed_items)} 部**"
                + (
                    f"、已存在跳過 **{len(skipped_existing_items)} 部**"
                    if skipped_existing_items
                    else ""
                )
                + (
                    f"、RAM Disk 跳過 **{len(skipped_items)} 部**"
                    if skipped_items
                    else ""
                )
                + "。"
            )
        elif skipped_items or skipped_existing_items:
            st.warning(
                f"⚠️ **下載任務已全部處理完畢！** 本次共處理 `{total_count}` 部影片，"
                f"其中 **{len(success_items)} 部成功**"
                + (
                    f"、**{len(skipped_existing_items)} 部因已存在自動跳過**"
                    if skipped_existing_items
                    else ""
                )
                + (
                    f"、**{len(skipped_items)} 部因 RAM Disk 容量不足已自動跳過**"
                    if skipped_items
                    else ""
                )
                + "。"
            )
        elif len(success_items) > 0:
            is_local = st.session_state.get("storage_destination", "local") == "local"
            mode_note = (
                "已儲存至本機指定資料夾"
                if is_local
                else "100% 透過 RAM Disk 零硬碟讀寫封裝完畢"
            )
            st.success(
                f"🎉 **本次任務全數成功完成！** 共 `{len(success_items)}` 部影片，{mode_note}。"
            )
        else:
            st.warning(
                f"⚠️ **任務已結束，但沒有任何檔案被下載或儲存**（共處理 `{total_count}` 個項目）。請檢查輸入網址或上方設定。"
            )

        m_c1, m_c2, m_c3, m_c4 = st.columns(4)
        with m_c1:
            st.metric("處理任務總數", f"{total_count} 部")
        with m_c2:
            st.metric("✅ 成功完成", f"{len(success_items)} 部")
        with m_c3:
            st.metric("❌ 下載失敗", f"{len(failed_items)} 部")
        with m_c4:
            st.metric(
                "⏭️ 跳過 (已存在/容量)",
                f"{len(skipped_existing_items) + len(skipped_items)} 部",
            )

        if failed_items:
            st.divider()
            st.markdown("#### ❌ 下載失敗項目與錯誤原因")
            for f_i, f_item in enumerate(failed_items, 1):
                with st.expander(
                    f"#{f_i} ❌ {f_item.get('title', '未知項目')}", expanded=True
                ):
                    if f_item.get("url"):
                        st.markdown(f"**🔗 來源網址**: `{f_item.get('url')}`")
                    st.code(f_item.get("reason", "未知錯誤"), language="text")

            failed_urls = [it.get("url") for it in failed_items if it.get("url")]
            if failed_urls:
                st.markdown("**📋 快速複製失敗的網址**：")
                st.code("\n".join(failed_urls), language="text")

        if skipped_existing_items:
            st.divider()
            with st.expander(
                f"⏭️ 查看因已存在而自動跳過的檔案 ({len(skipped_existing_items)} 部)",
                expanded=True,
            ):
                for e_i, e_item in enumerate(skipped_existing_items, 1):
                    loc_str = (
                        f" ｜ 實際位置: `{e_item.get('matched_loc')}`"
                        if e_item.get("matched_loc")
                        else ""
                    )
                    st.markdown(
                        f"**{e_i}.** `{e_item.get('filename')}` ｜ 存在於: {e_item.get('where_desc')}{loc_str}"
                    )

        if skipped_items:
            st.divider()
            st.markdown("#### ⚠️ 因 RAM Disk 容量不足而跳過的檔案（保護 SSD 零磨損）")
            st.caption(
                "以下影片體積超出當前 RAM Disk 可用容量。為避免降級使用本機 SSD 暫存造成硬碟讀寫損耗，系統已依指示安全跳過："
            )

            for i, item in enumerate(skipped_items, 1):
                with st.expander(
                    f"#{i} 🎬 {item.get('title', '未知檔案')} (時長: {item.get('duration', '未知')})",
                    expanded=True,
                ):
                    c1, c2 = st.columns([2, 1])
                    with c1:
                        st.markdown(f"**🔗 來源網址**: `{item.get('url', '')}`")
                        st.markdown(
                            f"**🛡️ 跳過原因**: {item.get('reason', '超出可用容量')}"
                        )
                    with c2:
                        st.markdown(
                            f"**📦 預估/所需空間**: `{item.get('needed_size', '未知')}`"
                        )
                        st.markdown(
                            f"**💾 當時 RAM Disk 剩餘**: `{item.get('free_size', '未知')}`"
                        )

            skipped_urls = [it.get("url") for it in skipped_items if it.get("url")]
            if skipped_urls:
                st.markdown(
                    "**📋 快速複製所有跳過的網址**（可複製至下方再次嘗試或使用 Colab 下載）："
                )
                st.code("\n".join(skipped_urls), language="text")

            st.info(
                "💡 **後續處理建議**：\n\n"
                "1. **升級 RAM Disk 容量**：可在上方控制列將 RAM Disk 劃分容量加大（例如劃分 10GB）後重新貼上網址下載。\n"
                "2. **使用 Google Colab 雲端下載**：將「儲存目標位置」切換為 **Google Colab (雲端直存)**，利用 Google 雲端虛擬機 12GB 記憶體與 100GB 硬碟空間下載超長片，完全不佔用本機任何資源！"
            )

        if success_items:
            with st.expander(
                f"✅ 查看已成功完成的檔案清單 ({len(success_items)} 部)", expanded=False
            ):
                for s_i, s_item in enumerate(success_items, 1):
                    st.markdown(
                        f"**{s_i}.** `{s_item.get('filename')}` ｜ 大小: `{s_item.get('size_str')}` ｜ 儲存至: `{s_item.get('destination')}`"
                    )

        st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
        if st.button(
            "🧹 關閉並清除此報告",
            key="btn_clear_summary_report",
            use_container_width=True,
        ):
            st.session_state.last_batch_summary = None
            st.session_state.skipped_ramdisk_files = []
            st.session_state.successful_downloads = []
            st.session_state.failed_downloads = []
            st.session_state.skipped_existing_files = []
            st.rerun()


def dispatch_and_monitor_colab_task(urls, only_audio=False):
    """將下載或音訊提取任務預解析後派送至 Google Colab Worker，並即時輪詢進度。"""
    has_missav = any("missav" in u.lower() or "surrit" in u.lower() for u in urls)
    if has_missav:
        st.error(
            "❌ **MissAV (surrit.com) 啟用 Cloudflare 嚴格機房黑名單，全面封鎖 Google Cloud 機房 IP (AS15169)！**\n\n"
            "💡 **請切換上方「儲存目標位置」**：\n"
            "- ☁️ **「Google Drive (本機下載自動同步)」**：由本機家用寬頻下載 (100% 免疫 403)，配合 4GB RAM Disk (零 SSD 損耗)，完成後自動上傳至 Google Drive！\n"
            "- 📁 **「本機硬碟資料夾」**：直接下載至 Mac 本機。\n\n"
            "*(註：YouTube、Bilibili、FB、IG、Movieffm 等未阻擋機房 IP 的平台可正常使用 Colab 雲端直存)*"
        )
        st.stop()

    cookies = get_session_cookies()
    pref_q = st.session_state.get("pref_quality", "720p")
    yt_kw = get_yt_auth_kwargs()
    target_id, target_label = gdrive_service.get_target_folder()
    save_folder = target_label.strip("/") if target_label else "Download"

    resolved_map = {}
    with st.spinner("🔍 正在本機快速預解析串流與選單 (免疫 Cloudflare/WAF 阻擋)..."):
        for u in urls:
            try:
                m_items = get_media_items(
                    u,
                    cookies=cookies,
                    pref_quality=pref_q,
                    log_cb=default_log_info,
                    **yt_kw,
                )
                if m_items:
                    resolved_items = ensure_m3u8_prefetched(
                        m_items, pref_quality=pref_q
                    )
                    resolved_map[u] = resolved_items
                    resolved_map[u.rstrip("/")] = resolved_items
                    resolved_map[u.rstrip("/") + "/"] = resolved_items
                    norm_u = normalize_input_url(u)
                    resolved_map[norm_u] = resolved_items
                    resolved_map[norm_u.rstrip("/")] = resolved_items
                    resolved_map[norm_u.rstrip("/") + "/"] = resolved_items
            except Exception as e:
                tag = "音訊任務預解析失敗" if only_audio else "本機預解析失敗"
                default_log_info(f"{tag}: {u}, 原因: {e}")

    noun = "音訊提取任務" if only_audio else "下載任務"
    st.info(f"🚀 正在將 {len(urls)} 個{noun}派送至 Google Colab Worker...")
    try:
        task_id = gdrive_bridge.submit_cloud_task(
            urls=urls,
            save_folder=save_folder,
            only_audio=only_audio,
            cookies=cookies,
            resolved_map=resolved_map,
        )
    except Exception as e:
        st.error(f"❌ 任務發送至 Google Drive 佇列失敗: {e}")
        return

    if not task_id:
        return

    ack_noun = "音訊任務" if only_audio else "任務"
    st.success(f"✅ {ack_noun}已成功送達雲端佇列！(任務 ID: `{task_id}`)")
    status_placeholder = st.empty()
    progress_bar = st.progress(0.0)
    detail_placeholder = st.empty()

    timeout_sec = 7200
    start_time = time.time()
    spinner_msg = "⏳ Colab Worker 提取音訊中..." if only_audio else "⏳ Colab Worker 處理中..."
    prog_prefix = "Colab 音訊進度" if only_audio else "Colab 進度"

    with st.spinner(spinner_msg):
        while time.time() - start_time < timeout_sec:
            time.sleep(2)
            st_data = gdrive_bridge.get_cloud_task_status(task_id)
            if not st_data:
                status_placeholder.info(
                    "⏳ 正在等待 Colab 認領任務... (請確認 Colab 筆記本已執行 Mode 1 Worker)"
                )
                continue

            st_status = st_data.get("status", "running")
            item_name = st_data.get("current_item", "")
            pct = float(st_data.get("progress_pct", 0.0))
            speed_mb = float(st_data.get("speed_mb", 0.0))
            log_msg = st_data.get("log", "")

            progress_bar.progress(min(max(pct, 0.0), 1.0))
            speed_text = f" | ⚡ {speed_mb:.2f} MB/s" if speed_mb > 0 else ""
            status_placeholder.markdown(
                f"🤖 **{prog_prefix}**: `{item_name}` ({int(pct*100)}%){speed_text}"
            )
            if log_msg:
                detail_placeholder.caption(f"📝 最新訊息: `{log_msg}`")

            if st_status == "completed":
                progress_bar.progress(1.0)
                folder_link = (
                    gdrive_service.get_folder_link(target_id)
                    if target_id
                    else "https://drive.google.com/drive/my-drive"
                )
                st.balloons()
                done_msg = (
                    f"🎉 Colab 已全數提取完成！音訊檔已直接保存在 [我的雲端硬碟 `/{save_folder}/`]({folder_link})。"
                    if only_audio
                    else f"🎉 Colab 已全數下載並封裝完成！檔案已直接保存在 [我的雲端硬碟 `/{save_folder}/`]({folder_link})。"
                )
                st.success(done_msg)
                break
            elif st_status == "failed":
                st.error(f"❌ Colab 處理任務失敗: {log_msg}")
                err_detail = st_data.get("error")
                if err_detail:
                    with st.expander("🔍 查看詳細雲端錯誤日誌", expanded=True):
                        st.code(err_detail)
                break

