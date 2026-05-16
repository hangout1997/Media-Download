import yt_dlp
import json

url = "https://x.com/i/status/2055411826620329993"
ydl_opts = {
    'quiet': True,
    'extract_flat': False,
    'format': 'best',
}

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info(url, download=False)
    print("Direct URL:", info.get('url'))
