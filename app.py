import os
import sys
import re
import json
import time
import threading
import requests
import subprocess
import shutil
import tempfile
import gc
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import yt_dlp
import streamlit as st
import gdrive_service

# ── 全域共用 Session（跨任務複用 TCP 連線池 + DNS 快取）──────────────────────
_global_session = requests.Session()
_global_adapter = requests.adapters.HTTPAdapter(
    pool_connections=32, pool_maxsize=64, max_retries=3
)
_global_session.mount('https://', _global_adapter)
_global_session.mount('http://', _global_adapter)
_GLOBAL_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
}

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
DEFAULT_DOWNLOAD_DIR = "/Users/ericcheng/Downloads"

def load_download_dir():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                d = data.get("download_dir", "").strip()
                if d:
                    return d
        except Exception:
            pass
    return DEFAULT_DOWNLOAD_DIR

def save_download_dir(path):
    if not path or not path.strip():
        path = DEFAULT_DOWNLOAD_DIR
    path = os.path.abspath(os.path.expanduser(path.strip()))
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"download_dir": path}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return path

def choose_folder_dialog(current_dir):
    if not current_dir or not os.path.exists(current_dir):
        current_dir = DEFAULT_DOWNLOAD_DIR
    try:
        os.makedirs(current_dir, exist_ok=True)
    except Exception:
        current_dir = DEFAULT_DOWNLOAD_DIR
        
    current_dir = os.path.abspath(current_dir)

    # 1. macOS native Finder folder chooser via osascript
    # (註：勿在 tell application "Finder" 內呼叫，以免觸發 macOS TCC 隱私與 Access -54 錯誤)
    if sys.platform == "darwin":
        # 1a. 帶預設資料夾
        try:
            safe_dir = current_dir.replace('"', '\\"')
            script = f'POSIX path of (choose folder with prompt "請選擇下載儲存資料夾" default location POSIX file "{safe_dir}")'
            cmd = ["osascript", "-e", script]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                res_path = result.stdout.strip()
                if res_path and os.path.exists(res_path):
                    return res_path
        except Exception:
            pass

        # 1b. 若 1a 失敗，退回無預設位置的原生彈窗
        try:
            script = 'POSIX path of (choose folder with prompt "請選擇下載儲存資料夾")'
            cmd = ["osascript", "-e", script]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                res_path = result.stdout.strip()
                if res_path and os.path.exists(res_path):
                    return res_path
        except Exception:
            pass

    # 2. Linux zenity
    if sys.platform.startswith("linux"):
        try:
            cmd = ["zenity", "--file-selection", "--directory", f"--filename={current_dir}"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                res_path = result.stdout.strip()
                if res_path and os.path.exists(res_path):
                    return res_path
        except Exception:
            pass

    # 3. PyQt5 / PyQt6 / PySide6 / PySide2 Fallback
    for qt_mod in ["PyQt6.QtWidgets", "PyQt5.QtWidgets", "PySide6.QtWidgets", "PySide2.QtWidgets"]:
        try:
            mod = __import__(qt_mod, fromlist=["QApplication", "QFileDialog"])
            QApplication = getattr(mod, "QApplication")
            QFileDialog = getattr(mod, "QFileDialog")
            app = QApplication.instance() or QApplication([])
            folder = QFileDialog.getExistingDirectory(None, "請選擇下載儲存資料夾", current_dir)
            if folder and os.path.exists(folder):
                return folder
            break
        except Exception:
            pass

    # 4. Fallback to tkinter
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(initialdir=current_dir, title="請選擇下載儲存資料夾")
        root.destroy()
        if folder and os.path.exists(folder):
            return folder
    except Exception:
        pass

    return None

def create_temp_cookiefile(fb_cookie_str):
    if not fb_cookie_str:
        return None
    import tempfile
    import os
    # Parse cookies
    cookie_dict = {}
    items = fb_cookie_str.split(';')
    for item in items:
        item = item.strip()
        if not item:
            continue
        parts = item.split('=', 1)
        if len(parts) == 2:
            cookie_dict[parts[0].strip()] = parts[1].strip()
            
    if not cookie_dict:
        return None
        
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="fb_cookies_")
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write("# This file is generated automatically by Media Downloader\n")
        for k, v in cookie_dict.items():
            f.write(f".facebook.com\tTRUE\t/\tTRUE\t0\t{k}\t{v}\n")
    return path

def cn_to_an(cn_str):
    if cn_str.isdigit():
        return int(cn_str)
    
    zh_num = {'零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    
    if len(cn_str) == 1:
        return zh_num.get(cn_str, 1)
    
    # 處理「十一」~「十九」
    if cn_str.startswith('十'):
        if len(cn_str) == 2:
            return 10 + zh_num.get(cn_str[1], 0)
        return 10
        
    # 處理「二十」、「三十」...「九十」或「二十一」、「三十五」等
    if '十' in cn_str:
        parts = cn_str.split('十')
        prefix = zh_num.get(parts[0], 1)
        suffix = zh_num.get(parts[1], 0) if parts[1] else 0
        return prefix * 10 + suffix
        
    val = 0
    for char in cn_str:
        val = val * 10 + zh_num.get(char, 0)
    return val if val > 0 else 1

def extract_packer_blocks(html):
    blocks = []
    start_pattern = "eval(function(p,a,c,k,e,d)"
    idx = 0
    while True:
        idx = html.find(start_pattern, idx)
        if idx == -1:
            break
        paren_count = 0
        end_idx = idx
        for i in range(idx + 4, len(html)):
            if html[i] == '(':
                paren_count += 1
            elif html[i] == ')':
                paren_count -= 1
                if paren_count == -1:
                    end_idx = i
                    break
        blocks.append(html[idx:end_idx+1])
        idx = end_idx + 1
    return blocks

def unpack_dean_packer(packed_js):
    pattern = r'\}\s*\(\s*(["\'])((?:(?!\1).|\\.)*)\1\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(["\'])((?:(?!\5).|\\.)*)\5(?:\.split\([\'"]\|[\'"]\))?'
    match = re.search(pattern, packed_js, re.DOTALL)
    if not match:
        return ""
    
    packed_code = match.group(2)
    packed_code = packed_code.replace("\\'", "'").replace('\\"', '"')
    
    a = int(match.group(3))
    c = int(match.group(4))
    
    words_str = match.group(6)
    words = words_str.split('|')
    
    def baseN(num, b):
        if num == 0:
            return "0"
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        res = ""
        while num > 0:
            res = digits[num % b] + res
            num = num // b
        return res

    for i in range(c - 1, -1, -1):
        if i < len(words) and words[i]:
            encoded = baseN(i, a)
            pattern = r'\b' + re.escape(encoded) + r'\b'
            packed_code = re.sub(pattern, words[i], packed_code)
            
    return packed_code

def normalize_input_url(url_str):
    url_str = url_str.strip()
    if not url_str:
        return url_str
    if url_str.startswith("http://") or url_str.startswith("https://"):
        return url_str
    if os.path.exists(url_str):
        return url_str
    # 若包含常見影音平台網域但漏填 https:// (例如 missav.ai/..., youtube.com/...)
    if any(domain in url_str.lower() for domain in [".com", ".ai", ".tv", ".ws", ".net", ".org", ".me", ".co", "youtube", "facebook", "instagram", "tiktok", "twitter", "missav", "movieffm"]):
        return "https://" + url_str.lstrip('/')
    # 若輸入的是 MissAV / 平台番號與代碼 (例如 JD-054791cdbc62ac51e7c79c59f86b72960)
    if re.match(r'^[a-zA-Z0-9\-_]{5,}$', url_str):
        return f"https://missav.ai/{url_str}"
    return "https://" + url_str

def resolve_gimy_stream(player_url, page_url=''):
    if not player_url:
        return ''
    if player_url.startswith('http://') or player_url.startswith('https://'):
        return player_url
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'https://play.gimy.bot/jd/'
    }
    
    # 解析 Gimymax / Gimyplus 的 play.gimy.bot 串流 API
    if player_url.startswith('JD-') or player_url.startswith('JDQM-') or player_url.startswith('JDHG-'):
        api_url = f'https://play.gimy.bot/jd/api.php?url={player_url}'
    elif player_url.startswith('NS4K-') or player_url.startswith('NSYS-'):
        api_url = f'https://play.gimy.bot/ns/api.php?url={player_url}'
    elif player_url.startswith('qsvip-'):
        api_url = f'https://play.gimy.bot/qsvip/api.php?url={player_url}'
    else:
        api_url = f'https://play.gimy.bot/a/api.php?url={player_url}'
        
    try:
        r = requests.get(api_url, headers=headers, timeout=10)
        data = r.json()
        if data.get('code') == 200 and data.get('url'):
            return data.get('url')
    except Exception:
        pass
        
    return urllib.parse.urljoin(page_url, player_url)

