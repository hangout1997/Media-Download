# -*- coding: utf-8 -*-
"""yt-dlp 通用平台解析器 (YouTube, Bilibili, X, Facebook, IG, TikTok, Threads 等)。"""

import os
import re

from core.auth import (
    create_temp_cookiefile,
    normalize_cookies_dict,
    run_ytdlp_with_youtube_auth,
)


def parse_with_ytdlp(
    url,
    is_custom_hls=False,
    cookies=None,
    yt_auth_mode="auto",
    yt_cookie="",
    cached_cfb=None,
    on_cfb_detected=None,
    on_cfb_invalid=None,
):
    """透過 yt-dlp 解析串流資訊並回傳標準化 media_item 清單。"""
    target_url = url.replace("threads.com", "threads.net")
    norm_cookies = normalize_cookies_dict(cookies)

    temp_cookie_path = None
    if "facebook.com" in target_url or "fb.com" in target_url or "fb.watch" in target_url:
        temp_cookie_path = create_temp_cookiefile(
            norm_cookies["fb_cookie"], domain=".facebook.com"
        )
    elif "bilibili.com" in target_url or "b23.tv" in target_url:
        temp_cookie_path = create_temp_cookiefile(
            norm_cookies["bili_cookie"], domain=".bilibili.com"
        )

    ydl_opts = {
        "quiet": True,
        "extract_flat": False,
        "nocheckcertificate": True,
        "legacy_server_connect": True,
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web", "ios"],
            }
        },
    }
    if temp_cookie_path:
        ydl_opts["cookiefile"] = temp_cookie_path

    items = []
    try:
        try:
            info = run_ytdlp_with_youtube_auth(
                target_url,
                ydl_opts,
                lambda ydl: ydl.extract_info(target_url, download=False),
                auth_mode=yt_auth_mode,
                yt_cookie_str=yt_cookie or norm_cookies["yt_cookie"],
                cached_cfb=cached_cfb,
                on_cfb_detected=on_cfb_detected,
                on_cfb_invalid=on_cfb_invalid,
            )

            entries = info.get("entries", [info])
            for i, entry in enumerate(entries):
                if not entry:
                    continue
                title = entry.get("title") or info.get("title") or f"media_{i}"
                title = re.sub(r'[\\/:*?"<>|]', "_", title)

                ext = entry.get("ext")
                raw_url = entry.get("url")
                webpage_url = entry.get("webpage_url") or info.get("webpage_url") or target_url

                if raw_url and (raw_url.startswith("http://") or raw_url.startswith("https://")):
                    media_url = raw_url
                elif webpage_url and (
                    webpage_url.startswith("http://") or webpage_url.startswith("https://")
                ):
                    media_url = webpage_url
                elif target_url.startswith("http://") or target_url.startswith("https://"):
                    media_url = target_url
                else:
                    continue

                is_image = ext in ["jpg", "jpeg", "png", "webp"] or (
                    isinstance(media_url, str) and ".jpg" in media_url
                )
                http_headers = entry.get("http_headers") or info.get("http_headers") or {}

                items.append({
                    "url": media_url,
                    "title": title if len(entries) == 1 else f"{title}_{i+1}",
                    "ext": ext or ("jpg" if is_image else "mp4"),
                    "type": "image" if is_image else "video",
                    "headers": http_headers,
                    "webpage_url": webpage_url,
                    "is_ytdlp": True,
                })
        except Exception as e:
            err_msg = str(e)
            if any(
                kw in err_msg
                for kw in [
                    "No video formats found",
                    "Unsupported URL",
                    "Cannot parse data",
                    "Private video",
                    "login",
                ]
            ):
                if (
                    "facebook.com" in target_url
                    or "fb.com" in target_url
                    or "fb.watch" in target_url
                ):
                    raise ValueError(
                        "此 Facebook 連結可能為「純相片貼文」、「非影片內容」或「私密/限制級內容」。\n\n💡 **下載建議**：請確認您已在下方填入有效的 **Facebook Cookie**。私密社團、好友限閱、相片貼文或部分特定影片必須有 Cookie 授權才能進行下載。"
                    )
            if any(
                kw in err_msg
                for kw in ["412", "Precondition Failed", "啥都木有", "KeyError('result')"]
            ):
                if "bilibili.com" in target_url or "b23.tv" in target_url:
                    raise ValueError(
                        "Bilibili 伺服器存取被拒 (HTTP 412 風控限制或該影片/番劇已下架)。\n\n💡 **原因與建議**：\n1. 該番劇可能為大會員專屬、版權地區限制，或已被官方下架 (API 回傳「啥都木有」)。\n2. 若影片在瀏覽器可觀看，請在下方「🔑 Bilibili Cookie」填入您的登入 Cookie (`SESSDATA`) 以通過安全風控策略。"
                    )
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

    return items
