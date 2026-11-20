# -*- coding: utf-8 -*-
"""Gimy / Gimymax / Gimyplus / MacCMS 網站與劇集目錄解析器。"""

import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
import requests

from core.http_client import (
    GLOBAL_HEADERS,
    check_m3u8_accessible,
    cn_to_an,
    default_log_info,
    get_global_session,
)


def resolve_gimy_stream(player_url, page_url=""):
    """解析 Gimy 播放線路 Token (如 qsvip- / JD- / NS4K- / itdog-) 取得真實串流網址。"""
    if not player_url:
        return ""
    if player_url.startswith(("http://", "https://")):
        return player_url

    headers = {"User-Agent": GLOBAL_HEADERS["User-Agent"]}

    endpoints = []
    if player_url.startswith("qsvip-"):
        endpoints.append((
            f"https://v.attzy.com/ap/qs/api.php?url={urllib.parse.quote(player_url)}",
            {"Referer": f"https://v.attzy.com/ap/qs/?url={player_url}&jctype=qsvip"},
        ))
        endpoints.append((
            f"https://play.gimy.bot/qsvip/api.php?url={urllib.parse.quote(player_url)}",
            {"Referer": "https://play.gimy.bot/jd/"},
        ))
    elif player_url.startswith(("JD-", "JDQM-", "JDHG-")):
        endpoints.append((
            f"https://v.attzy.com/ap/jd/api.php?url={urllib.parse.quote(player_url)}",
            {"Referer": f"https://v.attzy.com/ap/jd/?url={player_url}&jctype=JD4K"},
        ))
        endpoints.append((
            f"https://play.gimy.bot/jd/api.php?url={urllib.parse.quote(player_url)}",
            {"Referer": "https://play.gimy.bot/jd/"},
        ))
    elif player_url.startswith(("NS4K-", "NSYS-")):
        endpoints.append((
            f"https://player.aigm.tv/n/parse.php?url={urllib.parse.quote(player_url)}",
            {"Referer": f"https://player.aigm.tv/n/?url={player_url}"},
        ))
        endpoints.append((
            f"https://play.gimy.bot/ns/api.php?url={urllib.parse.quote(player_url)}",
            {"Referer": "https://play.gimy.bot/jd/"},
        ))
    elif player_url.startswith("itdog-"):
        endpoints.append((
            f"https://v.attzy.com/ap/lb/api.php?url={urllib.parse.quote(player_url)}",
            {"Referer": f"https://v.attzy.com/ap/lb/?url={player_url}&jctype=itdog"},
        ))
    else:
        endpoints.append((
            f"https://v.attzy.com/ap/lb/api.php?url={urllib.parse.quote(player_url)}",
            {"Referer": "https://v.attzy.com/"},
        ))
        endpoints.append((
            f"https://v.attzy.com/ap/jd/api.php?url={urllib.parse.quote(player_url)}",
            {"Referer": "https://v.attzy.com/"},
        ))
        endpoints.append((
            f"https://play.gimy.bot/a/api.php?url={urllib.parse.quote(player_url)}",
            {"Referer": "https://play.gimy.bot/jd/"},
        ))

    for api_url, api_headers in endpoints:
        try:
            req_h = dict(headers)
            req_h.update(api_headers)
            r = requests.get(api_url, headers=req_h, timeout=10)
            data = r.json()
            resolved = data.get("url") or data.get("video") or data.get("playurl")
            if resolved and isinstance(resolved, str) and resolved.startswith(("http://", "https://")):
                return resolved
        except Exception:
            continue

    if player_url.startswith(("/", "./")) or any(
        ext in player_url.lower() for ext in [".m3u8", ".mp4", ".flv"]
    ):
        return urllib.parse.urljoin(page_url, player_url)

    raise ValueError(
        f"無法解析 Gimy 播放線路 ({player_url[:25]}...)，伺服器 API 解析失敗或未回傳有效影片網址。請在網頁切換至其他線路 (如 天堂雲、西瓜雲、極速雲等) 後再試。"
    )


