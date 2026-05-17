import os
import re
import json
import requests
import subprocess
import shutil
import tempfile
import streamlit as st

def get_media_items(url):
    items = []
    
    # Facebook 貼文特殊圖片下載邏輯
    if any(domain in url for domain in ["facebook.com", "fb.com", "fb.watch"]):
        try:
            import urllib.parse
            import html as html_lib
            
            # 讀取 Streamlit Session State 中的 Facebook Cookie
            fb_cookies_dict = {}
            if "fb_cookie" in st.session_state and st.session_state.fb_cookie:
                cookie_str = st.session_state.fb_cookie.strip()
                for item in cookie_str.split(';'):
                    item = item.strip()
                    if not item:
                        continue
                    parts = item.split('=', 1)
                    if len(parts) == 2:
                        fb_cookies_dict[parts[0]] = parts[1]
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'DNT': '1',
                'Connection': 'keep-alive'
            }
            session = requests.Session()
            if fb_cookies_dict:
                session.cookies.update(fb_cookies_dict)
                
            # 1. 解析跳轉，取得永久連結
            res = session.get(url, headers=headers, allow_redirects=True, timeout=15)
            final_url = res.url
            
            # 轉換為行動版網頁
            mobile_url = final_url.replace("www.facebook.com", "m.facebook.com")
            
            # 2. 使用行動版 Header 抓取內容，繞過登入牆
            mobile_headers = headers.copy()
            mobile_headers['User-Agent'] = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
            
            m_res = session.get(mobile_url, headers=mobile_headers, allow_redirects=True, timeout=15)
            html_content = m_res.text
            
            # 3. 提取貼文標題/群組名稱
            native_texts = re.findall(r'<div dir=\"auto\" class=\"native-text rslh\"[^>]*>(.*?)</div>', html_content, re.S)
            title_parts = []
            for nt in native_texts:
                clean = re.sub(r'<[^>]+>', ' ', nt)
                clean = html_lib.unescape(clean)
                clean = re.sub(r'\s+', ' ', clean).strip()
                if clean and clean not in ['開啟應用程式', '登入', '加入社團', '關於這個社團', '&nbsp;'] and not clean.startswith('本社團歡迎大家'):
                    title_parts.append(clean)
            
            group_name = ""
            post_desc = ""
            for tp in title_parts:
                if "http" in tp or "www." in tp:
                    continue
                if len(tp) > 10 and not group_name:
                    group_name = tp
                elif len(tp) > 10 and group_name and not post_desc:
                    post_desc = tp
                    break
                    
            post_title = "Facebook_Post"
            if group_name and post_desc:
                post_title = f"{group_name}_{post_desc}"
            elif group_name:
                post_title = group_name
                
            post_title = re.sub(r'[\\/:*?\"<>|]', '_', post_title).strip()
            post_title = post_title[:100]
            if not post_title:
                post_title = "Facebook_Post"
            
            # 4. 提取 scontent 圖片 (支援各種類型的 CDN 圖片連結)
            img_srcs = re.findall(r'<img[^>]+src=\"([^\"]+)\"', html_content)
            photo_urls = []
            for src in img_srcs:
                src = html_lib.unescape(src)
                src = urllib.parse.unquote(src)
                if 'scontent' in src:
                    # 過濾掉極小的頭像與表情圖示 (如 144x144, 48x48, 75x75)
                    if any(size in src for size in ['p144x144', 'p48x48', 'p75x75']):
                        continue
                    photo_urls.append(src)
            
            seen_ids = set()
            unique_photos = []
            for p_url in photo_urls:
                match = re.search(r'/([^/]+_n\.[a-z0-9]+)', p_url)
                if match:
                    filename = match.group(1)
                    parts = filename.split('_')
                    if len(parts) >= 2:
                        photo_id = '_'.join(parts[:2])
                        if photo_id not in seen_ids:
                            seen_ids.add(photo_id)
                            unique_photos.append(p_url)
                else:
                    if p_url not in seen_ids:
                        seen_ids.add(p_url)
                        unique_photos.append(p_url)
            
            if unique_photos:
                fb_items = []
                for idx, p_url in enumerate(unique_photos):
                    fb_items.append({
                        'url': p_url,
                        'title': f"{post_title}_{idx+1}" if len(unique_photos) > 1 else post_title,
                        'ext': 'jpg',
                        'type': 'image'
                    })
                return fb_items
        except Exception as e:
            # 錯誤時紀錄日誌，並降級使用原有的 yt-dlp 解析
            print(f"Facebook custom photo scrape failed: {e}, falling back to yt-dlp...")

    # 支援各大平台 (YouTube, X, Facebook, Instagram, TikTok 等)
    if any(domain in url for domain in ["x.com", "twitter.com", "t.co", "youtube.com", "youtu.be", "facebook.com", "fb.com", "fb.watch", "instagram.com", "ig.me", "tiktok.com"]):
        # yt-dlp 的 threads extractor 綁定 threads.net，若是 .com 則先替換
        url = url.replace("threads.com", "threads.net")
        
        import yt_dlp
        ydl_opts = {
            'quiet': True,
            'extract_flat': False,
            'format': 'best',
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # 處理可能的多個 entry (例如 Instagram Carousel 或 YouTube 播放清單)
            entries = info.get('entries', [info])
            
            for i, entry in enumerate(entries):
                title = entry.get('title') or info.get('title') or f"media_{i}"
                title = re.sub(r'[\\/:*?"<>|]', '_', title)
                
                # 取得副檔名與媒體類型
                ext = entry.get('ext')
                media_url = entry.get('url')
                
                if not media_url:
                    continue
                
                # 判斷是否為圖片 (有些平台會回傳 thumbnail 作為 entry)
                is_image = ext in ['jpg', 'jpeg', 'png', 'webp'] or entry.get('protocol') == 'https' and '.jpg' in media_url
                
                items.append({
                    'url': media_url,
                    'title': title if len(entries) == 1 else f"{title}_{i+1}",
                    'ext': ext or ('jpg' if is_image else 'mp4'),
                    'type': 'image' if is_image else 'video'
                })
        return items
            
    # 原有的 Gimymax 網頁解析邏輯
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/122.0.0.0"
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
    title = data.get("vod_data", {}).get("vod_name", "downloaded_media")
    
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
    
    items = [{
        'url': m3u8_url,
        'title': title,
        'ext': 'mp4',
        'type': 'video'
    }]
    return items

def download_media(media_item, force_audio=False):
    title = media_item['title']
    media_url = media_item['url']
    media_type = media_item['type']
    
    st.info(f"📍 正在處理媒體: **{title}** ({media_type})")
    
    # 決定最終副檔名
    if force_audio:
        ext = "mp3"
    else:
        ext = media_item['ext']
    
    downloads_dir = "/Users/ericcheng/Google Drive/我的雲端硬碟/美劇/New"
    os.makedirs(downloads_dir, exist_ok=True)
    out_path = os.path.join(downloads_dir, f"{title}.{ext}")
    
    # 優化 1：下載前檢查，避免重複下載覆寫
    if os.path.exists(out_path):
        st.success(f"⏭️ 檔案已存在，自動跳過: `{title}.{ext}`")
        return
        
    st.info(f"📍 檔案將以 {ext.upper()} 格式儲存至: `{out_path}`")
    
    try:
        if media_type == 'image':
            with st.spinner(f"⏳ 正在下載圖片 (RAM 處理中)..."):
                response = requests.get(media_url, timeout=30)
                response.raise_for_status()
                with open(out_path, "wb") as f:
                    f.write(response.content)
            st.success(f"✅ 圖片下載完成！`{title}.{ext}`")
            return

        # 影片處理 (含轉音訊)
        if force_audio:
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-i", media_url,
                "-vn",
                "-c:a", "libmp3lame",
                "-b:a", "192k",
                "-f", "mp3",    # 指定輸出格式為 mp3
                "pipe:1"        # 輸出到標準輸出 (stdout)
            ]
            spinner_msg = f"⏳ 正在下載與轉碼為 {ext.upper()} (100% RAM 處理中)..."
        else:
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-i", media_url,
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                "-f", "mp4",
                "-movflags", "frag_keyframe+empty_moov", 
                "pipe:1"
            ]
            spinner_msg = f"⏳ 正在下載影片串流並封裝為 {ext.upper()} (100% RAM 處理中)..."
        
        with st.spinner(spinner_msg):
            # 不使用 tempfile，直接將輸出捕捉到記憶體中 (stdout=subprocess.PIPE)
            process = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            if process.returncode == 0:
                # 寫入 Google Drive (一筆到底，對 GDrive 同步最友善，且過程中完全不碰 SSD)
                with open(out_path, "wb") as f:
                    f.write(process.stdout)
                
                st.success(f"✅ 下載完成！檔案已從 RAM 一次性寫入 Google Drive: `{title}.{ext}`")
            else:
                st.error("❌ FFmpeg 下載失敗！")
                with st.expander("檢視詳細錯誤日誌"):
                    st.text(process.stderr.decode('utf-8', errors='ignore'))
    except Exception as e:
        st.error(f"❌ 發生例外錯誤: {e}")
        st.caption("Note: 如果看到找不到指令的錯誤，請確認系統已安裝 FFmpeg (`brew install ffmpeg`)。")

