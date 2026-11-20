# -*- coding: utf-8 -*-
"""頂部控制列：儲存目標切換、Google Drive 授權與資料夾選擇器、Colab 心跳、RAM Disk 與資源管理。"""

import os
import signal
import time
import streamlit as st

from core.downloader import abort_active_download, get_active_download_info
from core.macos_utils import (
    BASE_DIR,
    check_dir_writable,
    force_os_memory_trim,
    load_download_dir,
    release_system_resources,
)
from services import gdrive_bridge, gdrive_service, ramdisk_manager
from ui.state import (
    DEST_COLAB,
    DEST_GDRIVE,
    DEST_LOCAL,
    cb_choose_folder,
    cb_dest_change,
    cb_dir_input_change,
    cb_set_folder,
)


@st.cache_data(ttl=120, show_spinner=False)
def _cached_subfolders(parent_id, _token_key):
    return gdrive_service.list_subfolders(parent_id)


def _gd_token_key():
    try:
        return os.path.getmtime(gdrive_service.TOKEN_FILE)
    except OSError:
        return 0


def _gd_set_target(folder_id, label):
    gdrive_service.set_target_folder(folder_id, label)
    st.session_state.pending_toast = (f"✅ 雲端目標已設為：{label}", "☁️")


def _render_gdrive_setup():
    """首次設定：引導使用者建立 OAuth 用戶端並上傳 credentials.json。"""
    st.info(
        "🧭 **首次連接只需做一次**：建立 Google OAuth 用戶端 → 上傳 JSON → 點授權。之後會自動記住登入狀態。"
    )
    with st.expander("📖 3 分鐘取得 credentials.json (逐步教學)", expanded=True):
        st.markdown(
            "1. 開啟 [Google Cloud Console - 建立專案](https://console.cloud.google.com/projectcreate)，建立任意名稱專案\n"
            "2. 啟用 [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)\n"
            "3. 設定 [OAuth 同意畫面](https://console.cloud.google.com/auth/branding)：使用者類型選 **外部**，"
            "並在 [目標對象](https://console.cloud.google.com/auth/audience) 將自己的 Gmail 加入 **測試使用者**\n"
            "4. 到 [用戶端](https://console.cloud.google.com/auth/clients) → 建立用戶端 → 應用程式類型選 **電腦版應用程式**\n"
            "5. 下載 JSON (檔名 `client_secret_xxx.json`)，直接拖到下方即可\n\n"
            "⚠️ 若同意畫面維持「測試中」狀態，Google 會每 7 天讓 Refresh Token 失效，屆時重新點授權即可；"
            "改為「發布為正式版」(不需送審，僅自己使用) 可避免。"
        )
    uploaded_cred = st.file_uploader(
        "上傳 OAuth 用戶端 JSON (client_secret_xxx.json / credentials.json):",
        type=["json"],
        key="gd_cred_upload",
    )
    if uploaded_cred:
        raw = uploaded_cred.getvalue()
        ok, msg = gdrive_service.validate_credentials_json(raw)
        if ok:
            with open(gdrive_service.CREDENTIALS_FILE, "wb") as f:
                f.write(raw)
            st.session_state.pending_toast = (
                "✅ credentials.json 已儲存，請點擊授權按鈕",
                "🔑",
            )
            st.rerun()
        else:
            st.error(f"❌ 檔案格式不正確：{msg}")


