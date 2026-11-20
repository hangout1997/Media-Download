# -*- coding: utf-8 -*-
"""FFmpeg 與 ffprobe 探測工具及具進度監聽/安全中斷之執行器。"""

import json
import re
import subprocess
import time
import requests

_cached_ffmpeg_hls_args = None


def get_ffmpeg_hls_args():
    """動態探測當前系統 FFmpeg 對 HLS demuxer 選項的相容性 (相容 FFmpeg 4.x ~ 9.x)。"""
    global _cached_ffmpeg_hls_args
    if _cached_ffmpeg_hls_args is not None:
        return list(_cached_ffmpeg_hls_args)

    opts = ["-allowed_extensions", "ALL"]
    try:
        proc = subprocess.run(
            ["ffmpeg", "-h", "demuxer=hls"], capture_output=True, text=True, timeout=3
        )
        hls_help = (proc.stdout or "") + (proc.stderr or "")
        if "-allowed_segment_extensions" in hls_help:
            opts += ["-allowed_segment_extensions", "ALL"]
        if "-extension_picky" in hls_help:
            opts += ["-extension_picky", "0"]
    except Exception:
        pass
    _cached_ffmpeg_hls_args = opts
    return list(_cached_ffmpeg_hls_args)


def get_media_duration(media_url, headers=None):
    """取得 m3u8 播放清單、線上串流或本機檔案的總時長（秒）。"""
    if "m3u8" in media_url:
        try:
            req_headers = {}
            if headers:
                req_headers.update(headers)
            res = requests.get(media_url, headers=req_headers, timeout=4)
            if res.status_code == 200:
                extinfs = re.findall(r"#EXTINF:([\d\.]+)", res.text)
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
                headers_arg += get_ffmpeg_hls_args()

        probe_cmd = ["ffprobe", "-v", "error"] + headers_arg + [
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            media_url,
        ]
        out = subprocess.check_output(
            probe_cmd,
            text=True,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        dur = float(out)
        if dur > 0:
            return dur
    except Exception:
        pass
    return 0.0


def get_audio_stream_info(media_path, headers=None):
    """透過 ffprobe 探測音訊串流資訊：(codec_name, bitrate_kbps, channels)。"""
    headers_arg = []
    if headers:
        headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        headers_arg = ["-headers", headers_str]
    if "m3u8" in media_path:
        headers_arg += get_ffmpeg_hls_args()

    probe_cmd = [
        "ffprobe", "-v", "error",
    ] + headers_arg + [
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,bit_rate,channels:format=bit_rate",
        "-of", "json",
        media_path,
    ]
    try:
        raw = subprocess.check_output(
            probe_cmd,
            text=True,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=6,
        )
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


def run_ffmpeg_with_progress(
    ffmpeg_cmd,
    total_duration=0.0,
    label="下載",
    progress_cb=None,
    abort_event=None,
):
    """
    執行 FFmpeg 並監聽 progress 輸出，且確保例外或中斷時不留殭屍進程。
    progress_cb: 可選 callback(pct: float, current_sec: float, total_sec: float, speed: str)
    abort_event: 可選 threading.Event，若被觸發則立即終止子進程並拋出 InterruptedError
    """
    cmd = [ffmpeg_cmd[0], "-y", "-progress", "pipe:2"] + ffmpeg_cmd[2:]

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    current_sec = 0.0
    speed = "1.0x"
    last_update_time = 0.0
    stderr_lines = []

    try:
        for line in iter(process.stderr.readline, b""):
            if abort_event is not None and abort_event.is_set():
                raise InterruptedError("下載任務已由使用者強制中斷。")

            line_str = line.decode("utf-8", errors="ignore").strip()
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
    finally:
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass

    if total_duration > 0 and process.returncode == 0 and progress_cb:
        progress_cb(1.0, total_duration, total_duration, speed)

    stderr_log = "\n".join(stderr_lines)
    return process.returncode, stderr_log
