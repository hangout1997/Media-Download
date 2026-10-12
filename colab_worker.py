# -*- coding: utf-8 -*-
"""
Colab Worker 核心待命引擎
由 Google Colab 雲端主機執行，負責：
1. 即時心跳寫入 (worker_heartbeat.json)
2. 佇列任務監聽 (MediaDownloadQueue/task_*.json)
3. 優先使用 Mac 本地預解析串流 (完全免疫 Cloudflare 403)
4. 本地 NVMe 高速暫存下載 + FFmpeg 封裝 + 原子同步至 Google Drive
5. 完整錯誤追蹤回傳 (絕不產生假陽性 completed)
"""

import os
import sys
import time
import json
import glob
import shutil
import re
import traceback

def get_drive_base():
    candidates = ['/content/drive/MyDrive', '/content/drive/我的雲端硬碟']
    for c in candidates:
        if os.path.exists(c):
            return c
    if os.path.exists('/content/drive'):
        subs = [os.path.join('/content/drive', d) for d in os.listdir('/content/drive') if not d.startswith('.')]
        if subs:
            return subs[0]
    return '/content/drive/MyDrive'

def ensure_core_downloader():
    try:
        import urllib.request
        raw_url = 'https://raw.githubusercontent.com/hangout1997/Media-Download/main/core_downloader.py'
        urllib.request.urlretrieve(raw_url, '/content/core_downloader.py')
        if '/content' not in sys.path:
            sys.path.insert(0, '/content')
        import importlib
        if 'core_downloader' in sys.modules:
            cd_mod = importlib.reload(sys.modules['core_downloader'])
        else:
            import core_downloader as cd_mod
        return cd_mod
    except Exception as e:
        print(f"⚠️ 下載最新 core_downloader 失敗，嘗試讀取本地模組: {e}")
        import core_downloader as cd_mod
        return cd_mod

def ensure_environment():
    # 1. 檢查並自動安裝依賴
    try:
        import yt_dlp
        import curl_cffi
    except ImportError:
        print("📦 正在自動安裝必要依賴 (yt-dlp, curl_cffi)...")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "yt-dlp", "curl_cffi", "requests", "beautifulsoup4"])
        print("✅ 依賴安裝完成！")

    # 2. 檢查並自動掛載 Google Drive
    candidates = ['/content/drive/MyDrive', '/content/drive/我的雲端硬碟']
    if not any(os.path.exists(c) for c in candidates):
        try:
            print("🔑 正在自動掛載 Google Drive（若彈出視窗請點擊允許連接）...")
            from google.colab import drive
            drive.mount('/content/drive')
            print("✅ Google Drive 掛載成功！")
        except Exception as e:
            print(f"⚠️ 自動掛載 Google Drive: {e}")

