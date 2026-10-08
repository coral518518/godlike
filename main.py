#!/usr/bin/env python3
"""
Godlike 主机自动续期 + 开机脚本（登录版）

优化点:
  - requests.Session 连接池复用 + 自动重试
  - 登录失败自动重试（最多 3 次）
  - 续期步骤自适应：服务端返回 current_time 时动态修正
  - WebSocket 开机增加状态预检，已在运行则跳过
  - 完善的异常隔离，单账号崩溃不影响其他账号
  - 日志脱敏 + TG 通知使用真实信息
"""

import os
import sys
import time
import random
import json
import re
import asyncio
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Tuple

import requests
import websockets
from playwright.sync_api import sync_playwright

# ---------- 配置 ----------
FRONT_BASE = "https://ultra.panel.godlike.host"
API_BASE = "https://panel.godlike.host/api/v2"
LOGIN_URL = f"{FRONT_BASE}/login"
OUTPUT_DIR = Path("scripts/Godlike")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CN_TZ = timezone(timedelta(hours=8))

MAX_LOGIN_RETRIES = 3          # 登录最大重试次数
VIDEO_STEPS = [30, 60, 90, 120, 150, 180, 210, 240]
STEP_WAIT = 28                 # 每步间隔秒数
ACCOUNT_INTERVAL = (5, 15)     # 多账号间隔随机范围（秒）

# ---------- 硬编码凭据（仓库已设为私有）----------
# 账号列表，格式: "邮箱-----密码"，可添加多个
HARDCODED_ACCOUNTS = [
]

# Telegram 通知
HARDCODED_TG_TOKEN = ""
HARDCODED_TG_CHAT_ID = ""

# ---------- HTTP Session（连接池复用）----------
_http_session: Optional[requests.Session] = None


def http() -> requests.Session:
    """全局 requests.Session，复用 TCP 连接"""
    global _http_session
    if _http_session is None:
        from urllib3.util.retry import Retry
        from requests.adapters import HTTPAdapter

        retry = Retry(total=3, backoff_factor=1,
                      status_forcelist=[500, 502, 503, 504],
                      allowed_methods=["GET", "POST"])
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)

        s = requests.Session()
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _http_session = s
    return _http_session


# ---------- 工具函数 ----------
def cn_time() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def mask_email(email: str) -> str:
    """仅用于工作流日志脱敏"""
    if not email or "@" not in email:
        return "***"
    user, domain = email.split("@", 1)
    parts = domain.split(".")
    masked_domain = "***." + parts[-1] if len(parts) >= 2 else "***"
    return f"{user[:3]}***@{masked_domain}"


def mask_server(server_id: str) -> str:
    if not server_id:
        return "***"
    return f"{server_id[:3]}***"


def log(msg: str, level: str = "INFO") -> None:
    tag = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    print(f"{tag} {msg}", flush=True)


def screenshot(page, name: str) -> Optional[str]:
    """截图保存，返回文件路径"""
    path = str(OUTPUT_DIR / f"{name}_{int(time.time())}.png")
    try:
        page.screenshot(path=path, full_page=True)
        log(f"截图已保存: {path}")
        return path
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return None


# ---------- Telegram 通知 ----------
def notify_tg(ok: bool, email: str = "", server: str = "",
              before: str = "", after: str = "",
              error_msg: str = "", screenshot: str = None) -> None:
    """TG 通知（使用真实邮箱和服务器ID）"""
    # 优先使用环境变量，回退到硬编码
    token = os.environ.get("TG_BOT_TOKEN", "").strip() or HARDCODED_TG_TOKEN
    chat_id = os.environ.get("TG_CHAT_ID", "").strip() or HARDCODED_TG_CHAT_ID
    if not token or not chat_id:
        return

    msg = "✅ 续期+开机成功\n\n" if ok else "❌ 操作失败\n\n"
    if email:
        msg += f"账号：{email}\n"
    if server:
        msg += f"服务器：{server}\n"
    if ok:
        if after:
            msg += f"下次可续期：{after}\n"
    else:
        if error_msg:
            msg += f"原因：{error_msg}\n"
    msg += f"\n时间：{cn_time()}\nGodlike Host Auto Renew"

    try:
        if screenshot and Path(screenshot).exists():
            with open(screenshot, "rb") as f:
                http().post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": msg},
                    files={"photo": f}, timeout=30)
        else:
            http().post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg,
                      "disable_web_page_preview": True},
                timeout=30)
        log("TG 通知已发送")
    except Exception as e:
        log(f"TG 通知发送失败: {e}", "WARN")