def get_media_items(url):
    url = normalize_input_url(url)
    items = []
    
    # 支援 Movieffm.net 電影與戲劇串流解析
    if "movieffm" in url.lower():
        import html as html_lib
        from curl_cffi import requests as curl_requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Referer": "https://www.movieffm.net/",
        }
        res = None
        last_err = None
        for imp in ["chrome124", "chrome120", "safari15_5"]:
            try:
                r = curl_requests.get(url, headers=headers, impersonate=imp, timeout=15)
                if r.status_code == 200:
                    res = r
                    break
                else:
                    last_err = f"HTTP Error {r.status_code}"
            except Exception as e:
                last_err = str(e)

        if not res:
            raise ValueError(f"Movieffm 解析失敗: {last_err}")

        html_content = res.text

        # 1. 提取影片標題
        title = "Movieffm_Video"
        t_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html_content)
        if not t_match:
            t_match = re.search(r'<title>(.*?)</title>', html_content)
        
        if t_match:
            title = html_lib.unescape(t_match.group(1)).strip()
            title = re.sub(r'\s*-\s*Movieffm.*$', '', title, flags=re.IGNORECASE).strip()
        else:
            t_js = re.search(r'title\s*:\s*["\']([^"\']+)["\']', html_content)
            if t_js:
                title = html_lib.unescape(t_js.group(1)).strip()

        title = re.sub(r'[\\/:*?"<>|]', '_', title).strip()
        if not title:
            title = "Movieffm_Video"

        # 2. 提取 m3u8 串流網址 (支援單部電影 1D 陣列與連續劇/動漫 2D 多集陣列)
        items = []
        
        # 策略 A: 解析 videourls JSON 陣列
        match = re.search(r'videourls\s*:\s*(\[\s*(?:\[.*?\]|\{.*?\})\s*\])', html_content, re.DOTALL)
        if match:
            try:
                clean_json = match.group(1).replace(r'\/', '/')
                data = json.loads(clean_json)
                
                # 情況 1: 2D 陣列 (電視劇 / 連續劇，外層是 source 線路，內層是該線路下的各集)
                if len(data) > 0 and isinstance(data[0], list):
                    # 預設提取主要播放源 (第 1 個 source) 的所有集數
                    primary_source = data[0]
                    total_eps = len(primary_source)
                    for idx, ep_item in enumerate(primary_source):
                        ep_url = ep_item.get('url')
                        if not ep_url or not ep_url.startswith('http'):
                            continue
                        
                        # 若只有單一集，直接使用原標題，不附加 - EP01
                        if total_eps == 1:
                            ep_title = title
                        else:
                            raw_name = str(ep_item.get('name', '')).strip()
                            if raw_name:
                                num_match = re.search(r'\d+', raw_name)
                                if num_match:
                                    ep_num = int(num_match.group())
                                    ep_title = f"{title} - EP{ep_num:02d}"
                                else:
                                    ep_title = f"{title} - {raw_name}"
                            else:
                                ep_title = f"{title} - EP{idx+1:02d}"
                            
                        items.append({
                            'url': ep_url,
                            'title': ep_title,
                            'ext': 'mp4',
                            'type': 'video',
                            'headers': {
                                'Referer': url,
                                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                            }
                        })

                # 情況 2: 1D 陣列 (單部電影多來源，取第 1 個有效來源)
                elif len(data) > 0 and isinstance(data[0], dict):
                    for item in data:
                        u = item.get('url')
                        if u and (u.startswith('http://') or u.startswith('https://')) and '.m3u8' in u:
                            items.append({
                                'url': u,
                                'title': title,
                                'ext': 'mp4',
                                'type': 'video',
                                'headers': {
                                    'Referer': url,
                                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                                }
                            })
                            break
            except Exception as e:
                print(f"Error parsing Movieffm videourls: {e}")

        # 策略 B: 正規表達式全局搜尋 m3u8 作為備援
        if not items:
            m3u8_matches = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html_content)
            if m3u8_matches:
                items.append({
                    'url': m3u8_matches[0].replace(r'\/', '/'),
                    'title': title,
                    'ext': 'mp4',
                    'type': 'video',
                    'headers': {
                        'Referer': url,
                        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                    }
                })

        if not items:
            raise ValueError("無法從 Movieffm 頁面中解析出有效的影片串流網址 (m3u8)。")

        return items

    if "missav" in url.lower():
        import html as html_lib
        from curl_cffi import requests as curl_requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        impersonates = ["chrome124", "chrome120", "chrome110", "safari15_5"]
        response = None
        last_err = None

        for imp in impersonates:
            try:
                res = curl_requests.get(url, headers=headers, impersonate=imp, timeout=15)
                if res.status_code == 200:
                    response = res
                    break
                else:
                    last_err = f"HTTP Error {res.status_code}"
            except Exception as e:
                last_err = str(e)

        if not response:
            raise ValueError(f"MissAV 解析失敗: HTTP 請求失敗 ({last_err})")

        try:
            # 1. 提取標題
            title = "MissAV_Video"
            og_title_match = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']', response.text)
            if og_title_match:
                title = og_title_match.group(1)
            else:
                og_title_match = re.search(r'<meta\s+content=["\'](.*?)["\']\s+property=["\']og:title["\']', response.text)
                if og_title_match:
                    title = og_title_match.group(1)
                else:
                    title_match = re.search(r'<title>(.*?)</title>', response.text)
                    if title_match:
                        title = title_match.group(1)

            title = html_lib.unescape(title).strip().rstrip('-').strip()
            title = re.sub(r'[\\/:*?"<>|]', '_', title)

            # 2. 尋找與解密 Dean Edwards Packer 區塊 (掃描所有區塊)
            blocks = extract_packer_blocks(response.text)
            m3u8_url = None

            for block in blocks:
                unpacked = unpack_dean_packer(block)
                source_1080p = re.search(r"source1280\s*=\s*['\"](https?://[^'\"]+?)['\"]", unpacked)
                source_720p = re.search(r"source842\s*=\s*['\"](https?://[^'\"]+?)['\"]", unpacked)
                source_playlist = re.search(r"source\s*=\s*['\"](https?://[^'\"]+?)['\"]", unpacked)
                source_generic = re.search(r"source\w*\s*=\s*['\"](https?://[^'\"]+?\.m3u8[^\'\"]*)['\"]", unpacked)
                m3u8_direct = re.search(r"['\"](https?://[^'\"]+?\.m3u8[^\'\"]*)['\"]", unpacked)

                if source_1080p:
                    m3u8_url = source_1080p.group(1)
                    break
                elif source_720p:
                    m3u8_url = source_720p.group(1)
                    break
                elif source_playlist:
                    m3u8_url = source_playlist.group(1)
                    break
                elif source_generic:
                    m3u8_url = source_generic.group(1)
                    break
                elif m3u8_direct:
                    m3u8_url = m3u8_direct.group(1)
                    break

            if not m3u8_url:
                raise ValueError("無法從頁面中解析出影片串流網址 (m3u8)。")

            items.append({
                'url': m3u8_url,
                'title': title,
                'ext': 'mp4',
                'type': 'video',
                'headers': {
                    'Referer': url,
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                }
            })
            return items

        except Exception as e:
            raise ValueError(f"MissAV 解析失敗: {e}")
    
    # 阻擋並辨識 Facebook 社團首頁，引導使用者使用貼文連結
    if "facebook.com/groups/" in url or "fb.com/groups/" in url:
        clean_url = url.split('?')[0].rstrip('/')
        parts = clean_url.split('/groups/')
        if len(parts) == 2 and '/' not in parts[1]:
            raise ValueError("此網址為「社團首頁」，並非單一貼文或影片。請在社團中找到貼文，點選其「發佈時間」獲取正確的貼文網址（例如含有 /permalink/ 或 /share/p/）再進行下載。")
            
    # Facebook 貼文特殊圖片下載邏輯
    if any(domain in url for domain in ["facebook.com", "fb.com", "fb.watch"]):
        try:
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
                
            # 1. 用 HEAD 請求獲取 302 重定向 Location，避開直接 GET 產生的 400 錯誤
            try:
                res = session.head(url, headers=headers, allow_redirects=False, timeout=10)
                final_url = res.headers.get('Location', url)
            except Exception:
                try:
                    res = session.get(url, headers=headers, allow_redirects=True, timeout=15)
                    final_url = res.url
                except Exception:
                    final_url = url
            
            # 轉換為基礎行動版網頁 (mbasic.facebook.com)，避開 React/JS 動態渲染，直接取得靜態 HTML 內容
            mobile_url = final_url.replace("www.facebook.com", "mbasic.facebook.com").replace("m.facebook.com", "mbasic.facebook.com")
            
            # 2. 使用行動版 Header 抓取內容，繞過登入牆
            mobile_headers = headers.copy()
            mobile_headers['User-Agent'] = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
            
            m_res = session.get(mobile_url, headers=mobile_headers, allow_redirects=True, timeout=15)
            html_content = m_res.text
            
            # 3. 提取貼文標題/群組名稱 (用於檔名)
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
            
            # 4. 嘗試提取相簿/貼文圖片合集 (Set ID) 以獲取完整的所有圖片 (例如 25 張)
            unique_photos = []
            set_match = re.search(r'set=(pcb\.\d+|a\.\d+)', html_content)
            if set_match:
                set_param = set_match.group(1)
                st.info(f"📸 偵測到多張相片合集 ({set_param})，正在透過基礎行動版擷取完整的相片清單...")
                
                # 使用 mbasic 讀取相集，因為結構極度單純且穩定
                set_url = f"https://mbasic.facebook.com/media/set/?set={set_param}"
                try:
                    set_res = session.get(set_url, headers=mobile_headers, timeout=15)
                    if set_res.status_code == 200:
                        set_html = set_res.text
                        # 擷取所有 /photo.php 連結
                        photo_links = re.findall(r'href=\"(/photo\.php\?[^\"]+)\"', set_html)
                        if not photo_links:
                            photo_links = re.findall(r"href=\'(/photo\.php\?[^\'\s]+)\'", set_html)
                            
                        if photo_links:
                            st.info(f"🔗 成功找到 {len(photo_links)} 張相片的連結，開始平行解析高清原始圖...")

                            # ── 平行抓取每張相片頁（提速 5–8x）────────────────
                            def _fetch_one_photo(p_link):
                                p_url = "https://mbasic.facebook.com" + html_lib.unescape(p_link)
                                try:
                                    p_res = session.get(p_url, headers=mobile_headers, timeout=10)
                                    if p_res.status_code == 200:
                                        p_html = p_res.text
                                        img_match = re.search(r'<img[^>]+src=\"([^\"]*scontent[^\"]*)\"', p_html)
                                        if not img_match:
                                            img_match = re.search(r'src=\"([^\"]*scontent[^\"]*)\"', p_html)
                                        if img_match:
                                            raw_img_url = html_lib.unescape(img_match.group(1))
                                            raw_img_url = urllib.parse.unquote(raw_img_url)
                                            if not any(size in raw_img_url for size in ['p144x144', 'p48x48', 'p75x75']):
                                                return raw_img_url
                                except Exception as e:
                                    print(f"Error scraping single photo page: {e}")
                                return None

                            with ThreadPoolExecutor(max_workers=8) as _photo_exec:
                                results = list(_photo_exec.map(_fetch_one_photo, photo_links))
                            unique_photos.extend([r for r in results if r])
                except Exception as e:
                    print(f"Failed to fetch set photos: {e}")
            
            # Fallback 5：如果沒有 Set ID 或 Set 擷取失敗，使用頁面中可見的圖片
            if not unique_photos:
                # 1. 匹配 img src 屬性 (支援雙引號與單引號)
                img_srcs = re.findall(r'<img[^>]+src=[\"\']([^\'\"]+)[\"\']', html_content)
                photo_urls = []
                for src in img_srcs:
                    src = html_lib.unescape(src)
                    src = urllib.parse.unquote(src)
                    if 'scontent' in src:
                        if any(size in src for size in ['p144x144', 'p48x48', 'p75x75']):
                            continue
                        photo_urls.append(src)
                
                # 2. 如果沒有匹配到 img 標籤，全局搜尋 HTML 中的 scontent 連結 (極致防禦)
                if not photo_urls:
                    raw_urls = re.findall(r'https?://[a-zA-Z0-9_\.\-\\/]+fbcdn[a-zA-Z0-9_\.\-\/\?\&=\+;%\\:]+', html_content)
                    for r_url in raw_urls:
                        r_url = r_url.replace('\\/', '/').replace('\\\\/', '/')
                        r_url = html_lib.unescape(urllib.parse.unquote(r_url))
                        if 'scontent' in r_url:
                            if any(size in r_url for size in ['p144x144', 'p48x48', 'p75x75']):
                                continue
                            photo_urls.append(r_url)
                
                seen_ids = set()
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
            else:
                # 如果擺明是相片貼文，且我們沒抓到任何照片，就直接拋出錯誤，阻止降級到 yt-dlp
                if any(p_pattern in url for p_pattern in ["/share/p/", "/posts/", "/permalink/", "/photos/", "/photo.php", "/photo/"]):
                    raise ValueError("此貼文為「Facebook 相片或非影片貼文」，必須提供有效的 Facebook Cookie 授權才能進行下載。\n\n💡 **下載相片建議**：請確認您已在下方填入有效的 **Facebook Cookie**。私密社團、好友限閱或部分公開貼文的相片必須有 Cookie 授權才能順利下載。")
        except Exception as e:
            # 錯誤時紀錄日誌，並降級使用原有的 yt-dlp 解析
            print(f"Facebook custom photo scrape failed: {e}, falling back to yt-dlp...")

    # 支援各大平台 (Movieffm, MissAV, Gimy, YouTube, X, Facebook, Instagram, TikTok 等與通用線上網址)
    is_custom_hls = any(k in url.lower() for k in ["missav", "movieffm", "gimymax", "gimyplus", "gimy"])
    if not is_custom_hls or any(domain in url for domain in ["x.com", "twitter.com", "t.co", "youtube.com", "youtu.be", "facebook.com", "fb.com", "fb.watch", "instagram.com", "ig.me", "tiktok.com"]):
        # yt-dlp 的 threads extractor 綁定 threads.net，若是 .com 則先替換
        url = url.replace("threads.com", "threads.net")
        
        import yt_dlp
        
        fb_cookie_str = st.session_state.get('fb_cookie')
        temp_cookie_path = None
        if "facebook.com" in url or "fb.com" in url or "fb.watch" in url:
            temp_cookie_path = create_temp_cookiefile(fb_cookie_str)
            
        ydl_opts = {
            'quiet': True,
            'extract_flat': False,
            'nocheckcertificate': True,
            'legacy_server_connect': True,
            'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'web', 'ios'],
                }
            },
        }
        if temp_cookie_path:
            ydl_opts['cookiefile'] = temp_cookie_path
            
        try:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    
                    # 處理可能的多個 entry (例如 Instagram Carousel 或 YouTube 播放清單)
                    entries = info.get('entries', [info])
                    
                    for i, entry in enumerate(entries):
                        if not entry:
                            continue
                        title = entry.get('title') or info.get('title') or f"media_{i}"
                        title = re.sub(r'[\\/:*?"<>|]', '_', title)
                        
                        ext = entry.get('ext')
                        raw_url = entry.get('url')
                        webpage_url = entry.get('webpage_url') or info.get('webpage_url') or url
                        
                        # 嚴格驗證媒體網址：必須為 HTTP/HTTPS 協議，防止將 ID 或相對路徑傳給 FFmpeg
                        if raw_url and (raw_url.startswith('http://') or raw_url.startswith('https://')):
                            media_url = raw_url
                        elif webpage_url and (webpage_url.startswith('http://') or webpage_url.startswith('https://')):
                            media_url = webpage_url
                        elif url.startswith('http://') or url.startswith('https://'):
                            media_url = url
                        else:
                            continue
                        
                        # 判斷是否為圖片 (有些平台會回傳 thumbnail 作為 entry)
                        is_image = ext in ['jpg', 'jpeg', 'png', 'webp'] or (isinstance(media_url, str) and '.jpg' in media_url)
                        
                        # 取得 http_headers (包含 User-Agent 等) 以避免 FFmpeg 連線 403 Forbidden
                        http_headers = entry.get('http_headers') or info.get('http_headers') or {}

                        items.append({
                            'url': media_url,
                            'title': title if len(entries) == 1 else f"{title}_{i+1}",
                            'ext': ext or ('jpg' if is_image else 'mp4'),
                            'type': 'image' if is_image else 'video',
                            'headers': http_headers,
                            'webpage_url': webpage_url
                        })
            except Exception as e:
                err_msg = str(e)
                if any(kw in err_msg for kw in ["No video formats found", "Unsupported URL", "Cannot parse data", "Private video", "login"]):
                    if "facebook.com" in url or "fb.com" in url or "fb.watch" in url:
                        raise ValueError("此 Facebook 連結可能為「純相片貼文」、「非影片內容」或「私密/限制級內容」。\n\n💡 **下載建議**：請確認您已在下方填入有效的 **Facebook Cookie**。私密社團、好友限閱、相片貼文或部分特定影片必須有 Cookie 授權才能進行下載。")
                if is_missav_or_gimy:
                    pass
                else:
                    raise e
        finally:
            try:
                if temp_cookie_path and os.path.exists(temp_cookie_path):
                    os.remove(temp_cookie_path)
            except Exception:
                pass
        if items:
            return items
            
    # 原有的 Gimymax 網頁解析邏輯
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/122.0.0.0"
    }
    response = requests.get(url, headers=headers)
    # 找尋 player_data 變數中的 JSON 資料
    match = re.search(r'var player_data=(.*?)</script>', response.text)
    if not match:
        raise ValueError("Cannot find player_data in the webpage.")
        
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        raise ValueError("Failed to parse player_data JSON.")
        
    raw_m3u8 = data.get("url")
    m3u8_url = resolve_gimy_stream(raw_m3u8, page_url=url)
    title = data.get("vod_data", {}).get("vod_name", "downloaded_media")
    
    # 將「第X季」替換為 S1, S2...
    match_s = re.search(r'第([一二三四五六七八九十\d]+)季', title)
    if match_s:
        num_str = match_s.group(1)
        s_num = cn_to_an(num_str)
        title = re.sub(r'第[一二三四五六七八九十\d]+季', f'S{s_num}', title)

    # 嘗試抓取集數資訊，將「第X集」替換為 E01, E02...
    ep_str = None
    
    # 策略 1: 舊有 Gimymax 邏輯 data-playname
    match_ep = re.search(r'data-playname="([^"]+)"', response.text)
    if match_ep:
        ep_raw = match_ep.group(1).strip()
        match_e = re.search(r'第(\d+)集', ep_raw)
        if match_e:
            ep_str = f"E{int(match_e.group(1)):02d}"
        else:
            ep_str = ep_raw
            
    # 策略 2: 針對 Gimymax / Gimyplus 或者是其他變體，從 URL filename 反查 HTML 中對應的 a 標籤
    if not ep_str:
        url_filename = url.split('/')[-1].split('?')[0]
        if url_filename:
            pattern = r'href=["\'][^\'\"]*' + re.escape(url_filename) + r'[^>]*>\s*(.*?)\s*</a>'
            matches = re.findall(pattern, response.text)
            if matches:
                ep_raw = None
                for m in matches:
                    m_clean = m.strip()
                    if "第" in m_clean or "集" in m_clean:
                        ep_raw = m_clean
                        break
                if not ep_raw:
                    for m in matches:
                        m_clean = m.strip()
                        if re.search(r'\d+', m_clean):
                            ep_raw = m_clean
                            break
                if not ep_raw:
                    ep_raw = matches[-1].strip()
                
                if ep_raw:
                    match_e = re.search(r'第(\d+)集', ep_raw)
                    if match_e:
                        ep_str = f"E{int(match_e.group(1)):02d}"
                    else:
                        ep_str = ep_raw

    # 策略 3: 如果還是沒拿到，但 URL 結尾有集數特徵 (例如 232804-3-10.html -> "10"，或是 ep_1.html -> "1")
    if not ep_str:
        url_filename = url.split('/')[-1].split('?')[0]
        url_id = url_filename.split('.')[0]
        match_num = re.search(r'[-_](\d+)$', url_id)
        if match_num:
            ep_str = f"E{int(match_num.group(1)):02d}"
            
    if ep_str:
        title = f"{title}_{ep_str}"
    
    # 處理檔名特殊字元
    title = re.sub(r'[\\/:*?"<>|]', '_', title)
    
    items = [{
        'url': m3u8_url,
        'title': title,
        'ext': 'mp4',
        'type': 'video',
        'headers': {
            'Referer': url,
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        }
    }]
    return items

