import os
import sys
import re
import json
import time
import socket
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
import media_converter
import ramdisk_manager

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
    gdrive_service.update_config(download_dir=path)
    return path

def check_dir_writable(path):
    """
    檢查資料夾是否具備寫入權限 (含偵測 macOS 對 Windows NTFS 等唯讀掛載磁碟)
    回傳 (is_writable: bool, reason: str)
    """
    if not path or not str(path).strip():
        return False, "未指定資料夾路徑"
    abs_path = os.path.abspath(os.path.expanduser(str(path).strip()))
    
    # 檢查或建立目錄
    if not os.path.exists(abs_path):
        try:
            os.makedirs(abs_path, exist_ok=True)
        except OSError as e:
            if getattr(e, "errno", None) == 30 or "read-only" in str(e).lower():
                return False, "此磁碟為唯讀檔案系統 (Read-only file system，如 Windows NTFS 外接硬碟在 macOS 預設無法寫入)。"
            if getattr(e, "errno", None) == 13 or "permission denied" in str(e).lower():
                return False, "權限不足，無法在此路徑建立目錄 (Permission denied)。"
            return False, f"無法建立目錄: {e}"
            
    # 建立臨時檔案測試真實寫入能力
    test_file = os.path.join(abs_path, f".write_test_{os.getpid()}_{int(time.time()*1000)}.tmp")
    try:
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("test")
        try:
            os.remove(test_file)
        except Exception:
            pass
        return True, ""
    except OSError as e:
        if getattr(e, "errno", None) == 30 or "read-only" in str(e).lower():
            return False, "此磁碟為唯讀檔案系統 (Read-only file system，如 Windows NTFS 格式外接硬碟在 macOS 預設僅供讀取)。"
        if getattr(e, "errno", None) == 13 or "permission denied" in str(e).lower():
            return False, "權限不足，無法在此目錄建立或寫入檔案 (Permission denied)。"
        return False, f"無法寫入此目錄: {e}"


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
    if any(domain in url_str.lower() for domain in [".com", ".ai", ".tv", ".ws", ".net", ".org", ".me", ".co", "youtube", "facebook", "instagram", "tiktok", "twitter", "missav", "movieffm", "mvffm"]):
        return "https://" + url_str.lstrip('/')
    # 若輸入的是 MissAV / 平台番號與代碼 (例如 JD-054791cdbc62ac51e7c79c59f86b72960)
    if re.match(r'^[a-zA-Z0-9\-_]{5,}$', url_str):
        if url_str.startswith(('qsvip-', 'NS4K-', 'NSYS-', 'itdog-')):
            return url_str
        return f"https://missav.ai/{url_str}"
    return "https://" + url_str

def resolve_gimy_stream(player_url, page_url=''):
    if not player_url:
        return ''
    if player_url.startswith(('http://', 'https://')):
        return player_url
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    }
    
    endpoints = []
    if player_url.startswith('qsvip-'):
        endpoints.append((
            f'https://v.attzy.com/ap/qs/api.php?url={urllib.parse.quote(player_url)}',
            {'Referer': f'https://v.attzy.com/ap/qs/?url={player_url}&jctype=qsvip'}
        ))
        endpoints.append((
            f'https://play.gimy.bot/qsvip/api.php?url={urllib.parse.quote(player_url)}',
            {'Referer': 'https://play.gimy.bot/jd/'}
        ))
    elif player_url.startswith(('JD-', 'JDQM-', 'JDHG-')):
        endpoints.append((
            f'https://v.attzy.com/ap/jd/api.php?url={urllib.parse.quote(player_url)}',
            {'Referer': f'https://v.attzy.com/ap/jd/?url={player_url}&jctype=JD4K'}
        ))
        endpoints.append((
            f'https://play.gimy.bot/jd/api.php?url={urllib.parse.quote(player_url)}',
            {'Referer': 'https://play.gimy.bot/jd/'}
        ))
    elif player_url.startswith(('NS4K-', 'NSYS-')):
        endpoints.append((
            f'https://player.aigm.tv/n/parse.php?url={urllib.parse.quote(player_url)}',
            {'Referer': f'https://player.aigm.tv/n/?url={player_url}'}
        ))
        endpoints.append((
            f'https://play.gimy.bot/ns/api.php?url={urllib.parse.quote(player_url)}',
            {'Referer': 'https://play.gimy.bot/jd/'}
        ))
    elif player_url.startswith('itdog-'):
        endpoints.append((
            f'https://v.attzy.com/ap/lb/api.php?url={urllib.parse.quote(player_url)}',
            {'Referer': f'https://v.attzy.com/ap/lb/?url={player_url}&jctype=itdog'}
        ))
    else:
        endpoints.append((
            f'https://v.attzy.com/ap/lb/api.php?url={urllib.parse.quote(player_url)}',
            {'Referer': 'https://v.attzy.com/'}
        ))
        endpoints.append((
            f'https://v.attzy.com/ap/jd/api.php?url={urllib.parse.quote(player_url)}',
            {'Referer': 'https://v.attzy.com/'}
        ))
        endpoints.append((
            f'https://play.gimy.bot/a/api.php?url={urllib.parse.quote(player_url)}',
            {'Referer': 'https://play.gimy.bot/jd/'}
        ))

    for api_url, api_headers in endpoints:
        try:
            req_h = dict(headers)
            req_h.update(api_headers)
            r = requests.get(api_url, headers=req_h, timeout=10)
            data = r.json()
            resolved = data.get('url') or data.get('video') or data.get('playurl')
            if resolved and isinstance(resolved, str) and resolved.startswith(('http://', 'https://')):
                return resolved
        except Exception:
            continue

    if player_url.startswith(('/', './')) or any(ext in player_url.lower() for ext in ['.m3u8', '.mp4', '.flv']):
        return urllib.parse.urljoin(page_url, player_url)

    raise ValueError(f"無法解析 Gimy 播放線路 ({player_url[:25]}...)，伺服器 API 解析失敗或未回傳有效影片網址。請在網頁切換至其他線路 (如 天堂雲、西瓜雲、極速雲等) 後再試。")

def is_valid_m3u8_content(chunk_bytes):
    """檢查內容是否為真正的 HLS m3u8 播放清單（排除 HTML 錯誤頁、防盜鏈頁面或 JSON）。"""
    if not chunk_bytes:
        return False
    clean = chunk_bytes.lstrip(b'\xef\xbb\xbf \t\r\n')
    if clean.startswith((b'<!DOCTYPE', b'<!doctype', b'<html', b'<HTML', b'<?xml', b'{"code"')):
        return False
    return b'#EXTM3U' in clean or b'#EXT-X-' in clean or b'#EXTINF' in clean

def check_m3u8_accessible(m3u8_url, headers=None, timeout=3.0):
    """快速探測 m3u8 串流是否可用 (排除 DNS 失敗、404、防盜鏈阻擋與回傳 HTML 的無效伺服器)。"""
    if not m3u8_url or not m3u8_url.startswith(('http://', 'https://')):
        return False

    req_headers = dict(_GLOBAL_HEADERS)
    if headers:
        req_headers.update(headers)

    # 1. requests 快速探測 (內建連線池與 DNS 解析超時)
    try:
        r = _global_session.get(m3u8_url, headers=req_headers, timeout=timeout, stream=True)
        if r.status_code in [200, 206]:
            chunk = next(r.iter_content(1024), b"")
            if is_valid_m3u8_content(chunk):
                return True
    except Exception:
        pass

    # 2. 備援：帶常見 Referer 重試 (部分 CDN 防盜鏈需特定 Referer)
    try:
        parsed = urllib.parse.urlparse(m3u8_url)
        for ref in ['https://www.movieffm.net/', 'https://www.mvffm.net/', f"{parsed.scheme}://{parsed.netloc}/"]:
            if req_headers.get('Referer') == ref:
                continue
            h2 = dict(req_headers)
            h2['Referer'] = ref
            r = _global_session.get(m3u8_url, headers=h2, timeout=timeout, stream=True)
            if r.status_code in [200, 206]:
                chunk = next(r.iter_content(1024), b"")
                if is_valid_m3u8_content(chunk):
                    return True
    except Exception:
        pass

    # 3. 備援：curl_cffi 模擬真實瀏覽器 TLS 指紋探測
    try:
        from curl_cffi import requests as curl_requests
        for imp in ["chrome124", "safari15_5"]:
            try:
                r = curl_requests.get(m3u8_url, headers=req_headers, impersonate=imp, timeout=timeout)
                if r.status_code in [200, 206] and is_valid_m3u8_content(r.content[:1024]):
                    return True
            except Exception:
                pass
    except Exception:
        pass

    return False


