#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Godlike 外链续期脚本（reCAPTCHA 音频破解版）

优化点:
  - requests.Session 连接池复用 + 自动重试
  - reCAPTCHA 破解流程模块化，状态追踪更清晰
  - 资源清理统一化（tempfile、page）
  - 日志系统统一，支持多级别输出
  - 异常隔离完善，单个 ID 失败不影响后续
  - 支持手动指定部分 ID 运行
"""

import os
import sys
import time
import random
import html
import tempfile
import subprocess
from pathlib import Path
from typing import Optional

import requests
from xvfbwrapper import Xvfb
from DrissionPage import ChromiumPage, ChromiumOptions

try:
    import speech_recognition as sr
    from pydub import AudioSegment
except ImportError:
    sr = None
    AudioSegment = None

# ============================================================
# 配置
# ============================================================
BASE_URL = "https://godlike.cool"
MAX_CAPTCHA_ATTEMPTS = 3        # 单次 reCAPTCHA 最大尝试次数
MAX_RETRIES_PER_ID = 20         # 每个 ID 最大重试次数（含换 IP）
SCREENSHOT_DIR = Path("output/screensshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# HTTP Session（连接池复用）
_http_session: Optional[requests.Session] = None


def http() -> requests.Session:
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


# ============================================================
# 日志
# ============================================================
def log(msg: str, level: str = "INFO"):
    tag = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    print(f"{tag} {msg}", flush=True)


# ============================================================
# 自定义异常
# ============================================================
class CaptchaBlocked(Exception):
    """IP 被 reCAPTCHA 封锁"""
    pass


# ============================================================
# Telegram 通知
# ============================================================
def send_tg_photo(token: str, chat_id: str, photo_path: str, caption: str):
    if not token or not chat_id:
        log("未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过通知", "WARN")
        return

    api_url = f"https://api.telegram.org/bot{token}/sendPhoto"
    if photo_path and os.path.exists(photo_path):
        try:
            with open(photo_path, "rb") as f:
                resp = http().post(api_url, data={"chat_id": chat_id,
                                                   "caption": caption},
                                   files={"photo": f}, timeout=30)
                resp.raise_for_status()
                log("Telegram 图片通知已发送")
                return
        except Exception as e:
            log(f"Telegram 图片通知失败，尝试纯文本: {e}", "WARN")

    try:
        resp = http().post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": caption}, timeout=30)
        resp.raise_for_status()
        log("Telegram 文本通知已发送")
    except Exception as e:
        log(f"Telegram 文本通知也失败: {e}", "ERROR")


def build_caption(status: str, godlike_id: str, reason: str = "") -> str:
    url = f"{BASE_URL}/{godlike_id}"
    if status == "success":
        title = "✅ 续订成功"
    elif status == "maxed":
        title = "⏳ 24小时上限"
    elif status == "cooldown":
        title = "⏳ 6分钟冷却期"
    else:
        title = "❌ 续订失败"
    lines = [title, "", f"URL: {url}"]
    if reason and status == "failure":
        lines.append(f"原因: {reason}")
    lines += ["", "Godlike Host Auto Renew"]
    return "\n".join(lines)


# ============================================================
# 截图
# ============================================================
def screenshot(page, name: str) -> Optional[str]:
    path = str(SCREENSHOT_DIR / name)
    try:
        page.get_screenshot(path=path)
        log(f"截图已保存: {path}")
        return path
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return None


# ============================================================
# WARP 换 IP
# ============================================================
def restart_warp() -> bool:
    log("正在重启 WARP 更换 IP...")
    try:
        old_ip = http().get("https://api.ipify.org", timeout=10).text.strip()
        log(f"当前 IP: {old_ip}")
    except Exception:
        old_ip = "未知"

    cmds = [
        ["sudo", "warp-cli", "--accept-tos", "disconnect"],
        ["sudo", "warp-cli", "--accept-tos", "registration", "delete"],
        ["sudo", "warp-cli", "--accept-tos", "registration", "new"],
        ["sudo", "warp-cli", "--accept-tos", "connect"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=True, timeout=30, capture_output=True)
        except subprocess.CalledProcessError as e:
            log(f"命令失败（忽略）: {' '.join(cmd)} → {e}", "WARN")
        time.sleep(3)

    time.sleep(10)
    try:
        new_ip = http().get("https://api.ipify.org", timeout=10).text.strip()
        log(f"WARP 重连完成，新 IP: {new_ip}")
        return new_ip != old_ip
    except Exception as e:
        log(f"获取新 IP 失败: {e}", "WARN")
        return False


# ============================================================
# reCAPTCHA 辅助
# ============================================================
def _get_frame(page, kind: str):
    """获取指定类型的 reCAPTCHA frame"""
    try:
        for frame in page.get_frames():
            if "recaptcha" in (frame.url or "") and kind in (frame.url or ""):
                return frame
    except Exception:
        pass
    return None


def _is_solved(page) -> bool:
    """检测 reCAPTCHA 是否已通过"""
    try:
        token = page.run_js(
            "return document.querySelector(\"textarea[name='g-recaptcha-response']\")?.value")
        if token and len(token) > 30:
            return True
    except Exception:
        pass
    anchor = _get_frame(page, "anchor")
    if anchor:
        try:
            checked = anchor.run_js(
                "return document.querySelector('#recaptcha-anchor')"
                "?.getAttribute('aria-checked') === 'true'")
            if checked:
                return True
        except Exception:
            pass
    return False


def _is_blocked(page) -> bool:
    """检测是否被 reCAPTCHA 封锁"""
    bframe = _get_frame(page, "bframe")
    if not bframe:
        return False
    try:
        return bool(bframe.run_js("""
            const h = document.querySelector('.rc-doscaptcha-header-text');
            if (h && h.textContent.toLowerCase().includes('try again later')) return true;
            return false;
        """))
    except Exception:
        return False


def _click_checkbox(page):
    """点击 reCAPTCHA 复选框"""
    anchor = None
    for _ in range(30):
        anchor = _get_frame(page, "anchor")
        if anchor:
            break
        time.sleep(1)
    if not anchor:
        raise RuntimeError("未找到 reCAPTCHA anchor frame")

    cb = anchor.ele('#recaptcha-anchor', timeout=5)
    if not cb:
        raise RuntimeError("未找到复选框元素")

    page.actions.move_to(cb, duration=random.uniform(0.4, 1.0))
    time.sleep(random.uniform(0.2, 0.5))
    try:
        cb.click()
    except Exception:
        cb.click(by_js=True)
    time.sleep(3)

    if _is_blocked(page):
        raise CaptchaBlocked("点击复选框后 IP 被封锁")


def _switch_to_audio(page) -> bool:
    """切换到音频验证模式"""
    bframe = _get_frame(page, "bframe")
    if not bframe:
        return False

    # 已在音频模式
    try:
        inp = bframe.ele('#audio-response', timeout=1)
        if inp and inp.states.is_displayed:
            return True
    except Exception:
        pass

    for _ in range(3):
        try:
            btn = bframe.ele('#recaptcha-audio-button', timeout=3)
            if btn:
                try:
                    btn.click()
                except Exception:
                    btn.click(by_js=True)
                time.sleep(3)
                if _is_blocked(page):
                    raise CaptchaBlocked("切换音频后 IP 被封锁")
                inp = bframe.ele('#audio-response', timeout=1)
                if inp and inp.states.is_displayed:
                    return True
        except CaptchaBlocked:
            raise
        except Exception:
            pass
        time.sleep(2)
    return False


def _get_audio_url(page) -> Optional[str]:
    """获取音频挑战 URL"""
    bframe = _get_frame(page, "bframe")
    if not bframe:
        return None
    for _ in range(10):
        try:
            for sel in ['.rc-audiochallenge-tdownload-link',
                        '.rc-audiochallenge-ndownload-link',
                        '#audio-source']:
                el = bframe.ele(sel, timeout=0.5)
                if el:
                    href = el.attr('href') or el.attr('src')
                    if href and len(href) > 10:
                        return html.unescape(href)
        except Exception:
            pass
        time.sleep(1)
    return None


def _reload_challenge(page):
    """刷新 reCAPTCHA 挑战"""
    bframe = _get_frame(page, "bframe")
    if not bframe:
        return
    try:
        btn = bframe.ele('#recaptcha-reload-button', timeout=2)
        if btn:
            try:
                btn.click()
            except Exception:
                btn.click(by_js=True)
            time.sleep(3)
    except Exception:
        pass


def _download_audio(url: str) -> Optional[str]:
    """下载音频文件"""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://www.google.com/",
    }
    variants = [url]
    if "recaptcha.net" in url:
        variants.append(url.replace("recaptcha.net", "www.google.com"))
    elif "google.com" in url:
        variants.append(url.replace("www.google.com", "recaptcha.net"))

    for u in variants:
        try:
            r = http().get(u, headers=headers, timeout=30)
            r.raise_for_status()
            if len(r.content) < 1000:
                continue
            fd, path = tempfile.mkstemp(suffix=".mp3")
            with os.fdopen(fd, "wb") as f:
                f.write(r.content)
            return path
        except Exception:
            pass
    return None


def _recognize_audio(mp3_path: str) -> Optional[str]:
    """语音识别"""
    if sr is None or AudioSegment is None:
        log("speech_recognition / pydub 未安装", "ERROR")
        return None
    wav_path = None
    try:
        wav_path = mp3_path.replace(".mp3", ".wav")
        AudioSegment.from_mp3(mp3_path).export(wav_path, format="wav")
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as src:
            audio = recognizer.record(src)
        return recognizer.recognize_google(audio)
    except Exception as e:
        log(f"音频识别失败: {e}", "WARN")
        return None
    finally:
        for p in [mp3_path, wav_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def _fill_and_verify(page, text: str):
    """填写验证码并提交"""
    bframe = _get_frame(page, "bframe")
    if not bframe:
        return
    try:
        inp = bframe.ele('#audio-response', timeout=2)
        if inp:
            inp.click()
            inp.clear()
            inp.input(text)
            time.sleep(random.uniform(0.5, 1.2))
        verify = bframe.ele('#recaptcha-verify-button', timeout=2)
        if verify:
            try:
                verify.click()
            except Exception:
                verify.click(by_js=True)
    except Exception as e:
        log(f"填写验证码异常: {e}", "WARN")


def solve_recaptcha(page) -> bool:
    """完整的 reCAPTCHA 破解流程，返回是否成功"""
    # 等待 reCAPTCHA 加载
    for _ in range(20):
        if _get_frame(page, "anchor"):
            break
        time.sleep(1)
    else:
        raise RuntimeError("reCAPTCHA anchor frame 加载超时")

    dl_fail_count = 0
    for attempt in range(MAX_CAPTCHA_ATTEMPTS):
        log(f"reCAPTCHA 破解尝试 {attempt + 1}/{MAX_CAPTCHA_ATTEMPTS}")

        if _is_solved(page):
            log("reCAPTCHA 已通过")
            return True
        if _is_blocked(page):
            raise CaptchaBlocked("IP 被 reCAPTCHA 封锁")

        # 第一次点击复选框
        if attempt == 0:
            _click_checkbox(page)
            time.sleep(2)
            if _is_solved(page):
                log("复选框点击后直接通过（无图形验证）")
                return True

        # 切换音频模式
        if not _switch_to_audio(page):
            log("无法切换到音频模式，重试", "WARN")
            time.sleep(3)
            continue

        time.sleep(random.uniform(2, 4))
        if _is_blocked(page):
            raise CaptchaBlocked("切换音频后 IP 被封锁")

        # 获取音频 URL
        audio_url = _get_audio_url(page)
        if not audio_url:
            log("未获取到音频 URL，刷新挑战", "WARN")
            _reload_challenge(page)
            time.sleep(3)
            continue

        # 下载音频
        mp3 = _download_audio(audio_url)
        if not mp3:
            dl_fail_count += 1
            log(f"音频下载失败（第{dl_fail_count}次）", "WARN")
            if dl_fail_count >= 3:
                raise RuntimeError("音频连续下载失败3次")
            _reload_challenge(page)
            time.sleep(random.uniform(3, 6))
            continue
        dl_fail_count = 0

        # 识别音频
        text = _recognize_audio(mp3)
        if not text:
            log("音频识别失败，刷新挑战", "WARN")
            _reload_challenge(page)
            time.sleep(3)
            continue

        log(f"识别结果: [{text}]")
        _fill_and_verify(page, text)
        time.sleep(5)

        if _is_solved(page):
            log("reCAPTCHA 验证通过！")
            return True

        log("验证未通过，刷新挑战重试", "WARN")
        _reload_challenge(page)
        time.sleep(random.uniform(2, 4))

    return False


# ============================================================
# 构建 Chrome 实例
# ============================================================
def build_page() -> ChromiumPage:
    co = ChromiumOptions()
    co.set_browser_path('/usr/bin/google-chrome')
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--disable-gpu')
    co.set_argument('--disable-setuid-sandbox')
    co.set_argument('--disable-software-rasterizer')
    co.set_argument('--disable-extensions')
    co.set_argument('--no-first-run')
    co.set_argument('--no-default-browser-check')
    co.set_argument('--disable-popup-blocking')
    co.set_argument('--window-size=1280,720')
    co.set_argument('--log-level=3')
    co.set_user_data_path(tempfile.mkdtemp())
    co.auto_port()
    co.headless(False)

    page = ChromiumPage(co)

    # 反指纹注入
    page.add_init_js("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
        const getP = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return 'Intel Inc.';
            if (p === 37446) return 'Intel(R) UHD Graphics 630';
            return getP.apply(this, [p]);
        };
    """)
    return page


