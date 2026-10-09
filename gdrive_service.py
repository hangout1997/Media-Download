import os
import re
import time
import json
import mimetypes
import threading
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# 允許的 OAuth 範圍：完整 Drive 檔案管理 (需能瀏覽使用者既有資料夾)
SCOPES = ['https://www.googleapis.com/auth/drive']

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "gdrive_token.json")
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

FOLDER_MIME = 'application/vnd.google-apps.folder'
DEFAULT_FOLDER_NAME = "Download"

# 以 token 檔案 mtime 為 key 快取 service / 帳號資訊，避免每次 Streamlit rerun 都打 API
_cache_lock = threading.Lock()
_service_cache = {"key": None, "service": None}
_account_cache = {"key": None, "email": None}


class GDriveAuthRequired(Exception):
    """尚未授權或 Token 已失效，需使用者重新登入。"""


# ========================================================
# 設定檔 (與 app.py 共用 config.json，採合併寫入)
# ========================================================
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def update_config(**kwargs):
    data = load_config()
    data.update(kwargs)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return data


def get_target_folder():
    """回傳 (folder_id, folder_path_label)。folder_id 為 None 代表使用預設 /Download/。"""
    cfg = load_config()
    return cfg.get("gdrive_folder_id"), cfg.get("gdrive_folder_label") or f"/{DEFAULT_FOLDER_NAME}/"


def set_target_folder(folder_id, label):
    update_config(gdrive_folder_id=folder_id, gdrive_folder_label=label)


# ========================================================
# 授權
# ========================================================
def get_credentials_path():
    if os.path.exists(CREDENTIALS_FILE):
        return CREDENTIALS_FILE
    # 支援 client_secret*.json 命名 (Google Console 預設下載檔名)
    for fn in sorted(os.listdir(BASE_DIR)):
        if fn.startswith("client_secret") and fn.endswith(".json"):
            return os.path.join(BASE_DIR, fn)
    return None


def validate_credentials_json(raw_bytes):
    """驗證上傳的 OAuth 用戶端 JSON 是否為「桌面應用程式」類型。回傳 (ok, msg)。"""
    try:
        data = json.loads(raw_bytes)
    except Exception:
        return False, "不是有效的 JSON 檔案"
    if "installed" in data:
        return True, "OK"
    if "web" in data:
        return False, "這是「網頁應用程式」類型的用戶端，請改建立「電腦版應用程式 (Desktop app)」類型"
    if data.get("type") == "service_account":
        return False, "這是 Service Account 金鑰，不適用；請建立 OAuth 用戶端 ID (電腦版應用程式)"
    return False, "格式不符，缺少 'installed' 欄位"


def _token_key():
    try:
        return os.path.getmtime(TOKEN_FILE)
    except OSError:
        return None


def _load_creds(refresh=True):
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    except Exception:
        return None
    if creds and creds.expired and creds.refresh_token and refresh:
        try:
            creds.refresh(Request())
            with open(TOKEN_FILE, 'w', encoding='utf-8') as token:
                token.write(creds.to_json())
        except Exception as e:
            print(f"Token refresh failed: {e}")
            return None
    return creds if creds and creds.valid else None


def is_authenticated():
    """檢查是否已有有效 (或可刷新) 的 Google Drive Token。不發出網路請求。"""
    if not os.path.exists(TOKEN_FILE):
        return False
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        return bool(creds and (creds.valid or (creds.expired and creds.refresh_token)))
    except Exception:
        return False


def run_oauth_flow(timeout_seconds=180):
    """開啟瀏覽器進行 OAuth 授權 (阻塞直到完成或逾時)。"""
    cred_path = get_credentials_path()
    if not cred_path:
        raise FileNotFoundError("找不到 credentials.json，請先完成首次設定。")
    flow = InstalledAppFlow.from_client_secrets_file(cred_path, SCOPES)
    try:
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            timeout_seconds=timeout_seconds,
            prompt='consent',
            access_type='offline',
            authorization_prompt_message="",
            success_message="✅ Google Drive 授權成功！可關閉此分頁回到 Media Downloader。",
        )
    except AttributeError:
        # 逾時未完成授權時，oauthlib 會對 None 呼叫 .replace() 拋出 AttributeError
        creds = None
    if not creds:
        raise TimeoutError("授權逾時，請重新點擊授權按鈕。")
    with open(TOKEN_FILE, 'w', encoding='utf-8') as token:
        token.write(creds.to_json())
    return creds