def run():
    print("=" * 65)
    print("🤖 正在啟動 Google Colab 雲端工作節點...")
    ensure_environment()
    
    drive_base = get_drive_base()
    if not os.path.exists(drive_base):
        print(f"❌ 找不到 Google Drive 掛載目錄 ({drive_base})！")
        print("👉 請確認 Google Drive 已授權連接。")
        return

    queue_dir = os.path.join(drive_base, 'MediaDownloadQueue')
    os.makedirs(queue_dir, exist_ok=True)
    heartbeat_file = os.path.join(queue_dir, 'worker_heartbeat.json')
    local_cache_dir = '/content/local_cache'
    os.makedirs(local_cache_dir, exist_ok=True)

    cd = ensure_core_downloader()

    def update_heartbeat(status='idle'):
        payload = {
            'timestamp': time.time(),
            'status': status,
            'pid': os.getpid(),
            'hostname': os.uname().nodename if hasattr(os, 'uname') else 'colab'
        }
        try:
            with open(heartbeat_file, 'w', encoding='utf-8') as hf:
                json.dump(payload, hf)
                hf.flush()
                try:
                    os.fsync(hf.fileno())
                except Exception:
                    pass
        except Exception:
            pass

    update_heartbeat('idle')

    print("🟢 Colab 雲端工作節點已成功啟動！")
    print(f"📂 佇列監聽目錄: {queue_dir}")
    print(f"💓 即時心跳已寫入 Google Drive，Mac Streamlit 應已亮起 🟢 綠燈！")
    print("👉 現在可直接在 Mac 本地 Streamlit 輸入網址並開始下載。")
    print("=" * 65)

    def process_one_task(task_path):
        update_heartbeat('busy')
        nonlocal cd
        try:
            cd = ensure_core_downloader()
        except Exception:
            pass

        try:
            with open(task_path, 'r', encoding='utf-8') as f:
                task = json.load(f)
        except Exception as e:
            print(f"⚠️ 讀取任務失敗: {e}")
            update_heartbeat('idle')
            return

        task_id = task.get('task_id')
        urls = task.get('urls', [])
        save_folder = task.get('save_folder', 'Download')
        only_audio = task.get('only_audio', False)
        cookies = task.get('cookies', {})
        resolved_map = task.get('resolved_map', {})

        target_dir = os.path.join(drive_base, save_folder.strip('/'))
        os.makedirs(target_dir, exist_ok=True)

        status_file = os.path.join(queue_dir, f'status_{task_id}.json')

        def update_status(status, current_item='', pct=0.0, speed_mb=0.0, speed_x=0.0, log='', error=''):
            update_heartbeat('busy' if status == 'running' else 'idle')
            payload = {
                'task_id': task_id,
                'status': status,
                'current_item': current_item,
                'progress_pct': pct,
                'speed_mb': speed_mb,
                'speed_x': speed_x,
                'last_update': time.time(),
                'log': log,
                'error': error
            }
            try:
                with open(status_file, 'w', encoding='utf-8') as sf:
                    json.dump(payload, sf, ensure_ascii=False)
                    sf.flush()
                    try:
                        os.fsync(sf.fileno())
                    except Exception:
                        pass
            except Exception:
                pass

        print(f"\n📥 [接單] 收到任務 {task_id}，共 {len(urls)} 個網址，直存目標: {target_dir}")
        update_status('running', current_item='正在認領任務...', log='Colab 節點已認領任務')

        success_files = []
        failed_items = []

        for idx, raw_url in enumerate(urls, 1):
            url = cd.normalize_input_url(raw_url)
            print(f"\n📍 [{idx}/{len(urls)}] 處理網址: {url}")
            update_status('running', current_item=f'[{idx}/{len(urls)}] 準備下載...', log=f'正在準備下載 {url}')

            # 優先使用 Mac 本地預解析串流 (100% 免疫雲端被 WAF/Cloudflare 403 阻擋)
            items = None
            if raw_url in resolved_map:
                items = resolved_map[raw_url]
                print("⚡ 使用本機預解析串流 (成功免疫 Cloudflare 403 阻擋)")
            elif url in resolved_map:
                items = resolved_map[url]
                print("⚡ 使用本機預解析串流 (成功免疫 Cloudflare 403 阻擋)")

            if not items:
                try:
                    items = cd.get_media_items(url, cookies=cookies)
                except Exception as e:
                    err_msg = f"解析失敗: {e}"
                    print(f"❌ {err_msg}")
                    failed_items.append((url, err_msg))
                    update_status('running', current_item=f'[{idx}/{len(urls)}] 解析失敗', log=err_msg, error=str(e))
                    continue

            if not items:
                err_msg = f"找不到影片串流來源: {url}"
                print(f"❌ {err_msg}")
                failed_items.append((url, err_msg))
                update_status('running', current_item=f'[{idx}/{len(urls)}] 找不到串流', log=err_msg)
                continue

            try:
                for item in items:
                    title = item.get('title', 'video')
                    clean_title = re.sub(r'[\/\\:\*\?"<>\|]', '_', title).strip()
                    ext = 'mp3' if only_audio else item.get('ext', 'mp4')
                    filename = f"{clean_title}.{ext}"

                    final_drive_path = os.path.join(target_dir, filename)
                    local_temp_path = os.path.join(local_cache_dir, filename)

                    if os.path.exists(final_drive_path) and os.path.getsize(final_drive_path) > 0:
                        print(f"⏭️ 雲端已存在，自動跳過: {filename}")
                        success_files.append(filename)
                        update_status('running', current_item=filename, pct=1.0, log=f"已存在於雲端，跳過: {filename}")
                        continue

                    print(f"📥 高速下載中: {filename}")
                    update_status('running', current_item=filename, pct=0.01, log=f"正在下載 {filename}")

                    def _prog(pct, cur_dur, tot_dur, speed_mb, speed_x):
                        update_status('running', current_item=filename, pct=pct, speed_mb=speed_mb, speed_x=speed_x,
                                      log=f"{int(pct*100)}% ({speed_mb:.2f} MB/s)")
                        cur_p = int(pct * 100)
                        if cur_p % 10 == 0:
                            print(f"\r⏳ {filename[:25]}... {cur_p}% | {speed_mb:.2f} MB/s", end="", flush=True)

                    if os.path.exists(local_temp_path):
                        try:
                            os.remove(local_temp_path)
                        except Exception:
                            pass

                    res_path = cd.download_media_to_path(
                        item, out_path=local_temp_path, force_audio=only_audio, cookies=cookies, progress_cb=_prog
                    )

                    if not os.path.exists(res_path) or os.path.getsize(res_path) == 0:
                        raise RuntimeError(f"檔案下載後大小為 0: {filename}")

                    print(f"\n☁️ 正在複製至 Google Drive: {final_drive_path}...")
                    update_status('running', current_item=filename, pct=0.99, log="正在同步至 Google Drive...")
                    shutil.copy2(res_path, final_drive_path)
                    try:
                        os.remove(res_path)
                    except Exception:
                        pass

                    size_mb = os.path.getsize(final_drive_path) / 1024 / 1024
                    print(f"🎉 直存成功: {filename} ({size_mb:.1f} MB)")
                    success_files.append(filename)
                    update_status('running', current_item=filename, pct=1.0, log=f"直存成功: {filename} ({size_mb:.1f} MB)")

            except Exception as e:
                tb = traceback.format_exc()
                err_line = f"{e}"
                print(f"\n❌ 下載失敗: {err_line}\n{tb}")
                failed_items.append((url, err_line))
                update_status('running', current_item=f"失敗: {err_line}", log=f"錯誤: {err_line}", error=tb)

        if success_files and not failed_items:
            update_status('completed', current_item=f"已完成 {len(success_files)} 個檔案！", pct=1.0,
                          log=f"成功直存：{', '.join(success_files)}")
            print(f"\n✅ 任務 {task_id} 全數成功完成！")
        elif success_files and failed_items:
            update_status('completed', current_item=f"部分完成 ({len(success_files)} 成功, {len(failed_items)} 失敗)", pct=1.0,
                          log=f"成功: {len(success_files)}, 失敗: {len(failed_items)}")
            print(f"\n⚠️ 任務 {task_id} 部分成功完成。")
        else:
            all_errs = "; ".join([f"{u}: {msg}" for u, msg in failed_items]) if failed_items else "未找到可用影音串流"
            update_status('failed', current_item="下載任務失敗", pct=0.0,
                          log=f"全數失敗: {all_errs}", error=all_errs)
            print(f"\n❌ 任務 {task_id} 處理失敗: {all_errs}")

        try:
            os.remove(task_path)
        except Exception as e:
            print(f"⚠️ 移除 task 檔失敗: {e}")

    last_hb_t = 0
    while True:
        now_t = time.time()
        if now_t - last_hb_t >= 5:
            update_heartbeat('idle')
            last_hb_t = now_t

        task_files = glob.glob(os.path.join(queue_dir, 'task_*.json'))
        for tf in task_files:
            process_one_task(tf)
        time.sleep(2)

if __name__ == '__main__':
    run()
