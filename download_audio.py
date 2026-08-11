import os
import re
import json
import argparse
import requests
import yt_dlp

def get_stream_info(url):
    print(f"Fetching {url}...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    
    # 找尋 player_data 變數中的 JSON 資料
    match = re.search(r'var player_data=(.*?)</script>', response.text)
    if not match:
        raise ValueError("Cannot find player_data in the webpage.")
        
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        raise ValueError("Failed to parse player_data JSON.")
        
    m3u8_url = data.get("url")
    title = data.get("vod_data", {}).get("vod_name", "downloaded_audio")
    
    # 處理檔名特殊字元
    title = re.sub(r'[\\/:*?"<>|]', '_', title)
    return m3u8_url, title

def download_audio(m3u8_url, title):
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    downloads_dir = "/Users/ericcheng/Downloads"
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("download_dir"):
                    downloads_dir = data["download_dir"]
        except Exception:
            pass
    os.makedirs(downloads_dir, exist_ok=True)
    # 設定最終輸出名稱，會帶入副檔名 (.mp3)
    outtmpl = os.path.join(downloads_dir, f"{title}.%(ext)s")
    
    print(f"\n[Info] Parsing Title: {title}")
    print(f"[Info] Stream URL: {m3u8_url}")
    print(f"[Info] Output Path: {outtmpl.replace('.%(ext)s', '.mp3')}")
    
    # yt-dlp 選項：提取音訊，轉碼為 mp3
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': outtmpl,
        'quiet': False,
        'no_warnings': True
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print("\n[Start] Downloading and extracting audio...")
            ydl.download([m3u8_url])
            print("\n[Success] Download completed successfully!")
    except Exception as e:
        print(f"\n[Error] Download failed: {e}")
        print("Note: FFmpeg is required for audio extraction. If you see an FFmpeg error, please install it via 'brew install ffmpeg'.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gimymax Audio Downloader")
    parser.add_argument("url", nargs="?", help="URL of the video page")
    args = parser.parse_args()
    
    target_url = args.url or input("請輸入影片網址 (例如 https://gimymax.com/ep/...): ").strip()
    
    if target_url:
        try:
            m3u8_url, title = get_stream_info(target_url)
            if not m3u8_url:
                print("Error: Could not extract m3u8 stream URL.")
            else:
                download_audio(m3u8_url, title)
        except Exception as e:
            print(f"An error occurred: {e}")
    else:
        print("No URL provided.")
