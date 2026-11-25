# -*- coding: utf-8 -*-
"""
Google Colab CLI Headless 派發服務
透過官方 `google-colab-cli` (`colab`) 直接從本機開機、上傳任務與最新核心模組、
在 Colab 雲端執行下載並透過 Google Drive API 直存至目標資料夾，完成後自動關機釋放算力。
"""

import json
import os
import pwd
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid

from core.macos_utils import BASE_DIR

EVENT_PREFIX = "@@COLAB_EVENT@@"


def _real_home():
    """取得系統真實使用者家目錄（避免啟動環境覆寫 HOME 導致 macOS Keychain 讀取不到 Colab OAuth Token）。"""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return os.path.expanduser("~")


def _colab_env():
    env = os.environ.copy()
    env["HOME"] = _real_home()
    return env


def find_colab_bin():
    """尋找本機安裝的 `colab` 執行檔路徑（相容 GUI/venv 啟動時 PATH 缺少 ~/.local/bin 的情境）。"""
    found = shutil.which("colab")
    if found:
        return found
    candidates = [
        os.path.join(_real_home(), ".local/bin/colab"),
        "/opt/homebrew/bin/colab",
        "/usr/local/bin/colab",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def is_colab_cli_available():
    """檢查本機是否已安裝 google-colab-cli。"""
    return find_colab_bin() is not None


def get_colab_usage(timeout=20):
    """查詢目前授權帳號的 Colab Compute Units 餘額與使用率。"""
    colab_bin = find_colab_bin()
    if not colab_bin:
        return {"ok": False, "error": "未安裝 google-colab-cli"}
    try:
        proc = subprocess.run(
            [colab_bin, "usage"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_colab_env(),
        )
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode != 0:
            return {"ok": False, "error": out.strip() or f"exit code {proc.returncode}"}

        balance = None
        rate = None
        active = 0
        m_bal = re.search(r"Current balance:\s*([\d.]+)", out)
        if m_bal:
            balance = float(m_bal.group(1))
        m_rate = re.search(r"Usage rate:\s*([\d.]+)/hr", out)
        if m_rate:
            rate = float(m_rate.group(1))
        m_act = re.search(r"Active assignments:\s*(\d+)", out)
        if m_act:
            active = int(m_act.group(1))

        return {
            "ok": True,
            "balance": balance,
            "rate": rate,
            "active_assignments": active,
            "raw": out.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "查詢超時（可能需要先在終端機執行 colab usage 完成 OAuth 授權）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _build_code_bundle(tar_path):
    """將本機最新的 core_downloader.py、core/ 與 parsers/ 打包成 tar.gz。"""
    include_items = ["core_downloader.py", "core", "parsers"]

    def _filter(tarinfo):
        if "__pycache__" in tarinfo.name or tarinfo.name.endswith(".pyc"):
            return None
        return tarinfo

    with tarfile.open(tar_path, "w:gz") as tar:
        for rel in include_items:
            full = os.path.join(BASE_DIR, rel)
            if os.path.exists(full):
                tar.add(full, arcname=rel, filter=_filter)


_REMOTE_RUNNER_CODE = r'''# -*- coding: utf-8 -*-
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import traceback
import types

EVENT_PREFIX = "@@COLAB_EVENT@@"


def emit(event_type, **kwargs):
    payload = {"type": event_type, **kwargs}
    print(EVENT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def install_deps():
    needed = [
        ("yt_dlp", "yt-dlp"),
        ("curl_cffi", "curl_cffi"),
        ("bs4", "beautifulsoup4"),
        ("googleapiclient", "google-api-python-client"),
        ("google.oauth2", "google-auth"),
        ("google_auth_oauthlib", "google-auth-oauthlib"),
    ]
    missing = []
    for mod, pkg in needed:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        emit("status", current_item="安裝雲端依賴套件...", pct=0.02, log=f"安裝: {', '.join(missing)}")
        uv_bin = shutil.which("uv")
        if uv_bin:
            cmd = [uv_bin, "pip", "install", "--system", "-q"] + missing
        else:
            cmd = [sys.executable, "-m", "pip", "install", "-q"] + missing
        subprocess.run(cmd, check=True)


def setup_dummy_ramdisk():
    if "ramdisk_manager" not in sys.modules:
        _dummy = types.ModuleType("ramdisk_manager")
        _dummy.is_ramdisk_mounted = lambda: False
        _dummy.get_scratch_dir = lambda min_free_bytes=0: tempfile.gettempdir()
        _dummy.get_ramdisk_usage = lambda: (0, 0, 0, 0)
        _dummy.clean_scratch_files = lambda *a, **kw: None
        _dummy.clean_all_ramdisk_files = lambda: 0
        _dummy.MOUNT_POINT = "/Volumes/MediaRAMDisk"
        sys.modules["ramdisk_manager"] = _dummy


def build_drive_service(token_path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(token_path)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def ensure_drive_folder(service, target_folder_id, save_folder):
    if target_folder_id:
        return target_folder_id
    folder_name = (save_folder or "Download").strip("/").split("/")[-1] or "Download"
    q = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    res = service.files().list(q=q, spaces="drive", fields="files(id, name)").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    created = service.files().create(body=meta, fields="id").execute()
    return created["id"]


def drive_file_exists(service, folder_id, filename):
    safe_name = filename.replace("'", "\\'")
    q = f"name = '{safe_name}' and '{folder_id}' in parents and trashed = false"
    res = service.files().list(q=q, spaces="drive", fields="files(id, name, size)").execute()
    for f in res.get("files", []):
        if int(f.get("size", 0) or 0) > 0:
            return True
    return False


def upload_to_drive(service, folder_id, local_path, filename, on_upload_progress=None):
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(local_path, resumable=True, chunksize=10 * 1024 * 1024)
    meta = {"name": filename, "parents": [folder_id]}
    req = service.files().create(body=meta, media_body=media, fields="id, name")
    response = None
    while response is None:
        status, response = req.next_chunk()
        if status and on_upload_progress:
            on_upload_progress(status.progress())
    return response


def main():
    os.chdir("/content")
    if "/content" not in sys.path:
        sys.path.insert(0, "/content")

    bundle_path = "/content/media_dl_pkg.tar.gz"
    if os.path.exists(bundle_path):
        with tarfile.open(bundle_path, "r:gz") as tar:
            tar.extractall("/content")

    install_deps()
    setup_dummy_ramdisk()

    import core_downloader as cd

    with open("/content/task_payload.json", "r", encoding="utf-8") as f:
        task = json.load(f)

    urls = task.get("urls", [])
    save_folder = task.get("save_folder", "Download")
    target_folder_id = task.get("target_folder_id")
    only_audio = bool(task.get("only_audio", False))
    cookies = task.get("cookies") or {}
    resolved_map = task.get("resolved_map") or {}
    skip_duplicates = bool(task.get("skip_duplicates", True))

    token_path = "/content/gdrive_token.json"
    if not os.path.exists(token_path):
        emit("failed", log="缺少 gdrive_token.json，無法直傳 Google Drive", error="Missing /content/gdrive_token.json")
        sys.exit(1)

    drive_service = build_drive_service(token_path)
    folder_id = ensure_drive_folder(drive_service, target_folder_id, save_folder)

    work_dir = "/content/media_dl_work"
    os.makedirs(work_dir, exist_ok=True)

    success_files = []
    failed_items = []
    total_urls = max(len(urls), 1)

    for idx, raw_url in enumerate(urls, 1):
        url = cd.normalize_input_url(raw_url)
        emit("status", current_item=f"[{idx}/{total_urls}] 準備下載...", pct=(idx - 1) / total_urls, log=f"處理 {url}")

        items = None
        for ck in [raw_url, url, raw_url.rstrip("/"), raw_url.rstrip("/") + "/", url.rstrip("/"), url.rstrip("/") + "/"]:
            if ck in resolved_map:
                items = resolved_map[ck]
                break

        if not items:
            try:
                items = cd.get_media_items(url, cookies=cookies)
            except Exception as e:
                failed_items.append({"url": url, "error": str(e)})
                emit("log", log=f"❌ 解析失敗: {url} ({e})")
                continue

        if not items:
            failed_items.append({"url": url, "error": "找不到影片串流來源"})
            emit("log", log=f"❌ 找不到影片串流來源: {url}")
            continue

        for item in items:
            title = item.get("title", "video")
            clean_title = re.sub(r'[\/\\:\*\?"<>\|]', "_", title).strip()
            ext = "mp3" if only_audio else item.get("ext", "mp4")
            filename = f"{clean_title}.{ext}"
            local_temp_path = os.path.join(work_dir, filename)

            if skip_duplicates and drive_file_exists(drive_service, folder_id, filename):
                success_files.append(filename)
                emit("status", current_item=filename, pct=idx / total_urls, log=f"⏭️ 雲端已存在，跳過: {filename}")
                continue

            if os.path.exists(local_temp_path):
                try:
                    os.remove(local_temp_path)
                except Exception:
                    pass

            def _prog(pct, cur_dur, tot_dur, speed_mb, speed_x):
                overall = ((idx - 1) + min(max(pct, 0.0), 1.0) * 0.85) / total_urls
                emit(
                    "status",
                    current_item=filename,
                    pct=overall,
                    speed_mb=speed_mb,
                    log=f"下載中 {int(pct * 100)}% ({speed_mb:.2f} MB/s)",
                )

            try:
                if only_audio:
                    ok, err = cd.extract_audio_to_path(item, local_temp_path, cookies=cookies, progress_cb=_prog)
                else:
                    ok, err = cd.download_media_to_path(item, local_temp_path, cookies=cookies, progress_cb=_prog)

                if not ok or not os.path.exists(local_temp_path) or os.path.getsize(local_temp_path) == 0:
                    failed_items.append({"url": url, "title": filename, "error": err or "下載後檔案為空"})
                    emit("log", log=f"❌ 下載失敗: {filename} ({err})")
                    continue

                def _up_prog(up_pct):
                    overall = ((idx - 1) + 0.85 + min(max(up_pct, 0.0), 1.0) * 0.15) / total_urls
                    emit("status", current_item=filename, pct=overall, log=f"☁️ 直傳 Google Drive {int(up_pct * 100)}%")

                emit("status", current_item=filename, pct=((idx - 1) + 0.85) / total_urls, log=f"☁️ 正在直傳 Google Drive: {filename}")
                upload_to_drive(drive_service, folder_id, local_temp_path, filename, on_upload_progress=_up_prog)
                success_files.append(filename)
                emit("status", current_item=filename, pct=idx / total_urls, log=f"✅ 已直存雲端: {filename}")
            except Exception as e:
                failed_items.append({"url": url, "title": filename, "error": traceback.format_exc()})
                emit("log", log=f"❌ 處理例外: {filename} ({e})")
            finally:
                if os.path.exists(local_temp_path):
                    try:
                        os.remove(local_temp_path)
                    except Exception:
                        pass

    if failed_items and not success_files:
        emit("failed", log=f"全部 {len(failed_items)} 個項目失敗", error=json.dumps(failed_items, ensure_ascii=False, indent=2))
        sys.exit(1)

    emit(
        "completed",
        success_files=success_files,
        failed_items=failed_items,
        log=f"完成 {len(success_files)} 個檔案" + (f"，失敗 {len(failed_items)} 個" if failed_items else ""),
    )


if __name__ == "__main__":
    main()
'''


def run_media_task_via_colab_cli(
    urls,
    resolved_map,
    save_folder="Download",
    target_folder_id=None,
    only_audio=False,
    cookies=None,
    token_path=None,
    skip_duplicates=True,
    gpu=None,
    on_progress=None,
    on_log=None,
):
    """
    透過 `google-colab-cli` 建立臨時 Colab VM、執行下載並直傳 Google Drive，最後保證釋放 VM。
    回傳 dict: {"status": "completed"|"failed", "success_files": [...], "failed_items": [...], "log": str, "error": str}
    """
    colab_bin = find_colab_bin()
    if not colab_bin:
        return {"status": "failed", "log": "找不到 colab CLI", "error": "請先執行 `uv tool install google-colab-cli`"}

    if not token_path or not os.path.exists(token_path):
        return {
            "status": "failed",
            "log": "缺少 Google Drive 授權 Token",
            "error": "請先在上方完成 Google Drive 授權登入，才能讓 Colab VM 直存檔案。",
        }

    session_name = f"media_dl_{uuid.uuid4().hex[:6]}"
    tmp_dir = tempfile.mkdtemp(prefix="colab_cli_media_")

    def _emit_log(msg):
        if on_log:
            on_log(msg)

    def _emit_prog(item, pct, speed_mb=0.0, log=""):
        if on_progress:
            on_progress(item, pct, speed_mb, log)

    session_created = False
    try:
        # 1. 準備打包檔與 payload
        _emit_prog("打包本機核心引擎...", 0.01, 0.0, "正在打包 core_downloader 與任務清單")
        bundle_path = os.path.join(tmp_dir, "media_dl_pkg.tar.gz")
        _build_code_bundle(bundle_path)

        payload_path = os.path.join(tmp_dir, "task_payload.json")
        with open(payload_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "urls": urls,
                    "resolved_map": resolved_map or {},
                    "save_folder": save_folder,
                    "target_folder_id": target_folder_id,
                    "only_audio": only_audio,
                    "cookies": cookies or {},
                    "skip_duplicates": skip_duplicates,
                },
                f,
                ensure_ascii=False,
            )

        runner_path = os.path.join(tmp_dir, "colab_cli_runner.py")
        with open(runner_path, "w", encoding="utf-8") as f:
            f.write(_REMOTE_RUNNER_CODE)

        # 2. 開啟 Colab Session
        new_cmd = [colab_bin, "new", "-s", session_name]
        if gpu:
            new_cmd.extend(["--gpu", gpu])
        _emit_prog(f"啟動 Colab 雲端主機 ({session_name})...", 0.03, 0.0, f"執行: {' '.join(new_cmd)}")
        res_new = subprocess.run(new_cmd, capture_output=True, text=True, timeout=180, env=_colab_env())
        if res_new.returncode != 0:
            err_msg = (res_new.stderr or res_new.stdout or "").strip()
            return {"status": "failed", "log": "啟動 Colab VM 失敗", "error": err_msg}
        session_created = True

        # 3. 上傳程式包、任務設定與 Drive Token
        _emit_prog("上傳任務與授權至 Colab VM...", 0.05, 0.0, "同步 core_downloader 與設定檔")
        for local_f, remote_f in [
            (bundle_path, "/content/media_dl_pkg.tar.gz"),
            (payload_path, "/content/task_payload.json"),
            (token_path, "/content/gdrive_token.json"),
        ]:
            res_up = subprocess.run(
                [colab_bin, "upload", "-s", session_name, local_f, remote_f],
                capture_output=True,
                text=True,
                timeout=120,
                env=_colab_env(),
            )
            if res_up.returncode != 0:
                err_msg = (res_up.stderr or res_up.stdout or "").strip()
                return {"status": "failed", "log": f"上傳 {os.path.basename(local_f)} 失敗", "error": err_msg}

        # 4. 遠端執行並即時串流進度
        _emit_prog("Colab VM 開始執行下載任務...", 0.08, 0.0, "遠端執行中")
        proc = subprocess.Popen(
            [colab_bin, "exec", "-s", session_name, "--timeout", "1800", "-f", runner_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=_colab_env(),
        )

        final_result = None
        raw_logs = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if line.startswith(EVENT_PREFIX):
                try:
                    ev = json.loads(line[len(EVENT_PREFIX) :])
                    ev_type = ev.get("type")
                    if ev_type == "status":
                        _emit_prog(
                            ev.get("current_item", ""),
                            float(ev.get("pct", 0.0)),
                            float(ev.get("speed_mb", 0.0)),
                            ev.get("log", ""),
                        )
                    elif ev_type == "log":
                        _emit_log(ev.get("log", ""))
                    elif ev_type in ("completed", "failed"):
                        final_result = {
                            "status": ev_type,
                            "success_files": ev.get("success_files", []),
                            "failed_items": ev.get("failed_items", []),
                            "log": ev.get("log", ""),
                            "error": ev.get("error", ""),
                        }
                except Exception:
                    raw_logs.append(line)
            else:
                raw_logs.append(line)
                if line.strip():
                    _emit_log(line.strip())

        ret_code = proc.wait()
        if final_result:
            return final_result
        if ret_code == 0:
            return {"status": "completed", "success_files": [], "failed_items": [], "log": "Colab 執行完畢", "error": ""}
        return {
            "status": "failed",
            "log": f"Colab 遠端執行異常 (exit={ret_code})",
            "error": "\n".join(raw_logs[-80:]),
        }
    finally:
        if session_created:
            try:
                subprocess.run(
                    [colab_bin, "stop", "-s", session_name],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=_colab_env(),
                )
            except Exception:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
