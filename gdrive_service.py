import os
import io
import time
import json
import mimetypes
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

# 允許的 OAuth 範圍：完整 Drive 檔案管理
SCOPES = ['https://www.googleapis.com/auth/drive']

TOKEN_FILE = os.path.join(os.path.dirname(__file__), "gdrive_token.json")
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")

def get_credentials_path():
    if os.path.exists(CREDENTIALS_FILE):
        return CREDENTIALS_FILE
    # 支援 client_secret.json 命名
    alt = os.path.join(os.path.dirname(__file__), "client_secret.json")
    if os.path.exists(alt):
        return alt
    return None

def is_authenticated():
    """檢查是否已有有效的 Google Drive Token。"""
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            if creds and (creds.valid or creds.expired and creds.refresh_token):
                return True
        except Exception:
            pass
    return False

def get_gdrive_service():
    """取得 Google Drive API service 物件，自動處理 Token 刷新。"""
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception:
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(TOKEN_FILE, 'w', encoding='utf-8') as token:
                token.write(creds.to_json())
        except Exception as e:
            print(f"Token refresh failed: {e}")
            creds = None

    if not creds or not creds.valid:
        cred_path = get_credentials_path()
        if not cred_path:
            raise FileNotFoundError(
                "找不到 credentials.json！請至 Google Cloud Console 建立 OAuth 2.0 Client ID (桌面應用程式) 並下載 credentials.json 放入專案資料夾。"
            )
        flow = InstalledAppFlow.from_client_secrets_file(cred_path, SCOPES)
        # 本地授權伺服器
        creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())

    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    return service

def get_connected_account_email():
    """取得當前已授權的 Google 帳號 Email。"""
    try:
        service = get_gdrive_service()
        about = service.about().get(fields="user(displayName,emailAddress)").execute()
        user = about.get('user', {})
        return user.get('emailAddress') or user.get('displayName') or "已連接"
    except Exception:
        return None

def get_or_create_download_folder(service, folder_name="Download"):
    """在 Google Drive 雲端搜尋指定資料夾，不存在時自動於雲端建立。"""
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = response.get('files', [])
    
    if files:
        return files[0].get('id')
    
    # 不存在則於雲端建立
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder'
    }
    folder = service.files().create(body=file_metadata, fields='id').execute()
    return folder.get('id')

def upload_file_directly_to_gdrive(file_path, original_filename=None, progress_callback=None):
    """
    將檔案直接以分塊串流上傳至 Google Drive 雲端的 'Download' 資料夾。
    上傳完成後回傳雲端檔案資訊。
    """
    service = get_gdrive_service()
    folder_id = get_or_create_download_folder(service, "Download")
    
    filename = original_filename or os.path.basename(file_path)
    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type:
        mime_type = 'video/mp4' if filename.endswith('.mp4') else 'application/octet-stream'

    file_metadata = {
        'name': filename,
        'parents': [folder_id]
    }

    # 10MB 分塊上傳以支援大檔案與即時進度監控
    chunk_size = 10 * 1024 * 1024
    media = MediaFileUpload(file_path, mimetype=mime_type, chunksize=chunk_size, resumable=True)
    
    request = service.files().create(body=file_metadata, media_body=media, fields='id, name, webViewLink, size')
    
    response = None
    start_time = time.time()
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    while response is None:
        status, response = request.next_chunk()
        if status and progress_callback:
            progress_pct = status.progress()
            uploaded_bytes = int(progress_pct * file_size)
            elapsed = max(0.1, time.time() - start_time)
            speed_mb = (uploaded_bytes / 1024 / 1024) / elapsed
            progress_callback(progress_pct, uploaded_bytes, file_size, speed_mb)

    if progress_callback:
        progress_callback(1.0, file_size, file_size, 0.0)

    return response

def revoke_gdrive_auth():
    """登出並刪除本地 Token。"""
    if os.path.exists(TOKEN_FILE):
        try:
            os.remove(TOKEN_FILE)
            return True
        except Exception:
            pass
    return False
