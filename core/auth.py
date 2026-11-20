# -*- coding: utf-8 -*-
"""Cookie 暫存檔案管理與 YouTube Bot 驗證自動重試模組。"""

import copy
import os
import tempfile
import yt_dlp


def normalize_cookies_dict(cookies):
    """統一整理並相容本機與 Colab 傳入的 Cookie 字典鍵名。"""
    if not cookies or not isinstance(cookies, dict):
        return {"fb_cookie": "", "bili_cookie": "", "yt_cookie": ""}
    return {
        "fb_cookie": str(cookies.get("fb_cookie") or cookies.get("facebook") or "").strip(),
        "bili_cookie": str(cookies.get("bili_cookie") or cookies.get("bilibili") or "").strip(),
        "yt_cookie": str(cookies.get("yt_cookie") or cookies.get("youtube") or "").strip(),
    }


def create_temp_cookiefile(cookie_str, domain=".facebook.com"):
    """將 Cookie 字串或 Netscape cookies.txt 內容寫入暫存檔並回傳路徑。"""
    if not cookie_str or not str(cookie_str).strip():
        return None
    raw = str(cookie_str).strip()
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="ytdlp_cookies_")

    # 若使用者直接貼上 Netscape cookies.txt 格式，原樣寫入
    if "# Netscape HTTP Cookie File" in raw or any(
        line.count("\t") >= 6
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ):
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if "# Netscape HTTP Cookie File" not in raw:
                f.write("# Netscape HTTP Cookie File\n")
            f.write(raw + "\n")
        return path

    # Parse semicolon-separated Cookie header string
    cookie_dict = {}
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        parts = item.split("=", 1)
        if len(parts) == 2:
            cookie_dict[parts[0].strip()] = parts[1].strip()

    if not cookie_dict:
        os.close(fd)
        try:
            os.remove(path)
        except Exception:
            pass
        return None

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write("# This file is generated automatically by Media Downloader\n")
        for k, v in cookie_dict.items():
            f.write(f"{domain}\tTRUE\t/\tTRUE\t0\t{k}\t{v}\n")
    return path


def is_youtube_url(url_str):
    u = (url_str or "").lower()
    return any(d in u for d in ["youtube.com", "youtu.be", "googlevideo.com"])


_is_youtube_url = is_youtube_url


def get_available_yt_browsers():
    """偵測本機實際存在 Cookies 資料庫的瀏覽器，避免對未使用的瀏覽器報錯。"""
    home = os.path.expanduser("~")
    candidates = [
        ("chrome", ("chrome",), os.path.join(home, "Library/Application Support/Google/Chrome/Default/Cookies")),
        ("comet", ("chrome", os.path.join(home, "Library/Application Support/Comet/Default")), os.path.join(home, "Library/Application Support/Comet/Default/Cookies")),
        ("brave", ("brave",), os.path.join(home, "Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies")),
        ("edge", ("edge",), os.path.join(home, "Library/Application Support/Microsoft Edge/Default/Cookies")),
        ("firefox", ("firefox",), os.path.join(home, "Library/Application Support/Firefox/Profiles")),
        ("safari", ("safari",), os.path.join(home, "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies")),
    ]
    return [(name, cfb_arg) for name, cfb_arg, check_path in candidates if os.path.exists(check_path)]


_get_available_yt_browsers = get_available_yt_browsers