def get_media_items(url):
    url = normalize_input_url(url)
    items = []
    
    # 支援 Movieffm / Mvffm 電影與戲劇串流解析
    if any(k in url.lower() for k in ["movieffm", "mvffm"]):
        import html as html_lib
        from curl_cffi import requests as curl_requests
        from urllib.parse import urlparse
        parsed_origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Referer": parsed_origin,
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
            title = re.sub(r'\s*-\s*(?:Movieffm|mvffm).*$', '', title, flags=re.IGNORECASE).strip()
        else:
            t_js = re.search(r'title\s*:\s*["\']([^"\']+)["\']', html_content)
            if t_js:
                title = html_lib.unescape(t_js.group(1)).strip()

        title = re.sub(r'[\\/:*?"<>|]', '_', title).strip()
        if not title:
            title = "Movieffm_Video"

        # 2. 提取 m3u8 串流網址 (支援多線路自動探測備援、單部電影與連續劇多集)
        items = []
        
        # 策略 A: 解析 videourls JSON 陣列
        match = re.search(r'videourls\s*:\s*(\[\s*(?:\[.*?\]|\{.*?\})\s*\])', html_content, re.DOTALL)
        if match:
            try:
                clean_json = match.group(1).replace(r'\/', '/')
                data = json.loads(clean_json)
                
                # 情況 1: 2D 陣列 (電視劇 / 連續劇，外層是多條播放來源線路 FLV 1, FLV 2..., 內層是該線路下的各集)
                if len(data) > 0 and isinstance(data[0], list):
                    # 找出所有可用存活的線路 (使用 ThreadPoolExecutor 併發探測加速)
                    def _probe_source(item):
                        s_idx, src = item
                        if isinstance(src, list) and len(src) > 0:
                            ep1_url = src[0].get("url")
                            if ep1_url and check_m3u8_accessible(ep1_url, headers=headers):
                                return (s_idx, src, len(src))
                        return None

                    with ThreadPoolExecutor(max_workers=min(10, max(1, len(data)))) as executor:
                        probed = list(executor.map(_probe_source, enumerate(data)))
                        valid_sources = [r for r in probed if r is not None]

                    # 決定標準集數：取第一條可用主線路的集數，或有效線路的眾數集數
                    if valid_sources:
                        primary_source = valid_sources[0][1]
                        total_eps = len(primary_source)
                    else:
                        primary_source = data[0] if len(data) > 0 and isinstance(data[0], list) else []
                        total_eps = len(primary_source)
                    
                    for idx in range(total_eps):
                        chosen_ep_url = None
                        chosen_raw_name = ""
                        
                        # 1. 優先從 primary_source 直接取得（該線路已在前面併發驗證過可用）
                        if idx < len(primary_source):
                            ep_item = primary_source[idx]
                            ep_url = ep_item.get('url')
                            if ep_url and ep_url.startswith('http'):
                                chosen_raw_name = str(ep_item.get('name', '')).strip()
                                chosen_ep_url = ep_url
                        
                        # 2. 若 primary_source 沒有該集，再從其他 valid_sources 取得
                        if not chosen_ep_url:
                            for _, source_list, _ in valid_sources:
                                if idx < len(source_list):
                                    ep_item = source_list[idx]
                                    ep_url = ep_item.get('url')
                                    if ep_url and ep_url.startswith('http'):
                                        chosen_raw_name = str(ep_item.get('name', '')).strip()
                                        chosen_ep_url = ep_url
                                        break
                        
                        # 3. 若仍未找到，退回 data 所有線路
                        if not chosen_ep_url:
                            for source_list in data:
                                if isinstance(source_list, list) and idx < len(source_list):
                                    ep_item = source_list[idx]
                                    ep_url = ep_item.get('url')
                                    if ep_url and ep_url.startswith('http'):
                                        if not chosen_raw_name:
                                            chosen_raw_name = str(ep_item.get('name', '')).strip()
                                        chosen_ep_url = ep_url
                                        break
                        if not chosen_ep_url and idx < len(primary_source):
                            chosen_ep_url = primary_source[idx].get('url')
                            if not chosen_raw_name:
                                chosen_raw_name = str(primary_source[idx].get('name', '')).strip()

                        if not chosen_ep_url:
                            continue

                        # 命名處理
                        if total_eps == 1:
                            ep_title = title
                        else:
                            if chosen_raw_name:
                                num_match = re.search(r'\d+', chosen_raw_name)
                                if num_match:
                                    ep_num = int(num_match.group())
                                    ep_title = f"{title} - EP{ep_num:02d}"
                                else:
                                    ep_title = f"{title} - {chosen_raw_name}"
                            else:
                                ep_title = f"{title} - EP{idx+1:02d}"
                                
                        items.append({
                            'url': chosen_ep_url,
                            'title': ep_title,
                            'ext': 'mp4',
                            'type': 'video',
                            'headers': {
                                'Referer': url,
                                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                            }
                        })

                # 情況 2: 1D 陣列 (單部電影多來源)
                elif len(data) > 0 and isinstance(data[0], dict):
                    valid_candidates = []
                    for item in data:
                        u = item.get('url')
                        if u and (u.startswith('http://') or u.startswith('https://')) and '.m3u8' in u:
                            valid_candidates.append(u)
                    
                    def _probe_cand(u):
                        if check_m3u8_accessible(u, headers=headers):
                            return u
                        return None

                    with ThreadPoolExecutor(max_workers=min(5, max(1, len(valid_candidates)))) as executor:
                        probed_cands = [res for res in executor.map(_probe_cand, valid_candidates) if res is not None]

                    chosen_movie_url = probed_cands[0] if probed_cands else None
                    backup_movie_urls = probed_cands[1:] if len(probed_cands) > 1 else []

                    # 若並行檢測未通過，退回用較寬鬆的超時重試
                    if not chosen_movie_url:
                        for cand_url in valid_candidates:
                            if check_m3u8_accessible(cand_url, headers=headers, timeout=5.0):
                                chosen_movie_url = cand_url
                                break

                    if chosen_movie_url:
                        items.append({
                            'url': chosen_movie_url,
                            'backup_urls': backup_movie_urls,
                            'title': title,
                            'ext': 'mp4',
                            'type': 'video',
                            'headers': {
                                'Referer': url,
                                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                            }
                        })
            except Exception as e:
                print(f"Error parsing Movieffm videourls: {e}")

        # 策略 B: 正規表達式全局搜尋 m3u8 作為備援
        if not items:
            m3u8_matches = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html_content)
            for m_url in m3u8_matches:
                clean_url = m_url.replace(r'\/', '/')
                if check_m3u8_accessible(clean_url, headers=headers):
                    items.append({
                        'url': clean_url,
                        'title': title,
                        'ext': 'mp4',
                        'type': 'video',
                        'headers': {
                            'Referer': url,
                            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                        }
                    })
                    break

        if not items:
            raise ValueError("無法從 Movieffm 頁面中解析出有效的影片串流網址 (m3u8)。所有提供之播放線路均無法連線或伺服器已失效。")

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
    is_custom_hls = any(k in url.lower() for k in ["missav", "movieffm", "mvffm", "gimymax", "gimyplus", "gimy"])
    if not is_custom_hls or any(domain in url for domain in ["x.com", "twitter.com", "t.co", "youtube.com", "youtu.be", "facebook.com", "fb.com", "fb.watch", "instagram.com", "ig.me", "tiktok.com"]):
        # yt-dlp 的 threads extractor 綁定 threads.net，若是 .com 則先替換
        url = url.replace("threads.com", "threads.net")
        
        import yt_dlp
        
        fb_cookie_str = st.session_state.get('fb_cookie')
        bili_cookie_str = st.session_state.get('bili_cookie')
        temp_cookie_path = None
        if "facebook.com" in url or "fb.com" in url or "fb.watch" in url:
            temp_cookie_path = create_temp_cookiefile(fb_cookie_str)
        elif "bilibili.com" in url or "b23.tv" in url:
            temp_cookie_path = create_temp_cookiefile(bili_cookie_str)
            
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
                            'webpage_url': webpage_url,
                            'is_ytdlp': True
                        })
            except Exception as e:
                err_msg = str(e)
                if any(kw in err_msg for kw in ["No video formats found", "Unsupported URL", "Cannot parse data", "Private video", "login"]):
                    if "facebook.com" in url or "fb.com" in url or "fb.watch" in url:
                        raise ValueError("此 Facebook 連結可能為「純相片貼文」、「非影片內容」或「私密/限制級內容」。\n\n💡 **下載建議**：請確認您已在下方填入有效的 **Facebook Cookie**。私密社團、好友限閱、相片貼文或部分特定影片必須有 Cookie 授權才能進行下載。")
                if any(kw in err_msg for kw in ["412", "Precondition Failed", "啥都木有", "KeyError('result')"]):
                    if "bilibili.com" in url or "b23.tv" in url:
                        raise ValueError("Bilibili 伺服器存取被拒 (HTTP 412 風控限制或該影片/番劇已下架)。\n\n💡 **原因與建議**：\n1. 該番劇可能為大會員專屬、版權地區限制，或已被官方下架 (API 回傳「啥都木有」)。\n2. 若影片在瀏覽器可觀看，請在下方「🔑 Bilibili Cookie」填入您的登入 Cookie (`SESSDATA`) 以通過安全風控策略。")
                if is_custom_hls:
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
            
    # 原有的 Gimymax 網頁解析邏輯 (支援正常集數頁面與直接傳入的播放線路 token /ep/qsvip-xxx 等)
    token_match = re.search(r'(qsvip-[a-zA-Z0-9_\-]+|JD[A-Z]*-[a-zA-Z0-9_\-]+|NS[A-Z0-9]*-[a-zA-Z0-9_\-]+|itdog-[a-zA-Z0-9_\-]+)', url)
    if token_match and not url.endswith('.html'):
        raw_m3u8 = token_match.group(1)
        m3u8_url = resolve_gimy_stream(raw_m3u8, page_url=url)
        referer = 'https://v.attzy.com/' if any(d in m3u8_url for d in ['hxx', 'attzy', 'telegram', 'shenhua']) else url
        return [{
            'url': m3u8_url,
            'title': 'Gimy_Video',
            'ext': 'mp4',
            'type': 'video',
            'headers': {
                'Referer': referer,
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            }
        }]

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": url,
    }
    response = None
    try:
        r = _global_session.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            response = r
    except Exception:
        pass
    if not response or response.status_code != 200:
        try:
            from curl_cffi import requests as curl_requests
            r = curl_requests.get(url, headers=headers, impersonate="chrome124", timeout=15)
            if r.status_code == 200:
                response = r
        except Exception:
            pass

    if not response:
        response = requests.get(url, headers=headers, timeout=15)

    # 策略 0: Gimy 新版動態 API 播放器架構 (data-pk 配合 /api.php/pk/q 接口)
    pk_match = re.search(r'data-pk=["\']([^"\']+)["\']', response.text)
    if pk_match:
        pk = pk_match.group(1).strip()
        parsed_origin = f"{urllib.parse.urlparse(url).scheme}://{urllib.parse.urlparse(url).netloc}"
        api_url = f"{parsed_origin}/api.php/pk/q?k={urllib.parse.quote(pk)}"
        api_headers = dict(headers)
        api_headers['Referer'] = url
        try:
            r_api = _global_session.get(api_url, headers=api_headers, cookies=getattr(response, 'cookies', None), timeout=10)
            if r_api.status_code == 200:
                pk_data = r_api.json()
                lines_list = pk_data.get('l', [])
                if lines_list and isinstance(lines_list, list):
                    # 提取所有候選線路
                    candidates = [l_item.get('u', '') for l_item in lines_list if l_item.get('u')]
                    
                    # 依序解析各線路真實串流網址
                    valid_streams = []
                    for raw_cand in candidates:
                        try:
                            stream_u = resolve_gimy_stream(raw_cand, page_url=url)
                            if stream_u and stream_u not in valid_streams:
                                valid_streams.append(stream_u)
                        except Exception:
                            continue

                    # 並行探測可用線路
                    def _probe_stream(u):
                        ref = 'https://v.attzy.com/' if any(d in u for d in ['hxx', 'attzy', 'telegram', 'shenhua']) else url
                        probe_h = {'Referer': ref, 'User-Agent': headers['User-Agent']}
                        if check_m3u8_accessible(u, headers=probe_h):
                            return u
                        return None

                    with ThreadPoolExecutor(max_workers=min(5, max(1, len(valid_streams)))) as executor:
                        probed = [s for s in executor.map(_probe_stream, valid_streams) if s is not None]

                    chosen_url = probed[0] if probed else (valid_streams[0] if valid_streams else None)
                    backup_urls = probed[1:] if len(probed) > 1 else []

                    if chosen_url:
                        # 標題提取
                        title = "Gimy_Video"
                        h1_m = re.search(r'<h1\b[^>]*>(.*?)</h1>', response.text, re.DOTALL)
                        if h1_m:
                            h1_content = h1_m.group(1)
                            a_m = re.search(r'<a\b[^>]*>(.*?)</a>', h1_content, re.DOTALL)
                            span_m = re.search(r'<span\b[^>]*>(.*?)</span>', h1_content, re.DOTALL)
                            base_name = re.sub(r'<[^>]+>', '', a_m.group(1)).strip() if a_m else ""
                            ep_name = re.sub(r'<[^>]+>', '', span_m.group(1)).strip() if span_m else ""
                            ep_name = re.sub(r'^[-\s]+', '', ep_name).strip()

                            match_s = re.search(r'第([一二三四五六七八九十\d]+)季', base_name)
                            if match_s:
                                s_num = cn_to_an(match_s.group(1))
                                base_name = re.sub(r'第[一二三四五六七八九十\d]+季', f'S{s_num}', base_name)

                            if ep_name:
                                match_e = re.search(r'第(\d+)集', ep_name)
                                if match_e:
                                    ep_str = f"E{int(match_e.group(1)):02d}"
                                else:
                                    ep_str = ep_name
                                title = f"{base_name} - {ep_str}" if base_name else ep_str
                            elif base_name:
                                title = base_name
                            else:
                                raw_h1 = re.sub(r'<[^>]+>', ' ', h1_content)
                                title = ' '.join(raw_h1.split()).strip()
                        else:
                            t_m = re.search(r'<title>(.*?)</title>', response.text)
                            if t_m:
                                t_raw = t_m.group(1).strip()
                                t_raw = re.sub(r'\s*(?:線上看)?\s*-\s*劇迷.*$', '', t_raw, flags=re.IGNORECASE).strip()
                                if t_raw:
                                    title = t_raw

                        title = re.sub(r'[\\/:*?"<>|]', '_', title).strip()
                        if not title:
                            title = "Gimy_Video"

                        referer = 'https://v.attzy.com/' if any(d in chosen_url for d in ['hxx', 'attzy', 'telegram', 'shenhua']) else url
                        return [{
                            'url': chosen_url,
                            'backup_urls': backup_urls,
                            'title': title,
                            'ext': 'mp4',
                            'type': 'video',
                            'headers': {
                                'Referer': referer,
                                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                            }
                        }]
        except Exception as e:
            print(f"Error fetching Gimy pk API: {e}")

    # 尋找播放器 JSON 資料 (相容 MacCMS 的 var player_aaaa = {...} 或 var player_data = {...} 等變體)
    data = None
    # 策略 1: 尋找直接賦值 JSON 物件的 player_xxxx 變數 (例如 MacCMS 的 var player_aaaa = {...})
    for m in re.finditer(r'var\s+(player_[a-zA-Z0-9_]*)\s*=\s*(\{.*?\})\s*(?:;|</script>)', response.text, re.DOTALL):
        try:
            candidate = json.loads(m.group(2))
            if isinstance(candidate, dict) and ('url' in candidate or 'vod_data' in candidate):
                data = candidate
                break
        except Exception:
            continue

    # 策略 2: 尋找 var player_data = ... (可能是直接 JSON 或變數指標如 var player_data = player_aaaa;)
    if not data:
        m_pd = re.search(r'var\s+player_data\s*=\s*(.*?)\s*(?:;|</script>)', response.text, re.DOTALL)
        if m_pd:
            content = m_pd.group(1).strip()
            try:
                data = json.loads(content)
            except Exception:
                var_match = re.match(r'^[a-zA-Z0-9_]+$', content)
                if var_match:
                    target_var = var_match.group(0)
                    m_var = re.search(rf'var\s+{target_var}\s*=\s*({{.*?}})\s*(?:;|</script>)', response.text, re.DOTALL)
                    if m_var:
                        try:
                            data = json.loads(m_var.group(1))
                        except Exception:
                            pass

    # 策略 3: 全文匹配含有 "flag":"play" 的 JSON 物件
    if not data:
        for m in re.finditer(r'(\{"flag":"play"[^<]+?\})', response.text):
            try:
                candidate = json.loads(m.group(1))
                if isinstance(candidate, dict) and ('url' in candidate or 'vod_data' in candidate):
                    data = candidate
                    break
            except Exception:
                continue

    if not data:
        if token_match:
            raw_m3u8 = token_match.group(1)
            m3u8_url = resolve_gimy_stream(raw_m3u8, page_url=url)
            referer = 'https://v.attzy.com/' if any(d in m3u8_url for d in ['hxx', 'attzy', 'telegram', 'shenhua']) else url
            return [{
                'url': m3u8_url,
                'title': 'Gimy_Video',
                'ext': 'mp4',
                'type': 'video',
                'headers': {
                    'Referer': referer,
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                }
            }]
        raise ValueError("Cannot find player_data in the webpage.")
        
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
    
    referer = 'https://v.attzy.com/' if any(d in m3u8_url for d in ['hxx', 'attzy', 'telegram', 'shenhua']) else url
    items = [{
        'url': m3u8_url,
        'title': title,
        'ext': 'mp4',
        'type': 'video',
        'headers': {
            'Referer': referer,
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

def filter_and_clean_m3u8_ads(m3u8_text, base_url):
    """
    智慧過濾 m3u8 中的賭博與插播廣告切片 (片頭貼片廣告、中插短廣告、片尾貼片廣告、第三方廣告 CDN 切片)。
    回傳: (clean_segment_urls, clean_segment_durations, clean_m3u8_text, filtered_ad_count, filtered_ad_duration)
    """
    lines = m3u8_text.splitlines()
    raw_segments = []
    current_dur = 2.0
    disc_flag = False
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-DISCONTINUITY"):
            disc_flag = True
        elif line.startswith("#EXTINF:"):
            dur_match = re.search(r"#EXTINF:([\d\.]+)", line)
            if dur_match:
                current_dur = float(dur_match.group(1))
        elif not line.startswith("#"):
            full_url = urllib.parse.urljoin(base_url, line)
            raw_segments.append({
                'url': full_url,
                'raw_line': line,
                'dur': current_dur,
                'disc': disc_flag
            })
            disc_flag = False
            current_dur = 2.0

    if not raw_segments:
        return [], [], m3u8_text, 0, 0.0

    total_stream_dur = sum(s['dur'] for s in raw_segments)

    # 識別 sections (由 discontinuity 分割的區塊)
    sections = []
    cur_sec = []
    for s in raw_segments:
        if s['disc'] and cur_sec:
            sections.append(cur_sec)
            cur_sec = []
        cur_sec.append(s)
    if cur_sec:
        sections.append(cur_sec)

    clean_segments = []
    filtered_ads = []
    ad_keywords = ["/ad/", "/ads/", "/adv/", "guanggao", "advertisement", "notice.ts", "banner", "ad_"]

    AD_MAX_DURATION = 35.0  # 賭博/插播廣告區塊時長通常在 3~35 秒內

    # 判斷是否為「廣告插入型」串流：
    # 真正的廣告插入串流必定存在顯著較長的主正片 section (例如 >= 90s 或單一 section 佔總片長 >= 30%)
    # 若所有 section 都是 ~20s 的短區塊 (如 Telegram 分片或特殊切片源)，說明整部影片都是以頻繁 discontinuity 分段，絕非廣告！
    has_dominant_feature = any(
        sum(s['dur'] for s in sec) >= 90.0 or (sum(s['dur'] for s in sec) / total_stream_dur >= 0.3)
        for sec in sections
    )
    short_sections = [sec for sec in sections if sum(s['dur'] for s in sec) <= AD_MAX_DURATION]
    short_sec_total_dur = sum(sum(s['dur'] for s in sec) for sec in short_sections)

    # 只有當串流長度足夠、存在長正片區塊、且短區塊時長佔比在合理廣告範圍內 (<= 20%) 時，才允許依 section 切割判定廣告
    allow_section_ad_filter = (
        total_stream_dur > 180.0 and
        len(sections) > 1 and
        has_dominant_feature and
        (short_sec_total_dur / total_stream_dur <= 0.20)
    )

    for sec_idx, sec in enumerate(sections):
        sec_dur = sum(s['dur'] for s in sec)
        sec_len = len(sec)
        is_ad_section = False

        if allow_section_ad_filter:
            # 規則 A: 片頭貼片廣告判定 (第 1 個 section，時長 <= 35s)
            if sec_idx == 0 and sec_dur <= AD_MAX_DURATION:
                is_ad_section = True

            # 規則 B: 片尾貼片廣告判定 (最後 1 個 section，時長 <= 35s)
            elif sec_idx == len(sections) - 1 and sec_dur <= AD_MAX_DURATION:
                is_ad_section = True

            # 規則 C: 中插短廣告判定 (中間短區塊，時長 <= 35s 且切片數 <= 15)
            elif 0 < sec_idx < len(sections) - 1 and sec_dur <= AD_MAX_DURATION and sec_len <= 15:
                is_ad_section = True

        if is_ad_section:
            filtered_ads.extend(sec)
        else:
            for s in sec:
                url_lower = s['url'].lower()
                if any(kw in url_lower for kw in ad_keywords):
                    filtered_ads.append(s)
                else:
                    clean_segments.append(s)

    # 兜底防護：若過濾後切片為空，或剩餘時長嚴重小於原本總時長的 50% (且總片長 > 60s)，表示發生誤殺，自動回退
    clean_total_dur = sum(s['dur'] for s in clean_segments)
    if not clean_segments or (total_stream_dur > 60.0 and clean_total_dur / total_stream_dur < 0.5):
        clean_segments = []
        filtered_ads = []
        for s in raw_segments:
            url_lower = s['url'].lower()
            if any(kw in url_lower for kw in ad_keywords):
                filtered_ads.append(s)
            else:
                clean_segments.append(s)

    clean_urls = [s['url'] for s in clean_segments]
    clean_durs = [s['dur'] for s in clean_segments]
    total_ad_dur = sum(s['dur'] for s in filtered_ads)

    # 構建乾淨的 m3u8 文本
    clean_m3u8_lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10", "#EXT-X-PLAYLIST-TYPE:VOD"]
    for s in clean_segments:
        clean_m3u8_lines.append(f"#EXTINF:{s['dur']:.6f},")
        clean_m3u8_lines.append(s['url'])
    clean_m3u8_lines.append("#EXT-X-ENDLIST")
    clean_m3u8_text = "\n".join(clean_m3u8_lines)

    return clean_urls, clean_durs, clean_m3u8_text, len(filtered_ads), total_ad_dur

def download_fast_parallel_hls(m3u8_url, out_path=None, extra_headers=None, max_workers=None, label="影片"):
    """平行 HLS 下載引擎（全量 RAM 模式：24G RAM，最大化速度，零磁碟 I/O，自動過濾賭博貼片廣告）。"""
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
    clean_text = text.lstrip('\ufeff \t\r\n')
    if not (clean_text.startswith("#EXT") or "#EXTM3U" in clean_text or "#EXT-X-" in clean_text):
        raise ValueError(f"伺服器回傳非有效的 m3u8 內容 (可能為網頁阻擋或無效連結): {clean_text[:120]}")

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
            clean_sub = text.lstrip('\ufeff \t\r\n')
            if not (clean_sub.startswith("#EXT") or "#EXTM3U" in clean_sub or "#EXT-X-" in clean_sub):
                raise ValueError(f"取得之子播放清單非有效 m3u8: {clean_sub[:120]}")

    # ── 智慧廣告切片過濾 (片頭賭博貼片、中插廣告與第三方廣告 CDN) ───────────
    segment_urls, segment_durations, clean_m3u8_text, ad_count, ad_dur = filter_and_clean_m3u8_ads(text, m3u8_url)
    if ad_count > 0:
        st.toast(f"🛡️ 已自動過濾 {ad_count} 個廣告切片 ({ad_dur:.1f} 秒賭博/貼片廣告)，為您保留純淨正片！", icon="🛡️")

    total_segments = len(segment_urls)
    if total_segments == 0:
        # Fallback: 若過濾後無切片，嘗試從原始 m3u8 文字直接提取所有切片 URI
        fallback_urls = [
            urllib.parse.urljoin(m3u8_url, line.strip())
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if fallback_urls:
            segment_urls = fallback_urls
            segment_durations = [2.0] * len(segment_urls)
            total_segments = len(segment_urls)
        else:
            raise ValueError("m3u8 播放列表中找不到任何影片切片 (segments)。")

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

    scratch_dir = ramdisk_manager.get_scratch_dir()
    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False, dir=scratch_dir) as tmp_ts:
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

def gdrive_precheck_exists(filename):
    """雲端模式下載前先檢查目標資料夾是否已有同名檔案，有則顯示訊息並回傳 True。"""
    if not st.session_state.get('gdrive_skip_dup', True):
        return False
    try:
        folder_id = gdrive_service.resolve_target_folder_id()
        existing = gdrive_service.find_existing_file(filename, folder_id)
    except Exception:
        return False
    if existing:
        _, label = gdrive_service.get_target_folder()
        link = existing.get('webViewLink')
        link_md = f" [🔗 開啟檔案]({link})" if link else ""
        st.success(f"⏭️ 雲端 `{label}` 已存在，自動跳過: `{filename}`{link_md}")
        return True
    return False

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

            _, target_label = gdrive_service.get_target_folder()
            prog_bar = st.progress(0.0)
            status_text = st.empty()

            def _upload_cb(pct, cur_bytes, tot_bytes, speed_mb):
                pct_val = min(1.0, max(0.0, pct))
                prog_bar.progress(pct_val)
                cur_mb = cur_bytes / 1024 / 1024
                tot_mb = tot_bytes / 1024 / 1024
                status_text.markdown(f"☁️ **Google Drive 雲端直送中**: `{pct_val*100:.1f}%` ({cur_mb:.1f} MB / {tot_mb:.1f} MB) | 上傳速度: `{speed_mb:.2f} MB/s`")

            with st.spinner(f"☁️ 正在直送至 Google Drive {target_label}{filename}..."):
                res = gdrive_service.upload_file_directly_to_gdrive(
                    local_temp_or_final_path,
                    original_filename=filename,
                    progress_callback=_upload_cb,
                    skip_if_exists=st.session_state.get('gdrive_skip_dup', True),
                )

            prog_bar.empty()
            status_text.empty()

            link = res.get('webViewLink') if res else None
            folder_link = gdrive_service.get_folder_link(res['folder_id']) if res and res.get('folder_id') else None
            link_md = f" [🔗 開啟檔案]({link})" if link else ""
            folder_md = f" ｜ [📂 開啟資料夾]({folder_link})" if folder_link else ""
            if res and res.get('skipped'):
                st.info(f"⏭️ 雲端 `{target_label}` 已存在同名檔案，已跳過上傳：`{filename}`{link_md}{folder_md}")
            else:
                st.success(f"🎉 **雲端直送成功！** `{target_label}{filename}`{link_md}{folder_md}（本機無殘留）")
            return True
        except gdrive_service.GDriveAuthRequired as e:
            st.error(f"❌ {e} 請至上方「☁️ Google Drive」區塊重新授權後再試。")
            return False
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
        if gdrive_precheck_exists(filename):
            return
        temp_dir = ramdisk_manager.get_scratch_dir()
        out_path = os.path.join(temp_dir, f"gdrive_tmp_{int(time.time()*1000)}_{filename}")
        _, _tgt_label = gdrive_service.get_target_folder()
        is_ram = ramdisk_manager.is_ramdisk_mounted() and temp_dir == ramdisk_manager.MOUNT_POINT
        storage_note = "🚀 RAM Disk 純記憶體封裝" if is_ram else "暫存封裝"
        st.info(f"☁️ 檔案將在{storage_note}後，**直接直送至 Google Drive `{_tgt_label}{filename}`**（零硬碟磨損，不保留於本機）")
    else:
        downloads_dir = st.session_state.get('download_dir', load_download_dir())
        is_writable, write_err = check_dir_writable(downloads_dir)
        if not is_writable:
            show_error_log_box(
                "❌ 無法下載：目標儲存目錄為唯讀或無寫入權限！",
                f"目標路徑: {downloads_dir}\n原因: {write_err}\n\n💡 建議：若為外接硬碟 (如 Windows NTFS)，macOS 預設為唯讀模式無法直接寫入。\n請至上方將儲存路徑切換為 Mac 本機資料夾 (如 Downloads)，或使用支援 macOS 寫入的磁碟格式 (如 APFS、exFAT)。",
                title="目錄寫入權限錯誤 (Read-only)"
            )
            return
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
            cand_m3u8s = [media_url] + [u for u in media_item.get('backup_urls', []) if u != media_url]
            for c_idx, c_url in enumerate(cand_m3u8s):
                try:
                    if c_idx > 0:
                        st.info(f"🔄 嘗試切換至備用串流線路 ({c_idx+1}/{len(cand_m3u8s)}): `{c_url}`")
                    download_fast_parallel_hls(c_url, out_path=out_path, extra_headers=extra_headers, max_workers=16, label="影片")
                    if not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
                        raise ValueError("封裝後輸出檔案不存在或為空")
                    finish_output_file(out_path, filename)
                    return
                except Exception as hls_err:
                    if c_idx == len(cand_m3u8s) - 1:
                        st.warning(f"⚠️ 多線程下載失敗 ({hls_err})，降級使用標準 FFmpeg 串流處理...")

        webpage_url = media_item.get('webpage_url', '')
        is_direct_stream = "m3u8" in media_url or any(media_url.lower().split('?')[0].endswith(ext) for ext in ['.mp4', '.m4a', '.mp3', '.flv', '.ts', '.webm', '.mkv'])
        is_ytdlp_site = media_item.get('is_ytdlp') or any(k in media_url or k in webpage_url for k in ["youtube.com", "youtu.be", "googlevideo.com", "bilibili.com", "b23.tv", "twitter.com", "x.com", "instagram.com", "tiktok.com", "facebook.com", "fb.com", "fb.watch"])

        # 若非純串流檔案（為網頁連結）或為 yt-dlp 原生支援平台（如 YouTube, Bilibili 等），由 yt-dlp 完整負責音訊與視訊下載與合併
        if (is_ytdlp_site or not is_direct_stream) and not force_audio:
            target_url = webpage_url or media_url
            temp_cookie = None
            platform_name = "Bilibili" if any(d in target_url for d in ["bilibili", "b23.tv"]) else ("YouTube" if "youtu" in target_url else "線上")
            try:
                out_base = os.path.splitext(out_path)[0]
                
                # Cookie 設定 (Bilibili / Facebook)
                bili_cookie_str = st.session_state.get('bili_cookie')
                fb_cookie_str = st.session_state.get('fb_cookie')
                cookie_to_use = None
                if any(d in target_url for d in ["bilibili.com", "b23.tv"]) and bili_cookie_str:
                    cookie_to_use = bili_cookie_str
                elif any(d in target_url for d in ["facebook.com", "fb.com", "fb.watch"]) and fb_cookie_str:
                    cookie_to_use = fb_cookie_str
                
                if cookie_to_use:
                    temp_cookie = create_temp_cookiefile(cookie_to_use)

                ydl_opts = {
                    'outtmpl': f"{out_base}.%(ext)s",
                    'quiet': True,
                    'overwrites': True,
                    'nocheckcertificate': True,
                    'legacy_server_connect': True,
                    'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
                    'merge_output_format': 'mp4',
                    'http_headers': {
                        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                    },
                    'extractor_args': {
                        'youtube': {
                            'player_client': ['android', 'web', 'ios'],
                        }
                    },
                }
                if temp_cookie:
                    ydl_opts['cookiefile'] = temp_cookie

                if any(d in target_url for d in ["bilibili.com", "b23.tv"]):
                    ydl_opts['http_headers']['Referer'] = 'https://www.bilibili.com/'

                with st.spinner(f"⏳ 正在透過 yt-dlp 下載 {platform_name} 1080p 高清影片..."):
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
                    show_error_log_box("❌ 下載失敗！無法產生影片檔案。", f"Target Output Path: {out_path}", title=f"{platform_name} 下載失敗", url=target_url)
                    return
            except Exception as yt_err:
                err_str = str(yt_err)
                if any(kw in err_str for kw in ["412", "Precondition Failed", "啥都木有", "KeyError('result')"]):
                    friendly_msg = "❌ Bilibili 存取受阻：觸發嗶哩嗶哩安全風控策略 (HTTP 412) 或該番劇需登入/大會員。\n\n💡 **原因與建議**：\n1. 該影片/番劇可能已下架、地區限制或需要大會員登入。\n2. 請於上方填入 **Bilibili Cookie (SESSDATA)** 以通過身分驗證及風控限制。"
                    show_error_log_box(friendly_msg, traceback.format_exc(), title="Bilibili 412 風控限制", url=target_url)
                else:
                    show_error_log_box(f"❌ {platform_name} 下載失敗: {yt_err}", traceback.format_exc(), title=f"{platform_name} 下載詳細錯誤日誌", url=target_url)
                return
            finally:
                if temp_cookie and os.path.exists(temp_cookie):
                    try:
                        os.remove(temp_cookie)
                    except Exception:
                        pass

        is_valid_url = isinstance(media_url, str) and (media_url.startswith("http://") or media_url.startswith("https://"))
        is_valid_file = isinstance(media_url, str) and os.path.exists(media_url)

        if not is_valid_url and not is_valid_file:
            show_error_log_box(
                "❌ 無法開始下載！媒體網址或檔案路徑無效。",
                f"錯誤傳入的 Input: '{media_url}'\n說明: 該輸入非有效的 HTTP/HTTPS 網址，且本機找不到此檔案。請確認輸入的網址格式（需包含 http:// 或 https://）或本機檔案是否存在。",
                url=media_url
            )
            return

        if is_valid_url:
            parsed_host = urllib.parse.urlparse(media_url).hostname
            if parsed_host:
                try:
                    socket.getaddrinfo(parsed_host, 443 if media_url.startswith("https") else 80, proto=socket.IPPROTO_TCP)
                except Exception:
                    friendly_msg = f"❌ 影片伺服器網域失效：主機名稱 `{parsed_host}` 在網際網路上不存在 (DNS 解析失敗)。\n\n💡 **原因與建議**：該影片來源伺服器已被官方下線或 CDN 網域已過期失效。\n若您是在 Movieffm / Gimy 等影音網站觀看，請回到該網頁**切換其他播放線路**（如 FLV 2、FLV 3、線路 B、海外線路等）再進行下載。"
                    show_error_log_box(friendly_msg, f"Target Host: {parsed_host}\nTarget URL: {media_url}\nError: [Errno 8] nodename nor servname provided, or not known", title="DNS 無效與伺服器已下線診斷", url=media_url)
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
            if any(k in stderr_log for k in ["Failed to resolve hostname", "nodename nor servname provided", "Name or service not known", "Could not resolve host", "Temporary failure in name resolution"]):
                parsed_host = urllib.parse.urlparse(media_url).hostname or "串流伺服器"
                friendly_msg = f"❌ 影片伺服器連線失敗：串流主機網域 `{parsed_host}` 無法解析或已失效。\n\n💡 **原因與建議**：該影片來源伺服器已下線或 CDN 網址已過期。若是在 Movieffm / Gimy 等影音網站觀看，請嘗試切換至其他播放線路 (如 FLV 2, FLV 3...)。"
                show_error_log_box(friendly_msg, stderr_log, title="伺服器連線與 DNS 錯誤日誌", url=media_url)
            elif "Read-only file system" in stderr_log or "read-only" in stderr_log.lower():
                friendly_msg = "❌ 下載失敗：儲存目標目錄為「唯讀 (Read-only)」！\n\n💡 **原因與建議**：您的儲存目錄位於唯讀磁碟（例如 Windows NTFS 格式外接硬碟在 macOS 預設無法寫入）。請更換儲存位置至 Mac 本機硬碟（如「下載」資料夾）或使用支援寫入的磁碟格式 (如 APFS、exFAT)。"
                show_error_log_box(friendly_msg, stderr_log, title="唯讀磁碟寫入錯誤 (Read-only file system)", url=media_url)
            else:
                show_error_log_box("❌ FFmpeg 下載失敗！", stderr_log, url=media_url)
        
        gc.collect()
    except Exception as e:
        err_str = str(e)
        if any(k in err_str for k in ["Failed to resolve hostname", "nodename nor servname provided", "Name or service not known", "Could not resolve host"]):
            parsed_host = urllib.parse.urlparse(media_url).hostname or "串流伺服器"
            friendly_msg = f"❌ 影片伺服器連線失敗：串流主機網域 `{parsed_host}` 無法解析或已失效。\n\n💡 **原因與建議**：該影片來源伺服器已下線或 CDN 網址已過期。請嘗試切換其他播放線路。"
            show_error_log_box(friendly_msg, traceback.format_exc(), title="DNS 解析與連線錯誤日誌", url=media_url)
        else:
            show_error_log_box(f"❌ 發生例外錯誤: {e}", traceback.format_exc(), title="Exception 堆疊追蹤資訊", url=media_url)
        st.caption("Note: 如果看到找不到指令的錯誤，請確認系統已安裝 FFmpeg (`brew install ffmpeg`)。")

def get_audio_stream_info(media_path, headers=None):
    """透過 ffprobe 探測音訊串流資訊：(codec_name, bitrate_kbps, channels)。"""
    headers_arg = []
    if headers:
        headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        headers_arg = ["-headers", headers_str]
    if "m3u8" in media_path:
        headers_arg += ["-allowed_segment_extensions", "ALL", "-extension_picky", "0"]

    probe_cmd = [
        "ffprobe", "-v", "error"
    ] + headers_arg + [
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,bit_rate,channels:format=bit_rate",
        "-of", "json",
        media_path
    ]
    try:
        raw = subprocess.check_output(probe_cmd, text=True, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
        data = json.loads(raw)
        streams = data.get("streams", [])
        codec = ""
        bitrate_kbps = None
        channels = 2
        if streams:
            s = streams[0]
            codec = s.get("codec_name", "").lower()
            channels = int(s.get("channels", 2))
            br = s.get("bit_rate")
            if br and str(br).isdigit():
                bitrate_kbps = int(br) // 1000
        if not bitrate_kbps:
            fmt_br = data.get("format", {}).get("bit_rate")
            if fmt_br and str(fmt_br).isdigit():
                if not streams or len(data.get("streams", [])) == 1:
                    bitrate_kbps = int(fmt_br) // 1000
        return codec, bitrate_kbps, channels
    except Exception:
        return "", None, 2

def extract_local_audio(video_path, audio_format, title=None, headers=None):
    if title:
        base_name = title
    else:
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        
    is_gdrive = st.session_state.get('storage_destination') == "gdrive_cloud"
    if is_gdrive:
        out_dir = ramdisk_manager.get_scratch_dir()
    else:
        out_dir = st.session_state.get('download_dir', load_download_dir())
        is_writable, write_err = check_dir_writable(out_dir)
        if not is_writable:
            show_error_log_box(
                "❌ 無法提取音訊：目標儲存目錄為唯讀或無寫入權限！",
                f"目標路徑: {out_dir}\n原因: {write_err}\n\n💡 建議：若為外接硬碟 (如 Windows NTFS)，macOS 預設為唯讀模式無法直接寫入。\n請至上方將儲存路徑切換為 Mac 本機資料夾 (如 Downloads)，或使用支援 macOS 寫入的磁碟格式 (如 APFS、exFAT)。",
                title="目錄寫入權限錯誤 (Read-only)"
            )
            return
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

    if is_valid_url:
        parsed_host = urllib.parse.urlparse(video_path).hostname
        if parsed_host:
            try:
                socket.getaddrinfo(parsed_host, 443 if video_path.startswith("https") else 80, proto=socket.IPPROTO_TCP)
            except Exception:
                friendly_msg = f"❌ 影片伺服器網域失效：主機名稱 `{parsed_host}` 在網際網路上不存在 (DNS 解析失敗)。\n\n💡 **原因與建議**：該影片來源伺服器已被官方下線或 CDN 網域已過期失效。\n若您是在 Movieffm / Gimy 等影音網站觀看，請回到該網頁**切換其他播放線路**（如 FLV 2、FLV 3、線路 B、海外線路等）再進行音訊提取。"
                show_error_log_box(friendly_msg, f"Target Host: {parsed_host}\nTarget URL: {video_path}\nError: [Errno 8] nodename nor servname provided, or not known", title="DNS 無效與伺服器已下線診斷", url=video_path)
                return

    is_youtube_or_online = is_valid_url and ("m3u8" not in video_path.lower())
    is_mono_96k = "96kbps" in audio_format or "單聲道" in audio_format

    if is_youtube_or_online:
        target_ext = "mp3" if audio_format == "MP3" else ("m4a" if audio_format == "M4A" else "mp4")
        filename = f"{base_name}.{target_ext}"
        expected_out_path = os.path.join(out_dir, filename)
        if not is_gdrive and os.path.exists(expected_out_path):
            st.success(f"⏭️ 檔案已存在: `{expected_out_path}`")
            return
        if is_gdrive and gdrive_precheck_exists(filename):
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
            elif is_mono_96k:
                postprocessors.append({
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'm4a',
                    'preferredquality': '96',
                })
            elif audio_format == "MP4 (AAC音訊 - 相容AI轉錄)" or audio_format == "MP4":
                postprocessors.append({
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'm4a',
                    'preferredquality': '192',
                })
            else:
                # 預設 (原始格式)：保留原始最佳串流封裝為 m4a，不強制位元率重編碼
                postprocessors.append({
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'm4a',
                })

            temp_cookie = None
            bili_cookie_str = st.session_state.get('bili_cookie')
            fb_cookie_str = st.session_state.get('fb_cookie')
            cookie_to_use = None
            if any(d in video_path for d in ["bilibili.com", "b23.tv"]) and bili_cookie_str:
                cookie_to_use = bili_cookie_str
            elif any(d in video_path for d in ["facebook.com", "fb.com", "fb.watch"]) and fb_cookie_str:
                cookie_to_use = fb_cookie_str
            
            if cookie_to_use:
                temp_cookie = create_temp_cookiefile(cookie_to_use)

            ydl_opts = {
                'outtmpl': f"{out_base}.%(ext)s",
                'quiet': True,
                'overwrites': True,
                'nocheckcertificate': True,
                'legacy_server_connect': True,
                'format': 'bestaudio/best',
                'postprocessors': postprocessors,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                },
                'extractor_args': {
                    'youtube': {
                        'player_client': ['android', 'web', 'ios'],
                    }
                },
            }
            if temp_cookie:
                ydl_opts['cookiefile'] = temp_cookie
            if any(d in video_path for d in ["bilibili.com", "b23.tv"]):
                ydl_opts['http_headers']['Referer'] = 'https://www.bilibili.com/'

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
                if target_ext == "mp4":
                    final_mp4_path = os.path.join(out_dir, f"{base_name}.mp4")
                    if is_mono_96k:
                        orig_codec, orig_br_kbps, orig_channels = get_audio_stream_info(matched_file)
                        # 若原檔位元率低於 96kbps，不強制拉高至 96k
                        if orig_br_kbps and orig_br_kbps < 96:
                            if orig_codec == "aac" and orig_channels <= 1:
                                mono_cmd = ["ffmpeg", "-y", "-i", matched_file, "-vn", "-c:a", "copy", "-bsf:a", "aac_adtstoasc", final_mp4_path]
                            else:
                                mono_cmd = ["ffmpeg", "-y", "-i", matched_file, "-vn", "-c:a", "aac", "-ac", "1", "-b:a", f"{orig_br_kbps}k", final_mp4_path]
                        else:
                            mono_cmd = ["ffmpeg", "-y", "-i", matched_file, "-vn", "-c:a", "aac", "-ac", "1", "-b:a", "96k", final_mp4_path]

                        try:
                            subprocess.run(mono_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                            if os.path.exists(matched_file) and matched_file != final_mp4_path:
                                os.remove(matched_file)
                            matched_file = final_mp4_path
                        except Exception:
                            pass
                    elif actual_ext in ["m4a", "aac"]:
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
            err_str = str(yt_err)
            if any(kw in err_str for kw in ["412", "Precondition Failed", "啥都木有", "KeyError('result')"]):
                friendly_msg = "❌ Bilibili 存取受阻：觸發嗶哩嗶哩安全風控策略 (HTTP 412) 或該番劇需登入/大會員。\n\n💡 **原因與建議**：\n1. 該影片/番劇可能已下架、地區限制或需要大會員登入。\n2. 請於「線上影片下載」分頁填入 **Bilibili Cookie (SESSDATA)** 以通過身分驗證及風控限制。"
                show_error_log_box(friendly_msg, traceback.format_exc(), title="Bilibili 412 風控限制", url=video_path)
            else:
                show_error_log_box(f"❌ 線上網址音訊提取失敗: {yt_err}", traceback.format_exc(), title="線上網址音訊提取詳細錯誤日誌", url=video_path)
            return
        finally:
            if temp_cookie and os.path.exists(temp_cookie):
                try:
                    os.remove(temp_cookie)
                except Exception:
                    pass

    ext = ""
    ffmpeg_cmd = []
    out_path = ""
    
    headers_arg = []
    if headers:
        headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        headers_arg = ["-headers", headers_str]
        
    if "m3u8" in video_path:
        headers_arg += ["-allowed_segment_extensions", "ALL", "-extension_picky", "0"]
    
    if is_mono_96k:
        # 96kbps, 單聲道格式 (智慧上限：若原音訊低於 96k 則不膨脹)
        orig_codec, orig_br_kbps, orig_channels = get_audio_stream_info(video_path, headers=headers)
        ext = "mp4"
        out_path = os.path.join(out_dir, f"{base_name}.{ext}")
        if orig_br_kbps and orig_br_kbps < 96:
            if orig_codec == "aac" and orig_channels <= 1:
                ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", video_path, "-vn", "-c:a", "copy", "-bsf:a", "aac_adtstoasc", out_path]
            else:
                ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", video_path, "-vn", "-c:a", "aac", "-ac", "1", "-b:a", f"{orig_br_kbps}k", out_path]
        else:
            ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", video_path, "-vn", "-c:a", "aac", "-ac", "1", "-b:a", "96k", out_path]
    elif audio_format == "MP3":
        ext = "mp3"
        out_path = os.path.join(out_dir, f"{base_name}.{ext}")
        ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", video_path, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", out_path]
    elif audio_format == "M4A":
        ext = "m4a"
        out_path = os.path.join(out_dir, f"{base_name}.{ext}")
        ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", video_path, "-vn", "-c:a", "aac", "-b:a", "192k", out_path]
    elif audio_format == "MP4 (AAC音訊 - 相容AI轉錄)" or audio_format == "MP4":
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
    if is_gdrive and gdrive_precheck_exists(os.path.basename(out_path)):
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
            if any(k in stderr_log for k in ["Failed to resolve hostname", "nodename nor servname provided", "Name or service not known", "Could not resolve host", "Temporary failure in name resolution"]):
                parsed_host = urllib.parse.urlparse(video_path).hostname or "串流伺服器"
                friendly_msg = f"❌ 影片伺服器連線失敗：主機網域 `{parsed_host}` 無法解析或已失效。\n\n💡 **原因與建議**：該影片來源伺服器已下線或 CDN 網址已過期。若是在 Movieffm / Gimy 等影音網站，請嘗試切換至其他播放線路 (如 FLV 2, FLV 3...)。"
                show_error_log_box(friendly_msg, stderr_log, title="伺服器連線與 DNS 錯誤日誌", url=video_path)
            elif "Read-only file system" in stderr_log or "read-only" in stderr_log.lower():
                friendly_msg = f"❌ 提取音訊失敗：儲存目標目錄為「唯讀 (Read-only)」！\n\n💡 **原因與建議**：您的儲存目錄位於唯讀磁碟（例如 Windows NTFS 格式外接硬碟在 macOS 預設無法寫入）。請更換儲存位置至 Mac 本機硬碟（如「下載」資料夾）或使用支援寫入的磁碟格式 (如 APFS、exFAT)。"
                show_error_log_box(friendly_msg, stderr_log, title="唯讀磁碟寫入錯誤 (Read-only file system)", url=video_path)
            else:
                show_error_log_box(f"❌ 提取 `{base_name}` 失敗！", stderr_log, url=video_path)
        
        gc.collect()
    except Exception as e:
        err_str = str(e)
        if any(k in err_str for k in ["Failed to resolve hostname", "nodename nor servname provided", "Name or service not known", "Could not resolve host"]):
            parsed_host = urllib.parse.urlparse(video_path).hostname or "串流伺服器"
            friendly_msg = f"❌ 影片伺服器連線失敗：主機網域 `{parsed_host}` 無法解析或已失效。\n\n💡 **原因與建議**：該影片來源伺服器已下線或 CDN 網址已過期。請嘗試切換其他播放線路。"
            show_error_log_box(friendly_msg, traceback.format_exc(), title="DNS 解析與連線錯誤日誌", url=video_path)
        else:
            show_error_log_box(f"❌ 發生例外錯誤: {e}", traceback.format_exc(), title="Exception 堆疊追蹤資訊", url=video_path)

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

    # 5. 清理 RAM Disk 暫存檔案
    if ramdisk_manager.is_ramdisk_mounted():
        c_ram = ramdisk_manager.clean_scratch_files()
        if c_ram > 0:
            released_info.append(f"🚀 已清理 RAM Disk 暫存區中 {c_ram} 個暫存檔案。")
        else:
            released_info.append("🚀 RAM Disk 暫存區已為空。")

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
    st.session_state.storage_destination = gdrive_service.load_config().get("storage_destination", "local")
if "gdrive_skip_dup" not in st.session_state:
    st.session_state.gdrive_skip_dup = gdrive_service.load_config().get("gdrive_skip_duplicates", True)

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
_DEST_LOCAL = "📁 本機硬碟資料夾 (Local Disk)"
_DEST_GDRIVE = "☁️ Google Drive 雲端直送 (不存本地硬碟)"

def _cb_dest_change():
    mode = "gdrive_cloud" if st.session_state.dest_radio == _DEST_GDRIVE else "local"
    st.session_state.storage_destination = mode
    gdrive_service.update_config(storage_destination=mode)

if "dest_radio" not in st.session_state:
    st.session_state.dest_radio = _DEST_GDRIVE if st.session_state.storage_destination == "gdrive_cloud" else _DEST_LOCAL

st.radio(
    "📦 **儲存目標模式**:",
    options=[_DEST_LOCAL, _DEST_GDRIVE],
    key="dest_radio",
    horizontal=True,
    on_change=_cb_dest_change,
    help="雲端直送：檔案先在系統暫存區完成封裝，再以 Google Drive API 分塊上傳至指定資料夾，上傳後立即刪除本機暫存。選擇會自動記住。",
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
    """首次設定：引導使用者建立 OAuth 用戶端並上傳 credentials.json"""
    st.info("🧭 **首次連接只需做一次**：建立 Google OAuth 用戶端 → 上傳 JSON → 點授權。之後會自動記住登入狀態。")
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
    uploaded_cred = st.file_uploader("上傳 OAuth 用戶端 JSON (client_secret_xxx.json / credentials.json):", type=["json"], key="gd_cred_upload")
    if uploaded_cred:
        raw = uploaded_cred.getvalue()
        ok, msg = gdrive_service.validate_credentials_json(raw)
        if ok:
            with open(gdrive_service.CREDENTIALS_FILE, "wb") as f:
                f.write(raw)
            st.session_state.pending_toast = ("✅ credentials.json 已儲存，請點擊授權按鈕", "🔑")
            st.rerun()
        else:
            st.error(f"❌ 檔案格式不正確：{msg}")


def _render_gdrive_folder_picker():
    """雲端目標資料夾選擇器：瀏覽 / 建立 / 貼上網址"""
    if "gd_browse_stack" not in st.session_state:
        st.session_state.gd_browse_stack = [("root", "我的雲端硬碟")]
    stack = st.session_state.gd_browse_stack
    cur_id, _ = stack[-1]
    cur_label = "/" + "/".join(n for _, n in stack[1:]) + ("/" if len(stack) > 1 else "")

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
                "子資料夾", options=list(range(len(subs))), format_func=lambda i: f"📁 {names[i]}",
                index=None, placeholder="(此層無子資料夾)" if not subs else "選擇子資料夾…",
                label_visibility="collapsed", key=f"gd_pick_{cur_id}",
            )
        with b2:
            if st.button("➡️ 進入", use_container_width=True, disabled=picked is None, key="gd_enter"):
                stack.append((subs[picked]["id"], subs[picked]["name"]))
                st.rerun()

        a1, a2, a3 = st.columns(3)
        with a1:
            if st.button("⬆️ 上一層", use_container_width=True, disabled=len(stack) <= 1, key="gd_up"):
                stack.pop()
                st.rerun()
        with a2:
            if st.button("✅ 存到「目前位置」", type="primary", use_container_width=True, key="gd_set_here"):
                _gd_set_target(cur_id, cur_label)
                st.rerun()
        with a3:
            if st.button("↩️ 重設為 /Download/", use_container_width=True, key="gd_reset"):
                _gd_set_target(None, f"/{gdrive_service.DEFAULT_FOLDER_NAME}/")
                st.rerun()

        n1, n2 = st.columns([3, 1])
        with n1:
            new_name = st.text_input("新資料夾名稱", placeholder="在目前位置建立新資料夾，例如：影片/2026", label_visibility="collapsed", key="gd_new_name")
        with n2:
            if st.button("➕ 建立並設為目標", use_container_width=True, disabled=not (new_name or "").strip(), key="gd_create"):
                try:
                    parent_id, parent_label = cur_id, cur_label
                    # 支援 "A/B" 一次建立多層
                    for part in [p.strip() for p in new_name.split("/") if p.strip()]:
                        existing = next((f for f in gdrive_service.list_subfolders(parent_id) if f["name"] == part), None)
                        node = existing or gdrive_service.create_folder(part, parent_id)
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
        if st.button("✅ 設為目標資料夾", use_container_width=True, disabled=not url_in, key="gd_url_set"):
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


if st.session_state.storage_destination == "gdrive_cloud":
    cred_path = gdrive_service.get_credentials_path()
    is_auth = gdrive_service.is_authenticated()
    user_email = gdrive_service.get_connected_account_email() if is_auth else None

    if is_auth and user_email:
        target_id, target_label = gdrive_service.get_target_folder()
        folder_link = gdrive_service.get_folder_link(target_id) if target_id else "https://drive.google.com/drive/my-drive"
        col_g1, col_g2 = st.columns([4, 1])
        with col_g1:
            st.success(f"🟢 **已連接**：`{user_email}`\n\n📂 **上傳至**：[我的雲端硬碟 `{target_label}`]({folder_link})")
        with col_g2:
            if st.button("🚪 登出", use_container_width=True):
                gdrive_service.revoke_gdrive_auth()
                st.rerun()

        def _cb_skip_dup():
            gdrive_service.update_config(gdrive_skip_duplicates=st.session_state.gdrive_skip_dup)

        st.checkbox("⏭️ 雲端已有同名檔案時自動跳過 (下載前先檢查，節省流量)", key="gdrive_skip_dup", on_change=_cb_skip_dup)

        with st.expander("📂 變更雲端目標資料夾", expanded=False):
            _render_gdrive_folder_picker()
    else:
        if is_auth and not user_email:
            st.warning("⚠️ Google Drive 授權已失效 (Token 過期或被撤銷)，請重新授權。")
        elif cred_path:
            st.warning("⚠️ 尚未連接 Google Drive 帳號。")

        if cred_path:
            c_a1, c_a2 = st.columns([3, 1])
            with c_a1:
                do_auth = st.button("🔑 授權連接 Google Drive (開啟瀏覽器登入)", type="primary", use_container_width=True)
            with c_a2:
                if st.button("🗑️ 更換 JSON", use_container_width=True, help="刪除目前的 credentials.json 重新上傳"):
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
                    st.session_state.pending_toast = ("✅ Google Drive 授權成功！", "☁️")
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

    # 即時檢測當前儲存路徑是否具備寫入權限
    current_target_dir = st.session_state.get('download_dir', load_download_dir())
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

# 2. 4GB RAM Disk 純記憶體模式控制列
ram_mounted = ramdisk_manager.is_ramdisk_mounted()
col_ram1, col_ram2 = st.columns([3, 1])
with col_ram1:
    if ram_mounted:
        tot, used, free, pct = ramdisk_manager.get_ramdisk_usage()
        st.success(
            f"🚀 **4GB APFS RAM Disk 運行中 (100% 零 SSD 磨損)**\n\n"
            f"容量：剩餘 `{free/1024/1024/1024:.2f} GB` / `{tot/1024/1024/1024:.2f} GB` (已用 {pct*100:.1f}%) ｜ 掛載點：`{ramdisk_manager.MOUNT_POINT}`"
        )
    else:
        st.info(
            "💡 **4GB RAM Disk 未掛載**：目前使用 macOS 系統 SSD 暫存。\n\n"
            "建議點右側掛載 4GB APFS RAM Disk，切片封裝將全數在記憶體進行，達成 100% 零硬碟讀寫與極速封裝。"
        )

with col_ram2:
    st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
    if ram_mounted:
        if st.button("⏏️ 卸載 RAM Disk", use_container_width=True, help="立即歸還 4GB 記憶體給 macOS 系統"):
            ok, msg = ramdisk_manager.unmount_ramdisk()
            if ok:
                st.session_state.pending_toast = ("✅ 已卸載 RAM Disk 並歸還 4GB 記憶體", "⏏️")
            else:
                st.error(msg)
            st.rerun()
    else:
        if st.button("🚀 掛載 4GB RAM Disk", type="primary", use_container_width=True, help="免 root 建立 4GB APFS 虛擬記憶體磁碟"):
            with st.spinner("正在向 macOS 申請劃分 4GB APFS RAM Disk..."):
                ok, msg = ramdisk_manager.mount_ramdisk(size_gb=4)
                if ok:
                    st.session_state.pending_toast = ("✅ 4GB APFS RAM Disk 掛載成功！", "🚀")
                else:
                    st.error(msg)
                st.rerun()

# 3. 系統資源清理控制列 (並排按鈕)
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
            ramdisk_manager.unmount_ramdisk()
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

tab1, tab2, tab3 = st.tabs(["🌐 線上影片下載", "📁 影片音訊提取", "🔄 媒體格式轉換"])

with tab1:
    st.markdown("將 Movieffm, Gimymax, MissAV, Bilibili, X, YouTube, Facebook, IG, TikTok 等影片網址直接下載。")
    
    target_urls = st.text_area("🔗 請輸入影片網址 (每行一個):", placeholder="https://www.bilibili.com/video/BV... \nhttps://www.movieffm.net/movies/your-name/ \nhttps://gimymax.com/ep/... \nhttps://youtube.com/watch?v=... \nhttps://www.facebook.com/watch/?v=...")
    
    c_cookie1, c_cookie2 = st.columns(2)
    with c_cookie1:
        fb_cookie_str = st.text_input("🔑 Facebook Cookie (選填):", type="password", placeholder="c_user=xxxx; xs=xxxx; ...", help="若要下載私密社團、好友貼文或無法下載相片時填入。")
        st.session_state.fb_cookie = fb_cookie_str
    with c_cookie2:
        bili_cookie_str = st.text_input("🔑 Bilibili Cookie (選填):", type="password", placeholder="SESSDATA=xxxx; buvid3=xxxx; ...", help="若下載 Bilibili 遇 412 風控限制、番劇或需大會員/高畫質時填入。")
        st.session_state.bili_cookie = bili_cookie_str
    
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
        elif st.session_state.get('storage_destination') == "gdrive_cloud" and not gdrive_service.is_authenticated():
            st.error("❌ **尚未連接 Google Drive**：請先在上方完成授權，或切換回「📁 本機硬碟資料夾」模式。")
        elif st.session_state.get('storage_destination') == "local" and not check_dir_writable(st.session_state.get('download_dir', load_download_dir()))[0]:
            target_check_dir = st.session_state.get('download_dir', load_download_dir())
            _, reason = check_dir_writable(target_check_dir)
            st.error(f"❌ **無法開始下載：目標儲存目錄為唯讀或無寫入權限！**\n\n📍 路徑：`{target_check_dir}`\n⚠️ 原因：{reason}\n\n💡 請切換至本機可寫入的資料夾（如 Downloads）後再下載。")
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
    audio_format = st.selectbox("🎵 請選擇輸出音訊格式:", ["預設 (原始格式 - AAC一律存為MP4)", "96kbps, 單聲道 (MP4 - AI轉錄推薦)", "MP4 (AAC音訊 - 相容AI轉錄)", "MP3", "M4A"])
    
    if st.button("▶️ 開始提取音訊", type="primary", use_container_width=True):
        raw_input_path = local_video_path.strip()
        input_path = normalize_input_url(raw_input_path)
        if not input_path:
            st.warning("⚠️ 請先輸入路徑！")
        elif st.session_state.get('storage_destination') == "gdrive_cloud" and not gdrive_service.is_authenticated():
            st.error("❌ **尚未連接 Google Drive**：請先在上方完成授權，或切換回「📁 本機硬碟資料夾」模式。")
        elif st.session_state.get('storage_destination') == "local" and not check_dir_writable(st.session_state.get('download_dir', load_download_dir()))[0]:
            target_check_dir = st.session_state.get('download_dir', load_download_dir())
            _, reason = check_dir_writable(target_check_dir)
            st.error(f"❌ **無法開始提取：目標儲存目錄為唯讀或無寫入權限！**\n\n📍 路徑：`{target_check_dir}`\n⚠️ 原因：{reason}\n\n💡 請切換至本機可寫入的資料夾（如 Downloads）後再試。")
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

with tab3:
    st.markdown("支援 **圖片 (JPEG/PNG/WEBP)**、**視訊 (MP4/MOV/MKV/GIF)**、**音訊 (MP3/WAV/M4A/FLAC)** 的本地高效格式轉換，支援單檔與整批資料夾批次處理。")

    conv_category = st.radio("📂 選擇轉換類型", ["🖼️ 圖片轉換", "🎬 影片轉換", "🎵 音訊轉換"], horizontal=True)
    source_mode = st.radio("📥 來源方式", ["📤 上傳檔案", "📁 本機路徑 / 資料夾批次"], horizontal=True)

    out_dir = st.session_state.get('download_dir', load_download_dir())
    st.caption(f"💾 轉換輸出儲存目錄：`{out_dir}`")

    # 1. 圖片轉換
    if conv_category == "🖼️ 圖片轉換":
        col_img1, col_img2 = st.columns(2)
        with col_img1:
            img_target_fmt = st.selectbox("目標格式", ["JPEG (.jpg)", "PNG (.png)", "WEBP (.webp)"], index=0)
            img_fmt_clean = "JPEG" if "JPEG" in img_target_fmt else ("PNG" if "PNG" in img_target_fmt else "WEBP")
        with col_img2:
            img_quality = st.slider("壓縮品質 (Quality)", min_value=10, max_value=100, value=95, step=5, help="針對 JPEG / WEBP 格式有效。")

        with st.expander("🛠️ 進階圖片處理選項", expanded=False):
            col_opt1, col_opt2 = st.columns(2)
            with col_opt1:
                bg_option = st.selectbox("透明背景填補色 (轉 JPEG 時必備)", ["白色 (預設)", "黑色", "自訂透明不補色 (限 PNG/WEBP)"])
                bg_color = (255, 255, 255) if bg_option.startswith("白色") else ((0, 0, 0) if bg_option.startswith("黑色") else (255, 255, 255))
            with col_opt2:
                resize_opt = st.selectbox("最大邊長限制 (等比例縮放)", ["維持原尺寸", "1920 px (Full HD)", "1280 px (HD)", "800 px (縮圖)"])
                max_dim_map = {"維持原尺寸": None, "1920 px (Full HD)": 1920, "1280 px (HD)": 1280, "800 px (縮圖)": 800}
                max_dim = max_dim_map.get(resize_opt)

        if source_mode == "📤 上傳檔案":
            uploaded_files = st.file_uploader(
                "選擇或拖曳圖片檔案 (可多選，支援 PNG, WEBP, BMP, HEIC, TIFF, JPG...)",
                type=["png", "webp", "bmp", "tiff", "tif", "jpg", "jpeg", "heic", "heif", "avif", "gif"],
                accept_multiple_files=True
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
                            ext_clean = "jpg" if img_fmt_clean == "JPEG" else img_fmt_clean.lower()
                            dest_path = media_converter.get_safe_output_path(out_dir, base_n, ext_clean)
                            temp_in = os.path.join(tempfile.gettempdir(), uf.name)
                            try:
                                with open(temp_in, "wb") as f_tmp:
                                    f_tmp.write(uf.getbuffer())
                                ok, msg = media_converter.convert_image(
                                    temp_in, dest_path, target_format=img_fmt_clean,
                                    quality=img_quality, bg_color=bg_color, max_dimension=max_dim
                                )
                                if ok:
                                    success_count += 1
                                    st.write(f"✅ `{uf.name}` ➔ `{os.path.basename(dest_path)}`")
                                else:
                                    st.error(f"❌ `{uf.name}`: {msg}")
                            finally:
                                if os.path.exists(temp_in):
                                    try:
                                        os.remove(temp_in)
                                    except Exception:
                                        pass
                            prog_bar.progress((idx + 1) / len(uploaded_files))
                        st.success(f"🎉 圖片轉換完成！成功 {success_count}/{len(uploaded_files)} 個檔案，已儲存至：`{out_dir}`")
        else:
            in_path = st.text_input("📁 請輸入本地圖片檔案或資料夾路徑:", placeholder="/Users/ericcheng/Pictures/my_photos")
            is_recursive = st.checkbox("包含子資料夾內的所有圖片", value=True)
            if in_path and os.path.exists(in_path):
                if os.path.isfile(in_path):
                    target_list = [in_path]
                else:
                    target_list = media_converter.scan_files_for_conversion(in_path, category="image", recursive=is_recursive)
                st.caption(f"ℹ️ 偵測到 **{len(target_list)}** 個符合的圖片檔案。")
                if target_list and st.button("🚀 開始批次轉換圖片", type="primary", use_container_width=True):
                    is_writable, write_err = check_dir_writable(out_dir)
                    if not is_writable:
                        st.error(f"❌ 目標目錄無寫入權限：{write_err}")
                    else:
                        prog_bar = st.progress(0)
                        success_count = 0
                        for idx, f_path in enumerate(target_list):
                            base_n = os.path.splitext(os.path.basename(f_path))[0]
                            ext_clean = "jpg" if img_fmt_clean == "JPEG" else img_fmt_clean.lower()
                            dest_path = media_converter.get_safe_output_path(out_dir, base_n, ext_clean)
                            ok, msg = media_converter.convert_image(
                                f_path, dest_path, target_format=img_fmt_clean,
                                quality=img_quality, bg_color=bg_color, max_dimension=max_dim
                            )
                            if ok:
                                success_count += 1
                                st.write(f"✅ `{os.path.basename(f_path)}` ➔ `{os.path.basename(dest_path)}`")
                            else:
                                st.error(f"❌ `{os.path.basename(f_path)}`: {msg}")
                            prog_bar.progress((idx + 1) / len(target_list))
                        st.success(f"🎉 圖片批次轉換完成！成功 {success_count}/{len(target_list)} 個檔案，已儲存至：`{out_dir}`")
            elif in_path:
                st.warning("⚠️ 輸入的路徑不存在，請確認路徑正確。")

    # 2. 影片轉換
    elif conv_category == "🎬 影片轉換":
        col_v1, col_v2 = st.columns(2)
        with col_v1:
            vid_target_fmt = st.selectbox("目標格式", ["MP4 (廣泛相容)", "MOV (Apple / 剪輯)", "MKV (多軌封裝)", "GIF (高品質動圖)", "WEBM (網頁串流)"])
            vid_fmt_clean = vid_target_fmt.split()[0].upper()
        with col_v2:
            vid_mode_opt = st.selectbox(
                "轉檔模式",
                ["⚡ 智慧加速 (相容則無損拷貝，不相容則 VideoToolbox 硬解)", "🚀 純無損封裝 (Remux - 極速零失真)", "🛠️ 重新編碼 (Transcode - 相容性高)"]
            )
            mode_key = "smart" if vid_mode_opt.startswith("⚡") else ("remux" if vid_mode_opt.startswith("🚀") else "transcode")

        with st.expander("🛠️ 進階影片選項", expanded=False):
            col_vo1, col_vo2 = st.columns(2)
            with col_vo1:
                res_opt = st.selectbox("解析度調整", ["維持原解析度", "1080p", "720p", "480p"])
                res_key = "original" if res_opt.startswith("維持") else res_opt
            with col_vo2:
                q_opt = st.selectbox("畫質/位元率配置", ["高品質 (High)", "標準 (Medium)", "輕巧/低碼率 (Low)"])
                q_key = "high" if "High" in q_opt else ("medium" if "Medium" in q_opt else "low")

        if source_mode == "📤 上傳檔案":
            uploaded_videos = st.file_uploader(
                "選擇或拖曳影片檔案 (支援 MP4, MKV, MOV, AVI, WEBM, FLV, TS...)",
                type=["mp4", "mkv", "mov", "avi", "webm", "flv", "ts", "m4v"],
                accept_multiple_files=True
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
                            dest_path = media_converter.get_safe_output_path(out_dir, base_n, vid_fmt_clean.lower())
                            temp_in = os.path.join(tempfile.gettempdir(), uv.name)
                            try:
                                with open(temp_in, "wb") as f_tmp:
                                    f_tmp.write(uv.getbuffer())
                                with st.spinner(f"正在轉換 {uv.name}..."):
                                    ok, msg = media_converter.convert_video(
                                        temp_in, dest_path, target_format=vid_fmt_clean,
                                        mode=mode_key, resolution=res_key, quality_preset=q_key
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
            in_vid_path = st.text_input("📁 請輸入本地影片檔案或資料夾路徑:", placeholder="/Users/ericcheng/Movies")
            is_vid_recursive = st.checkbox("包含子資料夾內的所有影片", value=True)
            if in_vid_path and os.path.exists(in_vid_path):
                if os.path.isfile(in_vid_path):
                    vid_list = [in_vid_path]
                else:
                    vid_list = media_converter.scan_files_for_conversion(in_vid_path, category="video", recursive=is_vid_recursive)
                st.caption(f"ℹ️ 偵測到 **{len(vid_list)}** 個符合的影片檔案。")
                if vid_list and st.button("🚀 開始批次轉換影片", type="primary", use_container_width=True):
                    is_writable, write_err = check_dir_writable(out_dir)
                    if not is_writable:
                        st.error(f"❌ 目標目錄無寫入權限：{write_err}")
                    else:
                        prog_bar = st.progress(0)
                        for idx, v_path in enumerate(vid_list):
                            base_n = os.path.splitext(os.path.basename(v_path))[0]
                            dest_path = media_converter.get_safe_output_path(out_dir, base_n, vid_fmt_clean.lower())
                            with st.spinner(f"正在轉換 `{os.path.basename(v_path)}`..."):
                                ok, msg = media_converter.convert_video(
                                    v_path, dest_path, target_format=vid_fmt_clean,
                                    mode=mode_key, resolution=res_key, quality_preset=q_key
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
            aud_target_fmt = st.selectbox("目標格式", ["MP3 (泛用通用)", "WAV (無損/剪輯推薦)", "M4A (AAC 高音質)", "FLAC (無損壓縮)", "OGG"])
            aud_fmt_clean = aud_target_fmt.split()[0].upper()
        with col_a2:
            preset_aud = st.selectbox("場景預設", ["標準音質 (192 kbps)", "超高音質 (320 kbps)", "輕巧音質 (128 kbps)", "🎙️ 語音辨識/Whisper 專用 (16kHz 單聲道 WAV)"])

        sample_rate = 16000 if "Whisper" in preset_aud else None
        channels = 1 if "Whisper" in preset_aud else None
        if "Whisper" in preset_aud:
            aud_fmt_clean = "WAV"
            aud_br = "128k"
        else:
            aud_br = "320k" if "320" in preset_aud else ("128k" if "128" in preset_aud else "192k")

        if source_mode == "📤 上傳檔案":
            uploaded_auds = st.file_uploader(
                "選擇或拖曳音訊/影片檔案 (直接提取音訊並轉檔)",
                type=["mp3", "wav", "m4a", "flac", "aac", "ogg", "mp4", "mkv", "mov", "webm"],
                accept_multiple_files=True
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
                            dest_path = media_converter.get_safe_output_path(out_dir, base_n, aud_fmt_clean.lower())
                            temp_in = os.path.join(tempfile.gettempdir(), ua.name)
                            try:
                                with open(temp_in, "wb") as f_tmp:
                                    f_tmp.write(ua.getbuffer())
                                ok, msg = media_converter.convert_audio(
                                    temp_in, dest_path, target_format=aud_fmt_clean,
                                    audio_bitrate=aud_br, sample_rate=sample_rate, channels=channels
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
            in_aud_path = st.text_input("📁 請輸入本地音訊或影片檔案/資料夾路徑:", placeholder="/Users/ericcheng/Music")
            is_aud_recursive = st.checkbox("包含子資料夾內的所有音訊/影片", value=True)
            if in_aud_path and os.path.exists(in_aud_path):
                if os.path.isfile(in_aud_path):
                    aud_list = [in_aud_path]
                else:
                    aud_list = media_converter.scan_files_for_conversion(in_aud_path, category="all", recursive=is_aud_recursive)
                st.caption(f"ℹ️ 偵測到 **{len(aud_list)}** 個可處理檔案。")
                if aud_list and st.button("🚀 開始批次轉換音訊", type="primary", use_container_width=True):
                    is_writable, write_err = check_dir_writable(out_dir)
                    if not is_writable:
                        st.error(f"❌ 目標目錄無寫入權限：{write_err}")
                    else:
                        prog_bar = st.progress(0)
                        for idx, a_path in enumerate(aud_list):
                            base_n = os.path.splitext(os.path.basename(a_path))[0]
                            dest_path = media_converter.get_safe_output_path(out_dir, base_n, aud_fmt_clean.lower())
                            ok, msg = media_converter.convert_audio(
                                a_path, dest_path, target_format=aud_fmt_clean,
                                audio_bitrate=aud_br, sample_rate=sample_rate, channels=channels
                            )
                            if ok:
                                st.write(f"✅ {msg}")
                            else:
                                st.error(f"❌ `{os.path.basename(a_path)}`: {msg}")
                            prog_bar.progress((idx + 1) / len(aud_list))
                        st.success(f"🎉 音訊批次轉換完成！儲存目錄：`{out_dir}`")
            elif in_aud_path:
                st.warning("⚠️ 輸入的路徑不存在，請確認路徑正確。")
