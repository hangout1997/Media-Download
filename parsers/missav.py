# -*- coding: utf-8 -*-
"""MissAV 網站解析器與 Dean Edwards Packer JS 解包工具。"""

import html as html_lib
import re


def extract_packer_blocks(html):
    """擷取 HTML 中所有 Dean Edwards Packer `eval(function(p,a,c,k,e,d)...)` 區塊。"""
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
            if html[i] == "(":
                paren_count += 1
            elif html[i] == ")":
                paren_count -= 1
                if paren_count == -1:
                    end_idx = i
                    break
        blocks.append(html[idx : end_idx + 1])
        idx = end_idx + 1
    return blocks


def unpack_dean_packer(packed_js):
    """解密 Dean Edwards Packer 混淆之 JavaScript 程式碼。"""
    pattern = r'\}\s*\(\s*(["\'])((?:(?!\1).|\\.)*)\1\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(["\'])((?:(?!\5).|\\.)*)\5(?:\.split\([\'"]\|[\'"]\))?'
    match = re.search(pattern, packed_js, re.DOTALL)
    if not match:
        return ""

    packed_code = match.group(2)
    packed_code = packed_code.replace("\\'", "'").replace('\\"', '"')

    a = int(match.group(3))
    c = int(match.group(4))

    words_str = match.group(6)
    words = words_str.split("|")

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
            word_pat = r"\b" + re.escape(encoded) + r"\b"
            packed_code = re.sub(word_pat, words[i], packed_code)

    return packed_code


def parse_missav(url, pref_quality="720p"):
    """解析 MissAV 頁面並依畫質偏好擷取主從 m3u8 與預抓內容。"""
    from curl_cffi import requests as curl_requests

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
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
        og_title_match = re.search(
            r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']',
            response.text,
        )
        if og_title_match:
            title = og_title_match.group(1)
        else:
            og_title_match = re.search(
                r'<meta\s+content=["\'](.*?)["\']\s+property=["\']og:title["\']',
                response.text,
            )
            if og_title_match:
                title = og_title_match.group(1)
            else:
                title_match = re.search(r"<title>(.*?)</title>", response.text)
                if title_match:
                    title = title_match.group(1)

        title = html_lib.unescape(title).strip().rstrip("-").strip()
        title = re.sub(r'[\\/:*?"<>|]', "_", title)

        # 2. 尋找與解密 Dean Edwards Packer 區塊 (掃描所有區塊)
        blocks = extract_packer_blocks(response.text)
        m3u8_url = None
        backup_m3u8s = []

        for block in blocks:
            unpacked = unpack_dean_packer(block)
            source_1080p = re.search(r"source1280\s*=\s*['\"](https?://[^'\"]+?)['\"]", unpacked)
            source_720p = re.search(r"source842\s*=\s*['\"](https?://[^'\"]+?)['\"]", unpacked)
            source_playlist = re.search(r"source\s*=\s*['\"](https?://[^'\"]+?)['\"]", unpacked)
            source_generic = re.search(
                r"source\w*\s*=\s*['\"](https?://[^'\"]+?\.m3u8[^\'\"]*)['\"]", unpacked
            )
            m3u8_direct = re.search(
                r"['\"](https?://[^'\"]+?\.m3u8[^\'\"]*)['\"]", unpacked
            )

            u_1080 = source_1080p.group(1) if source_1080p else None
            u_720 = source_720p.group(1) if source_720p else None
            u_def = (
                source_playlist.group(1)
                if source_playlist
                else (
                    source_generic.group(1)
                    if source_generic
                    else (m3u8_direct.group(1) if m3u8_direct else None)
                )
            )

            if pref_quality == "720p":
                if u_720:
                    m3u8_url = u_720
                    if u_1080:
                        backup_m3u8s.append(u_1080)
                    break
                elif u_1080:
                    m3u8_url = u_1080
                    break
                elif u_def:
                    m3u8_url = u_def
                    break
            else:
                if u_1080:
                    m3u8_url = u_1080
                    if u_720:
                        backup_m3u8s.append(u_720)
                    break
                elif u_720:
                    m3u8_url = u_720
                    break
                elif u_def:
                    m3u8_url = u_def
                    break

        if not m3u8_url:
            raise ValueError("無法在 MissAV 頁面中找到有效的 m3u8 串流網址")

        # 嘗試直接獲取 m3u8 選單內容，免去後續重複請求
        m3u8_text = None
        try:
            r_m3u8 = curl_requests.get(
                m3u8_url,
                headers={
                    "Referer": url,
                    "Origin": "https://missav.ai",
                    "User-Agent": headers["User-Agent"],
                },
                impersonate="chrome124",
                timeout=15,
            )
            if r_m3u8.status_code == 200 and r_m3u8.text:
                m3u8_text = r_m3u8.text
        except Exception:
            pass

        return [
            {
                "url": m3u8_url,
                "title": title,
                "ext": "mp4",
                "type": "video",
                "headers": {
                    "Referer": url,
                    "Origin": "https://missav.ai",
                    "User-Agent": headers["User-Agent"],
                },
                "m3u8_content": m3u8_text,
                "backup_urls": backup_m3u8s,
            }
        ]
    except Exception as e:
        raise ValueError(f"MissAV 解析失敗: {e}")