def extract_local_audio(video_path, audio_format, title=None):
    if title:
        base_name = title
    else:
        base_name = os.path.splitext(os.path.basename(video_path))[0]
    out_dir = "/Users/ericcheng/Google Drive/我的雲端硬碟/美劇/New"
    os.makedirs(out_dir, exist_ok=True)
    
    ext = ""
    fmt = ""
    ffmpeg_cmd = []
    
    if audio_format == "MP3":
        ext = "mp3"
        ffmpeg_cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", "pipe:1"]
    elif audio_format == "M4A":
        ext = "m4a"
        ffmpeg_cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-c:a", "aac", "-b:a", "192k", "-f", "mp4", "-movflags", "frag_keyframe+empty_moov", "pipe:1"]
    else:
        try:
            probe_cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
            codec = subprocess.check_output(probe_cmd, text=True).strip()
            
            if codec == "aac":
                ext = "aac"
                fmt = "adts"
            elif codec == "mp3":
                ext = "mp3"
                fmt = "mp3"
            elif codec == "opus":
                ext = "opus"
                fmt = "opus"
            else:
                ext = "m4a"
                ffmpeg_cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-c:a", "aac", "-b:a", "192k", "-f", "mp4", "-movflags", "frag_keyframe+empty_moov", "pipe:1"]
                codec = "unknown"
                
            if codec != "unknown":
                ffmpeg_cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-c:a", "copy", "-f", fmt, "pipe:1"]
        except Exception as e:
            st.warning(f"⚠️ `{base_name}` 無法解析原始音訊格式，將預設轉換為 MP3。")
            ext = "mp3"
            ffmpeg_cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", "pipe:1"]

    out_path = os.path.join(out_dir, f"{base_name}.{ext}")
    
    if os.path.exists(out_path):
        st.success(f"⏭️ 檔案已存在: `{out_path}`")
        return
        
    try:
        with st.spinner(f"⏳ 正在提取 `{base_name}` 音訊為 {ext.upper()} (100% RAM 處理中)..."):
            process = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if process.returncode == 0:
                with open(out_path, "wb") as f:
                    f.write(process.stdout)
                st.success(f"✅ 提取完成！音訊已儲存至: `{out_path}`")
            else:
                st.error(f"❌ 提取 `{base_name}` 失敗！")
                with st.expander("錯誤日誌"):
                    st.text(process.stderr.decode('utf-8', errors='ignore'))
    except Exception as e:
        st.error(f"❌ 發生例外錯誤: {e}")