def format_time(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def show_error_log_box(error_msg, log_text=None, title="詳細錯誤日誌", url=None):
    st.error(error_msg)
    if log_text or url:
        full_log = ""
        if url:
            full_log += f"🔗 相關網址 (Target URL):\n{url}\n\n"
        if log_text:
            full_log += str(log_text).strip()
            
        with st.expander(f"📋 {title} (點擊開啟一鍵複製)", expanded=True):
            st.caption("💡 點擊下方框框右上角的 **「📋 複製 (Copy)」** 按鈕，即可快速複製完整日誌（包含目標網址）提供給 AI：")
            st.code(full_log.strip(), language="log")

def get_media_duration(media_url, headers=None):
    if "m3u8" in media_url:
        try:
            req_headers = {}
            if headers:
                req_headers.update(headers)
            res = requests.get(media_url, headers=req_headers, timeout=4)
            if res.status_code == 200:
                extinfs = re.findall(r'#EXTINF:([\d\.]+)', res.text)
                if extinfs:
                    return sum(float(x) for x in extinfs)
        except Exception:
            pass
    
    # 支援本地影片檔案與非 m3u8 串流，使用 ffprobe 獲取精準媒體總時長
    try:
        headers_arg = []
        if headers:
            headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            headers_arg = ["-headers", headers_str]
            if "m3u8" in media_url:
                headers_arg += ["-allowed_segment_extensions", "ALL", "-extension_picky", "0"]
        
        probe_cmd = ["ffprobe", "-v", "error"] + headers_arg + [
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            media_url
        ]
        out = subprocess.check_output(probe_cmd, text=True, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).strip()
        dur = float(out)
        if dur > 0:
            return dur
    except Exception:
        pass
    return 0.0

def run_ffmpeg_with_progress(ffmpeg_cmd, total_duration=0.0, label="下載"):
    progress_bar = st.progress(0.0)
    status_text = st.empty()

    cmd = [ffmpeg_cmd[0], "-y", "-progress", "pipe:2"] + ffmpeg_cmd[2:]
    
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE
    )

    current_sec = 0.0
    speed = "1.0x"
    last_update_time = 0.0
    stderr_lines = []

    for line in iter(process.stderr.readline, b''):
        line_str = line.decode('utf-8', errors='ignore').strip()
        if not line_str:
            continue
        stderr_lines.append(line_str)
        if len(stderr_lines) > 50:
            stderr_lines.pop(0)

        if line_str.startswith("out_time_us="):
            try:
                us = int(line_str.split("=")[1])
                current_sec = us / 1000000.0
            except ValueError:
                pass
        elif line_str.startswith("speed="):
            speed = line_str.split("=")[1].strip()

        now = time.time()
        if now - last_update_time >= 0.5:
            last_update_time = now
            if total_duration > 0:
                pct = min(1.0, max(0.0, current_sec / total_duration))
                pct_num = pct * 100
                progress_bar.progress(pct)
                status_text.markdown(f"⏳ **{label}進度**: `{pct_num:.1f}%` ({format_time(current_sec)} / {format_time(total_duration)}) | 速度: `{speed}`")
            else:
                status_text.markdown(f"⏳ **{label}處理中...** 已完成 `{format_time(current_sec)}` | 速度: `{speed}`")

    process.wait()

    if total_duration > 0:
        progress_bar.progress(1.0)
    status_text.empty()
    progress_bar.empty()

    stderr_log = "\n".join(stderr_lines)
    return process.returncode, stderr_log

