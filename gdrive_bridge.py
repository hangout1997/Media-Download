# -*- coding: utf-8 -*-
"""
Google Drive Bridge Service
用於 本地端 Streamlit 與 Google Colab 雲端工作節點之間的雙向任務分派與進度監控橋樑。
透過 Google Drive 上的特定目錄 (預設 /MediaDownloadQueue/) 進行：
1. 本地端發送任務 (寫入 task_<id>.json)
2. Colab 雲端節點認領任務並實時回寫 status_<id>.json
3. 本地端 Streamlit 輪詢 status_<id>.json 渲染進度條與日誌
"""

import os
import json
import time
import uuid
import tempfile
import gdrive_service

QUEUE_FOLDER_NAME = "MediaDownloadQueue"
_queue_folder_id = None

def get_or_create_queue_folder():
    """取得或在 Google Drive 上建立工作佇列資料夾。"""
    global _queue_folder_id
    if _queue_folder_id:
        return _queue_folder_id
    service = gdrive_service.get_gdrive_service()
    _queue_folder_id = gdrive_service.get_or_create_download_folder(service, QUEUE_FOLDER_NAME)
    return _queue_folder_id

def submit_cloud_task(urls, save_folder="Download", only_audio=False, cookies=None, resolved_map=None):
    """
    由本地端提交下載任務至 Google Drive 佇列。
    resolved_map: 本地預先解析之媒體項目對照表 {url: [media_item, ...]}，免疫雲端被 WAF/Cloudflare 403 阻擋。
    回傳 task_id (str)
    """
    service = gdrive_service.get_gdrive_service()
    folder_id = get_or_create_queue_folder()

    task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    task_payload = {
        "task_id": task_id,
        "created_at": time.time(),
        "status": "pending",
        "save_folder": save_folder,
        "only_audio": only_audio,
        "urls": urls,
        "resolved_map": resolved_map or {},
        "cookies": cookies or {},
        "current_item": "",
        "progress_pct": 0.0,
        "speed_mb": 0.0,
        "speed_x": 0.0,
        "log_messages": ["任務已由本機發送至 Google Drive 佇列，等待 Colab 認領..."]
    }

    # 寫入暫存檔案後上傳
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as tf:
        json.dump(task_payload, tf, ensure_ascii=False, indent=2)
        tmp_name = tf.name

    try:
        filename = f"{task_id}.json"
        from googleapiclient.http import MediaFileUpload
        file_metadata = {
            'name': filename,
            'parents': [folder_id]
        }
        media = MediaFileUpload(tmp_name, mimetype='application/json')
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return task_id
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except Exception:
                pass

def get_cloud_task_status(task_id):
    """
    由本地端查詢指定任務的最新狀態與進度。
    回傳 dict (或 None 若尚未產生 status)
    """
    service = gdrive_service.get_gdrive_service()
    folder_id = get_or_create_queue_folder()

    status_filename = f"status_{task_id}.json"
    q = f"name = '{status_filename}' and '{folder_id}' in parents and trashed = false"
    res = service.files().list(q=q, fields='files(id, name, modifiedTime)', spaces='drive').execute()
    files = res.get('files', [])

    if not files:
        # 檢查原始 task 是否還在 (代表 Colab 尚未認領或剛啟動)
        q_task = f"name = '{task_id}.json' and '{folder_id}' in parents and trashed = false"
        res_task = service.files().list(q=q_task, fields='files(id, name)', spaces='drive').execute()
        if res_task.get('files'):
            return {
                "status": "pending",
                "progress_pct": 0.0,
                "current_item": "等待 Colab 雲端節點認領中...",
                "log_messages": ["任務已置於佇列，Colab 待命中..."]
            }
        return None

    file_id = files[0]['id']
    content = service.files().get_media(fileId=file_id).execute()
    try:
        return json.loads(content.decode('utf-8'))
    except Exception:
        return None

def clean_old_queue_files(max_age_hours=24):
    """清理超過指定時間的舊任務檔案。"""
    try:
        service = gdrive_service.get_gdrive_service()
        folder_id = get_or_create_queue_folder()
        res = service.files().list(q=f"'{folder_id}' in parents and trashed = false", fields='files(id, name, createdTime)').execute()
        for f in res.get('files', []):
            if f['name'].startswith("task_") or f['name'].startswith("status_"):
                pass
    except Exception:
        pass

COLAB_NOTEBOOK_URL = "https://colab.research.google.com/github/hangout1997/Media-Download/blob/main/Media_Download_Colab.ipynb"

def get_worker_heartbeat(timeout_seconds=35):
    """
    查詢 Colab Worker 的即時心跳狀態。
    回傳 dict: {
        'online': bool,
        'last_seen_seconds': float,
        'last_update': float,
        'status': 'idle' / 'busy' / 'offline',
        'message': str
    }
    """
    try:
        service = gdrive_service.get_gdrive_service()
        folder_id = get_or_create_queue_folder()
        q = f"name = 'worker_heartbeat.json' and '{folder_id}' in parents and trashed = false"
        res = service.files().list(q=q, fields='files(id, name, modifiedTime)', spaces='drive').execute()
        files = res.get('files', [])
        if not files:
            return {'online': False, 'last_seen_seconds': -1, 'status': 'offline', 'message': '尚未偵測到 Worker 心跳 (離線)'}

        content = service.files().get_media(fileId=files[0]['id']).execute()
        data = json.loads(content.decode('utf-8'))
        last_time = float(data.get('timestamp', 0))
        now = time.time()
        diff = max(0, now - last_time)

        if diff <= timeout_seconds:
            worker_st = data.get('status', 'idle')
            st_text = "處理任務中" if worker_st == 'busy' else "空閒待命中"
            return {
                'online': True,
                'last_seen_seconds': round(diff, 1),
                'last_update': last_time,
                'status': worker_st,
                'message': f"Worker 在線 ({st_text}，心跳: {int(diff)} 秒前)"
            }
        else:
            return {
                'online': False,
                'last_seen_seconds': round(diff, 1),
                'last_update': last_time,
                'status': 'offline',
                'message': f"Worker 已離線 (最後心跳: {int(diff)} 秒前)"
            }
    except Exception as e:
        return {'online': False, 'last_seen_seconds': -1, 'status': 'offline', 'message': f"心跳查詢失敗: {e}"}