# ========================================================
# Streamlit Web App Interface
# ========================================================
st.set_page_config(page_title="Gimymax Media Downloader", page_icon="🎬", layout="centered")

st.title("🎬 媒體下載與音訊提取器")

tab1, tab2 = st.tabs(["🌐 線上影片下載", "📁 影片音訊提取"])

with tab1:
    st.markdown("將 Gimymax, X, YouTube, Facebook, IG, TikTok 等影片網址直接下載。")
    
    target_urls = st.text_area("🔗 請輸入影片網址 (每行一個，最多15個):", placeholder="https://gimymax.com/ep/... \nhttps://youtube.com/watch?v=... \nhttps://www.facebook.com/watch/?v=...")
    
    fb_cookie_str = st.text_input("🔑 Facebook Cookie (選填，用於下載私密社團或好友貼文圖片):", type="password", placeholder="c_user=xxxx; xs=xxxx; ...", help="若要下載私密社團、好友貼文或無法下載時，請在 Chrome 開啟 Facebook -> 按 F12 -> 於 Application (應用程式) -> Cookies 中複製 c_user 與 xs 拼接（或直接複製整段 Cookie 值）並在此貼上。下載公開內容免填。")
    st.session_state.fb_cookie = fb_cookie_str
    
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
    
    btn_video = st.button("⬇️ 開始批次下載影片", type="primary", use_container_width=True)
    
    if btn_video:
        urls = [url.strip() for url in target_urls.split('\n') if url.strip()]
        
        if not urls:
            st.warning("⚠️ 請先輸入網址！")
        elif len(urls) > 15:
            st.error("⚠️ 一次最多只能輸入 15 個網址，請減少數量後重試！")
        else:
            st.info(f"📥 準備下載 {len(urls)} 個影片檔案...")
            
            # 建立進度條
            progress_bar = st.progress(0)
            
            # 遍歷網址進行下載
            for i, url in enumerate(urls):
                current_num = i + 1
                st.markdown(f"### 📍 正在處理第 {current_num}/{len(urls)} 個...")
                
                try:
                    st.text(f"正在擷取網頁資訊: {url}")
                    with st.spinner("🔍 尋找影片串流中..."):
                        media_items = get_media_items(url)
                    
                    if not media_items:
                        st.error(f"❌ 找不到有效的影片串流網址: {url}")
                    else:
                        for item in media_items:
                            download_media(item)
                        
                except Exception as e:
                    st.error(f"❌ 處理 {url} 時發生錯誤: {e}")
                
                # 更新進度條
                progress_bar.progress(current_num / len(urls))
                st.divider()
                
            st.balloons()
            st.success("🎉 所有下載任務處理完畢！")