def download_fast_parallel_hls(m3u8_url, out_path=None, extra_headers=None, max_workers=None, label="影片"):
    """平行 HLS 下載引擎（全量 RAM 模式：24G RAM，最大化速度，零磁碟 I/O）。"""
    from curl_cffi import requests as curl_requests

    req_headers = dict(_GLOBAL_HEADERS)
    if extra_headers:
        req_headers.update(extra_headers)
    if 'Referer' not in req_headers:
        req_headers['Referer'] = f"https://{urllib.parse.urlparse(m3u8_url).netloc}/"

    # 複用全域 Session，不重建
    session = _global_session

    # 每個 worker 維持專屬長連線（讓 CDN 連線「暖身」提速）
    thread_local = threading.local()

    def get_curl_session():
        if not hasattr(thread_local, "curl_session"):
            thread_local.curl_session = curl_requests.Session(impersonate="chrome124")
        return thread_local.curl_session

    def fetch_text(url):
        try:
            r = session.get(url, headers=req_headers, timeout=30)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        try:
            r = get_curl_session().get(url, headers=req_headers, timeout=30)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        raise ValueError(f"無法讀取 m3u8 串流選單 ({url})")

    text = fetch_text(m3u8_url)

    # ── Master Playlist：優先用 RESOLUTION 高度排序，其次 BANDWIDTH ──────────
    if "#EXT-X-STREAM-INF" in text:
        sub_playlists = []
        current_inf = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-STREAM-INF"):
                bw_match = re.search(r"BANDWIDTH=(\d+)", line)
                res_match = re.search(r"RESOLUTION=\d+x(\d+)", line)
                current_inf['bw'] = int(bw_match.group(1)) if bw_match else 0
                current_inf['height'] = int(res_match.group(1)) if res_match else 0
            elif line and not line.startswith("#"):
                sub_playlists.append((
                    current_inf.get('height', 0),
                    current_inf.get('bw', 0),
                    urllib.parse.urljoin(m3u8_url, line)
                ))
                current_inf = {}
        if sub_playlists:
            # 優先 RESOLUTION 高度，相同高度再比 BANDWIDTH
            sub_playlists.sort(key=lambda x: (x[0], x[1]), reverse=True)
            m3u8_url = sub_playlists[0][2]
            text = fetch_text(m3u8_url)

    segment_urls = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            segment_urls.append(urllib.parse.urljoin(m3u8_url, line))

    total_segments = len(segment_urls)
    if total_segments == 0:
        raise ValueError("m3u8 播放列表中找不到任何影片切片 (segments)。")

    extinfs = [float(x) for x in re.findall(r'#EXTINF:([\d\.]+)', text)]
    if len(extinfs) == total_segments:
        segment_durations = extinfs
    else:
        total_dur = sum(extinfs) if extinfs else 0.0
        avg_dur = total_dur / total_segments if (total_dur > 0 and total_segments > 0) else 1.0
        segment_durations = [avg_dur] * total_segments

    total_duration = sum(segment_durations)

    # ── 動態計算最佳 worker 數：min(32, segments, cpu*4) ─────────────────────
    cpu_count = os.cpu_count() or 4
    if max_workers is None:
        max_workers = min(32, total_segments, cpu_count * 4)
    else:
        max_workers = min(max_workers, total_segments)

    progress_bar = st.progress(0.0)
    status_text = st.empty()

    # ── 全量 RAM 模式：切片全部存入記憶體，封裝時一次性寫入，零磁碟 I/O ──────
    # 24G RAM 環境下，2h 影片約佔 2–4 GB，完全在記憶體範圍內
    segments_data = [b""] * total_segments

    def download_segment(args):
        idx, seg_url = args
        for attempt in range(3):
            try:
                r = session.get(seg_url, headers=req_headers, timeout=15)
                if r.status_code == 200 and len(r.content) > 0:
                    return idx, r.content
            except Exception:
                pass
            try:
                r = get_curl_session().get(seg_url, headers=req_headers, timeout=15)
                if r.status_code == 200 and len(r.content) > 0:
                    return idx, r.content
            except Exception:
                pass
            # exponential backoff：第 1 次失敗等 0.5s，第 2 次等 1s
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
        return idx, b""

    t0 = time.time()
    last_update_time = 0.0
    completed = 0
    completed_duration = 0.0
    total_downloaded_bytes = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_segment, (i, url)): i
                   for i, url in enumerate(segment_urls)}
        for f in as_completed(futures):
            idx, content = f.result()
            segments_data[idx] = content
            completed += 1
            completed_duration += segment_durations[idx]
            total_downloaded_bytes += len(content)

            now = time.time()
            if now - last_update_time >= 0.25 or completed == total_segments:
                last_update_time = now
                elapsed = max(0.1, now - t0)
                speed_x = completed_duration / elapsed
                mb_downloaded = total_downloaded_bytes / 1024 / 1024
                speed_mb = mb_downloaded / elapsed
                if total_duration > 0:
                    pct = min(1.0, max(0.0, completed_duration / total_duration))
                    status_text.markdown(
                        f"⏳ **{label}進度**: `{pct * 100:.1f}%` "
                        f"({format_time(completed_duration)} / {format_time(total_duration)}) "
                        f"| 速度: `{speed_x:.2f}x` (`{speed_mb:.2f} MB/s`) "
                        f"| 線程: `{max_workers}`"
                    )
                else:
                    pct = completed / total_segments
                    status_text.markdown(
                        f"⏳ **{label}處理中...** 已完成 `{completed}/{total_segments}` "
                        f"(`{mb_downloaded:.1f} MB`) "
                        f"| 速度: `{speed_x:.2f}x` (`{speed_mb:.2f} MB/s`)"
                    )
                progress_bar.progress(pct)

    status_text.markdown("⚡ **多線程切片下載完成，正在無損封裝為 MP4...**")

    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tmp_ts:
        tmp_ts_path = tmp_ts.name
        for chunk in segments_data:
            if chunk:
                tmp_ts.write(chunk)

    del segments_data
    gc.collect()

    try:
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", tmp_ts_path,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            out_path
        ]
        proc = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise ValueError(f"FFmpeg 無損封裝失敗: {proc.stderr.decode('utf-8', errors='ignore')}")

        proc.stdout = None
        proc.stderr = None
        del proc
        gc.collect()

    finally:
        try:
            if os.path.exists(tmp_ts_path):
                os.remove(tmp_ts_path)
        except Exception:
            pass

    progress_bar.empty()
    status_text.empty()

