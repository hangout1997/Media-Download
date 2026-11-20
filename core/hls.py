# -*- coding: utf-8 -*-
"""HLS 播放清單廣告過濾與平行滑動視窗高速下載引擎。"""

import gc
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.http_client import (
    GLOBAL_HEADERS,
    default_log_toast,
    format_time,
    get_global_session,
)


class RAMDiskSpaceExceededError(Exception):
    """當影片切片或封裝預估/實際體積超出 RAM Disk 容量時拋出，嚴格禁止降級使用 SSD 暫存（零 SSD 磨損原則）。"""

    def __init__(self, title, url, needed_bytes=0, free_bytes=0, reason=""):
        self.title = title
        self.url = url
        self.needed_bytes = needed_bytes
        self.free_bytes = free_bytes
        self.reason = reason
        super().__init__(reason)


def _get_ramdisk_manager():
    if "ramdisk_manager" in sys.modules:
        return sys.modules["ramdisk_manager"]
    try:
        from services import ramdisk_manager as rm
        return rm
    except ImportError:
        try:
            import ramdisk_manager as rm
            return rm
        except ImportError:
            return None


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
                "url": full_url,
                "raw_line": line,
                "dur": current_dur,
                "disc": disc_flag,
            })
            disc_flag = False
            current_dur = 2.0

    if not raw_segments:
        return [], [], m3u8_text, 0, 0.0

    total_stream_dur = sum(s["dur"] for s in raw_segments)

    # 識別 sections (由 discontinuity 分割的區塊)
    sections = []
    cur_sec = []
    for s in raw_segments:
        if s["disc"] and cur_sec:
            sections.append(cur_sec)
            cur_sec = []
        cur_sec.append(s)
    if cur_sec:
        sections.append(cur_sec)

    clean_segments = []
    filtered_ads = []
    ad_keywords = [
        "/ad/", "/ads/", "/adv/", "guanggao", "advertisement",
        "notice.ts", "banner", "ad_",
    ]

    AD_MAX_DURATION = 35.0  # 賭博/插播廣告區塊時長通常在 3~35 秒內

    # 真正的廣告插入串流必定存在顯著較長的主正片 section (例如 >= 90s 或單一 section 佔總片長 >= 30%)
    has_dominant_feature = any(
        sum(s["dur"] for s in sec) >= 90.0
        or (sum(s["dur"] for s in sec) / total_stream_dur >= 0.3)
        for sec in sections
    )
    short_sections = [
        sec for sec in sections if sum(s["dur"] for s in sec) <= AD_MAX_DURATION
    ]
    short_sec_total_dur = sum(sum(s["dur"] for s in sec) for sec in short_sections)

    allow_section_ad_filter = (
        total_stream_dur > 180.0
        and len(sections) > 1
        and has_dominant_feature
        and (short_sec_total_dur / total_stream_dur <= 0.20)
    )

    for sec_idx, sec in enumerate(sections):
        sec_dur = sum(s["dur"] for s in sec)
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
                url_lower = s["url"].lower()
                if any(kw in url_lower for kw in ad_keywords):
                    filtered_ads.append(s)
                else:
                    clean_segments.append(s)

    # 兜底防護：若過濾後切片為空，或剩餘時長嚴重小於原本總時長的 50% (且總片長 > 60s)，表示發生誤殺，自動回退
    clean_total_dur = sum(s["dur"] for s in clean_segments)
    if not clean_segments or (
        total_stream_dur > 60.0 and clean_total_dur / total_stream_dur < 0.5
    ):
        clean_segments = []
        filtered_ads = []
        for s in raw_segments:
            url_lower = s["url"].lower()
            if any(kw in url_lower for kw in ad_keywords):
                filtered_ads.append(s)
            else:
                clean_segments.append(s)

    clean_urls = [s["url"] for s in clean_segments]
    clean_durs = [s["dur"] for s in clean_segments]
    total_ad_dur = sum(s["dur"] for s in filtered_ads)

    # 構建乾淨的 m3u8 文本
    clean_m3u8_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:10",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for s in clean_segments:
        clean_m3u8_lines.append(f"#EXTINF:{s['dur']:.6f},")
        clean_m3u8_lines.append(s["url"])
    clean_m3u8_lines.append("#EXT-X-ENDLIST")
    clean_m3u8_text = "\n".join(clean_m3u8_lines)

    return clean_urls, clean_durs, clean_m3u8_text, len(filtered_ads), total_ad_dur


