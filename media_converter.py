import os
import subprocess
import shutil
from PIL import Image, ImageOps

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif", ".heic", ".heif", ".avif", ".ico"}
SUPPORTED_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts", ".m4v", ".wmv"}
SUPPORTED_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".wma"}

def get_safe_output_path(out_dir, base_name, target_ext):
    """
    產生安全的輸出路徑，若已有同名檔案則附加編號以防覆蓋
    """
    target_ext = target_ext.lstrip('.').lower()
    candidate = os.path.join(out_dir, f"{base_name}.{target_ext}")
    if not os.path.exists(candidate):
        return candidate
    
    counter = 1
    while True:
        candidate = os.path.join(out_dir, f"{base_name}_{counter}.{target_ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1

def convert_image(
    input_source,
    output_path,
    target_format="JPEG",
    quality=95,
    bg_color=(255, 255, 255),
    max_dimension=None
):
    """
    圖片格式轉換核心函式
    :param input_source: 檔案路徑 (str) 或 BytesIO / 檔案物件
    :param output_path: 目標儲存路徑
    :param target_format: 目標格式 ("JPEG", "PNG", "WEBP")
    :param quality: JPEG/WEBP 品質 (1-100)
    :param bg_color: RGBA 轉 RGB 時透明背景替換顏色，預設白色 (255, 255, 255)
    :param max_dimension: 最大寬/高限制 (等比例縮放)，None 表示不縮放
    :return: (bool, str) 成功與否與訊息
    """
    target_format = target_format.upper()
    if target_format == "JPG":
        target_format = "JPEG"
        
    is_path = isinstance(input_source, str)
    is_heic = False
    
    if is_path:
        ext = os.path.splitext(input_source)[1].lower()
        if ext in [".heic", ".heif"]:
            is_heic = True

    # 針對 HEIC 且是在 macOS 上，優先嘗試 sips
    if is_heic and shutil.which("sips") and target_format == "JPEG":
        try:
            cmd = ["/usr/bin/sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality), input_source, "--out", output_path]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0 and os.path.exists(output_path):
                return True, f"成功轉換 HEIC 圖片至 {output_path}"
        except Exception:
            pass

    try:
        with Image.open(input_source) as img:
            # 1. 自動修正拍攝方向 (EXIF Transpose)
            img = ImageOps.exif_transpose(img)

            # 2. 處理尺寸縮放
            if max_dimension and max_dimension > 0:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

            # 3. 處理格式色彩模式
            if target_format == "JPEG":
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    # 透明通道處理：貼合在指定背景色上，避免黑底或報錯
                    img_rgba = img.convert("RGBA")
                    background = Image.new("RGB", img_rgba.size, bg_color)
                    background.paste(img_rgba, mask=img_rgba.split()[3])
                    final_img = background
                elif img.mode != "RGB":
                    final_img = img.convert("RGB")
                else:
                    final_img = img
                final_img.save(output_path, "JPEG", quality=quality, optimize=True)
            elif target_format == "PNG":
                img.save(output_path, "PNG", optimize=True)
            elif target_format == "WEBP":
                img.save(output_path, "WEBP", quality=quality)
            else:
                img.save(output_path, target_format)

            return True, f"成功轉換為 {target_format}：{os.path.basename(output_path)}"
    except Exception as e:
        # Fallback: 若 Pillow 無法開啟（如特殊 HEIC 或 AVIF），嘗試使用 ffmpeg 或 sips
        if is_path and shutil.which("ffmpeg"):
            try:
                ffmpeg_cmd = ["ffmpeg", "-y", "-i", input_source]
                if max_dimension:
                    ffmpeg_cmd.extend(["-vf", f"scale='min({max_dimension},iw)':-1"])
                if target_format == "JPEG":
                    ffmpeg_cmd.extend(["-q:v", "2", output_path])
                else:
                    ffmpeg_cmd.append(output_path)
                subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                if os.path.exists(output_path):
                    return True, f"透過 FFmpeg 成功轉換為 {target_format}：{os.path.basename(output_path)}"
            except Exception as fe:
                return False, f"轉換失敗：Pillow 錯誤 ({e})；FFmpeg 錯誤 ({fe})"
        return False, f"圖片轉換失敗: {e}"

def convert_video(
    input_path,
    output_path,
    target_format="MP4",
    mode="smart",
    resolution="original",
    quality_preset="high"
):
    """
    視訊轉換與封裝
    :param input_path: 本機影片檔案路徑
    :param output_path: 目標路徑
    :param target_format: MP4, MOV, MKV, GIF, WEBM
    :param mode: 'smart' (相容直接複製，不相容則硬解轉碼), 'remux' (純封裝拷貝), 'transcode' (強制轉碼)
    :param resolution: 'original', '1080p', '720p', '480p'
    :param quality_preset: 'high', 'medium', 'low'
    :return: (bool, str)
    """
    target_format = target_format.upper()
    
    # 1. 處理轉動圖 GIF (兩段式調色盤優化)
    if target_format == "GIF":
        scale_map = {
            "original": "scale=iw:-1:flags=lanczos",
            "1080p": "scale=1920:-1:flags=lanczos",
            "720p": "scale=1280:-1:flags=lanczos",
            "480p": "scale=480:-1:flags=lanczos"
        }
        scale_filter = scale_map.get(resolution, "scale=480:-1:flags=lanczos")
        fps = 15 if quality_preset != "high" else 20
        vf_filter = f"fps={fps},{scale_filter},split[s0][s1];[s0]palettegen=max_colors=256[p];[s1][p]paletteuse=dither=bayer"
        
        cmd = ["ffmpeg", "-y", "-i", input_path, "-vf", vf_filter, output_path]
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            return True, f"成功轉換為優質 GIF：{os.path.basename(output_path)}"
        except subprocess.CalledProcessError as e:
            return False, f"GIF 轉換失敗: {e.stderr.decode('utf-8', errors='ignore')}"

    # 2. 快速無損封裝 (Remux) 嘗試
    if mode in ("smart", "remux"):
        remux_cmd = ["ffmpeg", "-y", "-i", input_path, "-c", "copy"]
        if target_format == "MP4":
            remux_cmd.extend(["-movflags", "+faststart"])
        remux_cmd.append(output_path)
        
        try:
            subprocess.run(remux_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                return True, f"⚡ 極速無損重新封裝 (Remux) 完成：{os.path.basename(output_path)}"
        except Exception:
            if mode == "remux":
                return False, "編碼格式不相容，無法直接無損封裝 (Remux)。請切換為「相容轉碼」模式。"
            # mode == 'smart' 則自動降級到重新編碼

    # 3. 重新編碼 (Transcode) - 優先啟用 macOS VideoToolbox 硬體加速
    scale_args = []
    if resolution == "1080p":
        scale_args = ["-vf", "scale=-2:1080"]
    elif resolution == "720p":
        scale_args = ["-vf", "scale=-2:720"]
    elif resolution == "480p":
        scale_args = ["-vf", "scale=-2:480"]

    bitrate_map = {
        "high": ("6000k", "192k"),
        "medium": ("3500k", "160k"),
        "low": ("1800k", "128k")
    }
    v_bitrate, a_bitrate = bitrate_map.get(quality_preset, ("4000k", "160k"))

    # 構建硬體加速參數 (macOS h264_videotoolbox)
    cmd = ["ffmpeg", "-y", "-i", input_path]
    cmd.extend(scale_args)

    if target_format in ("MP4", "MOV"):
        cmd.extend([
            "-c:v", "h264_videotoolbox",
            "-b:v", v_bitrate,
            "-c:a", "aac",
            "-b:a", a_bitrate,
            "-movflags", "+faststart"
        ])
    elif target_format == "MKV":
        cmd.extend([
            "-c:v", "h264_videotoolbox",
            "-b:v", v_bitrate,
            "-c:a", "aac",
            "-b:a", a_bitrate
        ])
    elif target_format == "WEBM":
        cmd.extend([
            "-c:v", "libvpx-vp9",
            "-b:v", v_bitrate,
            "-c:a", "libopus",
            "-b:a", a_bitrate
        ])
    else:
        cmd.extend(["-c:v", "libx264", "-c:a", "aac"])

    cmd.append(output_path)

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0:
            return True, f"✅ 影片轉碼完成：{os.path.basename(output_path)}"
        
        # 若硬解失敗（如非標準解析度），降級為軟解 libx264
        fallback_cmd = ["ffmpeg", "-y", "-i", input_path]
        fallback_cmd.extend(scale_args)
        fallback_cmd.extend(["-c:v", "libx264", "-crf", "23", "-c:a", "aac", "-b:a", a_bitrate])
        if target_format == "MP4":
            fallback_cmd.extend(["-movflags", "+faststart"])
        fallback_cmd.append(output_path)
        
        fallback_res = subprocess.run(fallback_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if fallback_res.returncode == 0:
            return True, f"✅ 影片軟解轉碼完成：{os.path.basename(output_path)}"
        return False, f"轉碼失敗: {fallback_res.stderr[-300:]}"
    except Exception as e:
        return False, f"執行例外: {e}"

def convert_audio(
    input_path,
    output_path,
    target_format="MP3",
    audio_bitrate="192k",
    sample_rate=None,
    channels=None
):
    """
    音訊格式轉換
    :param input_path: 本機音訊或影片檔案路徑
    :param output_path: 目標輸出路徑
    :param target_format: MP3, WAV, M4A, FLAC, OGG
    :param audio_bitrate: '128k', '192k', '320k'
    :param sample_rate: 如 16000 (Whisper 適用), 44100, 48000
    :param channels: 1 (單聲道), 2 (立體聲)
    :return: (bool, str)
    """
    target_format = target_format.upper()
    cmd = ["ffmpeg", "-y", "-i", input_path, "-vn"]

    if sample_rate:
        cmd.extend(["-ar", str(sample_rate)])
    if channels:
        cmd.extend(["-ac", str(channels)])

    if target_format == "MP3":
        cmd.extend(["-c:a", "libmp3lame", "-b:a", audio_bitrate])
    elif target_format == "WAV":
        cmd.extend(["-c:a", "pcm_s16le"])
    elif target_format == "M4A":
        cmd.extend(["-c:a", "aac", "-b:a", audio_bitrate])
    elif target_format == "FLAC":
        cmd.extend(["-c:a", "flac"])
    elif target_format == "OGG":
        cmd.extend(["-c:a", "libopus", "-b:a", audio_bitrate])
    else:
        cmd.extend(["-c:a", "copy"])

    cmd.append(output_path)

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0:
            return True, f"🎵 音訊轉換完成：{os.path.basename(output_path)}"
        return False, f"音訊轉換失敗: {res.stderr[-300:]}"
    except Exception as e:
        return False, f"執行例外: {e}"

def scan_files_for_conversion(target_dir, category="image", recursive=True):
    """
    掃描指定資料夾中的目標檔案
    """
    if category == "image":
        valid_exts = SUPPORTED_IMAGE_EXTS
    elif category == "video":
        valid_exts = SUPPORTED_VIDEO_EXTS
    elif category == "audio":
        valid_exts = SUPPORTED_AUDIO_EXTS
    else:
        valid_exts = SUPPORTED_IMAGE_EXTS | SUPPORTED_VIDEO_EXTS | SUPPORTED_AUDIO_EXTS

    found = []
    if not os.path.exists(target_dir):
        return found

    if recursive:
        for root, _, files in os.walk(target_dir):
            for f in sorted(files):
                if os.path.splitext(f)[1].lower() in valid_exts:
                    found.append(os.path.join(root, f))
    else:
        for f in sorted(os.listdir(target_dir)):
            full = os.path.join(target_dir, f)
            if os.path.isfile(full) and os.path.splitext(f)[1].lower() in valid_exts:
                found.append(full)
    return found