def finish_output_file(local_temp_or_final_path, filename):
    """
    完成檔案處理並依據儲存模式分發：
    - 若選擇 '☁️ Google Drive 雲端直送 (Cloud API)'：
      直接透過 Google Drive REST API 將串流上傳至 Google Drive 雲端的 'Download' 資料夾，
      上傳完成後立刻刪除本機暫存檔並回收記憶體，確保本機硬碟 100% 零殘留！
    - 若選擇 '📁 本地硬碟資料夾'：
      檔案保留於使用者指定的本機資料夾中。
    """
    is_gdrive_mode = st.session_state.get('storage_destination') == "gdrive_cloud"
    if is_gdrive_mode:
        try:
            if not gdrive_service.is_authenticated():
                st.error("❌ 尚未完成 Google Drive 授權！請先至上方儲存設定區點擊「授權連接 Google Drive」完成登入。")
                return False

            prog_bar = st.progress(0.0)
            status_text = st.empty()

            def _upload_cb(pct, cur_bytes, tot_bytes, speed_mb):
                pct_val = min(1.0, max(0.0, pct))
                prog_bar.progress(pct_val)
                cur_mb = cur_bytes / 1024 / 1024
                tot_mb = tot_bytes / 1024 / 1024
                status_text.markdown(f"☁️ **Google Drive 雲端直送中**: `{pct_val*100:.1f}%` ({cur_mb:.1f} MB / {tot_mb:.1f} MB) | 上傳速度: `{speed_mb:.2f} MB/s`")

            with st.spinner(f"☁️ 正在直送至 Google Drive 雲端 /Download/{filename}..."):
                res = gdrive_service.upload_file_directly_to_gdrive(
                    local_temp_or_final_path,
                    original_filename=filename,
                    progress_callback=_upload_cb
                )

            prog_bar.empty()
            status_text.empty()

            link = res.get('webViewLink') if res else None
            link_md = f" [🔗 點此在 Google Drive 中查看]({link})" if link else ""
            st.success(f"🎉 **雲端直送成功！** 檔案已直接存放於 Google Drive 雲端 `/Download/{filename}`{link_md}（本機無殘留任何檔案）")
            return True
        except Exception as e:
            show_error_log_box(f"❌ Google Drive 雲端直送失敗: {e}", traceback.format_exc(), title="Google Drive API 上傳錯誤")
            return False
        finally:
            if os.path.exists(local_temp_or_final_path):
                try:
                    os.remove(local_temp_or_final_path)
                except Exception:
                    pass
            gc.collect()
    else:
        st.success(f"✅ 下載完成！檔案已儲存至本機: `{local_temp_or_final_path}`")
        gc.collect()
        return True