def _render_gdrive_folder_picker():
    """雲端目標資料夾選擇器：瀏覽 / 建立 / 貼上網址。"""
    if "gd_browse_stack" not in st.session_state:
        st.session_state.gd_browse_stack = [("root", "我的雲端硬碟")]
    stack = st.session_state.gd_browse_stack
    cur_id, _ = stack[-1]
    cur_label = "/" + "/".join(n for _, n in stack[1:]) + (
        "/" if len(stack) > 1 else ""
    )

    tab_b, tab_u = st.tabs(["🗂️ 瀏覽資料夾", "🔗 貼上資料夾網址"])
    with tab_b:
        st.caption(f"目前瀏覽位置：`我的雲端硬碟{cur_label}`")
        try:
            subs = _cached_subfolders(cur_id, _gd_token_key())
        except Exception as e:
            subs = []
            st.error(f"❌ 無法讀取資料夾列表：{e}")

        names = [f["name"] for f in subs]
        b1, b2 = st.columns([3, 1])
        with b1:
            picked = st.selectbox(
                "子資料夾",
                options=list(range(len(subs))),
                format_func=lambda i: f"📁 {names[i]}",
                index=None,
                placeholder="(此層無子資料夾)" if not subs else "選擇子資料夾…",
                label_visibility="collapsed",
                key=f"gd_pick_{cur_id}",
            )
        with b2:
            if st.button(
                "➡️ 進入",
                use_container_width=True,
                disabled=picked is None,
                key="gd_enter",
            ):
                stack.append((subs[picked]["id"], subs[picked]["name"]))
                st.rerun()

        a1, a2, a3 = st.columns(3)
        with a1:
            if st.button(
                "⬆️ 上一層",
                use_container_width=True,
                disabled=len(stack) <= 1,
                key="gd_up",
            ):
                stack.pop()
                st.rerun()
        with a2:
            if st.button(
                "✅ 存到「目前位置」",
                type="primary",
                use_container_width=True,
                key="gd_set_here",
            ):
                _gd_set_target(cur_id, cur_label)
                st.rerun()
        with a3:
            if st.button(
                "↩️ 重設為 /Download/", use_container_width=True, key="gd_reset"
            ):
                _gd_set_target(None, f"/{gdrive_service.DEFAULT_FOLDER_NAME}/")
                st.rerun()

        n1, n2 = st.columns([3, 1])
        with n1:
            new_name = st.text_input(
                "新資料夾名稱",
                placeholder="在目前位置建立新資料夾，例如：影片/2026",
                label_visibility="collapsed",
                key="gd_new_name",
            )
        with n2:
            if st.button(
                "➕ 建立並設為目標",
                use_container_width=True,
                disabled=not (new_name or "").strip(),
                key="gd_create",
            ):
                try:
                    parent_id, parent_label = cur_id, cur_label
                    for part in [p.strip() for p in new_name.split("/") if p.strip()]:
                        existing = next(
                            (
                                f
                                for f in gdrive_service.list_subfolders(parent_id)
                                if f["name"] == part
                            ),
                            None,
                        )
                        node = existing or gdrive_service.create_folder(
                            part, parent_id
                        )
                        parent_id = node["id"]
                        parent_label = f"{parent_label.rstrip('/')}/{part}/"
                    _cached_subfolders.clear()
                    _gd_set_target(parent_id, parent_label)
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 建立資料夾失敗：{e}")

    with tab_u:
        url_in = st.text_input(
            "貼上 Google Drive 資料夾網址或 ID",
            placeholder="https://drive.google.com/drive/folders/1AbCdEf...",
            key="gd_url_input",
            help="可貼共用雲端硬碟 (Shared Drive) 或別人共用給你的資料夾 (需有編輯權限)",
        )
        if st.button(
            "✅ 設為目標資料夾",
            use_container_width=True,
            disabled=not url_in,
            key="gd_url_set",
        ):
            fid = gdrive_service.parse_folder_input(url_in)
            if not fid:
                st.error("❌ 無法從輸入內容解析出資料夾 ID")
            else:
                try:
                    meta = gdrive_service.get_folder_meta(fid)
                    try:
                        label = gdrive_service.get_folder_path_label(fid)
                    except Exception:
                        label = f"/{meta.get('name', fid)}/"
                    _gd_set_target(fid, label)
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 無法存取此資料夾：{e}")


