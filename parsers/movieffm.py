# -*- coding: utf-8 -*-
"""Movieffm / Mvffm 網站解析器。"""

import base64
import html as html_lib
import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from core.http_client import check_m3u8_accessible


def _decode_movieffm_url(raw_u):
    """解碼 Movieffm/Mvffm 加密串流網址 (支援 Base64、Urlencode 與原生 HTTP 網址)。"""
    if not raw_u or not isinstance(raw_u, str):
        return ""
    u = raw_u.strip().replace(r"\/", "/")
    if u.startswith(("http://", "https://")):
        return u
    try:
        pad = len(u) % 4
        padded = u + ("=" * (4 - pad) if pad else "")
        b = base64.b64decode(padded)
        dec = urllib.parse.unquote(b.decode("utf-8", errors="ignore"))
        if dec.startswith(("http://", "https://")):
            return dec
    except Exception:
        pass
    try:
        u_unq = urllib.parse.unquote(u)
        pad = len(u_unq) % 4
        padded = u_unq + ("=" * (4 - pad) if pad else "")
        b = base64.b64decode(padded)
        dec = urllib.parse.unquote(b.decode("utf-8", errors="ignore"))
        if dec.startswith(("http://", "https://")):
            return dec
    except Exception:
        pass
    return u


def parse_movieffm(url):
    """解析 Movieffm / Mvffm 單部電影與多集戲劇頁面。"""
    from curl_cffi import requests as curl_requests

    parsed_origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
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
    t_match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html_content,
    )
    if not t_match:
        t_match = re.search(r"<title>(.*?)</title>", html_content)

    if t_match:
        title = html_lib.unescape(t_match.group(1)).strip()
        title = re.sub(
            r"\s*-\s*(?:Movieffm|mvffm).*$", "", title, flags=re.IGNORECASE
        ).strip()
    else:
        t_js = re.search(r'title\s*:\s*["\']([^"\']+)["\']', html_content)
        if t_js:
            title = html_lib.unescape(t_js.group(1)).strip()

    title = re.sub(r'[\\/:*?"<>|]', "_", title).strip()
    if not title:
        title = "Movieffm_Video"

    # 2. 提取 m3u8 串流網址 (支援多線路自動探測備援、單部電影與連續劇多集)
    items = []

    # 策略 A: 解析 videourls JSON 陣列
    match = re.search(
        r"videourls\s*[:=]\s*(\[\s*(?:\[.*?\]|\{.*?\})\s*\])", html_content, re.DOTALL
    )
    if match:
        try:
            clean_json = match.group(1).replace(r"\/", "/")
            data = json.loads(clean_json)

            def _normalize_entry_url(ent):
                if isinstance(ent, dict) and "url" in ent:
                    ent["url"] = _decode_movieffm_url(ent.get("url"))
                return ent

            if isinstance(data, list):
                for sub in data:
                    if isinstance(sub, list):
                        for it in sub:
                            _normalize_entry_url(it)
                    elif isinstance(sub, dict):
                        _normalize_entry_url(sub)

            # 情況 1: 2D 陣列 (電視劇 / 連續劇)
            if len(data) > 0 and isinstance(data[0], list):
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

                if valid_sources:
                    primary_source = valid_sources[0][1]
                    total_eps = len(primary_source)
                else:
                    primary_source = (
                        data[0]
                        if len(data) > 0 and isinstance(data[0], list)
                        else []
                    )
                    total_eps = len(primary_source)

                def _find_ep_in_source(src_list, target_ep_num, target_name, pos_idx):
                    if not isinstance(src_list, list):
                        return None
                    if target_ep_num is not None:
                        for it in src_list:
                            m = re.search(r"\d+", str(it.get("name", "")))
                            if m and int(m.group()) == target_ep_num:
                                u = it.get("url")
                                if u and u.startswith("http"):
                                    return u
                    if target_name:
                        for it in src_list:
                            if str(it.get("name", "")).strip() == target_name:
                                u = it.get("url")
                                if u and u.startswith("http"):
                                    return u
                    if pos_idx < len(src_list):
                        u = src_list[pos_idx].get("url")
                        if u and u.startswith("http"):
                            return u
                    return None

                for idx in range(total_eps):
                    chosen_ep_url = None
                    chosen_raw_name = ""

                    if idx < len(primary_source):
                        ep_item = primary_source[idx]
                        ep_url = ep_item.get("url")
                        if ep_url and ep_url.startswith("http"):
                            chosen_raw_name = str(ep_item.get("name", "")).strip()
                            chosen_ep_url = ep_url

                    if not chosen_ep_url:
                        for _, source_list, _ in valid_sources:
                            if idx < len(source_list):
                                ep_item = source_list[idx]
                                ep_url = ep_item.get("url")
                                if ep_url and ep_url.startswith("http"):
                                    chosen_raw_name = str(ep_item.get("name", "")).strip()
                                    chosen_ep_url = ep_url
                                    break

                    if not chosen_ep_url:
                        for source_list in data:
                            if isinstance(source_list, list) and idx < len(source_list):
                                ep_item = source_list[idx]
                                ep_url = ep_item.get("url")
                                if ep_url and ep_url.startswith("http"):
                                    if not chosen_raw_name:
                                        chosen_raw_name = str(
                                            ep_item.get("name", "")
                                        ).strip()
                                    chosen_ep_url = ep_url
                                    break
                    if not chosen_ep_url and idx < len(primary_source):
                        chosen_ep_url = primary_source[idx].get("url")
                        if not chosen_raw_name:
                            chosen_raw_name = str(
                                primary_source[idx].get("name", "")
                            ).strip()

                    if not chosen_ep_url:
                        continue

                    target_ep_num = None
                    if chosen_raw_name:
                        num_match = re.search(r"\d+", chosen_raw_name)
                        if num_match:
                            target_ep_num = int(num_match.group())

                    if total_eps == 1:
                        ep_title = title
                    else:
                        if target_ep_num is not None:
                            ep_title = f"{title} - EP{target_ep_num:02d}"
                        elif chosen_raw_name:
                            ep_title = f"{title} - {chosen_raw_name}"
                        else:
                            ep_title = f"{title} - EP{idx+1:02d}"

                    backup_ep_urls = []
                    for _, src_list, _ in valid_sources:
                        cand_u = _find_ep_in_source(
                            src_list, target_ep_num, chosen_raw_name, idx
                        )
                        if cand_u and cand_u != chosen_ep_url and cand_u not in backup_ep_urls:
                            backup_ep_urls.append(cand_u)
                    for src_list in data:
                        cand_u = _find_ep_in_source(
                            src_list, target_ep_num, chosen_raw_name, idx
                        )
                        if cand_u and cand_u != chosen_ep_url and cand_u not in backup_ep_urls:
                            backup_ep_urls.append(cand_u)

                    items.append({
                        "url": chosen_ep_url,
                        "backup_urls": backup_ep_urls,
                        "title": ep_title,
                        "ext": "mp4",
                        "type": "video",
                        "headers": {
                            "Referer": url,
                            "User-Agent": headers["User-Agent"],
                        },
                    })

            # 情況 2: 1D 陣列 (單部電影多來源)
            elif len(data) > 0 and isinstance(data[0], dict):
                valid_candidates = []
                for item in data:
                    u = item.get("url")
                    if u and (u.startswith("http://") or u.startswith("https://")) and ".m3u8" in u:
                        valid_candidates.append(u)

                def _probe_cand(u):
                    if check_m3u8_accessible(u, headers=headers):
                        return u
                    return None

                with ThreadPoolExecutor(
                    max_workers=min(5, max(1, len(valid_candidates)))
                ) as executor:
                    probed_cands = [
                        r for r in executor.map(_probe_cand, valid_candidates) if r is not None
                    ]

                chosen_movie_url = probed_cands[0] if probed_cands else None
                backup_movie_urls = probed_cands[1:] if len(probed_cands) > 1 else []

                if not chosen_movie_url:
                    for cand_url in valid_candidates:
                        if check_m3u8_accessible(cand_url, headers=headers, timeout=5.0):
                            chosen_movie_url = cand_url
                            break

                if not chosen_movie_url and valid_candidates:
                    chosen_movie_url = valid_candidates[0]
                    backup_movie_urls = [
                        c for c in valid_candidates[1:] if c != chosen_movie_url
                    ]

                if chosen_movie_url:
                    items.append({
                        "url": chosen_movie_url,
                        "backup_urls": backup_movie_urls,
                        "title": title,
                        "ext": "mp4",
                        "type": "video",
                        "headers": {
                            "Referer": url,
                            "User-Agent": headers["User-Agent"],
                        },
                    })
        except Exception as e:
            print(f"Error parsing Movieffm videourls: {e}")

    # 策略 B: 正規表達式全局搜尋 m3u8 作為備援
    if not items:
        m3u8_matches = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html_content)
        for m_url in m3u8_matches:
            clean_url = m_url.replace(r"\/", "/")
            if check_m3u8_accessible(clean_url, headers=headers):
                items.append({
                    "url": clean_url,
                    "title": title,
                    "ext": "mp4",
                    "type": "video",
                    "headers": {
                        "Referer": url,
                        "User-Agent": headers["User-Agent"],
                    },
                })
                break

    if not items:
        b64_cands = re.findall(r'["\']([a-zA-Z0-9+/=]{20,})["\']', html_content)
        for cand in b64_cands:
            dec_u = _decode_movieffm_url(cand)
            if dec_u and dec_u.startswith(("http://", "https://")) and ".m3u8" in dec_u:
                if check_m3u8_accessible(dec_u, headers=headers):
                    items.append({
                        "url": dec_u,
                        "title": title,
                        "ext": "mp4",
                        "type": "video",
                        "headers": {
                            "Referer": url,
                            "User-Agent": headers["User-Agent"],
                        },
                    })
                    break

    if not items:
        raise ValueError(
            "無法從 Movieffm 頁面中解析出有效的影片串流網址 (m3u8)。所有提供之播放線路均無法連線或伺服器已失效。"
        )

    return items