with tab2:
    st.markdown("從本地檔案或線上播放清單提取出純音訊，完全在記憶體內處理，避免硬碟損耗。")
    local_video_path = st.text_input("📁 請輸入路徑 (本地影片/資料夾，或 YouTube, FB, IG 等網址/播放清單):", placeholder="/Users/ericcheng/Movies/ 或 https://youtube.com/playlist?list=... 或 https://www.facebook.com/watch/?v=...")
    audio_format = st.selectbox("🎵 請選擇輸出音訊格式:", ["預設 (原始格式)", "M4A", "MP3"])
    
    if st.button("▶️ 開始提取音訊", type="primary", use_container_width=True):
        input_path = local_video_path.strip()
        if not input_path:
            st.warning("⚠️ 請先輸入路徑！")
        elif input_path.startswith("http://") or input_path.startswith("https://"):
            import yt_dlp
            urls_to_process = []
            
            ydl_opts = {
                'quiet': True,
                'extract_flat': 'in_playlist',
            }
            
            try:
                with st.spinner("🔍 正在解析線上網址/播放清單..."):
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(input_path, download=False)
                        if 'entries' in info:
                            for entry in info['entries']:
                                urls_to_process.append(entry.get('url'))
                        else:
                            urls_to_process.append(input_path)
            except Exception as e:
                st.error(f"❌ 解析網址失敗: {e}")
                
            urls_to_process = [u for u in urls_to_process if u]
            
            if urls_to_process:
                st.info(f"📥 準備處理 {len(urls_to_process)} 個線上影片...")
                progress_bar = st.progress(0)
                
                for i, url in enumerate(urls_to_process):
                    st.markdown(f"### 📍 正在處理第 {i+1}/{len(urls_to_process)} 個影片...")
                    try:
                        with st.spinner("🔍 尋找最佳串流..."):
                            media_items = get_media_items(url)
                            
                        if media_items:
                            for item in media_items:
                                extract_local_audio(item['url'], audio_format, title=item['title'])
                        else:
                            st.error(f"❌ 無法取得串流: {url}")
                    except Exception as e:
                        st.error(f"❌ 發生例外錯誤: {e}")
                    
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
                # 取得資料夾內所有的影片檔
                valid_exts = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".ts"}
                for root, dirs, files in os.walk(input_path):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in valid_exts:
                            video_files.append(os.path.join(root, f))
                
                if not video_files:
                    st.warning("⚠️ 該資料夾內找不到任何支援的影片檔案！")
            
            if video_files:
                st.info(f"📥 準備處理 {len(video_files)} 個影片檔案...")
                progress_bar = st.progress(0)
                
                for i, v_path in enumerate(video_files):
                    st.markdown(f"### 📍 正在處理第 {i+1}/{len(video_files)} 個檔案: `{os.path.basename(v_path)}`")
                    extract_local_audio(v_path, audio_format)
                    progress_bar.progress((i + 1) / len(video_files))
                    st.divider()
                    
                st.balloons()
                st.success("🎉 所有本地提取任務處理完畢！")