def get_gdrive_service(interactive=False):
    """
    取得 Drive service (含快取)。
    interactive=False 時若未授權直接拋出 GDriveAuthRequired，避免下載途中突然彈出瀏覽器卡住流程。
    """
    with _cache_lock:
        key = _token_key()
        if key is not None and _service_cache["key"] == key and _service_cache["service"] is not None:
            cached_creds = _service_cache.get("creds")
            if cached_creds is not None and cached_creds.valid:
                return _service_cache["service"]

        creds = _load_creds(refresh=True)
        if not creds:
            if not interactive:
                raise GDriveAuthRequired("Google Drive 授權已失效或尚未登入，請重新授權。")
            creds = run_oauth_flow()

        service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        _service_cache["key"] = _token_key()
        _service_cache["service"] = service
        _service_cache["creds"] = creds
        return service


def get_connected_account_email():
    """取得當前已授權的 Google 帳號 Email (依 token 快取)。"""
    key = _token_key()
    if key is not None and _account_cache["key"] == key and _account_cache["email"]:
        return _account_cache["email"]
    try:
        service = get_gdrive_service()
        about = service.about().get(fields="user(displayName,emailAddress)").execute()
        user = about.get('user', {})
        email = user.get('emailAddress') or user.get('displayName') or "已連接"
        _account_cache["key"] = _token_key()
        _account_cache["email"] = email
        return email
    except Exception:
        return None


def revoke_gdrive_auth():
    """登出：嘗試撤銷遠端 Token 並刪除本地 Token。"""
    try:
        creds = _load_creds(refresh=False)
        if creds and creds.token:
            import requests
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": creds.refresh_token or creds.token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=5,
            )
    except Exception:
        pass
    with _cache_lock:
        _service_cache.update(key=None, service=None)
        _account_cache.update(key=None, email=None)
    if os.path.exists(TOKEN_FILE):
        try:
            os.remove(TOKEN_FILE)
            return True
        except Exception:
            pass
    return False


# ========================================================
# 資料夾操作
# ========================================================
def _q_escape(s):
    return s.replace("\\", "\\\\").replace("'", "\\'")


def parse_folder_input(text):
    """從 Drive 資料夾網址或純 ID 解析出 folder ID。"""
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"/folders/([a-zA-Z0-9_-]{10,})", text)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]{10,})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{10,}", text):
        return text
    return None