def run_ytdlp_with_youtube_auth(
    target_url,
    base_opts,
    action_fn,
    auth_mode="auto",
    yt_cookie_str="",
    cached_cfb=None,
    on_cfb_detected=None,
    on_cfb_invalid=None,
):
    """
    執行 yt-dlp 操作，針對 YouTube「Sign in to confirm you're not a bot」驗證阻擋，
    依使用者設定或自動讀取本機瀏覽器 Cookies 重試。
    """
    if not is_youtube_url(target_url):
        with yt_dlp.YoutubeDL(base_opts) as ydl:
            return action_fn(ydl)

    auth_mode = auth_mode or "auto"
    yt_cookie_str = (yt_cookie_str or "").strip()

    def _run_with_opts(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            return action_fn(ydl)

    def _opts_with_cookies(cfb_arg=None, cookiefile_path=None):
        o = copy.deepcopy(base_opts)
        # 帶入 Cookies 時移除強制 android/ios client，讓 yt-dlp 使用支援 cookies 的預設/web client
        if "extractor_args" in o and "youtube" in o["extractor_args"]:
            o["extractor_args"] = copy.deepcopy(o["extractor_args"])
            o["extractor_args"].pop("youtube", None)
            if not o["extractor_args"]:
                o.pop("extractor_args", None)
        if cfb_arg:
            o["cookiesfrombrowser"] = cfb_arg
        if cookiefile_path:
            o["cookiefile"] = cookiefile_path
        return o

    # 1. 手動 Cookie 模式
    if auth_mode == "cookie":
        if not yt_cookie_str:
            raise ValueError("您已選擇「手動貼上 YouTube Cookie」，但尚未填入 Cookie 內容。")
        cf = create_temp_cookiefile(yt_cookie_str, domain=".youtube.com")
        try:
            return _run_with_opts(_opts_with_cookies(cookiefile_path=cf))
        finally:
            if cf and os.path.exists(cf):
                try:
                    os.remove(cf)
                except Exception:
                    pass

    # 2. 指定本機瀏覽器模式
    browser_map = {
        "chrome": ("chrome",),
        "comet": ("chrome", os.path.expanduser("~/Library/Application Support/Comet/Default")),
        "safari": ("safari",),
        "edge": ("edge",),
        "firefox": ("firefox",),
        "brave": ("brave",),
    }
    if auth_mode in browser_map:
        return _run_with_opts(_opts_with_cookies(cfb_arg=browser_map[auth_mode]))

    # 3. 自動模式：若本輪已有成功過的瀏覽器，優先直接帶入；否則先裸連，遇到 bot 驗證再依序嘗試本機實際存在 Cookies 的瀏覽器
    if cached_cfb:
        try:
            return _run_with_opts(_opts_with_cookies(cfb_arg=cached_cfb))
        except Exception:
            if on_cfb_invalid:
                on_cfb_invalid()

    try:
        return _run_with_opts(base_opts)
    except Exception as first_err:
        err_lower = str(first_err).lower()
        if not any(
            k in err_lower
            for k in [
                "not a bot",
                "sign in to confirm",
                "confirm you’re not a bot",
                "confirm you're not a bot",
            ]
        ):
            raise

        # 若有填手動 YouTube Cookie，優先用它重試
        if yt_cookie_str:
            cf = create_temp_cookiefile(yt_cookie_str, domain=".youtube.com")
            try:
                return _run_with_opts(_opts_with_cookies(cookiefile_path=cf))
            except Exception:
                pass
            finally:
                if cf and os.path.exists(cf):
                    try:
                        os.remove(cf)
                    except Exception:
                        pass

        # 依序嘗試本機實際存在 Cookies 的瀏覽器
        available_browsers = get_available_yt_browsers()
        browser_errors = []
        for b_name, cfb_arg in available_browsers:
            try:
                res = _run_with_opts(_opts_with_cookies(cfb_arg=cfb_arg))
                if on_cfb_detected:
                    on_cfb_detected(cfb_arg)
                return res
            except Exception as b_err:
                browser_errors.append(f"{b_name}: {b_err}")

        raise ValueError(
            "YouTube 要求登入驗證（Sign in to confirm you're not a bot），且自動讀取本機瀏覽器 Cookie 未成功。\n\n"
            "💡 **最快的解決方式（擇一）**：\n"
            "1. **用「手動貼上 YouTube Cookie」**（最穩）：在您平常看 YouTube 的瀏覽器（如 Comet）安裝「Get cookies.txt LOCALLY」擴充功能，於 YouTube 頁面匯出後貼到上方。\n"
            "2. **打開 Google Chrome 並登入一次 YouTube**：您的 Mac 已安裝 Chrome 但尚未建立設定檔；只要開啟 Chrome 登入 YouTube 後，本程式就能自動讀取。\n"
            "3. **若使用 VPN（如 Surfshark）**：YouTube 常封鎖 VPN 節點 IP，可嘗試暫停 VPN 或切換節點後再下載。\n\n"
            f"已偵測並嘗試的瀏覽器: {' | '.join(browser_errors) if browser_errors else '本機找不到已建立的 Chrome/Brave/Edge/Firefox/Safari Cookies 資料庫'}"
        )