# ============================================================
# 单个 ID 续期
# ============================================================
def renew_single_id(godlike_id: str, tg_token: str, tg_chat_id: str) -> bool:
    """对单个 Godlike ID 执行续期，包含换 IP 重试"""
    page_url = f"{BASE_URL}/{godlike_id}"
    log(f"{'=' * 60}")
    log(f"处理账号: {page_url}")
    log(f"{'=' * 60}")

    success = False
    failure_reason = ""
    screenshot_path = None
    final_status = None

    for attempt in range(1, MAX_RETRIES_PER_ID + 1):
        log(f"--- 尝试 {attempt}/{MAX_RETRIES_PER_ID} ---")
        page = None
        try:
            page = build_page()

            # ── 1. 访问续期页面 ──
            log(f"访问: {page_url}")
            page.get(page_url, retry=3)
            time.sleep(random.uniform(4, 7))

            page_html = page.html or ""
            if "Public Free Server Renewal" not in page_html:
                failure_reason = "页面加载异常"
                log(failure_reason, "WARN")
                continue

            # ── 2. 模拟人类行为 ──
            for _ in range(2):
                page.scroll.down(random.randint(100, 300))
                time.sleep(random.uniform(0.5, 1.2))
                page.actions.move(random.randint(200, 900),
                                  random.randint(100, 500))
                time.sleep(random.uniform(0.3, 0.8))

            # ── 3. 破解 reCAPTCHA ──
            log("开始破解 reCAPTCHA...")
            try:
                solved = solve_recaptcha(page)
            except CaptchaBlocked:
                log("IP 被封锁，换 IP 后重试", "WARN")
                failure_reason = "IP 被 reCAPTCHA 封锁"
                screenshot_path = screenshot(page,
                    f"godlike-{godlike_id}-blocked-{attempt}.png")
                page.quit()
                page = None
                restart_warp()
                time.sleep(5)
                continue
            except Exception as e:
                failure_reason = f"reCAPTCHA 异常: {e}"
                log(failure_reason, "ERROR")
                break

            if not solved:
                failure_reason = "reCAPTCHA 未通过"
                log(failure_reason, "WARN")
                screenshot_path = screenshot(page,
                    f"godlike-{godlike_id}-unsolved-{attempt}.png")
                page.quit()
                page = None
                restart_warp()
                time.sleep(5)
                continue

            # ── 4. 提交表单 ──
            log("reCAPTCHA 通过，点击 Renew Server 提交...")
            submit_btn = page.ele('xpath://button[@type="submit"]', timeout=5)
            if not submit_btn:
                submit_btn = page.ele(
                    'xpath://button[contains(text(),"Renew Server")]', timeout=3)
            if not submit_btn:
                failure_reason = "未找到提交按钮"
                log(failure_reason, "ERROR")
                break

            try:
                submit_btn.click()
            except Exception:
                submit_btn.click(by_js=True)

            # ── 5. 等待结果 ──
            time.sleep(8)
            result_html = (page.html or "").lower()

            if ("successfully renewed" in result_html or
                "server has been successfully renewed" in result_html):
                log("✅ 续订成功！")
                success = True
                final_status = "success"
                screenshot_path = screenshot(page,
                    f"godlike-{godlike_id}-success.png")
                break

            elif "maximum of 24 hours" in result_html:
                log("⏳ 服务器已累积24小时续期上限")
                success = True
                final_status = "maxed"
                failure_reason = "已达24小时上限"
                screenshot_path = screenshot(page,
                    f"godlike-{godlike_id}-maxed.png")
                break

            elif ("already renewed" in result_html or
                  "past 6 minutes" in result_html):
                log("⏳ 服务器正处于冷却期（6分钟内已有人续订）")
                success = False
                final_status = "cooldown"
                failure_reason = "6分钟冷却期"
                screenshot_path = screenshot(page,
                    f"godlike-{godlike_id}-cooldown.png")
                break

            else:
                log("未检测到成功/冷却期标志，重试", "WARN")
                failure_reason = "提交后未检测到结果"
                screenshot_path = screenshot(page,
                    f"godlike-{godlike_id}-unknown-{attempt}.png")
                page.quit()
                page = None
                restart_warp()
                time.sleep(5)
                continue

        except Exception as e:
            log(f"续期异常: {e}", "ERROR")
            failure_reason = f"运行异常: {str(e)[:200]}"
            if page:
                screenshot_path = screenshot(page,
                    f"godlike-{godlike_id}-error-{attempt}.png")
            if attempt < MAX_RETRIES_PER_ID:
                if page:
                    try:
                        page.quit()
                    except Exception:
                        pass
                    page = None
                restart_warp()
                time.sleep(5)
                continue
            break

        finally:
            if page:
                if not screenshot_path:
                    screenshot_path = screenshot(page,
                        f"godlike-{godlike_id}-final-{attempt}.png")
                try:
                    page.quit()
                except Exception:
                    pass

    # ── 6. 发送 Telegram 通知 ──
    if final_status is None:
        if success:
            final_status = "success"
        elif "冷却期" in (failure_reason or ""):
            final_status = "cooldown"
        elif "24小时" in (failure_reason or ""):
            final_status = "maxed"
        else:
            final_status = "failure"

    caption = build_caption(status=final_status,
                            godlike_id=godlike_id,
                            reason=failure_reason)
    send_tg_photo(tg_token, tg_chat_id, screenshot_path, caption)
    return success


