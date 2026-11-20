# -*- coding: utf-8 -*-
"""共用 HTTP Session、Headers、網址正規化與 m3u8 連通性探測工具。"""

import os
import re
import urllib.parse
import requests

# ── 全域共用 Session（跨任務複用 TCP 連線池 + DNS 快取）──────────────────────
_global_session = requests.Session()
_global_adapter = requests.adapters.HTTPAdapter(
    pool_connections=32, pool_maxsize=64, max_retries=3
)
_global_session.mount("https://", _global_adapter)
_global_session.mount("http://", _global_adapter)

GLOBAL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
_GLOBAL_HEADERS = GLOBAL_HEADERS


def get_global_session():
    """取得全域共用 requests.Session。"""
    return _global_session


def default_log_info(msg):
    """預設純 Python 資訊日誌 callback。"""
    print(f"ℹ️ {msg}")


def default_log_toast(msg, icon="ℹ️"):
    """預設純 Python 提示通知 callback。"""
    print(f"{icon} {msg}")


def cn_to_an(cn_str):
    """將中文數字（如「一」、「十一」、「二十三」）轉換為整數。"""
    if cn_str.isdigit():
        return int(cn_str)

    zh_num = {
        "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }

    if len(cn_str) == 1:
        return zh_num.get(cn_str, 1)

    if cn_str.startswith("十"):
        if len(cn_str) == 2:
            return 10 + zh_num.get(cn_str[1], 0)
        return 10

    if "十" in cn_str:
        parts = cn_str.split("十")
        prefix = zh_num.get(parts[0], 1)
        suffix = zh_num.get(parts[1], 0) if parts[1] else 0
        return prefix * 10 + suffix

    val = 0
    for char in cn_str:
        val = val * 10 + zh_num.get(char, 0)
    return val if val > 0 else 1


def format_time(seconds):
    """將秒數格式化為 MM:SS 或 HH:MM:SS。"""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def normalize_input_url(url_str):
    """清理並正規化使用者輸入的網址、本機路徑或平台番號。"""
    url_str = url_str.strip().strip("'\"").strip("`").strip()
    if not url_str:
        return url_str
    if url_str.startswith("http://") or url_str.startswith("https://"):
        return url_str
    if os.path.exists(url_str):
        return url_str
    if any(
        domain in url_str.lower()
        for domain in [
            ".com", ".ai", ".tv", ".ws", ".net", ".org", ".me", ".co", ".be",
            "youtube", "youtu.be", "facebook", "instagram", "tiktok",
            "twitter", "missav", "movieffm", "mvffm",
        ]
    ):
        return "https://" + url_str.lstrip("/")
    if re.match(r"^[a-zA-Z0-9\-_]{5,}$", url_str):
        if url_str.startswith(("qsvip-", "NS4K-", "NSYS-", "itdog-")):
            return url_str
        return f"https://missav.ai/{url_str}"
    return "https://" + url_str


def is_valid_m3u8_content(chunk_bytes):
    """檢查內容是否為真正的 HLS m3u8 播放清單（排除 HTML 錯誤頁、防盜鏈頁面或 JSON）。"""
    if not chunk_bytes:
        return False
    clean = chunk_bytes.lstrip(b"\xef\xbb\xbf \t\r\n")
    if clean.startswith((b"<!DOCTYPE", b"<!doctype", b"<html", b"<HTML", b"<?xml", b'{"code"')):
        return False
    return b"#EXTM3U" in clean or b"#EXT-X-" in clean or b"#EXTINF" in clean


def check_m3u8_accessible(m3u8_url, headers=None, timeout=3.0):
    """快速探測 m3u8 串流是否可用 (排除 DNS 失敗、404、防盜鏈阻擋與回傳 HTML 的無效伺服器)。"""
    if not m3u8_url or not m3u8_url.startswith(("http://", "https://")):
        return False

    req_headers = dict(GLOBAL_HEADERS)
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
        for ref in [
            "https://www.movieffm.net/",
            "https://www.mvffm.net/",
            f"{parsed.scheme}://{parsed.netloc}/",
        ]:
            if req_headers.get("Referer") == ref:
                continue
            h2 = dict(req_headers)
            h2["Referer"] = ref
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
