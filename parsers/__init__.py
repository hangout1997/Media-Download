# -*- coding: utf-8 -*-
"""純 Python 網站解析器統一入口與 m3u8 預抓取工具 (零 Streamlit 依賴)。"""

import re
import urllib.parse

from core.http_client import normalize_input_url
from parsers.facebook import parse_facebook_photos
from parsers.gimy import parse_gimy, resolve_gimy_stream
from parsers.missav import (
    extract_packer_blocks,
    parse_missav,
    unpack_dean_packer,
)
from parsers.movieffm import _decode_movieffm_url, parse_movieffm
from parsers.ytdlp_parser import parse_with_ytdlp


def get_media_items(
    url,
    cookies=None,
    pref_quality="720p",
    yt_auth_mode="auto",
    yt_cookie="",
    cached_cfb=None,
    on_cfb_detected=None,
    on_cfb_invalid=None,
    log_cb=None,
):
    """統一解析各類影片/相片網址並回傳標準化的媒體項目清單。"""
    url = normalize_input_url(url)

    # 1. Movieffm / Mvffm
    if any(k in url.lower() for k in ["movieffm", "mvffm"]):
        return parse_movieffm(url)

    # 2. MissAV
    if "missav" in url.lower():
        return parse_missav(url, pref_quality=pref_quality)

    # 3. Facebook 貼文相片解析 (含社團首頁阻擋)
    if any(domain in url for domain in ["facebook.com", "fb.com", "fb.watch"]):
        fb_items = parse_facebook_photos(url, cookies=cookies, log_cb=log_cb)
        if fb_items:
            return fb_items

    # 4. 通用 yt-dlp 支援平台
    is_custom_hls = any(
        k in url.lower()
        for k in ["missav", "movieffm", "mvffm", "gimymax", "gimyplus", "gimy"]
    )
    ytdlp_domains = [
        "x.com",
        "twitter.com",
        "t.co",
        "youtube.com",
        "youtu.be",
        "facebook.com",
        "fb.com",
        "fb.watch",
        "instagram.com",
        "ig.me",
        "tiktok.com",
    ]
    if not is_custom_hls or any(domain in url for domain in ytdlp_domains):
        items = parse_with_ytdlp(
            url,
            is_custom_hls=is_custom_hls,
            cookies=cookies,
            yt_auth_mode=yt_auth_mode,
            yt_cookie=yt_cookie,
            cached_cfb=cached_cfb,
            on_cfb_detected=on_cfb_detected,
            on_cfb_invalid=on_cfb_invalid,
        )
        if items:
            return items

    # 5. Gimy / MacCMS 解析 (支援單集與目錄頁併發展開)
    return parse_gimy(
        url,
        cookies=cookies,
        log_cb=log_cb,
        item_fetcher=lambda ep_u: get_media_items(
            ep_u,
            cookies=cookies,
            pref_quality=pref_quality,
            yt_auth_mode=yt_auth_mode,
            yt_cookie=yt_cookie,
            cached_cfb=cached_cfb,
            on_cfb_detected=on_cfb_detected,
            on_cfb_invalid=on_cfb_invalid,
            log_cb=log_cb,
        ),
    )


def ensure_m3u8_prefetched(items, pref_quality="720p"):
    """在本機端預先抓取 m3u8 選單內容，免除雲端 Colab 節點受 Cloudflare 403 阻擋。"""
    if not items:
        return items
    from curl_cffi import requests as curl_requests

    for it in items:
        m_url = it.get("url", "")
        if "m3u8" in m_url and not it.get("m3u8_content"):
            m_headers = it.get("headers", {})
            try:
                r = curl_requests.get(
                    m_url, headers=m_headers, impersonate="chrome124", timeout=15
                )
                if r.status_code == 200 and r.text:
                    m3u8_text = r.text
                    if "#EXT-X-STREAM-INF" in m3u8_text:
                        sub_playlists = []
                        cur_inf = {}
                        for line in m3u8_text.splitlines():
                            line = line.strip()
                            if line.startswith("#EXT-X-STREAM-INF"):
                                bw_m = re.search(r"BANDWIDTH=(\d+)", line)
                                res_m = re.search(r"RESOLUTION=\d+x(\d+)", line)
                                cur_inf["bw"] = int(bw_m.group(1)) if bw_m else 0
                                cur_inf["height"] = int(res_m.group(1)) if res_m else 0
                            elif line and not line.startswith("#"):
                                sub_playlists.append((
                                    cur_inf.get("height", 0),
                                    cur_inf.get("bw", 0),
                                    urllib.parse.urljoin(m_url, line),
                                ))
                                cur_inf = {}
                        if sub_playlists:
                            sub_playlists.sort(key=lambda x: (x[0], x[1]), reverse=True)
                            chosen = None
                            if pref_quality == "720p":
                                cand = [sp for sp in sub_playlists if sp[0] <= 720]
                                if cand:
                                    chosen = cand[0]
                            if not chosen:
                                chosen = sub_playlists[0]
                            sub_url = chosen[2]
                            r_sub = curl_requests.get(
                                sub_url,
                                headers=m_headers,
                                impersonate="chrome124",
                                timeout=15,
                            )
                            if r_sub.status_code == 200 and r_sub.text:
                                it["url"] = sub_url
                                m3u8_text = r_sub.text
                    it["m3u8_content"] = m3u8_text
            except Exception:
                pass
    return items


__all__ = [
    "_decode_movieffm_url",
    "ensure_m3u8_prefetched",
    "extract_packer_blocks",
    "get_media_items",
    "parse_facebook_photos",
    "parse_gimy",
    "parse_missav",
    "parse_movieffm",
    "parse_with_ytdlp",
    "resolve_gimy_stream",
    "unpack_dean_packer",
]