def render_header_controls():
    """渲染標題、儲存目標設定、RAM Disk 控制列、系統資源釋放列與孤兒任務監控。"""
    st.title("🎬 媒體下載與音訊提取器")

    # 1. 儲存目標選擇區
    st.radio(
        "📦 **儲存目標模式**:",
        options=[DEST_LOCAL, DEST_GDRIVE, DEST_COLAB],
        key="dest_radio",
        horizontal=True,
        on_change=cb_dest_change,
        help="選擇下載儲存方式。Colab 雲端直存模式由 Colab 遠端主機極速下載並直接封裝於雲端硬碟，本機零頻寬消耗。",
    )

    if st.session_state.storage_destination in ("gdrive_cloud", "colab_cloud"):
        if st.session_state.storage_destination == "colab_cloud":
            col_c1, col_c2 = st.columns([3, 1])
            with col_c1:
                st.info(
                    "🤖 **Google Colab 雲端直存模式**：由 Colab 遠端主機以千兆頻寬極速下載並直接封裝至 Google Drive，本機 0 頻寬消耗！"
                )
            with col_c2:
                st.markdown(
                    f'<a href="{gdrive_bridge.COLAB_NOTEBOOK_URL}" target="_blank">'
                    f'<button style="width:100%;margin-top:4px;padding:8px 0;background-color:#F9AB00;color:black;border:none;border-radius:6px;font-weight:bold;cursor:pointer;">'
                    f"🚀 開啟 Colab 筆記本</button></a>",
                    unsafe_allow_html=True,
                )

            hb_col1, hb_col2 = st.columns([5, 1])
            with hb_col1:
                hb = gdrive_bridge.get_worker_heartbeat()
                if hb.get("online"):
                    st.success(
                        f"🟢 **雲端工作節點在線中**：{hb.get('message')}，可直接輸入網址下載！"
                    )
                else:
                    st.warning(
                        f"🟡 **雲端工作節點目前離線** ({hb.get('message')})\n\n"
                        f"👉 若已在 Colab 執行模式一，可點擊右側「🔄 刷新狀態」；或點擊右上角「🚀 開啟 Colab 筆記本」重新點擊模式一即可。"
                    )
            with hb_col2:
                st.markdown("<div style='margin-top: 4px;'></div>", unsafe_allow_html=True)
                if st.button(
                    "🔄 刷新狀態",
                    use_container_width=True,
                    help="重新檢查 Colab Worker 心跳",
                ):
                    st.rerun()

        cred_path = gdrive_service.get_credentials_path()
        is_auth = gdrive_service.is_authenticated()
        user_email = gdrive_service.get_connected_account_email() if is_auth else None

        if is_auth and user_email:
            target_id, target_label = gdrive_service.get_target_folder()
            folder_link = (
                gdrive_service.get_folder_link(target_id)
                if target_id
                else "https://drive.google.com/drive/my-drive"
            )
            col_g1, col_g2 = st.columns([4, 1])
            with col_g1:
                st.success(
                    f"🟢 **已連接**：`{user_email}`\n\n📂 **上傳至**：[我的雲端硬碟 `{target_label}`]({folder_link})"
                )
            with col_g2:
                if st.button("🚪 登出", use_container_width=True):
                    gdrive_service.revoke_gdrive_auth()
                    st.rerun()

            def _cb_skip_dup():
                gdrive_service.update_config(
                    gdrive_skip_duplicates=st.session_state.gdrive_skip_dup
                )

            def _cb_save_local_copy():
                gdrive_service.update_config(
                    gdrive_save_local_copy=st.session_state.gdrive_save_local_copy
                )

            saved_local_dir = st.session_state.get("download_dir", load_download_dir())
            c_opt1, c_opt2 = st.columns([1.2, 1.3])
            with c_opt1:
                st.checkbox(
                    "⏭️ 雲端已有同名檔案時自動跳過",
                    key="gdrive_skip_dup",
                    on_change=_cb_skip_dup,
                    help="下載前先檢查目標資料夾，已有同名檔案則略過以節省流量與時間。",
                )
            with c_opt2:
                st.checkbox(
                    f"📥 同步在本地儲存一份 (位置: `{os.path.basename(saved_local_dir.rstrip('/')) or saved_local_dir}`)",
                    key="gdrive_save_local_copy",
                    on_change=_cb_save_local_copy,
                    help=f"若啟用，影片封裝完成後會先將檔案儲存至上一次使用的本地下載資料夾 ({saved_local_dir})，再上傳至雲端，確保本地立刻有檔案可看。",
                )

            with st.expander("📂 變更雲端目標資料夾", expanded=False):
                _render_gdrive_folder_picker()
        else:
            if is_auth and not user_email:
                st.warning(
                    "⚠️ Google Drive 授權已失效 (Token 過期或被撤銷)，請重新授權。"
                )
            elif cred_path:
                st.warning("⚠️ 尚未連接 Google Drive 帳號。")

            if cred_path:
                c_a1, c_a2 = st.columns([3, 1])
                with c_a1:
                    do_auth = st.button(
                        "🔑 授權連接 Google Drive (開啟瀏覽器登入)",
                        type="primary",
                        use_container_width=True,
                    )
                with c_a2:
                    if st.button(
                        "🗑️ 更換 JSON",
                        use_container_width=True,
                        help="刪除目前的 credentials.json 重新上傳",
                    ):
                        try:
                            os.remove(cred_path)
                        except Exception:
                            pass
                        st.rerun()
                if do_auth:
                    try:
                        with st.spinner("🌐 已開啟瀏覽器授權頁面，請在 3 分鐘內完成登入…"):
                            gdrive_service.revoke_gdrive_auth()
                            gdrive_service.run_oauth_flow(timeout_seconds=180)
                        st.session_state.pending_toast = (
                            "✅ Google Drive 授權成功！",
                            "☁️",
                        )
                        st.rerun()
                    except Exception as e:
                        err = str(e)
                        hint = ""
                        if "access_denied" in err or "403" in err:
                            hint = "\n\n💡 請確認已將此 Gmail 加入 OAuth 同意畫面的「測試使用者」。"
                        elif "redirect_uri" in err:
                            hint = "\n\n💡 OAuth 用戶端類型必須是「電腦版應用程式」，請點「更換 JSON」重新上傳。"
                        st.error(f"❌ 授權失敗: {err}{hint}")
            else:
                _render_gdrive_setup()
    else:
        col_dir1, col_dir2 = st.columns([3, 1])
        with col_dir1:
            st.text_input(
                "📁 下載儲存資料夾:",
                key="main_dir_input",
                on_change=cb_dir_input_change,
                help="所有下載與音訊檔都會儲存至此資料夾",
            )

        with col_dir2:
            st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
            st.button(
                "📂 選擇資料夾",
                key="main_browse_btn",
                use_container_width=True,
                on_click=cb_choose_folder,
            )

        current_target_dir = st.session_state.get("download_dir", load_download_dir())
        is_writable, write_err = check_dir_writable(current_target_dir)
        if not is_writable:
            st.error(
                f"🚫 **警告：目前下載儲存資料夾無法寫入 (唯讀 / Read-only)！**\n\n"
                f"📍 路徑：`{current_target_dir}`\n\n"
                f"⚠️ **原因**：{write_err}\n\n"
                f"💡 **建議解決方式**：\n"
                f"- 若為外接硬碟（如 Windows NTFS 格式），macOS 預設僅允許讀取、無法寫入。\n"
                f"- 請點擊上方「📂 選擇資料夾」或下方「常用儲存位置」切換至本機資料夾（如 Downloads），或將外接硬碟格式化/掛載為支援寫入之格式 (如 exFAT、APFS)。"
            )

        with st.expander("⚡ 常用儲存位置快捷切換", expanded=False):
            q_cols = st.columns(4)
            home_dir = os.path.expanduser("~")
            downloads_path = os.path.join(home_dir, "Downloads")
            desktop_path = os.path.join(home_dir, "Desktop")
            documents_path = os.path.join(home_dir, "Documents")
            proj_downloads = os.path.join(BASE_DIR, "downloads")

            with q_cols[0]:
                st.button(
                    "📥 下載 (Downloads)",
                    use_container_width=True,
                    on_click=cb_set_folder,
                    args=(downloads_path, "Downloads"),
                )
            with q_cols[1]:
                st.button(
                    "🖥️ 桌面 (Desktop)",
                    use_container_width=True,
                    on_click=cb_set_folder,
                    args=(desktop_path, "Desktop"),
                )
            with q_cols[2]:
                st.button(
                    "📁 文件 (Documents)",
                    use_container_width=True,
                    on_click=cb_set_folder,
                    args=(documents_path, "Documents"),
                )
            with q_cols[3]:
                st.button(
                    "📂 專案內 downloads",
                    use_container_width=True,
                    on_click=cb_set_folder,
                    args=(proj_downloads, "專案內部 downloads"),
                )

    # 2. RAM Disk 純記憶體模式控制列
    sys_ram_gb = ramdisk_manager.get_system_total_ram_gb()
    ram_mounted = ramdisk_manager.is_ramdisk_mounted()
    col_ram1, col_ram2 = st.columns([2.5, 1.5])
    with col_ram1:
        if ram_mounted:
            ramdisk_manager.auto_purge_if_full()
            tot, _, free, pct = ramdisk_manager.get_ramdisk_usage()
            tot_gb = tot / (1024 ** 3)
            st.success(
                f"🚀 **{tot_gb:.0f}GB APFS RAM Disk 運行中 (100% 零 SSD 磨損)**\n\n"
                f"容量：剩餘 `{free/1024/1024/1024:.2f} GB` / `{tot_gb:.2f} GB` (已用 {pct*100:.1f}%) ｜ 掛載點：`{ramdisk_manager.MOUNT_POINT}`"
            )
        else:
            st.info(
                f"💡 **RAM Disk 未掛載**（系統實體 RAM: {sys_ram_gb}GB）：目前使用 macOS 系統 SSD 暫存。\n\n"
                f"建議掛載 APFS RAM Disk（1080p 長片或電影推薦 8GB 以上），切片封裝全數在記憶體進行，達成 100% 零硬碟讀寫與極速封裝。\n\n"
                f"*(若剛透過終端機或 Rclone 完成掛載，可點右側「🔄」或按 `Cmd + R` 重新整理讀取共用磁碟)*"
            )

    with col_ram2:
        if ram_mounted:
            st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
            b_c1, b_c2 = st.columns(2)
            with b_c1:
                if st.button(
                    "🧹 清空暫存",
                    use_container_width=True,
                    help="清空 RAM Disk 內所有暫存與已完成檔案，秒級還原空間",
                ):
                    c = ramdisk_manager.clean_all_ramdisk_files()
                    force_os_memory_trim()
                    st.session_state.pending_toast = (
                        f"✅ 已清空 RAM Disk ({c} 個檔案) 並釋放記憶體！",
                        "🧹",
                    )
                    st.rerun()
            with b_c2:
                if st.button(
                    "⏏️ 卸載",
                    use_container_width=True,
                    help="立即歸還記憶體給 macOS 系統",
                ):
                    ok, msg = ramdisk_manager.unmount_ramdisk()
                    if ok:
                        st.session_state.pending_toast = (
                            "✅ 已卸載 RAM Disk 並歸還記憶體",
                            "⏏️",
                        )
                    else:
                        st.error(msg)
                    st.rerun()
        else:
            default_size = 10 if sys_ram_gb >= 24 else (6 if sys_ram_gb >= 16 else 4)
            size_options = (
                [6, 8, 10, 12]
                if sys_ram_gb >= 24
                else ([4, 6, 8] if sys_ram_gb >= 16 else [2, 4])
            )
            selected_size = st.selectbox(
                "劃分大小",
                size_options,
                index=size_options.index(default_size)
                if default_size in size_options
                else 0,
                format_func=lambda x: f"{x} GB 記憶體",
            )
            btn_c1, btn_c2 = st.columns([3, 1])
            with btn_c1:
                if st.button(
                    f"🚀 掛載 {selected_size}GB RAM Disk",
                    type="primary",
                    use_container_width=True,
                    help=f"免 root 建立 {selected_size}GB APFS 虛擬記憶體磁碟",
                ):
                    with st.spinner(
                        f"正在向 macOS 申請劃分 {selected_size}GB APFS RAM Disk..."
                    ):
                        ok, msg = ramdisk_manager.mount_ramdisk(size_gb=selected_size)
                        if ok:
                            st.session_state.pending_toast = (
                                f"✅ {selected_size}GB APFS RAM Disk 掛載成功！",
                                "🚀",
                            )
                        else:
                            st.error(msg)
                        st.rerun()
            with btn_c2:
                if st.button(
                    "🔄", help="重新偵測 RAM Disk 掛載狀態", use_container_width=True
                ):
                    st.rerun()

    # 3. 系統資源清理控制列
    def _clear_st_cache():
        st.cache_data.clear()
        st.cache_resource.clear()

    col_res1, col_res2 = st.columns(2)
    with col_res1:
        if st.button(
            "🧹 僅釋放記憶體快取 (不關閉服務)",
            use_container_width=True,
            help="清空下載快取、暫存 Cookie 與釋放垃圾回收 RAM",
        ):
            with st.spinner("正在釋放系統快取與垃圾回收中..."):
                info = release_system_resources(clear_cache_cb=_clear_st_cache)
                st.success("🧹 記憶體與快取清理完畢！")
                for msg in info:
                    if "關閉" not in msg and "終止" not in msg:
                        st.toast(msg, icon="ℹ️")

    with col_res2:
        if st.button(
            "♻️ 釋放所有資源並關閉", type="primary", use_container_width=True
        ):
            with st.spinner("正在釋放系統資源與關閉程式中..."):
                abort_active_download()
                ramdisk_manager.unmount_ramdisk()
                info = release_system_resources(clear_cache_cb=_clear_st_cache)
                for msg in info:
                    st.success(msg)
                st.toast("♻️ 資源釋放成功，程式即將關閉...")
                st.warning("⚠️ 程式已終止，請手動關閉此網頁分頁。")
                time.sleep(1.2)
                os.kill(os.getpid(), signal.SIGTERM)

    # ── 孤兒背景下載任務監控與緊急停止 ───────────────────────────────────────
    active_info = get_active_download_info()
    if active_info.get("active"):
        st.error(
            f"🚨 **偵測到後台目前有正在執行的下載任務**：`{active_info.get('title', '未知影片')}`\n\n"
            f"*(若您剛剛重新整理了網頁，後台多線程可能仍在繼續下載佔用頻寬與記憶體)*"
        )
        col_ab1, col_ab2 = st.columns([3, 1.2])
        with col_ab1:
            st.caption(
                "💡 點擊右側按鈕可發送即時中斷信號，秒級銷毀下載線程並清理暫存空間。"
            )
        with col_ab2:
            if st.button(
                "🛑 強制停止背景下載",
                type="primary",
                use_container_width=True,
                key="abort_orphan_task_btn",
            ):
                abort_active_download()
                st.session_state.pending_toast = (
                    "🛑 已強制終止背景下載任務並清除暫存！",
                    "🛑",
                )
                st.rerun()

    st.divider()
