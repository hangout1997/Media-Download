# -*- coding: utf-8 -*-
"""
Core Media Downloader Engine
跨平台核心下載引擎 (無 Streamlit 依賴，相容 本地端 與 Google Colab 雲端執行)
包含：Movieffm / Gimy / Bilibili / YouTube / Facebook / IG / TikTok 等完整解析器、
廣告切片智慧過濾、平行高速 HLS 下載與音訊提取。
"""

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

def _log_info(msg):
    # 若在 Streamlit 環境下且有作用中的 Context 則優先呼叫 st.info，否則直接 print
    try:
        import streamlit as _st
        if _st.runtime.exists():
            _st.info(msg)
            return
    except Exception:
        pass
    print(f"ℹ️ {msg}")

def _log_toast(msg, icon="ℹ️"):
    try:
        import streamlit as _st
        if _st.runtime.exists():
            _st.toast(msg, icon=icon)
            return
    except Exception:
        pass
    print(f"{icon} {msg}")

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


def get_media_items(url, cookies=None):
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

            # 嘗試直接獲取 m3u8 選單內容，免去後續重複請求
            m3u8_text = None
            try:
                r_m3u8 = curl_requests.get(m3u8_url, headers={
                    'Referer': url,
                    'Origin': 'https://missav.ai',
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                }, impersonate='chrome124', timeout=15)
                if r_m3u8.status_code == 200 and r_m3u8.text:
                    m3u8_text = r_m3u8.text
            except Exception:
                pass

            items.append({
                'url': m3u8_url,
                'title': title,
                'ext': 'mp4',
                'type': 'video',
                'headers': {
                    'Referer': url,
                    'Origin': 'https://missav.ai',
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                },
                'm3u8_content': m3u8_text
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
            fb_c = cookies.get("fb_cookie") if cookies else None
            if fb_c:
                cookie_str = fb_c.strip()
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
                _log_info(f"📸 偵測到多張相片合集 ({set_param})，正在透過基礎行動版擷取完整的相片清單...")
                
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
                            _log_info(f"🔗 成功找到 {len(photo_links)} 張相片的連結，開始平行解析高清原始圖...")

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
        
        fb_cookie_str = cookies.get('fb_cookie') if cookies else None
        bili_cookie_str = cookies.get('bili_cookie') if cookies else None
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
        # 策略 4: 檢查是否為劇集目錄/介紹頁面 (例如 /vod/240147.html 或 /detail/xxx.html)，提取其下所有集數播放連結
        from urllib.parse import urljoin
        
        # 優先尋找播放列表區塊 (如 class="playlist-grid" 或 class="content_playlist" 等)
        playlist_matches = re.findall(r'<(?:div|ul)[^>]+class=[\"\'][^\"\']*(?:playlist-grid|content_playlist|playlist_full|play_list)[^\"\']*[\"\'][^>]*>(.*?)</(?:div|ul)>', response.text, re.DOTALL | re.IGNORECASE)
        search_html = playlist_matches[0] if playlist_matches else response.text
        
        raw_ep_matches = re.findall(r'<a[^>]+href=[\"\']((?:/ep/|/play/|/vod-play-id-)[^\"\']+)[\"\'][^>]*>(.*?)</a>', search_html, re.DOTALL)
        if not raw_ep_matches and playlist_matches:
            raw_ep_matches = re.findall(r'<a[^>]+href=[\"\']((?:/ep/|/play/|/vod-play-id-)[^\"\']+)[\"\'][^>]*>(.*?)</a>', response.text, re.DOTALL)
        
        if raw_ep_matches:
            seen_hrefs = set()
            unique_ep_urls = []
            
            # 若有明確的 -1-, -2- 線路標記，優先鎖定出現次數最多的首條線路 (如 -1-)
            line_ids = [re.search(r'-(\d+)-\d+\.html', href).group(1) for href, _ in raw_ep_matches if re.search(r'-(\d+)-\d+\.html', href)]
            target_line = line_ids[0] if line_ids else None
            
            for href, _ in raw_ep_matches:
                full_ep_url = urljoin(url, href)
                if target_line and f"-{target_line}-" not in href and re.search(r'-\d+-\d+\.html', href):
                    continue
                if full_ep_url not in seen_hrefs:
                    seen_hrefs.add(full_ep_url)
                    unique_ep_urls.append(full_ep_url)
            
            if unique_ep_urls:
                _log_info(f"📺 偵測到劇集目錄/選單頁面，共解析出 {len(unique_ep_urls)} 個集數，正在併發解析各集串流...")
                
                def _fetch_single_ep(ep_u):
                    try:
                        return get_media_items(ep_u, cookies=cookies)
                    except Exception as e:
                        _log_info(f"⚠️ 解析單集失敗 ({ep_u}): {e}")
                        return []

                playlist_items = []
                with ThreadPoolExecutor(max_workers=min(10, max(1, len(unique_ep_urls)))) as executor:
                    for ep_result in executor.map(_fetch_single_ep, unique_ep_urls):
                        if ep_result:
                            playlist_items.extend(ep_result)
                
                if playlist_items:
                    return playlist_items

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



def run_ffmpeg_with_progress(ffmpeg_cmd, total_duration=0.0, label="下載", progress_cb=None):
    """
    執行 FFmpeg 並監聽 progress 輸出。
    progress_cb: 可選 callback(pct: float, current_sec: float, total_sec: float, speed: str)
    """
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
            else:
                pct = 0.0
            if progress_cb:
                progress_cb(pct, current_sec, total_duration, speed)

    process.wait()

    if total_duration > 0 and progress_cb:
        progress_cb(1.0, total_duration, total_duration, speed)

    stderr_log = "\n".join(stderr_lines)
    return process.returncode, stderr_log


def download_fast_parallel_hls(m3u8_url, out_path=None, extra_headers=None, max_workers=None, label="影片", progress_cb=None, scratch_dir=None, m3u8_content=None):
    """
    平行 HLS 下載引擎（全量 RAM 快取模式，自動過濾片頭賭博/貼片廣告）。
    progress_cb: 可選 callback(pct: float, completed_dur: float, total_dur: float, speed_mb: float, speed_x: float)
    scratch_dir: 封裝中繼目錄 (預設為 None，將依序嘗試 RAM Disk 或系統暫存)
    m3u8_content: 可選預先抓取的 m3u8 內容 (若提供則完全免除對 m3u8 的 HTTP 請求，100% 免疫 WAF 阻擋)
    """
    from curl_cffi import requests as curl_requests

    req_headers = dict(_GLOBAL_HEADERS)
    if extra_headers:
        req_headers.update(extra_headers)
    if 'Referer' not in req_headers:
        req_headers['Referer'] = f"https://{urllib.parse.urlparse(m3u8_url).netloc}/"
    if 'surrit' in m3u8_url or 'missav' in req_headers.get('Referer', ''):
        req_headers['Origin'] = 'https://missav.ai'
        if 'Referer' not in req_headers or not req_headers['Referer']:
            req_headers['Referer'] = 'https://missav.ai/'
        req_headers['Sec-Fetch-Dest'] = 'empty'
        req_headers['Sec-Fetch-Mode'] = 'cors'
        req_headers['Sec-Fetch-Site'] = 'cross-site'

    session = _global_session
    thread_local = threading.local()

    def get_curl_session():
        if not hasattr(thread_local, "curl_session"):
            thread_local.curl_session = curl_requests.Session(impersonate="chrome124")
        return thread_local.curl_session

    def fetch_text(url):
        # 優先嘗試 curl_cffi 模擬 Chrome 124 TLS 指紋 (免疫 Cloudflare WAF / 403 阻擋)
        err_details = []
        try:
            r = get_curl_session().get(url, headers=req_headers, timeout=30)
            if r.status_code == 200 and r.text:
                return r.text
            err_details.append(f"curl_cffi HTTP {r.status_code}")
        except Exception as e:
            err_details.append(f"curl_cffi: {e}")
        try:
            r = session.get(url, headers=req_headers, timeout=30)
            if r.status_code == 200 and r.text:
                return r.text
            err_details.append(f"requests HTTP {r.status_code}")
        except Exception as e:
            err_details.append(f"requests: {e}")
        diag = "; ".join(err_details)
        raise ValueError(f"無法讀取 m3u8 串流選單 ({url}): {diag}")

    # 若已預先提供有效 m3u8 內容，直接跳過網路請求 (避免雲端節點被 Cloudflare 403 阻擋)
    if m3u8_content and (m3u8_content.lstrip().startswith("#EXT") or "#EXTM3U" in m3u8_content or "#EXT-X-" in m3u8_content):
        text = m3u8_content
    else:
        text = fetch_text(m3u8_url)

    clean_text = text.lstrip('\ufeff \t\r\n')
    if not (clean_text.startswith("#EXT") or "#EXTM3U" in clean_text or "#EXT-X-" in clean_text):
        raise ValueError(f"伺服器回傳非有效的 m3u8 內容 (可能為網頁阻擋或無效連結): {clean_text[:120]}")

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
            sub_playlists.sort(key=lambda x: (x[0], x[1]), reverse=True)
            m3u8_url = sub_playlists[0][2]
            text = fetch_text(m3u8_url)
            clean_sub = text.lstrip('\ufeff \t\r\n')
            if not (clean_sub.startswith("#EXT") or "#EXTM3U" in clean_sub or "#EXT-X-" in clean_sub):
                raise ValueError(f"取得之子播放清單非有效 m3u8: {clean_sub[:120]}")

    segment_urls, segment_durations, clean_m3u8_text, ad_count, ad_dur = filter_and_clean_m3u8_ads(text, m3u8_url)
    if ad_count > 0:
        _log_toast(f"🛡️ 已自動過濾 {ad_count} 個廣告切片 ({ad_dur:.1f} 秒賭博/貼片廣告)，為您保留純淨正片！", icon="🛡️")

    total_segments = len(segment_urls)
    if total_segments == 0:
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

    cpu_count = os.cpu_count() or 4
    if max_workers is None:
        max_workers = min(32, total_segments, cpu_count * 4)
    else:
        max_workers = min(max_workers, total_segments)

    segments_data = [b""] * total_segments

    is_cf_protected = any(k in m3u8_url for k in ["surrit", "ppqrrs", "play-cdn", "bfikun"]) or "missav" in req_headers.get("Referer", "") or "missav" in req_headers.get("Origin", "")
    seg_fail_sample = []

    def download_segment(args):
        idx, seg_url = args
        for attempt in range(3):
            if is_cf_protected:
                try:
                    r = get_curl_session().get(seg_url, headers=req_headers, timeout=20)
                    if r.status_code == 200 and len(r.content) > 0:
                        return idx, r.content
                    elif r.status_code != 200 and len(seg_fail_sample) < 3:
                        seg_fail_sample.append(f"HTTP {r.status_code}")
                except Exception as e:
                    if len(seg_fail_sample) < 3:
                        seg_fail_sample.append(f"curl_cffi: {e}")
            try:
                r = session.get(seg_url, headers=req_headers, timeout=15)
                if r.status_code == 200 and len(r.content) > 0:
                    return idx, r.content
                elif r.status_code != 200 and len(seg_fail_sample) < 3:
                    seg_fail_sample.append(f"requests HTTP {r.status_code}")
            except Exception as e:
                if len(seg_fail_sample) < 3:
                    seg_fail_sample.append(f"requests: {e}")
            try:
                r = get_curl_session().get(seg_url, headers=req_headers, timeout=20)
                if r.status_code == 200 and len(r.content) > 0:
                    return idx, r.content
            except Exception:
                pass
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
                else:
                    pct = completed / total_segments
                if progress_cb:
                    progress_cb(pct, completed_duration, total_duration, speed_mb, speed_x)

    valid_count = sum(1 for c in segments_data if len(c) > 0)
    if total_segments > 0 and valid_count < total_segments * 0.7:
        fail_diag = f" (切片連線回傳: {'; '.join(seg_fail_sample)})" if seg_fail_sample else ""
        raise ValueError(f"切片下載成功率不足 (成功 {valid_count}/{total_segments})，來源伺服器防護阻擋了切片連線{fail_diag}。")

    if scratch_dir is None:
        try:
            import ramdisk_manager
            scratch_dir = ramdisk_manager.get_scratch_dir()
        except Exception:
            scratch_dir = tempfile.gettempdir()

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
            err_raw = proc.stderr.decode('utf-8', errors='ignore')
            err_lines = [
                l.strip() for l in err_raw.splitlines()
                if l.strip() and not any(l.strip().startswith(k) for k in [
                    "ffmpeg version", "configuration:", "built with", "libav", "libsw", "libpost", "  built with"
                ])
            ]
            clean_err = " | ".join(err_lines[-3:]) if err_lines else err_raw[-300:]
            raise ValueError(f"FFmpeg 無損封裝失敗: {clean_err}")
    finally:
        try:
            if os.path.exists(tmp_ts_path):
                os.remove(tmp_ts_path)
        except Exception:
            pass

    return out_path


def download_media_to_path(media_item, out_path, force_audio=False, cookies=None, progress_cb=None, scratch_dir=None):
    """
    通用媒體下載入口 (支援 HLS 多線程切片、yt-dlp、直接串流與圖片下載)。
    media_item: dict, 來自 get_media_items()
    out_path: 最終輸出路徑 (例如 /content/drive/MyDrive/Download/xxx.mp4)
    cookies: 可選 dict {'fb_cookie': ..., 'bili_cookie': ...}
    progress_cb: 可選 callback(pct: float, cur_dur: float, tot_dur: float, speed_mb: float, speed_x: float)
    scratch_dir: 可選中繼暫存目錄 (預設 None 使用系統暫存)
    """
    title = media_item.get('title', 'media')
    media_url = media_item.get('url', '')
    media_type = media_item.get('type', 'video')

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    # 1. 圖片處理
    if media_type == 'image':
        response = requests.get(media_url, timeout=30)
        response.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(response.content)
        return out_path

    extra_headers = media_item.get('headers', {})

    # 2. HLS / m3u8 多線程切片下載
    last_hls_err = None
    if "m3u8" in media_url and not force_audio:
        cand_m3u8s = [media_url] + [u for u in media_item.get('backup_urls', []) if u != media_url]
        m3u8_content = media_item.get('m3u8_content')
        for c_idx, c_url in enumerate(cand_m3u8s):
            try:
                download_fast_parallel_hls(
                    c_url,
                    out_path=out_path,
                    extra_headers=extra_headers,
                    max_workers=16,
                    label=title,
                    progress_cb=progress_cb,
                    scratch_dir=scratch_dir,
                    m3u8_content=m3u8_content if c_idx == 0 else None
                )
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return out_path
            except Exception as hls_err:
                last_hls_err = hls_err
                if c_idx == len(cand_m3u8s) - 1:
                    _log_info(f"多線程下載備援耗盡: {hls_err}")

        # 若串流屬於 Cloudflare 防護網域 (如 MissAV / surrit.com)，標準 FFmpeg CLI 必定會被 403 阻擋，直接拋出多線程詳細錯誤
        is_cf_protected = any(k in media_url for k in ["surrit", "ppqrrs", "play-cdn", "bfikun"]) or "missav" in extra_headers.get("Referer", "") or "missav" in extra_headers.get("Origin", "")
        if is_cf_protected and last_hls_err:
            raise RuntimeError(f"HLS 多線程下載失敗 (來源受 Cloudflare 防護，FFmpeg CLI 無法直連): {last_hls_err}")

    # 3. yt-dlp 支援平台 (YouTube, Bilibili, FB, IG 等)
    webpage_url = media_item.get('webpage_url', '')
    is_direct_stream = "m3u8" in media_url or any(media_url.lower().split('?')[0].endswith(ext) for ext in ['.mp4', '.m4a', '.mp3', '.flv', '.ts', '.webm', '.mkv'])
    is_ytdlp_site = media_item.get('is_ytdlp') or any(k in media_url or k in webpage_url for k in ["youtube.com", "youtu.be", "googlevideo.com", "bilibili.com", "b23.tv", "twitter.com", "x.com", "instagram.com", "tiktok.com", "facebook.com", "fb.com", "fb.watch"])

    if (is_ytdlp_site or not is_direct_stream) and not force_audio:
        target_url = webpage_url or media_url
        temp_cookie = None
        out_base = os.path.splitext(out_path)[0]

        fb_cookie_str = cookies.get('fb_cookie') if cookies else None
        bili_cookie_str = cookies.get('bili_cookie') if cookies else None
        if any(d in target_url for d in ["bilibili.com", "b23.tv"]) and bili_cookie_str:
            temp_cookie = create_temp_cookiefile(bili_cookie_str)
        elif any(d in target_url for d in ["facebook.com", "fb.com", "fb.watch"]) and fb_cookie_str:
            temp_cookie = create_temp_cookiefile(fb_cookie_str)

        try:
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

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([target_url])

            if os.path.exists(out_path):
                return out_path
            for e in ["mp4", "mkv", "webm"]:
                cand = f"{out_base}.{e}"
                if os.path.exists(cand):
                    if cand != out_path:
                        shutil.move(cand, out_path)
                    return out_path
        finally:
            if temp_cookie and os.path.exists(temp_cookie):
                try:
                    os.remove(temp_cookie)
                except Exception:
                    pass

    # 4. 標準 FFmpeg 直接串流下載
    if extra_headers is None:
        extra_headers = {}
    if "surrit" in media_url or "missav" in extra_headers.get("Referer", ""):
        extra_headers["Origin"] = "https://missav.ai"
        if "Referer" not in extra_headers:
            extra_headers["Referer"] = "https://missav.ai/"

    headers_arg = []
    if extra_headers:
        headers_str = "".join(f"{k}: {v}\r\n" for k, v in extra_headers.items())
        headers_arg = ["-headers", headers_str]
    if "m3u8" in media_url:
        headers_arg += ["-allowed_segment_extensions", "ALL", "-extension_picky", "0"]

    if force_audio:
        ffmpeg_cmd = ["ffmpeg", "-y"] + headers_arg + ["-i", media_url, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", out_path]
    else:
        reconnect_args = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5", "-multiple_requests", "1"]
        if "m3u8" in media_url:
            reconnect_args += ["-http_persistent", "1"]
        ffmpeg_cmd = ["ffmpeg", "-y"] + reconnect_args + headers_arg + ["-i", media_url, "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out_path]

    total_dur = get_media_duration(media_url, headers=extra_headers)
    ret, stderr = run_ffmpeg_with_progress(ffmpeg_cmd, total_duration=total_dur, label=title)
    if ret == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path

    err_lines = [
        l.strip() for l in stderr.splitlines()
        if l.strip() and not any(l.strip().startswith(k) for k in [
            "ffmpeg version", "configuration:", "built with", "libav", "libsw", "libpost", "  built with"
        ])
    ]
    actual_err = " | ".join(err_lines[-4:]) if err_lines else stderr[-300:]
    if last_hls_err:
        raise RuntimeError(f"多線程 HLS 下載失敗 ({last_hls_err})，且 FFmpeg 備援亦失敗: {actual_err}")
    raise RuntimeError(f"FFmpeg 下載失敗: {actual_err}")