def list_subfolders(parent_id="root"):
    service = get_gdrive_service()
    q = f"'{_q_escape(parent_id)}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
    folders, page_token = [], None
    while True:
        resp = service.files().list(
            q=q, spaces='drive', fields='nextPageToken, files(id, name)',
            orderBy='name', pageSize=200, pageToken=page_token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        folders.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return folders


def get_folder_meta(folder_id):
    """回傳 {'id','name','webViewLink'}；若不是資料夾或無權限則拋例外。"""
    service = get_gdrive_service()
    meta = service.files().get(
        fileId=folder_id, fields='id, name, mimeType, webViewLink, trashed',
        supportsAllDrives=True,
    ).execute()
    if meta.get('mimeType') != FOLDER_MIME:
        raise ValueError("此 ID 不是資料夾")
    if meta.get('trashed'):
        raise ValueError("此資料夾已在垃圾桶中")
    return meta


def get_folder_path_label(folder_id):
    """組出 /A/B/C/ 形式的路徑文字 (最多往上 8 層)。"""
    service = get_gdrive_service()
    parts, cur = [], folder_id
    for _ in range(8):
        meta = service.files().get(fileId=cur, fields='id, name, parents', supportsAllDrives=True).execute()
        parents = meta.get('parents') or []
        if not parents:
            # 抵達「我的雲端硬碟」根目錄 (根目錄本身名稱為 My Drive)，不加入路徑
            break
        parts.append(meta.get('name', ''))
        cur = parents[0]
    return "/" + "/".join(reversed(parts)) + "/" if parts else "/"


def create_folder(name, parent_id="root"):
    service = get_gdrive_service()
    meta = {'name': name, 'mimeType': FOLDER_MIME, 'parents': [parent_id]}
    return service.files().create(body=meta, fields='id, name, webViewLink', supportsAllDrives=True).execute()


def get_or_create_download_folder(service=None, folder_name=DEFAULT_FOLDER_NAME):
    """在「我的雲端硬碟」根目錄搜尋指定資料夾，不存在時自動建立。"""
    service = service or get_gdrive_service()
    q = (f"name = '{_q_escape(folder_name)}' and mimeType = '{FOLDER_MIME}' "
         f"and 'root' in parents and trashed = false")
    files = service.files().list(q=q, spaces='drive', fields='files(id, name)').execute().get('files', [])
    if files:
        return files[0]['id']
    return create_folder(folder_name, "root")['id']


def resolve_target_folder_id():
    """取得實際上傳的目標資料夾 ID；若設定的資料夾已失效則退回預設 /Download/。"""
    folder_id, _ = get_target_folder()
    if folder_id:
        try:
            get_folder_meta(folder_id)
            return folder_id
        except Exception:
            set_target_folder(None, f"/{DEFAULT_FOLDER_NAME}/")
    return get_or_create_download_folder()


def get_folder_link(folder_id):
    return f"https://drive.google.com/drive/folders/{folder_id}"


def find_existing_file(filename, folder_id):
    """在目標資料夾中尋找同名檔案，回傳檔案資訊或 None。"""
    service = get_gdrive_service()
    q = f"name = '{_q_escape(filename)}' and '{_q_escape(folder_id)}' in parents and trashed = false"
    files = service.files().list(
        q=q, spaces='drive', fields='files(id, name, size, webViewLink)', pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get('files', [])
    return files[0] if files else None


# ========================================================
# 上傳
# ========================================================
def upload_file_directly_to_gdrive(file_path, original_filename=None, progress_callback=None,
                                   folder_id=None, skip_if_exists=False):
    """
    將檔案以 10MB 分塊、可續傳方式上傳至 Google Drive 目標資料夾。
    回傳 dict：雲端檔案資訊，另含 'skipped' (bool) 與 'folder_id'。
    """
    service = get_gdrive_service()
    folder_id = folder_id or resolve_target_folder_id()

    filename = original_filename or os.path.basename(file_path)

    if skip_if_exists:
        existing = find_existing_file(filename, folder_id)
        if existing:
            existing['skipped'] = True
            existing['folder_id'] = folder_id
            return existing

    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type:
        mime_type = 'application/octet-stream'

    file_metadata = {'name': filename, 'parents': [folder_id]}

    chunk_size = 10 * 1024 * 1024
    media = MediaFileUpload(file_path, mimetype=mime_type, chunksize=chunk_size, resumable=True)
    request = service.files().create(
        body=file_metadata, media_body=media,
        fields='id, name, webViewLink, size', supportsAllDrives=True,
    )

    response = None
    start_time = time.time()
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    while response is None:
        # num_retries：遇 5xx / 網路中斷時自動指數退避重試，不必整檔重傳
        status, response = request.next_chunk(num_retries=5)
        if status and progress_callback:
            progress_pct = status.progress()
            uploaded_bytes = int(progress_pct * file_size)
            elapsed = max(0.1, time.time() - start_time)
            speed_mb = (uploaded_bytes / 1024 / 1024) / elapsed
            progress_callback(progress_pct, uploaded_bytes, file_size, speed_mb)

    if progress_callback:
        progress_callback(1.0, file_size, file_size, 0.0)

    response['skipped'] = False
    response['folder_id'] = folder_id
    return response