# ============================================================
# 主入口
# ============================================================
def parse_ids(raw: str) -> list:
    return [line.strip() for line in raw.replace(",", "\n").splitlines()
            if line.strip()]


def main():
    raw_ids_secret = os.getenv("GODLIKE_ID", "").strip()
    raw_ids_input = os.getenv("GODLIKE_ID_INPUT", "").strip()
    tg_token = os.getenv("TG_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()

    if not raw_ids_secret:
        log("GODLIKE_ID 未配置，退出", "ERROR")
        sys.exit(1)

    all_ids = parse_ids(raw_ids_secret)

    # 处理手动输入的指定 ID
    if raw_ids_input:
        input_ids = parse_ids(raw_ids_input)
        invalid = [i for i in input_ids if i not in all_ids]
        if invalid:
            log(f"以下 ID 不在 GODLIKE_ID 中，已忽略: {invalid}", "WARN")
        godlike_ids = [i for i in input_ids if i in all_ids]
        if not godlike_ids:
            log("指定的 ID 全部无效，退出", "ERROR")
            sys.exit(1)
        log(f"手动指定模式，共 {len(godlike_ids)} 个账号: {godlike_ids}")
    else:
        godlike_ids = all_ids
        log(f"自动模式，共 {len(godlike_ids)} 个账号: {godlike_ids}")

    # 启动虚拟显示
    vdisplay = Xvfb(width=1280, height=720, colordepth=24)
    vdisplay.start()

    total_success = 0
    try:
        for idx, gid in enumerate(godlike_ids, 1):
            log(f"\n{'#' * 60}")
            log(f"账号 {idx}/{len(godlike_ids)}: {gid}")
            log(f"{'#' * 60}")

            ok = renew_single_id(gid, tg_token, tg_chat_id)
            if ok:
                total_success += 1

            if idx < len(godlike_ids):
                wait = random.randint(5, 15)
                log(f"等待 {wait} 秒后处理下一个账号...")
                time.sleep(wait)
    finally:
        vdisplay.stop()

    log(f"\n全部完成：成功 {total_success}/{len(godlike_ids)}")
    if total_success < len(godlike_ids):
        sys.exit(1)


if __name__ == "__main__":
    main()
