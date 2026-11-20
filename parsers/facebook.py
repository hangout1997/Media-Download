# -*- coding: utf-8 -*-
"""Facebook 貼文相簿/多圖解析器（透過 mbasic 擷取高清原始圖）。"""

import html as html_lib
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import requests

from core.auth import normalize_cookies_dict
from core.http_client import default_log_info


def is_facebook_group_home(url):
    """判斷是否為 Facebook 社團首頁網址（非單一貼文）。"""
    if "facebook.com/groups/" in url or "fb.com/groups/" in url:
        clean_url = url.split("?")[0].rstrip("/")
        parts = clean_url.split("/groups/")
        if len(parts) == 2 and "/" not in parts[1]:
            return True
    return False


def parse_facebook_photos(url, cookies=None, log_cb=None):
    """嘗試從 Facebook 貼文擷取相片清單；若無相片或失敗則回傳 None 交由 yt-dlp 接手。"""
    if is_facebook_group_home(url):
        raise ValueError(
            "此網址為「社團首頁」，並非單一貼文或影片。請在社團中找到貼文，點選其「發佈時間」獲取正確的貼文網址（例如含有 /permalink/ 或 /share/p/）再進行下載。"
        )

    def _log(msg):
        if log_cb:
            log_cb(msg)
        else:
            default_log_info(msg)

    norm_cookies = normalize_cookies_dict(cookies)
    fb_c = norm_cookies["fb_cookie"]

    try:
        fb_cookies_dict = {}
        if fb_c:
            for item in fb_c.split(";"):
                item = item.strip()
                if not item:
                    continue
                parts = item.split("=", 1)
                if len(parts) == 2:
                    fb_cookies_dict[parts[0]] = parts[1]

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "DNT": "1",
            "Connection": "keep-alive",
        }
        session = requests.Session()
        if fb_cookies_dict:
            session.cookies.update(fb_cookies_dict)

        # 1. 用 HEAD 請求獲取 302 重定向 Location，避開直接 GET 產生的 400 錯誤
        try:
            res = session.head(url, headers=headers, allow_redirects=False, timeout=10)
            final_url = res.headers.get("Location", url)
        except Exception:
            try:
                res = session.get(url, headers=headers, allow_redirects=True, timeout=15)
                final_url = res.url
            except Exception:
                final_url = url

        # 轉換為基礎行動版網頁 (mbasic.facebook.com)
        mobile_url = final_url.replace("www.facebook.com", "mbasic.facebook.com").replace(
            "m.facebook.com", "mbasic.facebook.com"
        )

        # 2. 使用行動版 Header 抓取內容
        mobile_headers = headers.copy()
        mobile_headers["User-Agent"] = (
            "Mozilla/5.0 (Linux; Android 10; K) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        )

        m_res = session.get(
            mobile_url, headers=mobile_headers, allow_redirects=True, timeout=15
        )
        html_content = m_res.text

        # 3. 提取貼文標題/群組名稱 (用於檔名)
        native_texts = re.findall(
            r'<div dir="auto" class="native-text rslh"[^>]*>(.*?)</div>',
            html_content,
            re.S,
        )
        title_parts = []
        for nt in native_texts:
            clean = re.sub(r"<[^>]+>", " ", nt)
            clean = html_lib.unescape(clean)
            clean = re.sub(r"\s+", " ", clean).strip()
            if (
                clean
                and clean
                not in ["開啟應用程式", "登入", "加入社團", "關於這個社團", "&nbsp;"]
                and not clean.startswith("本社團歡迎大家")
            ):
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

        post_title = re.sub(r'[\\/:*?"<>|]', "_", post_title).strip()
        post_title = post_title[:100] or "Facebook_Post"

        # 4. 嘗試提取相簿/貼文圖片合集 (Set ID)
        unique_photos = []
        set_match = re.search(r"set=(pcb\.\d+|a\.\d+)", html_content)
        if set_match:
            set_param = set_match.group(1)
            _log(
                f"📸 偵測到多張相片合集 ({set_param})，正在透過基礎行動版擷取完整的相片清單..."
            )
            set_url = f"https://mbasic.facebook.com/media/set/?set={set_param}"
            try:
                set_res = session.get(set_url, headers=mobile_headers, timeout=15)
                if set_res.status_code == 200:
                    set_html = set_res.text
                    photo_links = re.findall(r'href="(/photo\.php\?[^"]+)"', set_html)
                    if not photo_links:
                        photo_links = re.findall(
                            r"href='(/photo\.php\?[^'\s]+)'", set_html
                        )

                    if photo_links:
                        _log(
                            f"🔗 成功找到 {len(photo_links)} 張相片的連結，開始平行解析高清原始圖..."
                        )

                        def _fetch_one_photo(p_link):
                            p_url = "https://mbasic.facebook.com" + html_lib.unescape(
                                p_link
                            )
                            try:
                                p_res = session.get(
                                    p_url, headers=mobile_headers, timeout=10
                                )
                                if p_res.status_code == 200:
                                    p_html = p_res.text
                                    img_match = re.search(
                                        r'<img[^>]+src="([^"]*scontent[^"]*)"', p_html
                                    )
                                    if not img_match:
                                        img_match = re.search(
                                            r'src="([^"]*scontent[^"]*)"', p_html
                                        )
                                    if img_match:
                                        raw_img_url = html_lib.unescape(
                                            img_match.group(1)
                                        )
                                        raw_img_url = urllib.parse.unquote(raw_img_url)
                                        if not any(
                                            size in raw_img_url
                                            for size in ["p144x144", "p48x48", "p75x75"]
                                        ):
                                            return raw_img_url
                            except Exception as e:
                                print(f"Error scraping single photo page: {e}")
                            return None

                        with ThreadPoolExecutor(max_workers=8) as _photo_exec:
                            results = list(_photo_exec.map(_fetch_one_photo, photo_links))
                        unique_photos.extend([r for r in results if r])
            except Exception as e:
                print(f"Failed to fetch set photos: {e}")

        # Fallback 5：使用頁面中可見的圖片
        if not unique_photos:
            img_srcs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_content)
            photo_urls = []
            for src in img_srcs:
                src = html_lib.unescape(src)
                src = urllib.parse.unquote(src)
                if "scontent" in src:
                    if any(
                        size in src for size in ["p144x144", "p48x48", "p75x75"]
                    ):
                        continue
                    photo_urls.append(src)

            if not photo_urls:
                raw_urls = re.findall(
                    r"https?://[a-zA-Z0-9_\.\-\\/]+fbcdn[a-zA-Z0-9_\.\-\/\?\&=\+;%\\:]+",
                    html_content,
                )
                for r_url in raw_urls:
                    r_url = r_url.replace("\\/", "/").replace("\\\\/", "/")
                    r_url = html_lib.unescape(urllib.parse.unquote(r_url))
                    if "scontent" in r_url:
                        if any(
                            size in r_url for size in ["p144x144", "p48x48", "p75x75"]
                        ):
                            continue
                        photo_urls.append(r_url)

            seen_ids = set()
            for p_url in photo_urls:
                match = re.search(r"/([^/]+_n\.[a-z0-9]+)", p_url)
                if match:
                    filename = match.group(1)
                    parts = filename.split("_")
                    if len(parts) >= 2:
                        photo_id = "_".join(parts[:2])
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
                    "url": p_url,
                    "title": (
                        f"{post_title}_{idx+1}" if len(unique_photos) > 1 else post_title
                    ),
                    "ext": "jpg",
                    "type": "image",
                })
            return fb_items
        else:
            if any(
                p_pattern in url
                for p_pattern in [
                    "/share/p/",
                    "/posts/",
                    "/permalink/",
                    "/photos/",
                    "/photo.php",
                    "/photo/",
                ]
            ):
                raise ValueError(
                    "此貼文為「Facebook 相片或非影片貼文」，必須提供有效的 Facebook Cookie 授權才能進行下載。\n\n💡 **下載相片建議**：請確認您已在下方填入有效的 **Facebook Cookie**。私密社團、好友限閱或部分公開貼文的相片必須有 Cookie 授權才能順利下載。"
                )
    except Exception as e:
        print(f"Facebook custom photo scrape failed: {e}, falling back to yt-dlp...")

    return None