def parse_gimy(url, cookies=None, log_cb=None, item_fetcher=None):
    """解析 Gimy / MacCMS 單集播放頁、線路 Token 及劇集總覽目錄頁。"""
    def _log(msg):
        if log_cb:
            log_cb(msg)
        else:
            default_log_info(msg)

    # 直接傳入的播放線路 token
    token_match = re.search(
        r"(qsvip-[a-zA-Z0-9_\-]+|JD[A-Z]*-[a-zA-Z0-9_\-]+|NS[A-Z0-9]*-[a-zA-Z0-9_\-]+|itdog-[a-zA-Z0-9_\-]+)",
        url,
    )
    if token_match and not url.endswith(".html"):
        raw_m3u8 = token_match.group(1)
        m3u8_url = resolve_gimy_stream(raw_m3u8, page_url=url)
        referer = (
            "https://v.attzy.com/"
            if any(d in m3u8_url for d in ["hxx", "attzy", "telegram", "shenhua"])
            else url
        )
        return [
            {
                "url": m3u8_url,
                "title": "Gimy_Video",
                "ext": "mp4",
                "type": "video",
                "headers": {
                    "Referer": referer,
                    "User-Agent": GLOBAL_HEADERS["User-Agent"],
                },
            }
        ]

    headers = {
        "User-Agent": GLOBAL_HEADERS["User-Agent"],
        "Referer": url,
    }
    session = get_global_session()
    response = None
    try:
        r = session.get(url, headers=headers, timeout=15)
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
        parsed_origin = (
            f"{urllib.parse.urlparse(url).scheme}://{urllib.parse.urlparse(url).netloc}"
        )
        api_url = f"{parsed_origin}/api.php/pk/q?k={urllib.parse.quote(pk)}"
        api_headers = dict(headers)
        api_headers["Referer"] = url
        try:
            r_api = session.get(
                api_url,
                headers=api_headers,
                cookies=getattr(response, "cookies", None),
                timeout=10,
            )
            if r_api.status_code == 200:
                pk_data = r_api.json()
                lines_list = pk_data.get("l", [])
                if lines_list and isinstance(lines_list, list):
                    candidates = [
                        l_item.get("u", "") for l_item in lines_list if l_item.get("u")
                    ]

                    valid_streams = []
                    for raw_cand in candidates:
                        try:
                            stream_u = resolve_gimy_stream(raw_cand, page_url=url)
                            if stream_u and stream_u not in valid_streams:
                                valid_streams.append(stream_u)
                        except Exception:
                            continue

                    def _probe_stream(u):
                        ref = (
                            "https://v.attzy.com/"
                            if any(
                                d in u for d in ["hxx", "attzy", "telegram", "shenhua"]
                            )
                            else url
                        )
                        probe_h = {"Referer": ref, "User-Agent": headers["User-Agent"]}
                        if check_m3u8_accessible(u, headers=probe_h):
                            return u
                        return None

                    with ThreadPoolExecutor(
                        max_workers=min(5, max(1, len(valid_streams)))
                    ) as executor:
                        probed = [
                            s for s in executor.map(_probe_stream, valid_streams) if s is not None
                        ]

                    chosen_url = (
                        probed[0]
                        if probed
                        else (valid_streams[0] if valid_streams else None)
                    )
                    backup_urls = probed[1:] if len(probed) > 1 else []

                    if chosen_url:
                        title = "Gimy_Video"
                        h1_m = re.search(r"<h1\b[^>]*>(.*?)</h1>", response.text, re.DOTALL)
                        if h1_m:
                            h1_content = h1_m.group(1)
                            a_m = re.search(r"<a\b[^>]*>(.*?)</a>", h1_content, re.DOTALL)
                            span_m = re.search(
                                r"<span\b[^>]*>(.*?)</span>", h1_content, re.DOTALL
                            )
                            base_name = (
                                re.sub(r"<[^>]+>", "", a_m.group(1)).strip() if a_m else ""
                            )
                            ep_name = (
                                re.sub(r"<[^>]+>", "", span_m.group(1)).strip()
                                if span_m
                                else ""
                            )
                            ep_name = re.sub(r"^[-\s]+", "", ep_name).strip()

                            match_s = re.search(r"第([一二三四五六七八九十\d]+)季", base_name)
                            if match_s:
                                s_num = cn_to_an(match_s.group(1))
                                base_name = re.sub(
                                    r"第[一二三四五六七八九十\d]+季",
                                    f"S{s_num}",
                                    base_name,
                                )

                            if ep_name:
                                match_e = re.search(r"第(\d+)集", ep_name)
                                if match_e:
                                    ep_str = f"E{int(match_e.group(1)):02d}"
                                else:
                                    ep_str = ep_name
                                title = f"{base_name} - {ep_str}" if base_name else ep_str
                            elif base_name:
                                title = base_name
                            else:
                                raw_h1 = re.sub(r"<[^>]+>", " ", h1_content)
                                title = " ".join(raw_h1.split()).strip()
                        else:
                            t_m = re.search(r"<title>(.*?)</title>", response.text)
                            if t_m:
                                t_raw = t_m.group(1).strip()
                                t_raw = re.sub(
                                    r"\s*(?:線上看)?\s*-\s*劇迷.*$",
                                    "",
                                    t_raw,
                                    flags=re.IGNORECASE,
                                ).strip()
                                if t_raw:
                                    title = t_raw

                        title = re.sub(r'[\\/:*?"<>|]', "_", title).strip() or "Gimy_Video"

                        referer = (
                            "https://v.attzy.com/"
                            if any(
                                d in chosen_url
                                for d in ["hxx", "attzy", "telegram", "shenhua"]
                            )
                            else url
                        )
                        return [
                            {
                                "url": chosen_url,
                                "backup_urls": backup_urls,
                                "title": title,
                                "ext": "mp4",
                                "type": "video",
                                "headers": {
                                    "Referer": referer,
                                    "User-Agent": headers["User-Agent"],
                                },
                            }
                        ]
        except Exception as e:
            print(f"Error fetching Gimy pk API: {e}")

    # 尋找播放器 JSON 資料 (相容 MacCMS 的 var player_aaaa = {...} 或 var player_data = {...})
    data = None
    for m in re.finditer(
        r"var\s+(player_[a-zA-Z0-9_]*)\s*=\s*(\{.*?\})\s*(?:;|</script>)",
        response.text,
        re.DOTALL,
    ):
        try:
            candidate = json.loads(m.group(2))
            if isinstance(candidate, dict) and ("url" in candidate or "vod_data" in candidate):
                data = candidate
                break
        except Exception:
            continue

    if not data:
        m_pd = re.search(
            r"var\s+player_data\s*=\s*(.*?)\s*(?:;|</script>)", response.text, re.DOTALL
        )
        if m_pd:
            content = m_pd.group(1).strip()
            try:
                data = json.loads(content)
            except Exception:
                var_match = re.match(r"^[a-zA-Z0-9_]+$", content)
                if var_match:
                    target_var = var_match.group(0)
                    m_var = re.search(
                        rf"var\s+{target_var}\s*=\s*({{.*?}})\s*(?:;|</script>)",
                        response.text,
                        re.DOTALL,
                    )
                    if m_var:
                        try:
                            data = json.loads(m_var.group(1))
                        except Exception:
                            pass

    if not data:
        for m in re.finditer(r'(\{"flag":"play"[^<]+?\})', response.text):
            try:
                candidate = json.loads(m.group(1))
                if isinstance(candidate, dict) and ("url" in candidate or "vod_data" in candidate):
                    data = candidate
                    break
            except Exception:
                continue

    if not data:
        # 策略 4: 劇集目錄/介紹頁面
        playlist_matches = re.findall(
            r'<(?:div|ul)[^>]+class=["\'][^"\']*(?:playlist-grid|content_playlist|playlist_full|play_list)[^"\']*["\'][^>]*>(.*?)</(?:div|ul)>',
            response.text,
            re.DOTALL | re.IGNORECASE,
        )
        search_html = playlist_matches[0] if playlist_matches else response.text

        raw_ep_matches = re.findall(
            r'<a[^>]+href=["\']((?:/ep/|/play/|/vod-play-id-)[^"\']+)["\'][^>]*>(.*?)</a>',
            search_html,
            re.DOTALL,
        )
        if not raw_ep_matches and playlist_matches:
            raw_ep_matches = re.findall(
                r'<a[^>]+href=["\']((?:/ep/|/play/|/vod-play-id-)[^"\']+)["\'][^>]*>(.*?)</a>',
                response.text,
                re.DOTALL,
            )

        if raw_ep_matches:
            seen_hrefs = set()
            unique_ep_urls = []
            line_ids = [
                re.search(r"-(\d+)-\d+\.html", href).group(1)
                for href, _ in raw_ep_matches
                if re.search(r"-(\d+)-\d+\.html", href)
            ]
            target_line = line_ids[0] if line_ids else None

            for href, _ in raw_ep_matches:
                full_ep_url = urljoin(url, href)
                if (
                    target_line
                    and f"-{target_line}-" not in href
                    and re.search(r"-\d+-\d+\.html", href)
                ):
                    continue
                if full_ep_url not in seen_hrefs:
                    seen_hrefs.add(full_ep_url)
                    unique_ep_urls.append(full_ep_url)

            if unique_ep_urls:
                _log(
                    f"📺 偵測到劇集目錄/選單頁面，共解析出 {len(unique_ep_urls)} 個集數，正在併發解析各集串流..."
                )
                fetcher = item_fetcher or (lambda ep_u: parse_gimy(ep_u, cookies=cookies, log_cb=log_cb))

                def _fetch_single_ep(ep_u):
                    try:
                        return fetcher(ep_u)
                    except Exception as e:
                        _log(f"⚠️ 解析單集失敗 ({ep_u}): {e}")
                        return []

                playlist_items = []
                with ThreadPoolExecutor(
                    max_workers=min(10, max(1, len(unique_ep_urls)))
                ) as executor:
                    for ep_result in executor.map(_fetch_single_ep, unique_ep_urls):
                        if ep_result:
                            playlist_items.extend(ep_result)

                if playlist_items:
                    return playlist_items

        if token_match:
            raw_m3u8 = token_match.group(1)
            m3u8_url = resolve_gimy_stream(raw_m3u8, page_url=url)
            referer = (
                "https://v.attzy.com/"
                if any(d in m3u8_url for d in ["hxx", "attzy", "telegram", "shenhua"])
                else url
            )
            return [
                {
                    "url": m3u8_url,
                    "title": "Gimy_Video",
                    "ext": "mp4",
                    "type": "video",
                    "headers": {
                        "Referer": referer,
                        "User-Agent": GLOBAL_HEADERS["User-Agent"],
                    },
                }
            ]
        raise ValueError("Cannot find player_data in the webpage.")

    raw_m3u8 = data.get("url")
    m3u8_url = resolve_gimy_stream(raw_m3u8, page_url=url)
    title = data.get("vod_data", {}).get("vod_name", "downloaded_media")

    match_s = re.search(r"第([一二三四五六七八九十\d]+)季", title)
    if match_s:
        s_num = cn_to_an(match_s.group(1))
        title = re.sub(r"第[一二三四五六七八九十\d]+季", f"S{s_num}", title)

    ep_str = None
    match_ep = re.search(r'data-playname="([^"]+)"', response.text)
    if match_ep:
        ep_raw = match_ep.group(1).strip()
        match_e = re.search(r"第(\d+)集", ep_raw)
        if match_e:
            ep_str = f"E{int(match_e.group(1)):02d}"
        else:
            ep_str = ep_raw

    if not ep_str:
        url_filename = url.split("/")[-1].split("?")[0]
        if url_filename:
            pattern = (
                r'href=["\'][^\'"]*' + re.escape(url_filename) + r'[^>]*>\s*(.*?)\s*</a>'
            )
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
                        if re.search(r"\d+", m_clean):
                            ep_raw = m_clean
                            break
                if not ep_raw:
                    ep_raw = matches[-1].strip()

                if ep_raw:
                    match_e = re.search(r"第(\d+)集", ep_raw)
                    if match_e:
                        ep_str = f"E{int(match_e.group(1)):02d}"
                    else:
                        ep_str = ep_raw

    if not ep_str:
        url_filename = url.split("/")[-1].split("?")[0]
        url_id = url_filename.split(".")[0]
        match_num = re.search(r"[-_](\d+)$", url_id)
        if match_num:
            ep_str = f"E{int(match_num.group(1)):02d}"

    if ep_str:
        title = f"{title}_{ep_str}"

    title = re.sub(r'[\\/:*?"<>|]', "_", title)

    referer = (
        "https://v.attzy.com/"
        if any(d in m3u8_url for d in ["hxx", "attzy", "telegram", "shenhua"])
        else url
    )
    return [
        {
            "url": m3u8_url,
            "title": title,
            "ext": "mp4",
            "type": "video",
            "headers": {
                "Referer": referer,
                "User-Agent": GLOBAL_HEADERS["User-Agent"],
            },
        }
    ]