# ---------- 登录并获取 Bearer Token + UUID ----------
def login_and_get_token(user: str, pwd: str, proxy: str = None
                        ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    使用 Playwright 登录面板，拦截请求获取 Bearer Token 和服务器 UUID。
    返回: (bearer_token, full_uuid, short_id)
    """
    bearer_token = None
    full_uuid = None
    short_id = None

    def on_request(request):
        nonlocal bearer_token, full_uuid, short_id
        # 拦截 Authorization header 中的 Bearer token
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer ") and "ptlc_" in auth:
            token = auth.replace("Bearer ", "").strip()
            if bearer_token != token:
                bearer_token = token
        # 从 URL 中提取服务器 UUID
        url = request.url
        m = re.search(
            r'/servers/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', url)
        if m and not full_uuid:
            full_uuid = m.group(1)
            short_id = full_uuid.split('-')[0]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": proxy} if proxy else None,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = browser.new_context(
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()
        page.on("request", on_request)

        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

            # 切换到账号密码登录
            switch = page.locator('button:has-text("Through login/password")')
            switch.wait_for(state="visible", timeout=15000)
            switch.click()
            page.wait_for_timeout(2000)

            # 填写表单
            page.locator('input[placeholder="Username or Email"]').wait_for(
                state="visible", timeout=15000)
            page.fill('input[placeholder="Username or Email"]', user)
            page.fill('input[placeholder="Password"]', pwd)

            # 点击登录
            for sel in ['button[type="submit"]', 'button:has-text("Login")']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible():
                        btn.click(timeout=5000)
                        break
                except Exception:
                    pass

            page.wait_for_timeout(5000)

            # 处理登录后的引导页面
            for _ in range(5):
                for sel in ['button:has-text("Go to my server")',
                            'button:has-text("Skip")']:
                    try:
                        el = page.locator(sel)
                        if el.count() > 0 and el.first.is_visible():
                            el.first.click()
                            page.wait_for_timeout(2000)
                            break
                    except Exception:
                        pass
                if '/server/' in page.url:
                    break
                page.wait_for_timeout(1000)

            # 从 URL 中提取 short_id
            if not short_id and '/server/' in page.url:
                parts = page.url.rstrip('/').split('/')
                for i, part in enumerate(parts):
                    if part == 'server' and i + 1 < len(parts) and len(parts[i + 1]) == 8:
                        short_id = parts[i + 1]
                        break

            # 从链接中提取 short_id
            if not short_id:
                links = page.locator('a[href*="/server/"]')
                for i in range(links.count()):
                    href = links.nth(i).get_attribute("href") or ""
                    parts = href.rstrip('/').split('/')
                    for j, part in enumerate(parts):
                        if part == 'server' and j + 1 < len(parts) and len(parts[j + 1]) == 8:
                            short_id = parts[j + 1]
                            break
                    if short_id:
                        break

            # 访问服务器页面以触发 API 请求获取 token
            if short_id and not bearer_token:
                page.goto(f"{FRONT_BASE}/server/{short_id}",
                          wait_until="domcontentloaded", timeout=30000)
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(3000)

            # 从页面 HTML 中提取 full_uuid
            if not full_uuid and short_id:
                try:
                    html_content = page.content()
                    m = re.search(
                        rf'{re.escape(short_id)}-[0-9a-f]{{4}}-[0-9a-f]{{4}}'
                        rf'-[0-9a-f]{{4}}-[0-9a-f]{{12}}',
                        html_content)
                    if m:
                        full_uuid = m.group(0)
                except Exception:
                    pass

            return bearer_token, full_uuid, short_id

        except Exception as e:
            log(f"登录异常: {e}", "ERROR")
            traceback.print_exc()
            return None, None, None
        finally:
            context.close()
            browser.close()


def login_with_retry(user: str, pwd: str, proxy: str = None
                     ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """登录带重试"""
    for attempt in range(1, MAX_LOGIN_RETRIES + 1):
        log(f"登录尝试 {attempt}/{MAX_LOGIN_RETRIES}")
        bearer, uuid, sid = login_and_get_token(user, pwd, proxy)
        if bearer and uuid:
            return bearer, uuid, sid
        if attempt < MAX_LOGIN_RETRIES:
            wait = random.randint(5, 10)
            log(f"登录失败，{wait}s 后重试...", "WARN")
            time.sleep(wait)
    return None, None, None


# ---------- API 公共 headers ----------
def api_headers(bearer_token: str) -> dict:
    return {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": FRONT_BASE,
        "Referer": f"{FRONT_BASE}/",
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    }


# ---------- 检查续期状态 ----------
def check_video_status(full_uuid: str, bearer_token: str) -> dict:
    url = f"{API_BASE}/servers/{full_uuid}/free-renewal/video/status?type=youtube_iter1&locale=en"
    try:
        resp = http().get(url, headers=api_headers(bearer_token), timeout=30)
        return resp.json()
    except Exception as e:
        log(f"状态查询失败: {e}", "ERROR")
        return {}


# ---------- 调用 start API ----------
def call_video_start(full_uuid: str, bearer_token: str) -> dict:
    url = f"{API_BASE}/servers/{full_uuid}/free-renewal/video/start?locale=en"
    try:
        resp = http().post(url, headers=api_headers(bearer_token),
                           json={"type": "youtube_iter1"}, timeout=30)
        return resp.json()
    except Exception as e:
        log(f"Start API 失败: {e}", "ERROR")
        return {}


# ---------- 单次 update-time ----------
def call_update_once(full_uuid: str, bearer_token: str,
                     watch_uuid: str, renewal_id: int,
                     video_time: int) -> dict:
    url = f"{API_BASE}/servers/{full_uuid}/free-renewal/video/update-time?locale=en"
    body = {
        "uuid": watch_uuid,
        "renewal_uuid": watch_uuid,
        "renewal_id": renewal_id,
        "video_time_watched": video_time,
    }
    try:
        resp = http().post(url, headers=api_headers(bearer_token),
                           json=body, timeout=30)
        return resp.json()
    except Exception as e:
        log(f"update-time 异常: {e}", "ERROR")
        return {}


# ---------- 模拟视频观看（自适应）----------
def simulate_video_watching(full_uuid: str, bearer_token: str,
                            watch_uuid: str, renewal_id: int
                            ) -> Tuple[bool, str]:
    steps = list(VIDEO_STEPS)
    total = len(steps) * STEP_WAIT
    log(f"开始续期（共{len(steps)}步 × {STEP_WAIT}s，预计{total}s）")

    last_resp = {}
    for i, video_time in enumerate(steps):
        log(f"步骤 {i + 1}/{len(steps)} 等待{STEP_WAIT}s → 上报{video_time}s")
        time.sleep(STEP_WAIT)

        resp = call_update_once(full_uuid, bearer_token, watch_uuid,
                                renewal_id, video_time)
        last_resp = resp
        success = resp.get("success", False)
        msg = resp.get("message", "")
        new_timer = resp.get("new_free_timer")

        if not success:
            # 时间偏差 → 用服务端返回的 current_time 修正后续步骤
            if "Invalid time increment" in msg:
                current = resp.get("current_time", 0)
                if current > 0:
                    adjusted = current + 30
                    log(f"时间偏差，调整为{adjusted}s重试", "WARN")
                    time.sleep(2)
                    resp2 = call_update_once(full_uuid, bearer_token,
                                             watch_uuid, renewal_id, adjusted)
                    if resp2.get("success"):
                        last_resp = resp2
                        new_timer = resp2.get("new_free_timer")
                        # 动态修正剩余步骤
                        remaining = len(steps) - i - 1
                        steps[i + 1:] = [adjusted + 30 * (j + 1)
                                         for j in range(remaining)]
                        if new_timer:
                            log(f"✅ 续期完成 new_free_timer={new_timer}")
                            return True, new_timer
                        continue
                    return False, resp2.get("message", "重试失败")
            return False, msg

        if new_timer:
            log(f"✅ 续期完成 new_free_timer={new_timer}")
            return True, new_timer

    new_timer = last_resp.get("new_free_timer")
    if last_resp.get("success"):
        return True, new_timer or "续期已提交"
    return False, last_resp.get("message", "未知错误")


# ---------- 获取 WS JWT ----------
def get_websocket_credentials(full_uuid: str, bearer_token: str
                              ) -> Tuple[Optional[str], Optional[str]]:
    url = f"{API_BASE}/servers/{full_uuid}/websocket?locale=en"
    try:
        resp = http().get(url, headers=api_headers(bearer_token), timeout=30)
        if resp.status_code == 200:
            data = resp.json().get("data", resp.json())
            return data.get("token"), data.get("socket")
        log(f"WS 凭证获取失败: HTTP {resp.status_code}", "ERROR")
        return None, None
    except Exception as e:
        log(f"获取 WS 凭证异常: {e}", "ERROR")
        return None, None


# ---------- WebSocket 开机 ----------
async def ws_start_server(socket_url: str, jwt: str) -> str:
    """
    通过 WebSocket 发送开机指令。
    返回值:
      "already_running" - 服务器已在运行，无需开机
      "started"         - 已发送开机指令并确认启动
      "sent"            - 已发送开机指令，等待超时但指令已发出
      "auth_failed"     - WS 认证失败
      "error"           - 其他异常
    """
    log("连接 WebSocket...")
    try:
        async with websockets.connect(
            socket_url,
            origin="https://ultra.panel.godlike.host",
            additional_headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            },
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        ) as ws:
            # ── 认证 ──
            await ws.send(json.dumps({"event": "auth", "args": [jwt]}))
            auth_success = False
            for _ in range(10):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    data = json.loads(msg)
                    if data.get("event") == "auth success":
                        auth_success = True
                        log("✅ WS 认证成功")
                        break
                except asyncio.TimeoutError:
                    break

            if not auth_success:
                log("WS 认证失败", "ERROR")
                return "auth_failed"

            # ── 收集服务器初始状态 ──
            current_status = "unknown"
            deadline = asyncio.get_event_loop().time() + 5
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    data = json.loads(msg)
                    event = data.get("event", "")

                    if event == "status":
                        current_status = data.get("args", ["unknown"])[0]
                        log(f"服务器当前状态: {current_status}")
                        break
                    elif event == "stats":
                        try:
                            state = json.loads(data["args"][0]).get("state", "")
                            if state:
                                current_status = state
                                log(f"服务器当前状态: {current_status}")
                                break
                        except Exception:
                            pass
                except asyncio.TimeoutError:
                    break
                except Exception:
                    break

            # ── 已在运行/启动中 → 跳过 ──
            if current_status in ("running", "starting"):
                log(f"服务器已在运行中（{current_status}），无需开机")
                #return "already_running"

            # ── 发送开机指令 ──
            await ws.send(json.dumps({"event": "set state", "args": ["start"]}))
            log("已发送开机指令，等待启动...")

            # ── 等待状态变更确认 ──
            for _ in range(60):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(msg)
                    event = data.get("event", "")

                    if event == "status":
                        new_status = data.get("args", [""])[0]
                        log(f"状态变更: {new_status}")
                        if new_status in ("starting", "running"):
                            log(f"✅ 服务器启动成功（{new_status}）")
                            return "started"
                    elif event == "stats":
                        try:
                            state = json.loads(data["args"][0]).get("state", "")
                            if state in ("starting", "running"):
                                log(f"✅ 服务器启动成功（{state}）")
                                return "started"
                        except Exception:
                            pass
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break

            log("等待超时，开机指令已发送", "WARN")
            return "sent"

    except Exception as e:
        log(f"WebSocket 异常: {e}", "ERROR")
        return "error"


def start_server_via_ws(full_uuid: str, bearer_token: str) -> str:
    jwt, socket_url = get_websocket_credentials(full_uuid, bearer_token)
    if not jwt or not socket_url:
        log("无法获取 WS 凭证", "ERROR")
        return "error"
    try:
        return asyncio.run(ws_start_server(socket_url, jwt))
    except Exception as e:
        log(f"WS 开机异常: {e}", "ERROR")
        return "error"


# ---------- 单账号主流程 ----------
def process_account(account_str: str, label: str = "", proxy: str = None) -> bool:
    """
    处理单个账号。account_str 格式: "邮箱-----密码"
    label 仅用于日志标识。
    """
    raw = account_str.strip()
    print(f" raw1 {raw}", flush=True)
    if not raw:
        return True  # 空账号跳过

    print(f" raw2 {raw}", flush=True)
    # 解析 邮箱-----密码
    try:
        parts = raw.split("-----")
        user = parts[0].strip()
        pwd = parts[1].strip()
    except Exception:
        log(f"{label} 格式错误", "ERROR")
        notify_tg(False, error_msg=f"{label} 格式错误")
        return False

    masked = mask_email(user)
    print(f"\n{'=' * 60}", flush=True)
    print(f"[INFO] 处理 {label} ({masked})", flush=True)
    print(f"{'=' * 60}", flush=True)

    # 1. 登录（带重试）
    bearer_token, full_uuid, short_id = login_with_retry(user, pwd, proxy)
    if not bearer_token:
        log("未能获取 Bearer Token", "ERROR")
        notify_tg(False, email=user, error_msg="未能获取 Bearer Token")
        return False
    if not full_uuid:
        log("未能获取服务器 UUID", "ERROR")
        notify_tg(False, email=user, error_msg="未能获取服务器 UUID")
        return False

    masked_server = mask_server(short_id)
    log(f"🔑 登录成功 | 服务器: {masked_server}")

    # 2. 检查续期状态
    status = check_video_status(full_uuid, bearer_token)
    can_watch = status.get("can_watch", None)
    time_until_next = status.get("time_until_next_video") or 0

    renew_ok = False
    cooldown_after = ""

    if not can_watch and time_until_next > 0:
        h = time_until_next // 3600
        m = (time_until_next % 3600) // 60
        cooldown_after = f"{h}h {m}m"
        log(f"已在冷却期，下次可续期: {cooldown_after}")
        renew_ok = True
    else:
        # 3. 续期
        start_resp = call_video_start(full_uuid, bearer_token)
        if not start_resp.get("success"):
            err = start_resp.get("message", "start API 失败")
            log(f"续期启动失败: {err}", "ERROR")
            notify_tg(False, email=user, server=short_id, error_msg=err)
            return False

        watch_uuid = start_resp.get("uuid", "")
        renewal_id = start_resp.get("renewal_id", 0)
        log(f"续期会话已建立 (renewal_id={renewal_id})")

        renew_ok, result = simulate_video_watching(
            full_uuid, bearer_token, watch_uuid, renewal_id)

        if not renew_ok:
            log(f"续期失败: {result}", "ERROR")
            notify_tg(False, email=user, server=short_id, error_msg=result)
            return False

        # 确认冷却时间
        time.sleep(2)
        status_after = check_video_status(full_uuid, bearer_token)
        time_until = status_after.get("time_until_next_video") or 0
        h = time_until // 3600
        m_min = (time_until % 3600) // 60
        cooldown_after = f"{h}h {m_min}m" if time_until > 0 else result
        log(f"续期成功，下次可续期: {cooldown_after}")

    # 4. WS 开机
    print(f"[INFO] ── 开机 ──", flush=True)
    start_result = start_server_via_ws(full_uuid, bearer_token)

    start_note_map = {
        "already_running": "✅ 服务器已在运行中，无需开机",
        "started":         "✅ 开机成功",
        "sent":            "✅ 开机指令已发送（等待超时）",
        "auth_failed":     "⚠️ WS 认证失败",
        "error":           "⚠️ 开机异常",
    }
    start_note = start_note_map.get(start_result, "⚠️ 未知状态")
    log(start_note)

    # 5. TG 通知
    notify_tg(
        ok=renew_ok,
        email=user,
        server=short_id,
        after=f"{cooldown_after}\n{start_note}",
    )

    log(f"✅ {label} 处理完成")
    return renew_ok


# ---------- 主入口 ----------
def main():
    proxy = os.environ.get("PROXY_SERVER", "").strip()
    if proxy:
        log(f"使用代理: {mask_server(proxy)}")

    # 优先使用环境变量中的账号，回退到硬编码
    accounts = []
    for i in range(1, 6):
        env_val = os.environ.get(f"GODLIKE_{i}", "").strip()
        if env_val:
            accounts.append((f"GODLIKE_{i}", env_val))
    if not accounts:
        for i, acc in enumerate(HARDCODED_ACCOUNTS, 1):
            if acc.strip():
                accounts.append((f"Account-{i}", acc.strip()))

    if not accounts:
        log("没有配置任何账号，退出", "ERROR")
        sys.exit(1)

    log(f"共 {len(accounts)} 个账号待处理")
    all_ok = True

    for idx, (label, acc_str) in enumerate(accounts):
        try:
            ok = process_account(acc_str, label, proxy if proxy else None)
            if not ok:
                all_ok = False
        except Exception as e:
            log(f"{label} 崩溃: {e}", "ERROR")
            traceback.print_exc()
            notify_tg(False, email=label, error_msg=f"脚本崩溃: {str(e)[:200]}")
            all_ok = False

        # 多账号间隔
        if idx < len(accounts) - 1:
            wait = random.randint(*ACCOUNT_INTERVAL)
            log(f"等待 {wait}s 后处理下一个账号...")
            time.sleep(wait)

    if all_ok:
        print("\n[INFO] 🎉 所有账号处理成功", flush=True)
        sys.exit(0)
    else:
        print("\n[ERROR] 部分账号失败", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
