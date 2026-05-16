import os
import re
import json
import requests
import subprocess
import streamlit as st

def get_stream_info(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    
    # 找尋 player_data 變數中的 JSON 資料
    match = re.search(r'var player_data=(.*?)</script>', response.text)
    if not match:
        raise ValueError("Cannot find player_data in the webpage.")
        
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        raise ValueError("Failed to parse player_data JSON.")
        
    m3u8_url = data.get("url")
    title = data.get("vod_data", {}).get("vod_name", "downloaded_audio")
    
    # 將「第X季」替換為 S1, S2...
    zh_num = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    match_s = re.search(r'第([一二三四五六七八九十\d]+)季', title)
    if match_s:
        num_str = match_s.group(1)
        s_num = int(num_str) if num_str.isdigit() else zh_num.get(num_str, 1)
        title = re.sub(r'第[一二三四五六七八九十\d]+季', f'S{s_num}', title)

    # 嘗試抓取集數資訊，將「第X集」替換為 E01, E02...
    match_ep = re.search(r'data-playname="([^"]+)"', response.text)
    if match_ep:
        ep_raw = match_ep.group(1)
        match_e = re.search(r'第(\d+)集', ep_raw)
        if match_e:
            ep_str = f"E{int(match_e.group(1)):02d}"
        else:
            ep_str = ep_raw
        title = f"{title}_{ep_str}"
    
    # 處理檔名特殊字元
    title = re.sub(r'[\\/:*?"<>|]', '_', title)
    return m3u8_url, title

def download_media(m3u8_url, title, mode="audio"):
    st.info(f"📍 解析到標題: **{title}**")
    
    ext = "mp3" if mode == "audio" else "mp4"
    downloads_dir = "/Users/ericcheng/Google Drive/我的雲端硬碟/美劇/New"
    os.makedirs(downloads_dir, exist_ok=True)
    out_path = os.path.join(downloads_dir, f"{title}.{ext}")
    
    st.info(f"📍 檔案將以 {ext.upper()} 格式儲存至: `{out_path}`")
    
    if mode == "audio":
        # 建構 FFmpeg 執行指令 (音訊)
        # -y: 覆寫已存在檔案
        # -i: 輸入網址 (M3U8)
        # -vn: 不需要影像 (Video None)
        # -c:a libmp3lame: 強制轉碼為 MP3
        # -b:a 192k: 設定 192k 音質
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", m3u8_url,
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", "192k",
            out_path
        ]
        spinner_msg = f"⏳ 正在下載與轉碼為 {ext.upper()}，這可能需要幾分鐘的時間..."
    else:
        # 建構 FFmpeg 執行指令 (影片)
        # -y: 覆寫已存在檔案
        # -i: 輸入網址 (M3U8)
        # -c copy: 複製原始視訊與音訊（不轉碼，無損直出）
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", m3u8_url,
            "-c", "copy",
            out_path
        ]
        spinner_msg = f"⏳ 正在下載影片串流並封裝為 {ext.upper()}，原始畫質無損直出中..."
    
    try:
        with st.spinner(spinner_msg):
            process = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            
            if process.returncode == 0:
                st.success(f"✅ 下載完成！檔案已存入您的 Downloads 資料夾: `{title}.{ext}`")
            else:
                st.error("❌ FFmpeg 下載失敗！")
                with st.expander("檢視詳細錯誤日誌"):
                    st.text(process.stderr)
    except Exception as e:
        st.error(f"❌ 發生例外錯誤: {e}")
        st.caption("Note: 如果看到找不到指令的錯誤，請確認系統已安裝 FFmpeg (`brew install ffmpeg`)。")

# ========================================================
# Streamlit Web App Interface
# ========================================================
st.set_page_config(page_title="Gimymax Media Downloader", page_icon="🎬", layout="centered")

st.title("🎬 線上短劇/影集 下載器")
st.markdown("將 Gimymax 等影片網址的串流直接下載，可選擇儲存為 **高音質MP3** 或 **原畫質MP4**。")

target_urls = st.text_area("🔗 請輸入影片網址 (每行一個，最多15個):", placeholder="https://gimymax.com/ep/... \nhttps://gimymax.com/ep/... ")

# 即時計算目前已輸入的有效網址數量，並提示剩餘可輸入數量
current_urls = [url.strip() for url in target_urls.split('\n') if url.strip()]
current_count = len(current_urls)
remaining = max(0, 15 - current_count)

if current_count > 15:
    st.error(f"⚠️ 目前已輸入 {current_count} 個網址，超過上限 15 個！")
elif current_count > 0:
    st.caption(f"ℹ️ 已輸入 **{current_count}** 個，您還可以再輸入 **{remaining}** 個網址。")
else:
    st.caption(f"ℹ️ 您可以輸入最多 **15** 個網址。")

col1, col2 = st.columns(2)

with col1:
    btn_video = st.button("⬇️ 開始批次下載影片", type="primary", use_container_width=True)
with col2:
    btn_audio = st.button("🎵 開始批次下載音訊", use_container_width=True)

if btn_video or btn_audio:
    mode = "video" if btn_video else "audio"
    
    urls = [url.strip() for url in target_urls.split('\n') if url.strip()]
    
    if not urls:
        st.warning("⚠️ 請先輸入網址！")
    elif len(urls) > 15:
        st.error("⚠️ 一次最多只能輸入 15 個網址，請減少數量後重試！")
    else:
        mode_str = "影片" if mode == "video" else "音頻"
        st.info(f"📥 準備下載 {len(urls)} 個{mode_str}檔案...")
        
        # 建立進度條
        progress_bar = st.progress(0)
        
        # 遍歷網址進行下載
        for i, url in enumerate(urls):
            current_num = i + 1
            st.markdown(f"### 📍 正在處理第 {current_num}/{len(urls)} 個...")
            
            try:
                st.text(f"正在擷取網頁資訊: {url}")
                with st.spinner("🔍 尋找影片串流中..."):
                    m3u8_url, title = get_stream_info(url)
                
                if not m3u8_url:
                    st.error(f"❌ 找不到有效的 m3u8 串流網址: {url}")
                else:
                    download_media(m3u8_url, title, mode=mode)
                    
            except Exception as e:
                st.error(f"❌ 處理 {url} 時發生錯誤: {e}")
            
            # 更新進度條
            progress_bar.progress(current_num / len(urls))
            st.divider()
            
        st.balloons()
        st.success("🎉 所有下載任務處理完畢！")