def download_media(media_item, force_audio=False):
    title = media_item['title']
    media_url = media_item['url']
    media_type = media_item['type']
    
    st.info(f"📍 正在處理媒體: **{title}** ({media_type})")
    
    if force_audio:
        ext = "mp3"
    else:
        ext = media_item['ext']
    
    filename = f"{title}.{ext}"
    is_gdrive = st.session_state.get('storage_destination') == "gdrive_cloud"

    if is_gdrive:
        temp_dir = tempfile.gettempdir()
        out_path = os.path.join(temp_dir, f"gdrive_tmp_{int(time.time()*1000)}_{filename}")
        st.info(f"☁️ 檔案將在 RAM/暫存封裝後，**直接直送至 Google Drive 雲端 `/Download/{filename}`**（不保留於本機）")
    else:
        downloads_dir = st.session_state.get('download_dir', load_download_dir())
        os.makedirs(downloads_dir, exist_ok=True)
        out_path = os.path.join(downloads_dir, filename)
        
        if os.path.exists(out_path):
            st.success(f"⏭️ 檔案已存在，自動跳過: `{filename}`")
            return
        st.info(f"📍 檔案將以 {ext.upper()} 格式儲存至本機: `{out_path}`")
    
    try:
        if media_type == 'image':
            with st.spinner(f"⏳ 正在下載圖片 (RAM 處理中)..."):
                response = requests.get(media_url, timeout=30)
                response.raise_for_status()
                with open(out_path, "wb") as f:
                    f.write(response.content)
            finish_output_file(out_path, filename)
            return

        extra_headers = media_item.get('headers', {})

        if "m3u8" in media_url and not force_audio:
            try:
                download_fast_parallel_hls(media_url, out_path=out_path, extra_headers=extra_headers, max_workers=16, label="影片")
                return
            except Exception as hls_err:
                st.warning(f"⚠️ 多線程下載失敗 ({hls_err})，降級使用標準 FFmpeg 串流處理...")

        webpage_url = media_item.get('webpage_url', '')
        is_youtube = any(k in media_url or k in webpage_url for k in ["youtube.com", "youtu.be", "googlevideo.com"])

        if is_youtube and not force_audio:
            target_url = webpage_url or media_url
            try:
                out_base = os.path.splitext(out_path)[0]
                ydl_opts = {
                    'outtmpl': f"{out_base}.%(ext)s",
                    'quiet': True,
                    'overwrites': True,
                    'nocheckcertificate': True,
                    'legacy_server_connect': True,
                    'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
                    'merge_output_format': 'mp4',
                    'extractor_args': {
                        'youtube': {
                            'player_client': ['android', 'web', 'ios'],
                        }
                    },
                }
                with st.spinner("⏳ 正在下載 YouTube 1080p 高清影片..."):
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([target_url])

                actual_out = None
                if os.path.exists(out_path):
                    actual_out = out_path
                else:
                    for e in ["mp4", "mkv", "webm"]:
                        candidate = f"{out_base}.{e}"
                        if os.path.exists(candidate):
                            actual_out = candidate
                            break

                if actual_out:
                    finish_output_file(actual_out, filename)
                    return
                else:
                    show_error_log_box("❌ 下載失敗！無法產生影片檔案。", f"Target Output Path: {out_path}", title="YouTube 下載失敗", url=target_url)
                    return
            except Exception as yt_err:
                show_error_log_box(f"❌ YouTube 下載失敗: {yt_err}", traceback.format_exc(), title="YouTube 下載詳細錯誤日誌", url=target_url)
                return

        is_valid_url = isinstance(media_url, str) and (media_url.startswith("http://") or media_url.startswith("https://"))
        is_valid_file = isinstance(media_url, str) and os.path.exists(media_url)

        if not is_valid_url and not is_valid_file:
            show_error_log_box(
                "❌ 無法開始下載！媒體網址或檔案路徑無效。",
                f"錯誤傳入的 Input: '{media_url}'\n說明: 該輸入非有效的 HTTP/HTTPS 網址，且本機找不到此檔案。請確認輸入的網址格式（需包含 http:// 或 https://）或本機檔案是否存在。",
                url=media_url
            )
            return

        headers_arg = []
        if extra_headers:
            headers_str = "".join(f"{k}: {v}\r\n" for k, v in extra_headers.items())
            headers_arg = ["-headers", headers_str]
        
        if "m3u8" in media_url:
            headers_arg += ["-allowed_segment_extensions", "ALL", "-extension_picky", "0"]

        if force_audio:
            ffmpeg_cmd = [
                "ffmpeg", "-y"
            ] + headers_arg + [
                "-i", media_url,
                "-vn",
                "-c:a", "libmp3lame",
                "-b:a", "192k",
                out_path
            ]
            label_text = "音訊轉碼"
        else:
            reconnect_args = [
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-multiple_requests", "1",
            ]
            if "m3u8" in media_url:
                reconnect_args += ["-http_persistent", "1"]

            ffmpeg_cmd = [
                "ffmpeg", "-y"
            ] + reconnect_args + headers_arg + [
                "-i", media_url,
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                "-movflags", "+faststart",
                out_path
            ]
            label_text = "影片下載"
        
        total_dur = get_media_duration(media_url, headers=extra_headers)

        returncode, stderr_log = run_ffmpeg_with_progress(
            ffmpeg_cmd, total_duration=total_dur, label=label_text
        )

        if returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            finish_output_file(out_path, filename)
        else:
            show_error_log_box("❌ FFmpeg 下載失敗！", stderr_log, url=media_url)
        
        gc.collect()
    except Exception as e:
        show_error_log_box(f"❌ 發生例外錯誤: {e}", traceback.format_exc(), title="Exception 堆疊追蹤資訊", url=media_url)
        st.caption("Note: 如果看到找不到指令的錯誤，請確認系統已安裝 FFmpeg (`brew install ffmpeg`)。")