def download_fast_parallel_hls(
    m3u8_url,
    out_path=None,
    extra_headers=None,
    max_workers=None,
    label="影片",
    progress_cb=None,
    status_cb=None,
    toast_cb=None,
    scratch_dir=None,
    m3u8_content=None,
    pref_quality="720p",
    abort_event=None,
    pause_event=None,
    strict_ramdisk=False,
    active_info=None,
    active_lock=None,
):
    """
    平行 HLS 下載引擎（滑動視窗串流寫入，支援 RAM Disk 零 SSD 磨損防護、自動過濾廣告切片與畫質選流）。
    progress_cb: 可選 callback(pct: float, completed_dur: float, total_dur: float, speed_mb: float, speed_x: float)
    status_cb: 可選 callback(status_markdown: str)
    toast_cb: 可選 callback(msg: str, icon: str)
    scratch_dir: 封裝中繼目錄 (預設為 None，依序嘗試 RAM Disk 或系統暫存)
    m3u8_content: 可選預先抓取的 m3u8 內容 (免除二次 HTTP 請求與 Cloudflare 403 阻擋)
    """
    from curl_cffi import requests as curl_requests

    if abort_event is not None:
        abort_event.clear()
    if active_info is not None and active_lock is not None:
        with active_lock:
            active_info["active"] = True
            active_info["title"] = label
            active_info["time"] = time.time()

    try:
        req_headers = dict(GLOBAL_HEADERS)
        if extra_headers:
            req_headers.update(extra_headers)
        if "Referer" not in req_headers:
            req_headers["Referer"] = f"https://{urllib.parse.urlparse(m3u8_url).netloc}/"
        if "surrit" in m3u8_url or "missav" in req_headers.get("Referer", ""):
            req_headers["Origin"] = "https://missav.ai"
            if "Referer" not in req_headers or not req_headers["Referer"]:
                req_headers["Referer"] = "https://missav.ai/"
            req_headers["Sec-Fetch-Dest"] = "empty"
            req_headers["Sec-Fetch-Mode"] = "cors"
            req_headers["Sec-Fetch-Site"] = "cross-site"

        session = get_global_session()
        thread_local = threading.local()

        def get_curl_session():
            if not hasattr(thread_local, "curl_session"):
                thread_local.curl_session = curl_requests.Session(impersonate="chrome124")
            return thread_local.curl_session

        def fetch_text(target_url):
            err_details = []
            try:
                r = get_curl_session().get(target_url, headers=req_headers, timeout=30)
                if r.status_code == 200 and r.text:
                    return r.text
                err_details.append(f"curl_cffi HTTP {r.status_code}")
            except Exception as e:
                err_details.append(f"curl_cffi: {e}")
            try:
                r = session.get(target_url, headers=req_headers, timeout=30)
                if r.status_code == 200 and r.text:
                    return r.text
                err_details.append(f"requests HTTP {r.status_code}")
            except Exception as e:
                err_details.append(f"requests: {e}")
            diag = "; ".join(err_details)
            raise ValueError(f"無法讀取 m3u8 串流選單 ({target_url}): {diag}")

        # 若已預先提供有效 m3u8 內容，直接跳過網路請求
        if m3u8_content and (
            m3u8_content.lstrip().startswith("#EXT")
            or "#EXTM3U" in m3u8_content
            or "#EXT-X-" in m3u8_content
        ):
            text = m3u8_content
        else:
            text = fetch_text(m3u8_url)

        clean_text = text.lstrip("\ufeff \t\r\n")
        if not (
            clean_text.startswith("#EXT")
            or "#EXTM3U" in clean_text
            or "#EXT-X-" in clean_text
        ):
            raise ValueError(
                f"伺服器回傳非有效的 m3u8 內容 (可能為網頁阻擋或無效連結): {clean_text[:120]}"
            )

        # ── Master Playlist：優先用 RESOLUTION 高度排序，其次 BANDWIDTH ──────
        selected_bw = 0
        if "#EXT-X-STREAM-INF" in text:
            sub_playlists = []
            current_inf = {}
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("#EXT-X-STREAM-INF"):
                    bw_match = re.search(r"BANDWIDTH=(\d+)", line)
                    res_match = re.search(r"RESOLUTION=\d+x(\d+)", line)
                    current_inf["bw"] = int(bw_match.group(1)) if bw_match else 0
                    current_inf["height"] = int(res_match.group(1)) if res_match else 0
                elif line and not line.startswith("#"):
                    sub_playlists.append((
                        current_inf.get("height", 0),
                        current_inf.get("bw", 0),
                        urllib.parse.urljoin(m3u8_url, line),
                    ))
                    current_inf = {}
            if sub_playlists:
                sub_playlists.sort(key=lambda x: (x[0], x[1]), reverse=True)
                chosen = None
                if pref_quality == "720p":
                    cand_720 = [sp for sp in sub_playlists if sp[0] <= 720]
                    if cand_720:
                        chosen = cand_720[0]
                if not chosen:
                    chosen = sub_playlists[0]
                selected_bw = chosen[1]
                m3u8_url = chosen[2]
                text = fetch_text(m3u8_url)
                clean_sub = text.lstrip("\ufeff \t\r\n")
                if not (
                    clean_sub.startswith("#EXT")
                    or "#EXTM3U" in clean_sub
                    or "#EXT-X-" in clean_sub
                ):
                    raise ValueError(f"取得之子播放清單非有效 m3u8: {clean_sub[:120]}")

        # ── 智慧廣告切片過濾 ──────────────────────────────────────────────────
        segment_urls, segment_durations, _, ad_count, ad_dur = filter_and_clean_m3u8_ads(
            text, m3u8_url
        )
        if ad_count > 0:
            msg = f"🛡️ 已自動過濾 {ad_count} 個廣告切片 ({ad_dur:.1f} 秒賭博/貼片廣告)，為您保留純淨正片！"
            if toast_cb:
                toast_cb(msg, icon="🛡️")
            else:
                default_log_toast(msg, icon="🛡️")

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

        # ── 智慧決定 TS 中繼暫存目錄（優先 RAM Disk，100% 零 SSD 寫入）───────
        rm = _get_ramdisk_manager()
        if total_duration > 0:
            fallback_rate = (
                (220 * 1024)
                if (pref_quality == "720p" or any(k in m3u8_url for k in ["720", "842"]))
                else (420 * 1024)
            )
            est_rate = (selected_bw / 8) if selected_bw > 0 else fallback_rate
            est_needed_bytes = int(total_duration * est_rate)
        else:
            seg_sz_mb = 1.0 if pref_quality == "720p" else 2.0
            est_needed_bytes = int(total_segments * seg_sz_mb * 1024 * 1024)

        is_out_on_ram = (
            rm is not None
            and rm.is_ramdisk_mounted()
            and out_path
            and "/Volumes/MediaRAMDisk" in out_path
        )
        multiplier = 2.1 if is_out_on_ram else 1.1
        required_ram_bytes = int(est_needed_bytes * multiplier + 300 * 1024 * 1024)

        if strict_ramdisk and rm is not None and rm.is_ramdisk_mounted():
            _, _, free_ram, _ = rm.get_ramdisk_usage()
            if free_ram < required_ram_bytes:
                rm.clean_scratch_files(preserve_mp4=True, max_age_seconds=300)
                _, _, free_ram, _ = rm.get_ramdisk_usage()
                if free_ram < required_ram_bytes:
                    raise RAMDiskSpaceExceededError(
                        title=label,
                        url=m3u8_url,
                        needed_bytes=required_ram_bytes,
                        free_bytes=free_ram,
                        reason=(
                            f"串流切片與封裝預估需 {required_ram_bytes/(1024**3):.2f} GB，"
                            f"超出 RAM Disk 剩餘可用容量 ({free_ram/(1024**3):.2f} GB)"
                        ),
                    )
            scratch_dir = rm.MOUNT_POINT
        elif scratch_dir is None:
            if rm is not None:
                try:
                    scratch_dir = rm.get_scratch_dir(min_free_bytes=required_ram_bytes)
                except Exception:
                    scratch_dir = tempfile.gettempdir()
            else:
                scratch_dir = tempfile.gettempdir()

        tmp_ts = None
        tmp_ts_path = None
        try:
            tmp_ts = tempfile.NamedTemporaryFile(suffix=".ts", delete=False, dir=scratch_dir)
            tmp_ts_path = tmp_ts.name
        except OSError:
            if (
                strict_ramdisk
                and rm is not None
                and rm.is_ramdisk_mounted()
                and scratch_dir == rm.MOUNT_POINT
            ):
                _, _, free_ram, _ = rm.get_ramdisk_usage()
                raise RAMDiskSpaceExceededError(
                    title=label,
                    url=m3u8_url,
                    needed_bytes=required_ram_bytes,
                    free_bytes=free_ram,
                    reason="RAM Disk 無法建立中繼 TS 暫存檔 (磁碟空間不足)",
                )
            if strict_ramdisk:
                raise
            tmp_ts = tempfile.NamedTemporaryFile(
                suffix=".ts", delete=False, dir=tempfile.gettempdir()
            )
            tmp_ts_path = tmp_ts.name

        pending_segments = {}
        next_write_idx = 0
        valid_count = 0

        is_cf_protected = (
            any(k in m3u8_url for k in ["surrit", "ppqrrs", "play-cdn", "bfikun"])
            or "missav" in req_headers.get("Referer", "")
            or "missav" in req_headers.get("Origin", "")
        )
        seg_fail_sample = []

        def download_segment(args):
            if abort_event is not None and abort_event.is_set():
                return -1, b""
            if pause_event is not None:
                pause_event.wait()
            if abort_event is not None and abort_event.is_set():
                return -1, b""

            idx, seg_url = args
            for attempt in range(3):
                if abort_event is not None and abort_event.is_set():
                    return -1, b""
                if pause_event is not None:
                    pause_event.wait()
                if abort_event is not None and abort_event.is_set():
                    return -1, b""

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
                    elif r.status_code != 200 and len(seg_fail_sample) < 3:
                        seg_fail_sample.append(f"curl_cffi HTTP {r.status_code}")
                except Exception as e:
                    if len(seg_fail_sample) < 3:
                        seg_fail_sample.append(f"curl_cffi: {e}")

                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
            return idx, b""

        t0 = time.time()
        last_update_time = 0.0
        completed = 0
        completed_duration = 0.0
        total_downloaded_bytes = 0

        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(download_segment, (i, url)): i
                    for i, url in enumerate(segment_urls)
                }
                for f in as_completed(futures):
                    if abort_event is not None and abort_event.is_set():
                        for rem_f in futures:
                            rem_f.cancel()
                        raise InterruptedError("下載任務已由使用者強制中斷。")

                    idx, content = f.result()
                    if abort_event is not None and abort_event.is_set():
                        for rem_f in futures:
                            rem_f.cancel()
                        raise InterruptedError("下載任務已由使用者強制中斷。")

                    completed += 1
                    completed_duration += segment_durations[idx]
                    content_len = len(content)
                    total_downloaded_bytes += content_len

                    if content_len > 0:
                        valid_count += 1

                    pending_segments[idx] = content

                    while next_write_idx in pending_segments:
                        chunk = pending_segments.pop(next_write_idx)
                        if len(chunk) > 0:
                            try:
                                tmp_ts.write(chunk)
                            except OSError as write_err:
                                if strict_ramdisk and (
                                    getattr(write_err, "errno", None) == 28
                                    or "No space left" in str(write_err)
                                ):
                                    for rem_f in futures:
                                        rem_f.cancel()
                                    free_ram = 0
                                    if rm is not None and rm.is_ramdisk_mounted():
                                        _, _, free_ram, _ = rm.get_ramdisk_usage()
                                    raise RAMDiskSpaceExceededError(
                                        title=label,
                                        url=m3u8_url,
                                        needed_bytes=total_downloaded_bytes,
                                        free_bytes=free_ram,
                                        reason="切片串流寫入中途 RAM Disk 空間耗盡 (No space left on device)",
                                    )
                                raise
                        del chunk
                        next_write_idx += 1

                    # 早期熔斷保護：若前 20 個切片全數失敗且無一成功，立即中斷切換備援線路
                    if completed >= 20 and valid_count == 0:
                        for rem_f in futures:
                            rem_f.cancel()
                        fail_diag = (
                            f" (切片連線回傳: {'; '.join(seg_fail_sample)})"
                            if seg_fail_sample
                            else ""
                        )
                        raise ValueError(
                            f"切片下載成功率不足 (前 {completed} 個切片全數遭來源阻擋，觸發線路熔斷備援){fail_diag}。"
                        )

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
                            progress_cb(
                                pct, completed_duration, total_duration, speed_mb, speed_x
                            )
                        if status_cb:
                            is_p = (pause_event is not None) and (not pause_event.is_set())
                            paused_badge = (
                                " ｜ ⏸️ **(已暫停-頻寬已釋出，點上方按鈕可恢復)**"
                                if is_p
                                else ""
                            )
                            if total_duration > 0:
                                status_cb(
                                    f"⏳ **{label}進度**: `{pct * 100:.1f}%` "
                                    f"({format_time(completed_duration)} / {format_time(total_duration)}) "
                                    f"| 速度: `{speed_x:.2f}x` (`{speed_mb:.2f} MB/s`) "
                                    f"| 線程: `{max_workers}`{paused_badge}"
                                )
                            else:
                                status_cb(
                                    f"⏳ **{label}處理中...** 已完成 `{completed}/{total_segments}` "
                                    f"(`{mb_downloaded:.1f} MB`) "
                                    f"| 速度: `{speed_x:.2f}x` (`{speed_mb:.2f} MB/s`){paused_badge}"
                                )

            tmp_ts.flush()
            tmp_ts.close()
            tmp_ts = None
            pending_segments.clear()

            if abort_event is not None and abort_event.is_set():
                raise InterruptedError("下載任務已由使用者強制中斷。")

            if total_segments > 0 and valid_count < total_segments * 0.7:
                fail_diag = (
                    f" (切片連線回傳: {'; '.join(seg_fail_sample)})"
                    if seg_fail_sample
                    else ""
                )
                raise ValueError(
                    f"切片下載成功率不足 (成功 {valid_count}/{total_segments})，來源伺服器防護阻擋了切片連線{fail_diag}。"
                )

            if status_cb:
                status_cb("⚡ **多線程切片串流寫入完成，正在透過 FFmpeg 無損封裝為 MP4...**")

            # ── 封裝前空間防護 (Zero-ENOSPC Safe Guard) ─────────────────────
            actual_ts_size = (
                os.path.getsize(tmp_ts_path)
                if (tmp_ts_path and os.path.exists(tmp_ts_path))
                else 0
            )
            if rm is not None:
                try:
                    is_target_on_ram = (
                        rm.is_ramdisk_mounted()
                        and out_path
                        and "/Volumes/MediaRAMDisk" in out_path
                    )
                    if is_target_on_ram:
                        _, _, free_ram, _ = rm.get_ramdisk_usage()
                        needed_output_space = actual_ts_size + 250 * 1024 * 1024
                        if free_ram < needed_output_space:
                            msg = (
                                f"TS 切片實際體積 ({actual_ts_size/1024/1024/1024:.2f} GB) "
                                f"加上 MP4 封裝空間超過 RAM Disk 剩餘可用容量 ({free_ram/1024/1024/1024:.2f} GB)"
                            )
                            if strict_ramdisk:
                                raise RAMDiskSpaceExceededError(
                                    title=label,
                                    url=m3u8_url,
                                    needed_bytes=needed_output_space,
                                    free_bytes=free_ram,
                                    reason=msg,
                                )
                            raise ValueError(msg + "，為保護 SSD 零磨損已自動安全跳過。")
                except (RAMDiskSpaceExceededError, ValueError):
                    raise
                except Exception:
                    pass

            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-i", tmp_ts_path,
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                "-movflags", "+faststart",
                out_path,
            ]
            proc = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                if out_path and os.path.exists(out_path):
                    try:
                        os.remove(out_path)
                    except Exception:
                        pass
                err_raw = proc.stderr.decode("utf-8", errors="ignore")
                if strict_ramdisk and (
                    "No space left on device" in err_raw or "code: -28" in err_raw
                ):
                    free_ram = 0
                    if rm is not None and rm.is_ramdisk_mounted():
                        _, _, free_ram, _ = rm.get_ramdisk_usage()
                    raise RAMDiskSpaceExceededError(
                        title=label,
                        url=m3u8_url,
                        needed_bytes=actual_ts_size,
                        free_bytes=free_ram,
                        reason="FFmpeg 封裝時 RAM Disk 空間不足 (No space left on device)",
                    )
                err_lines = [
                    l.strip()
                    for l in err_raw.splitlines()
                    if l.strip()
                    and not any(
                        l.strip().startswith(k)
                        for k in [
                            "ffmpeg version",
                            "configuration:",
                            "built with",
                            "libav",
                            "libsw",
                            "libpost",
                        ]
                    )
                ]
                clean_err = " | ".join(err_lines[-3:]) if err_lines else err_raw[-300:]
                raise ValueError(f"FFmpeg 無損封裝失敗: {clean_err}")

            proc.stdout = None
            proc.stderr = None
            del proc
            gc.collect()
        finally:
            if tmp_ts:
                try:
                    tmp_ts.close()
                except Exception:
                    pass
            try:
                if tmp_ts_path and os.path.exists(tmp_ts_path):
                    os.remove(tmp_ts_path)
            except Exception:
                pass
            pending_segments.clear()
            gc.collect()

        return out_path
    finally:
        if active_info is not None and active_lock is not None:
            with active_lock:
                active_info["active"] = False
                active_info["title"] = ""