def extract_local_audio(video_path, audio_format, title=None, headers=None):
    if title:
        base_name = title
    else:
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        
    is_gdrive = st.session_state.get('storage_destination') == "gdrive_cloud"
    if is_gdrive:
        out_dir = tempfile.gettempdir()
    else:
        out_dir = st.session_state.get('download_dir', load_download_dir())
        os.makedirs(out_dir, exist_ok=True)
    
    is_valid_url = isinstance(video_path, str) and (video_path.startswith("http://") or video_path.startswith("https://"))
    is_valid_file = isinstance(video_path, str) and os.path.exists(video_path)

    if not is_valid_url and not is_valid_file:
        show_error_log_box(
            f"❌ 無法提取音訊！檔案或網址格式無效",
            f"傳入的路徑或網址: '{video_path}'\n說明: 該輸入非有效的 HTTP/HTTPS 網址，且本機找不到此檔案。請確認網址是否包含 http:// 或 https://，或確認本地檔案路徑正確。",
            url=video_path
        )
        return

    is_youtube_or_online = is_valid_url and ("m3u8" not in video_path.lower())
    if is_youtube_or_online:
        target_ext = "mp3" if audio_format == "MP3" else ("m4a" if audio_format == "M4A" else "mp4")
        filename = f"{base_name}.{target_ext}"
        expected_out_path = os.path.join(out_dir, filename)
        if not is_gdrive and os.path.exists(expected_out_path):
            st.success(f"⏭️ 檔案已存在: `{expected_out_path}`")
            return

        try:
            import yt_dlp
            out_base = os.path.splitext(expected_out_path)[0]
            postprocessors = []
            if audio_format == "MP3":
                postprocessors.append({
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                })
            elif audio_format == "M4A":
                postprocessors.append({
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'm4a',
                    'preferredquality': '192',
                })
            else:
                # 預設或 MP4: 提取最佳音訊後以 MP4 容器封裝 (相容 AI 轉錄)
                postprocessors.append({
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'm4a',
                    'preferredquality': '192',
                })

            ydl_opts = {
                'outtmpl': f"{out_base}.%(ext)s",
                'quiet': True,
                'overwrites': True,
                'nocheckcertificate': True,
                'legacy_server_connect': True,
                'format': 'bestaudio/best',
                'postprocessors': postprocessors,
                'extractor_args': {
                    'youtube': {
                        'player_client': ['android', 'web', 'ios'],
                    }
                },
            }
            with st.spinner("⏳ 正在透過 yt-dlp 從線上網址提取高品質音訊..."):
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([video_path])
            
            matched_file = None
            if os.path.exists(expected_out_path):
                matched_file = expected_out_path
            else:
                import glob
                candidates = glob.glob(f"{out_base}.*") + glob.glob(os.path.join(out_dir, f"{base_name[:30]}*"))
                if candidates:
                    matched_file = candidates[0]

            if matched_file:
                actual_ext = os.path.splitext(matched_file)[1].lower().lstrip('.')
                if target_ext == "mp4" and actual_ext in ["m4a", "aac"]:
                    final_mp4_path = os.path.join(out_dir, f"{base_name}.mp4")
                    if matched_file != final_mp4_path:
                        try:
                            subprocess.run(["ffmpeg", "-y", "-i", matched_file, "-vn", "-c:a", "copy", "-bsf:a", "aac_adtstoasc", final_mp4_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                            if os.path.exists(matched_file) and matched_file != final_mp4_path:
                                os.remove(matched_file)
                            matched_file = final_mp4_path
                        except Exception:
                            if os.path.exists(matched_file):
                                os.rename(matched_file, final_mp4_path)
                                matched_file = final_mp4_path
                    filename = f"{base_name}.mp4"

                if is_gdrive:
                    finish_output_file(matched_file, os.path.basename(matched_file))
                else:
                    st.success(f"✅ 提取完成！音訊已儲存至: `{matched_file}`")
                gc.collect()
                return
            else:
                show_error_log_box(f"❌ 提取 `{base_name}` 失敗！無法產出音訊檔。", f"Expected Output Path: {expected_out_path}", title="線上網址音訊提取失敗", url=video_path)
                return
        except Exception as yt_err:
            show_error_log_box(f"❌ 線上網址音訊提取失敗: {yt_err}", traceback.format_exc(), title="線上網址音訊提取詳細錯誤日誌", url=video_path)
            return

    ext = ""
    ffmpeg_cmd = []
    out_path = ""
    
    headers_arg = []
    if headers:
        headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        headers_arg = ["-headers", headers_str]
        
    if "m3u8" in video_path:
        headers_arg += ["-allowed_segment_extensions", "ALL", "-extension_picky", "0"]
    
    if audio_format == "MP3":
        ext = "mp3"
        out_path = os.path.join(out_dir, f"{base_name}.{ext}")
        ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", video_path, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", out_path]
    elif audio_format == "M4A":
        ext = "m4a"
        out_path = os.path.join(out_dir, f"{base_name}.{ext}")
        ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", video_path, "-vn", "-c:a", "aac", "-b:a", "192k", out_path]
    elif audio_format == "MP4" or "MP4" in audio_format:
        ext = "mp4"
        out_path = os.path.join(out_dir, f"{base_name}.{ext}")
        ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", video_path, "-vn", "-c:a", "aac", "-b:a", "192k", out_path]
    else:
        # 預設 (原始格式)：AAC 音訊一律儲存為相容性最高之 MP4 容器，避免 AI 轉錄失敗
        try:
            probe_cmd = ["ffprobe", "-v", "error"] + headers_arg + ["-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
            codec = subprocess.check_output(probe_cmd, text=True, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).strip().lower()
            
            if codec == "aac":
                ext = "mp4"
                out_path = os.path.join(out_dir, f"{base_name}.{ext}")
                ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", video_path, "-vn", "-c:a", "copy", "-bsf:a", "aac_adtstoasc", out_path]
            elif codec in ["mp3", "opus"]:
                ext = codec
                out_path = os.path.join(out_dir, f"{base_name}.{ext}")
                ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", video_path, "-vn", "-c:a", "copy", out_path]
            else:
                ext = "mp4"
                out_path = os.path.join(out_dir, f"{base_name}.{ext}")
                ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", video_path, "-vn", "-c:a", "aac", "-b:a", "192k", out_path]
        except Exception:
            st.warning(f"⚠️ `{base_name}` 無法解析原始音訊格式，將預設轉換為 MP4 (AAC 音訊)。")
            ext = "mp4"
            out_path = os.path.join(out_dir, f"{base_name}.{ext}")
            ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", video_path, "-vn", "-c:a", "aac", "-b:a", "192k", out_path]

    if not is_gdrive and os.path.exists(out_path):
        st.success(f"⏭️ 檔案已存在: `{out_path}`")
        return

    try:
        total_dur = get_media_duration(video_path, headers=headers)
        returncode, stderr_log = run_ffmpeg_with_progress(
            ffmpeg_cmd, total_duration=total_dur, label="音訊提取"
        )
        if returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            if is_gdrive:
                finish_output_file(out_path, f"{base_name}.{ext}")
            else:
                st.success(f"✅ 提取完成！音訊已儲存至: `{out_path}`")
        else:
            show_error_log_box(f"❌ 提取 `{base_name}` 失敗！", stderr_log)
        
        gc.collect()
    except Exception as e:
        show_error_log_box(f"❌ 發生例外錯誤: {e}", traceback.format_exc(), title="Exception 堆疊追蹤資訊")

def release_resources():
    released_info = []
    
    # 1. 垃圾回收
    collected = gc.collect()
    released_info.append(f"🧹 已執行 Python 垃圾回收，回收了 {collected} 個物件。")
    
    # 2. 清除 Streamlit 快取
    try:
        st.cache_data.clear()
        st.cache_resource.clear()
        released_info.append("💾 已清除 Streamlit 應用程式快取數據。")
    except Exception as e:
        released_info.append(f"⚠️ 清除快取時發生錯誤: {e}")
        
    # 3. 刪除暫存 Cookie 檔案
    import glob
    temp_dir = tempfile.gettempdir()
    cookie_patterns = [
        os.path.join(temp_dir, "fb_cookies_*.txt"),
        os.path.join(os.getcwd(), "fb_cookies_*.txt")
    ]
    
    deleted_files = 0
    for pattern in cookie_patterns:
        for fpath in glob.glob(pattern):
            try:
                os.remove(fpath)
                deleted_files += 1
            except Exception:
                pass
                
    if deleted_files > 0:
        released_info.append(f"🗑️ 已成功清理 {deleted_files} 個暫存 Cookie 檔案。")
    else:
        released_info.append("✨ 未偵測到殘留的暫存 Cookie 檔案。")
        
    # 4. 嘗試關閉殘留的 ffmpeg 與 yt-dlp 進程
    try:
        subprocess.run(["pkill", "-f", "ffmpeg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-f", "yt-dlp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        released_info.append("⚙️ 已嘗試終止背景殘留的 FFmpeg 與 yt-dlp 進程。")
    except Exception as e:
        released_info.append(f"⚠️ 嘗試終止進程時發生錯誤: {e}")
        
    return released_info

# ========================================================
# Streamlit Web App Interface
# ========================================================
st.set_page_config(page_title="Gimymax Media Downloader", page_icon="🎬", layout="centered")

# 初始化 session_state download_dir 與 main_dir_input
if "download_dir" not in st.session_state:
    st.session_state.download_dir = load_download_dir()
if "main_dir_input" not in st.session_state:
    st.session_state.main_dir_input = st.session_state.download_dir
if "storage_destination" not in st.session_state:
    st.session_state.storage_destination = "local"

# 回呼函數 (Callbacks: 於 Widget 實例化前優先執行，允許修改 session_state)
def _cb_choose_folder():
    chosen = choose_folder_dialog(st.session_state.download_dir)
    if chosen:
        saved = save_download_dir(chosen)
        st.session_state.download_dir = saved
        st.session_state.main_dir_input = saved
        st.session_state.pending_toast = (f"✅ 已成功將儲存位置更改為：\n{saved}", "📁")
    else:
        st.session_state.pending_toast = ("ℹ️ 未選擇新資料夾或取消選擇", "💡")

def _cb_set_folder(target_path, name):
    saved = save_download_dir(target_path)
    st.session_state.download_dir = saved
    st.session_state.main_dir_input = saved
    st.session_state.pending_toast = (f"✅ 已切換至 {name}", "📁")

def _cb_dir_input_change():
    val = st.session_state.main_dir_input
    if val:
        st.session_state.download_dir = save_download_dir(val)

# 顯示對應提示訊息
if "pending_toast" in st.session_state:
    t_msg, t_icon = st.session_state.pop("pending_toast")
    st.toast(t_msg, icon=t_icon)

st.title("🎬 媒體下載與音訊提取器")

# 1. 儲存目標選擇區
dest_option = st.radio(
    "📦 **儲存目標模式**:",
    options=["📁 本機硬碟資料夾 (Local Disk)", "☁️ Google Drive 雲端直送 (Cloud API - 不存本地硬碟)"],
    index=0 if st.session_state.storage_destination == "local" else 1,
    horizontal=True,
    help="選擇是否直接透過 Google Drive 官方 REST API 將檔案直接上傳至雲端 /Download/ 資料夾，完全不保留於本機硬碟。"
)

if "Google Drive" in dest_option:
    st.session_state.storage_destination = "gdrive_cloud"
else:
    st.session_state.storage_destination = "local"

if st.session_state.storage_destination == "gdrive_cloud":
    # 檢查 Google Drive 授權狀態
    is_auth = gdrive_service.is_authenticated()
    if is_auth:
        user_email = gdrive_service.get_connected_account_email()
        col_g1, col_g2 = st.columns([4, 1])
        with col_g1:
            st.success(f"🟢 **Google Drive 雲端已連接**：`{user_email}` ｜ 預設目標：`Google Drive 雲端 /Download/`")
        with col_g2:
            if st.button("🚪 登出帳號", use_container_width=True):
                gdrive_service.revoke_gdrive_auth()
                st.rerun()
    else:
        st.warning("⚠️ 尚未連接 Google Drive 帳號。請先完成 OAuth 授權以啟用雲端直送功能：")
        cred_path = gdrive_service.get_credentials_path()
        if cred_path:
            if st.button("🔑 點此授權連接 Google Drive (開啟瀏覽器授權登入)", type="primary", use_container_width=True):
                try:
                    with st.spinner("正在開啟 Google 授權頁面..."):
                        gdrive_service.get_gdrive_service()
                    st.success("✅ Google Drive 授權成功！")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 授權失敗: {e}")
        else:
            st.info("💡 **首次連接設定**：請將從 Google Cloud Console 下載的 `credentials.json` 放入專案資料夾，或透過下方直接上傳：")
            uploaded_cred = st.file_uploader("上傳 Google OAuth credentials.json:", type=["json"])
            if uploaded_cred:
                target_cred = os.path.join(os.path.dirname(__file__), "credentials.json")
                with open(target_cred, "wb") as f:
                    f.write(uploaded_cred.getbuffer())
                st.success("✅ `credentials.json` 已成功儲存！請點擊按鈕進行授權：")
                st.rerun()
else:
    # 本地硬碟模式控制列
    col_dir1, col_dir2 = st.columns([3, 1])
    with col_dir1:
        st.text_input(
            "📁 下載儲存資料夾:",
            key="main_dir_input",
            on_change=_cb_dir_input_change,
            help="所有下載與音訊檔都會儲存至此資料夾"
        )

    with col_dir2:
        st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
        st.button("📂 選擇資料夾", key="main_browse_btn", use_container_width=True, on_click=_cb_choose_folder)

    # 快捷資料夾選區 (常用捷徑)
    with st.expander("⚡ 常用儲存位置快捷切換", expanded=False):
        q_cols = st.columns(4)
        home_dir = os.path.expanduser("~")
        downloads_path = os.path.join(home_dir, "Downloads")
        desktop_path = os.path.join(home_dir, "Desktop")
        documents_path = os.path.join(home_dir, "Documents")
        proj_downloads = os.path.join(os.path.dirname(__file__), "downloads")
        
        with q_cols[0]:
            st.button("📥 下載 (Downloads)", use_container_width=True, on_click=_cb_set_folder, args=(downloads_path, "Downloads"))
        with q_cols[1]:
            st.button("🖥️ 桌面 (Desktop)", use_container_width=True, on_click=_cb_set_folder, args=(desktop_path, "Desktop"))
        with q_cols[2]:
            st.button("📁 文件 (Documents)", use_container_width=True, on_click=_cb_set_folder, args=(documents_path, "Documents"))
        with q_cols[3]:
            st.button("📂 專案內 downloads", use_container_width=True, on_click=_cb_set_folder, args=(proj_downloads, "專案內部 downloads"))

# 2. 系統資源清理控制列 (並排按鈕)
col_res1, col_res2 = st.columns(2)
with col_res1:
    if st.button("🧹 僅釋放記憶體快取 (不關閉服務)", use_container_width=True, help="清空下載快取、暫存 Cookie 與釋放垃圾回收 RAM"):
        with st.spinner("正在釋放系統快取與垃圾回收中..."):
            info = release_resources()
            st.success("🧹 記憶體與快取清理完畢！")
            for msg in info:
                if "關閉" not in msg and "終止" not in msg:
                    st.toast(msg, icon="ℹ️")

with col_res2:
    if st.button("♻️ 釋放所有資源並關閉", type="primary", use_container_width=True):
        with st.spinner("正在釋放系統資源與關閉程式中..."):
            info = release_resources()
            for msg in info:
                st.success(msg)
            st.toast("♻️ 資源釋放成功，程式即將關閉...")
            st.warning("⚠️ 程式已終止，請手動關閉此網頁分頁。")
            import time
            time.sleep(1.5)
            import os
            os._exit(0)

st.divider()

tab1, tab2 = st.tabs(["🌐 線上影片下載", "📁 影片音訊提取"])

with tab1:
    st.markdown("將 Movieffm, Gimymax, MissAV, X, YouTube, Facebook, IG, TikTok 等影片網址直接下載。")
    
    target_urls = st.text_area("🔗 請輸入影片網址 (每行一個):", placeholder="https://www.movieffm.net/movies/your-name/ \nhttps://gimymax.com/ep/... \nhttps://youtube.com/watch?v=... \nhttps://www.facebook.com/watch/?v=...")
    
    fb_cookie_str = st.text_input("🔑 Facebook Cookie (選填，用於下載私密社團或好友貼文圖片):", type="password", placeholder="c_user=xxxx; xs=xxxx; ...", help="若要下載私密社團、好友貼文或無法下載時，請在 Chrome 開啟 Facebook -> 按 F12 -> 於 Application (應用程式) -> Cookies 中複製 c_user 與 xs 拼接（或直接複製整段 Cookie 值）並在此貼上。下載公開內容免填。")
    st.session_state.fb_cookie = fb_cookie_str
    
    # 即時計算目前已輸入的有效網址數量
    current_urls = [url.strip() for url in target_urls.split('\n') if url.strip()]
    current_count = len(current_urls)
    
    if current_count > 0:
        st.caption(f"ℹ️ 已輸入 **{current_count}** 個網址。")
    
    btn_video = st.button("⬇️ 開始批次下載影片", type="primary", use_container_width=True)
    
    if btn_video:
        raw_urls = [url.strip() for url in target_urls.split('\n') if url.strip()]
        urls = [normalize_input_url(u) for u in raw_urls]
        
        if not urls:
            st.warning("⚠️ 請先輸入網址！")
        else:
            st.info(f"📥 準備下載 {len(urls)} 個影片檔案...")
            
            # 建立進度條
            progress_bar = st.progress(0)
            
            # 遍歷網址進行下載
            for i, url in enumerate(urls):
                current_num = i + 1
                st.markdown(f"### 📍 正在處理第 {current_num}/{len(urls)} 個...")
                
                # 偵測是否為社團首頁網址
                is_fb_group_home = False
                if "facebook.com/groups/" in url or "fb.com/groups/" in url:
                    clean_url = url.split('?')[0].rstrip('/')
                    parts = clean_url.split('/groups/')
                    if len(parts) == 2 and '/' not in parts[1]:
                        is_fb_group_home = True
                        
                if is_fb_group_home:
                    st.warning("⚠️ 偵測到您輸入的是 **社團首頁** 的網址，而非個別貼文！\n\n👉 **正確做法**：請在社團中找到該貼文，點擊貼文下方的 **「發佈時間」**（例如：3小時前、昨天下午 5:00），進入個別貼文頁面後，再複製網址貼到下載器中。")
                    progress_bar.progress(current_num / len(urls))
                    st.divider()
                    continue
                
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
                    show_error_log_box(f"❌ 處理 {url} 時發生錯誤: {e}", traceback.format_exc(), title="Exception 堆疊追蹤資訊", url=url)
                
                # 更新進度條
                progress_bar.progress(current_num / len(urls))
                st.divider()
                
            st.balloons()
            st.success("🎉 所有下載任務處理完畢！")

with tab2:
    st.markdown("從本地檔案或線上播放清單提取出純音訊，完全在記憶體內處理，避免硬碟損耗。")
    local_video_path = st.text_input("📁 請輸入路徑 (本地影片/資料夾，或 YouTube, FB, IG 等網址/播放清單):", placeholder="/Users/ericcheng/Movies/ 或 https://youtube.com/playlist?list=... 或 https://www.facebook.com/watch/?v=...")
    audio_format = st.selectbox("🎵 請選擇輸出音訊格式:", ["預設 (原始格式 - AAC一律存為MP4)", "MP4 (AAC音訊 - 相容AI轉錄)", "MP3", "M4A"])
    
    if st.button("▶️ 開始提取音訊", type="primary", use_container_width=True):
        raw_input_path = local_video_path.strip()
        input_path = normalize_input_url(raw_input_path)
        if not input_path:
            st.warning("⚠️ 請先輸入路徑！")
        elif input_path.startswith("http://") or input_path.startswith("https://"):
            import yt_dlp
            urls_to_process = []
            
            ydl_opts = {
                'quiet': True,
                'extract_flat': 'in_playlist',
                'nocheckcertificate': True,
                'legacy_server_connect': True,
            }
            
            try:
                with st.spinner("🔍 正在解析線上網址/播放清單..."):
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(input_path, download=False)
                        if 'entries' in info:
                            for entry in info['entries']:
                                if not entry:
                                    continue
                                u = entry.get('webpage_url') or entry.get('url') or entry.get('original_url')
                                if u and not u.startswith(('http://', 'https://')):
                                    if entry.get('id'):
                                        u = f"https://www.youtube.com/watch?v={entry.get('id')}"
                                if u:
                                    urls_to_process.append(u)
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
                                target_media = item['url'] if (item['url'].startswith('http://') or item['url'].startswith('https://')) else item.get('webpage_url', item['url'])
                                extract_local_audio(target_media, audio_format, title=item['title'], headers=item.get('headers'))
                        else:
                            st.error(f"❌ 無法取得串流: {url}")
                    except Exception as e:
                        show_error_log_box(f"❌ 發生例外錯誤: {e}", traceback.format_exc(), title="Exception 堆疊追蹤資訊")
                    
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
