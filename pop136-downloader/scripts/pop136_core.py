from __future__ import annotations

import concurrent.futures
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright


START_URL = "https://yuntu.pop136.com/patternlibrary/"
APP_VERSION = "2.1.1"
LOGIN_DEBUG_PORT = 9223
EXCLUDED_SUFFIXES = {".psd", ".eps"}
EXPECTED_CARDS_PER_PAGE = 60
FILE_DOWNLOAD_TIMEOUT_SECONDS = 2 * 60

# 扫描优化：只有体积小于此值的文件才开档嗅探 HTML 验证页标记。
# 依据：全库 112,175 个文件中 HTML 标记文件为 0；32/64/128/256/512 KB
# 五个阈值下「新旧实现逐名差集」均为 0（实测见 扫描优化/html-groundtruth.json）。
HTML_SNIFF_MAX_BYTES = 32 * 1024


class HtmlChallenge(RuntimeError):
    pass


class DownloadTimeout(RuntimeError):
    pass


class DetailUnavailable(RuntimeError):
    pass


def safe_filename(value: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", value.strip()).rstrip(". ")
    return name or "unnamed_file"


def unified_names(item_id: str, files: list[dict]) -> list[str]:
    """命名统一：``{图案ID}_{序号}{扩展名}``。

    序号取原文件名的前导数字；原名没有数字的，接在最大序号之后。
    同一图案的位图与矢量因此同号同名（如 1071450_2.jpg / 1071450_2.eps），
    资源管理器按名称排序时同图案的文件自然聚拢。
    """
    stems = [safe_filename(file["name"]) for file in files]
    slots: list[int | None] = []
    for stem in stems:
        digits = ""
        for char in stem.rsplit(".", 1)[0]:
            if char.isdigit():
                digits += char
            else:
                break
        slots.append(int(digits) if digits else None)
    highest = max((slot for slot in slots if slot is not None), default=0)
    next_slot = highest + 1
    used: set[str] = set()
    names: list[str] = []
    for stem, slot in zip(stems, slots):
        suffix = Path(stem).suffix
        if slot is None:
            slot, next_slot = next_slot, next_slot + 1
        candidate = f"{item_id}_{slot}{suffix}"
        index = 2
        while candidate.casefold() in used:
            candidate = f"{item_id}_{slot}-{index}{suffix}"
            index += 1
        used.add(candidate.casefold())
        names.append(candidate)
    return names


def existing_names(folder: Path) -> set[str]:
    """目录里「已下载且合法」的文件名集合（小写）。

    用一次 ``os.scandir`` 建立索引（``DirEntry`` 的存在性/体积来自目录枚举结果，
    几乎零额外 I/O），只对小于 ``HTML_SNIFF_MAX_BYTES`` 的少数文件开档确认
    不是 HTML 验证页 —— 取代「对 11 万个文件逐个开档读 128 字节」。

    等价性依据（实测全库 112,175 个文件，见 ``扫描优化/html-groundtruth.json``）：
    HTML 标记文件 0 个；32/64/128/256/512 KB 五个阈值的逐名差集均为 0。
    """
    ignored = (".part", ".seg", ".assemble")
    names: set[str] = set()
    try:
        entries = os.scandir(folder)
    except OSError:
        return names
    with entries:
        for entry in entries:
            key = entry.name.casefold()
            if key.endswith(ignored):
                continue
            try:
                if not entry.is_file():
                    continue
                size = entry.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue
            if size < HTML_SNIFF_MAX_BYTES and is_html_challenge(Path(entry.path)):
                continue
            names.add(key)
    return names


def is_valid_download(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with path.open("rb") as source:
            prefix = source.read(128).lstrip().lower()
    except OSError:
        return False
    return not (
        prefix.startswith(b"<script")
        or prefix.startswith(b"<html")
        or prefix.startswith(b"<!doctype html")
    )


def is_html_challenge(path: Path) -> bool:
    """Return whether a downloaded temporary file is an HTML verification page."""
    try:
        with path.open("rb") as source:
            prefix = source.read(128).lstrip().lower()
    except OSError:
        return False
    return (
        prefix.startswith(b"<script")
        or prefix.startswith(b"<html")
        or prefix.startswith(b"<!doctype html")
    )


def is_verification_error(error: str | None) -> bool:
    return "服务器返回网页验证" in (error or "")


def should_download(name: str) -> bool:
    return Path(name).suffix.casefold() not in EXCLUDED_SUFFIXES


def record_unified_names(item_id: str | None, record: dict) -> list[str]:
    """``record['files']`` 对应的统一新名（与 files 同序）。

    仅用于解析「旧状态文件」——那里 ``files[i]['name']`` 存的是原名。
    无法计算时返回空表，调用方按「无候选」处理。
    """
    files = record.get("files") or []
    if not item_id or not files:
        return []
    try:
        return unified_names(str(item_id), files)
    except Exception:
        return []


def candidate_names(item_id: str | None, record: dict, index: int) -> list[str]:
    """某条文件记录的「可接受磁盘名」候选：现名 + 原名 + （旧状态）统一新名。

    改名迁移期间，同一份文件可能以旧名或统一新名任一形态存在；
    只要任一形态是磁盘上的合法下载，就视为「已有」，绝不重复下载。
    """
    files = record.get("files") or []
    if index >= len(files):
        return []
    entry = files[index]
    names: list[str] = []
    for value in (entry.get("name"), entry.get("original")):
        if value:
            names.append(str(value))
    if not entry.get("original"):
        # 旧状态文件：name 存的是原名，需要额外补上统一新名
        unified = record_unified_names(item_id, record)
        if index < len(unified):
            names.append(str(unified[index]))
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(name)
    return unique


def file_present(
    target: Path,
    record: dict,
    index: int,
    item_id: str | None = None,
    known: set[str] | None = None,
) -> bool:
    """文件是否已在磁盘上（旧名 / 统一新名 / 记录原名，任一合法存在即算）。

    ``known`` 可传入 ``existing_names(target)`` 的预扫描结果；批量校验时用它
    可把 N 次文件打开降为 1 次目录扫描。
    """
    for name in candidate_names(item_id, record, index):
        key = name.casefold()
        if known is not None:
            if key in known:
                return True
        elif is_valid_download(target / name):
            return True
    return False


def cards_for_download(cards: list[dict]) -> list[dict]:
    """Download every card in the site's newest-to-oldest page order."""
    return cards


def page_batch(pending: list[dict]) -> list[dict]:
    return pending


def download_worker_count(job_count: int) -> int:
    return max(1, job_count)


def stable_concurrency_cap(logical_processors: int | None = None) -> int:
    # 2026-09-23 v1.7.7：上限 32 → 8。
    # 实测正反馈死循环：全成功→并发+2→爬到 18+ 触发 CDN "Download is starting" 失败
    # → 引擎卡死 → 守护软重启 → 周期性中断下载。Chrome 批次稳定上界本就是 8
    # （HTTP 并发仍可到 8）；Chrome 浏览器池单独限制为 6，并在失败时自动降回 4。
    processors = logical_processors if logical_processors is not None else (os.cpu_count() or 4)
    return max(4, min(8, int(processors)))


def next_concurrency_limit(current: int, completed: int, failures: int, cap: int) -> int:
    if failures:
        return max(4, current // 2)
    if completed >= current:
        return min(cap, current + 2)
    return current


def browser_batch_size(concurrency_limit: int) -> int:
    """Chrome navigations are heavier than HTTP workers; six balances speed and memory."""
    return max(1, min(6, concurrency_limit))


def close_extra_browser_pages(context, keep_page) -> int:
    """Keep the signed-in work page and remove windows left by interrupted runs."""
    closed = 0
    for candidate in list(context.pages):
        if candidate is keep_page:
            continue
        try:
            candidate.close()
            closed += 1
        except Exception:
            pass
    return closed


def create_browser_pool_tabs(context, anchor_page, count: int = 4) -> list:
    """Create download workers as tabs in the existing Chrome window, not new windows."""
    if count <= 0:
        return []
    session = context.new_cdp_session(anchor_page)
    created = []
    try:
        for index in range(count):
            marker = f"about:blank#pop136-pool-{os.getpid()}-{time.time_ns()}-{index}"
            session.send("Target.createTarget", {"url": marker, "newWindow": False})
            deadline = time.monotonic() + 5
            tab = None
            while time.monotonic() < deadline:
                tab = next((page for page in context.pages if page.url == marker), None)
                if tab is not None:
                    break
                anchor_page.wait_for_timeout(50)
            if tab is None:
                raise RuntimeError("无法在当前 Chrome 窗口创建下载标签页")
            created.append(tab)
        return created
    except Exception:
        for tab in created:
            try:
                tab.close()
            except Exception:
                pass
        raise
    finally:
        try:
            session.detach()
        except Exception:
            pass


def browser_launch_args() -> list[str]:
    # 2026-09-23 v1.7.6：去掉 --start-minimized。
    # 窗口一旦最小化，Chrome 会冻结该窗口的渲染与 JS → CDN 的 JS 机器人挑战无法通过
    # → App 的浏览器链路拿到 text/html 判「需要人工验证」→ 引擎重启 → 每次泄漏 8 个标签
    # → 内存涨到 20GB 后浏览器自杀 → session 级登录 cookie 丢失（当日实测的正反馈死循环起点）。
    return [
        "--disable-blink-features=AutomationControlled",
        "--window-position=60,60",
        "--window-size=1200,900",
    ]


def login_browser_launch_args(profile: Path) -> list[str]:
    """Open the manual verification window on-screen, overriding saved Chrome bounds."""
    return [
        f"--user-data-dir={profile}",
        "--new-window",
        f"--remote-debugging-port={LOGIN_DEBUG_PORT}",
        "--remote-allow-origins=*",
        "--window-position=60,60",
        "--window-size=1200,900",
        START_URL,
    ]


def cdp_ready(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{LOGIN_DEBUG_PORT}/json/version",
            timeout=timeout,
        ) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def wait_for_cdp(timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cdp_ready():
            return True
        time.sleep(0.25)
    return False


def download_browser_headless() -> bool:
    return False


def remember_browser_routes(state: dict, jobs: list[dict]) -> None:
    suffixes = {str(value).casefold() for value in state.get("browserOnlySuffixes", [])}
    suffixes.update(Path(job["name"]).suffix.casefold() for job in jobs if Path(job["name"]).suffix)
    state["browserOnlySuffixes"] = sorted(suffixes)


def use_browser_route(state: dict, name: str) -> bool:
    if Path(name).suffix.casefold() == ".ai":
        return False
    suffixes = {str(value).casefold() for value in state.get("browserOnlySuffixes", [])}
    return Path(name).suffix.casefold() in suffixes


def stream_page_jobs(cards, collect_files, submit_job) -> None:
    for card in cards:
        for job in collect_files(card):
            submit_job(job)


def should_checkpoint(completed_items: int) -> bool:
    return completed_items > 0 and completed_items % 20 == 0


def retry_delay_seconds(attempt: int) -> int:
    return min(60, max(5, attempt * 5))


def should_auto_resume(target: Path, profile: Path) -> bool:
    return (
        (target / "_download_state.json").is_file()
        and not (target / "_download_complete.flag").exists()
        and profile.is_dir()
    )


def auto_resume_ready(
    has_state: bool,
    manual_pause: bool,
    worker_alive: bool,
    runner_active: bool,
) -> bool:
    return has_state and not manual_pause and not worker_alive and not runner_active


def record_files_complete(
    target: Path,
    record: dict,
    item_id: str | None = None,
    known: set[str] | None = None,
) -> bool:
    files = record.get("files", [])
    selected = [index for index, file in enumerate(files) if should_download(file.get("name", ""))]
    if selected:
        return all(file_present(target, record, index, item_id, known=known) for index in selected)
    return bool(record.get("selection_checked") or (files and record.get("completed")))


def record_finished_for_run(
    target: Path,
    record: dict,
    item_id: str | None = None,
    known: set[str] | None = None,
) -> bool:
    files = record.get("files", [])
    selected = [index for index, file in enumerate(files) if should_download(file.get("name", ""))]
    if selected:
        return all(
            file_present(target, record, index, item_id, known=known)
            or files[index].get("status") == "skipped_timeout"
            for index in selected
        )
    return bool(
        record.get("status") == "detail_unavailable"
        or record.get("selection_checked")
        or (files and record.get("completed"))
    )


def pending_state_jobs(target: Path, state: dict) -> list[dict]:
    # 一次目录扫描换掉逐文件 exists/打开，改名迁移期间也只在「旧名与新名都不在」时才报待下。
    known = existing_names(target)
    jobs = []
    for item_id, record in state.get("processed", {}).items():
        for index, file in enumerate(record.get("files", [])):
            if not should_download(file.get("name", "")) or not file.get("url"):
                continue
            if file.get("status") == "skipped_timeout":
                continue
            if file_present(target, record, index, item_id, known=known):
                continue
            jobs.append({"id": item_id, "name": file["name"], "url": file["url"]})
    return jobs


def reactivate_timed_out_files(path: Path) -> int:
    if not path.is_file():
        return 0
    state = load_state(path)
    changed = 0
    for record in state.get("processed", {}).values():
        if record.get("status") == "detail_unavailable":
            record["status"] = "retry_detail"
            record.pop("error", None)
            changed += 1
        for file in record.get("files", []):
            if file.get("status") == "skipped_timeout":
                file["status"] = "retry_timeout"
                file.pop("error", None)
                changed += 1
    if changed:
        save_state(path, state)
    return changed


def next_page(state: dict) -> int:
    value = state.get("lastCompletedPage", state.get("page", 0))
    try:
        return max(1, int(value) + 1)
    except (TypeError, ValueError):
        return 1


def profile_lock_message(profile: Path) -> str | None:
    """Return a clear message when Chromium still owns the persistent profile."""
    if (profile / "lockfile").exists():
        return (
            f"登录状态目录仍被 Chrome 占用：{profile}\n"
            "请关闭使用该目录的浏览器窗口后，再点击“开始 / 续传”。"
        )
    return None


def load_state(path: Path, year: str = "all") -> dict:
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            state = {}
    else:
        state = {}
    state.setdefault("processed", {})
    state.setdefault("pageStats", {})
    cap = stable_concurrency_cap()
    state["concurrencyLimit"] = max(4, min(cap, int(state.get("concurrencyLimit", min(12, cap)))))
    state["year"] = "all"
    state["scope"] = "all_years"
    return state


def save_state(path: Path, state: dict) -> None:
    state["updatedAt"] = datetime.now().astimezone().isoformat()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _remote_size(session: requests.Session, url: str) -> int:
    response = session.head(url, allow_redirects=True, timeout=(20, 30))
    response.raise_for_status()
    if "text/html" in response.headers.get("Content-Type", "").casefold():
        raise HtmlChallenge("服务器返回了网页验证，而不是下载文件")
    value = response.headers.get("Content-Length", "")
    if not value.isdigit():
        raise RuntimeError("服务器未返回文件大小，无法校验")
    return int(value)


def candidate_urls(url: str) -> list[str]:
    raw = urlsplit(url)
    parsed = raw._replace(
        path=quote(raw.path, safe="/%:@"),
        query=quote(raw.query, safe="=&%/:?+,;@"),
        fragment=quote(raw.fragment, safe=""),
    )
    if re.fullmatch(r"imgyt\d+\.pop-fashion\.com", parsed.hostname or ""):
        hosts = [parsed.hostname] + [f"imgyt{index}.pop-fashion.com" for index in (1, 2, 3)]
        return [urlunsplit(parsed._replace(netloc=host)) for host in dict.fromkeys(hosts)]
    return [urlunsplit(parsed)]


def wait_for_full_card_page(page, cards_locator, page_no: int, scroll_rounds: int = 12) -> int:
    try:
        cards_locator.first.wait_for(state="visible", timeout=30_000)
    except PlaywrightTimeout as error:
        raise RuntimeError("未看到图案列表。请在打开的浏览器完成登录后重新点击开始。") from error
    count = cards_locator.count()
    for _ in range(scroll_rounds):
        if count >= EXPECTED_CARDS_PER_PAGE:
            return count
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(500)
        count = cards_locator.count()
    if count < EXPECTED_CARDS_PER_PAGE:
        raise RuntimeError(
            f"第 {page_no} 页只加载到 {count}/{EXPECTED_CARDS_PER_PAGE} 个图案；"
            "已停止翻页以避免漏下载，请稍后重试。"
        )
    return count


def _download_with_curl(
    url: str,
    destination: Path,
    stop_event: threading.Event,
    cookie_header: str = "",
    file_progress: Callable[[int, int | None], None] | None = None,
    deadline: float | None = None,
) -> dict:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("服务器要求网页验证，且系统未找到 curl.exe")
    partial = destination.with_name(destination.name + ".part")
    last_error = "curl 下载失败"
    for selected_url in candidate_urls(url):
        if stop_event.is_set():
            return {"status": "paused", "bytes": partial.stat().st_size if partial.exists() else 0}
        command = [
                curl, "--fail", "--location", "--silent", "--show-error",
                "--connect-timeout", "20", "--speed-time", "30", "--speed-limit", "1",
                "--retry", "5", "--retry-delay", "1", "--continue-at", "-",
        ]
        if cookie_header:
            command.extend(["--cookie", cookie_header])
        command.extend(["--output", str(partial), selected_url])
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        while process.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                process.terminate()
                process.communicate()
                raise DownloadTimeout("下载超过 2 分钟，已跳过")
            if file_progress and partial.exists():
                file_progress(partial.stat().st_size, None)
            if stop_event.wait(0.2):
                process.terminate()
                process.communicate()
                return {"status": "paused", "bytes": partial.stat().st_size if partial.exists() else 0}
        _, error_text = process.communicate()
        if process.returncode == 0 and is_valid_download(partial):
            os.replace(partial, destination)
            return {"status": "downloaded", "bytes": destination.stat().st_size}
        if process.returncode == 0 and is_html_challenge(partial):
            partial.unlink(missing_ok=True)
            raise HtmlChallenge("服务器返回网页验证，需在浏览器手动完成验证")
        last_error = error_text.strip() or f"curl 返回代码 {process.returncode}"
        if partial.exists() and not is_valid_download(partial):
            partial.unlink()
    raise RuntimeError(last_error)


def download_file(
    url: str,
    destination: Path,
    stop_event: threading.Event,
    cookie_header: str = "",
    file_progress: Callable[[int, int | None], None] | None = None,
) -> dict:
    deadline = time.monotonic() + FILE_DOWNLOAD_TIMEOUT_SECONDS
    last_progress_bytes = -1
    last_progress_time = 0.0

    def report_progress(done: int, total: int | None, force: bool = False) -> None:
        nonlocal last_progress_bytes, last_progress_time
        if not file_progress:
            return
        now = time.monotonic()
        if force or done - last_progress_bytes >= 512 * 1024 or now - last_progress_time >= 0.5:
            file_progress(done, total)
            last_progress_bytes = done
            last_progress_time = now

    if destination.exists():
        if is_valid_download(destination):
            return {"status": "skipped_exists", "bytes": destination.stat().st_size}
        destination.unlink()
    partial = destination.with_name(destination.name + ".part")
    if partial.exists() and not is_valid_download(partial):
        partial.unlink()
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 POP136LocalDownloader/1.0",
        "Referer": "https://yuntu.pop136.com/",
        "Origin": "https://yuntu.pop136.com",
        "Connection": "close",
    })
    if cookie_header:
        session.headers["Cookie"] = cookie_header
    try:
        total = _remote_size(session, url)
    except HtmlChallenge:
        return _download_with_curl(url, destination, stop_event, cookie_header, file_progress, deadline)
    choices = candidate_urls(url)
    selected_index = 0
    for attempt in range(1, 101):
        if time.monotonic() >= deadline:
            raise DownloadTimeout("下载超过 2 分钟，已跳过")
        if stop_event.is_set():
            return {"status": "paused", "bytes": partial.stat().st_size if partial.exists() else 0}
        current = partial.stat().st_size if partial.exists() else 0
        report_progress(current, total, force=True)
        if current == total:
            os.replace(partial, destination)
            return {"status": "downloaded", "bytes": total}
        if current > total:
            raise RuntimeError(f"断点尺寸异常：{current}>{total}")
        headers = {"Range": f"bytes={current}-{total - 1}"} if current else {}
        try:
            selected_url = choices[selected_index]
            with session.get(selected_url, headers=headers, allow_redirects=True, stream=True, timeout=(15, 5)) as response:
                response.raise_for_status()
                if "text/html" in response.headers.get("Content-Type", "").casefold():
                    raise HtmlChallenge("服务器返回网页验证，需在浏览器手动完成验证")
                if current and response.status_code != 206:
                    raise RuntimeError(f"服务器拒绝断点续传，HTTP {response.status_code}")
                with partial.open("ab" if current else "wb") as output:
                    # CDN 可能只返回几十 KiB 就停顿，8 KiB 写入可确保断点不丢。
                    for chunk in response.iter_content(chunk_size=8 * 1024):
                        if stop_event.is_set():
                            return {"status": "paused", "bytes": output.tell()}
                        if time.monotonic() >= deadline:
                            raise DownloadTimeout("下载超过 2 分钟，已跳过")
                        if chunk:
                            remaining = total - output.tell()
                            output.write(chunk[:remaining])
                            report_progress(output.tell(), total)
                            if output.tell() == total:
                                report_progress(total, total, force=True)
                                break
        except HtmlChallenge:
            # The CDN returned a verification page. Switch to the signed-in browser now,
            # rather than spending up to 100 HTTP retries on the same blocked route.
            raise
        except (requests.RequestException, OSError, RuntimeError):
            progressed = partial.stat().st_size if partial.exists() else 0
            if progressed == current:
                selected_index = (selected_index + 1) % len(choices)
            if attempt == 100:
                raise
            time.sleep(min(3, attempt))
    raise RuntimeError("下载重试次数已用尽")


class Pop136Engine:
    def __init__(
        self,
        target: Path,
        profile: Path,
        year: str,
        status: Callable[[str], None],
        stop_event: threading.Event,
        progress: Callable[[int, int, str], None] | None = None,
        file_progress: Callable[[str, int, int | None], None] | None = None,
    ) -> None:
        self.target = target
        self.profile = profile
        self.year = year
        self.status = status
        self.stop_event = stop_event
        self.progress = progress or (lambda _done, _total, _label: None)
        self.file_progress = file_progress or (lambda _name, _done, _total: None)
        self.state_path = target / "_download_state.json"
        self.log_path = target / "_download_log.txt"
        self._verification_page = None
        self._verification_url = ""
        self._browser_tabs = []

    def log(self, message: str) -> None:
        line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
        with self.log_path.open("a", encoding="utf-8") as output:
            output.write(line + "\n")
        self.status(message)

    @staticmethod
    def browser_path() -> Path:
        candidates = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise RuntimeError("未找到 Chrome 或 Edge")

    def run(self) -> None:
        self.target.mkdir(parents=True, exist_ok=True)
        self.profile.mkdir(parents=True, exist_ok=True)
        state = load_state(self.state_path, self.year)
        self.log(f"启动 v{APP_VERSION}，已扫描 {len(existing_names(self.target))} 个已有文件")
        if not cdp_ready():
            subprocess.Popen(
                [str(self.browser_path()), *login_browser_launch_args(self.profile)],
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            self.log("登录浏览器未运行，已自动打开")
        if not wait_for_cdp():
            raise RuntimeError("登录浏览器启动失败；请点击“1. 打开网页登录”后重试")
        with sync_playwright() as playwright:
            try:
                attached_browser = playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{LOGIN_DEBUG_PORT}"
                )
                context = attached_browser.contexts[0]
                self.log("已接管完成登录/验证码的浏览器")
            except Exception as error:
                self.log(f"浏览器启动失败：{error}")
                raise RuntimeError(
                    "浏览器启动或登录会话接管失败。请重新点击“打开网页登录”。\n"
                    f"原始错误：{error}"
                ) from error
            try:
                page = next(
                    (candidate for candidate in context.pages if "yuntu.pop136.com" in candidate.url),
                    context.pages[0] if context.pages else context.new_page(),
                )
                if page.is_closed():
                    raise RuntimeError("浏览器页面已关闭，请确认登录状态目录未被其他浏览器占用。")
                closed_pages = close_extra_browser_pages(context, page)
                if closed_pages:
                    self.log(f"已清理 {closed_pages} 个历史浏览器页面")
                self._browser_tabs = create_browser_pool_tabs(
                    context,
                    page,
                    browser_batch_size(stable_concurrency_cap()),
                )
                # 2026-09-23 v1.7.6：删除此处「把窗口最小化」的 CDP 调用。
                # 原代码：Browser.getWindowForTarget + setWindowBounds{windowState:minimized}
                # 它是「下载全失败」的直接开关（见 browser_launch_args 注释）。
                # 保留浏览器窗口可见即保留 CDN 挑战的 JS 执行环境。
                self._recover_pending(context, state)
                if self.stop_event.is_set():
                    self.log("任务已暂停")
                    return
                page_no = next_page(state)
                while not self.stop_event.is_set():
                    stop = self._process_page(page, page_no, state)
                    if stop:
                        break
                    page_no += 1
            finally:
                save_state(self.state_path, state)
                # 2026-09-23 v1.7.6：回收本轮自建的 8 个池标签。
                # 原代码从不关闭它们 → 每次引擎重启泄漏 8 个，实测堆到 321 个标签 /
                # Chrome 306 进程 / 20.44GB，最终压垮浏览器并丢掉登录态。
                # 现在每轮引擎结束（含异常退出）都会还回去，标签数恒定。
                for tab in self._browser_tabs:
                    try:
                        if not tab.is_closed():
                            tab.close()
                    except Exception:
                        pass
                self._browser_tabs = []
                # connect_over_cdp 的连接自然断开，登录 Chrome 保持打开。
        self.log("任务已暂停" if self.stop_event.is_set() else "目标年份下载完成")

    @staticmethod
    def _browser_cookie_header(context, url: str) -> str:
        """Use only the signed-in browser's cookies for this URL after manual verification."""
        try:
            cookies = context.cookies([url])
        except Exception:
            return ""
        return "; ".join(
            f"{cookie['name']}={cookie['value']}" for cookie in cookies
            if cookie.get("name") and cookie.get("value")
        )

    def _wait_for_manual_verification(self, context, url: str) -> None:
        """Keep the normal signed-in browser open; the user, not the app, completes verification."""
        raise RuntimeError("登录或验证状态已失效；请点击“打开网页登录”完成后再续传")

    def _recover_pending(self, context, state: dict) -> None:
        while not self.stop_event.is_set():
            recovery_jobs = pending_state_jobs(self.target, state)
            if not recovery_jobs:
                return
            self.log(f"先恢复 {len(recovery_jobs)} 个历史断点")
            cookie_header = self._browser_cookie_header(context, recovery_jobs[0]["url"])
            self._download_jobs(recovery_jobs, state, cookie_header)
            known = existing_names(self.target)
            for item_id, record in state["processed"].items():
                record["completed"] = record_files_complete(self.target, record, item_id, known=known)
            save_state(self.state_path, state)
            remaining = [
                job for job in recovery_jobs
                if not is_valid_download(self.target / job["name"])
                and self._job_status(state, job) != "skipped_timeout"
            ]
            if not remaining:
                if self._verification_page and not self._verification_page.is_closed():
                    self._verification_page.close()
                self._verification_page = None
                self._verification_url = ""
                return
            verification_job = next(
                (job for job in remaining if is_verification_error(self._job_error(state, job))),
                None,
            )
            if verification_job:
                remember_browser_routes(state, remaining)
                self.log("CDN 拦截了非浏览器下载，切换到已登录 Chrome 下载链路")
                self._download_browser_jobs(context, remaining, state)
                known = existing_names(self.target)
                for item_id, record in state["processed"].items():
                    record["completed"] = record_files_complete(self.target, record, item_id, known=known)
                save_state(self.state_path, state)
                remaining = [
                    job for job in recovery_jobs
                    if not is_valid_download(self.target / job["name"])
                    and self._job_status(state, job) != "skipped_timeout"
                ]
                if not remaining:
                    continue
                browser_verification_job = next(
                    (job for job in remaining if is_verification_error(self._job_error(state, job))),
                    None,
                )
                if browser_verification_job:
                    self._wait_for_manual_verification(context, browser_verification_job["url"])
                    continue
            raise RuntimeError("历史断点仍有失败项目；已保留断点，请稍后重试或生成诊断包")

    @staticmethod
    def _job_error(state: dict, job: dict) -> str:
        record = state.get("processed", {}).get(job["id"], {})
        for file in record.get("files", []):
            if file.get("name") == job["name"]:
                return str(file.get("error", ""))
        return ""

    @staticmethod
    def _job_status(state: dict, job: dict) -> str:
        record = state.get("processed", {}).get(job["id"], {})
        for file in record.get("files", []):
            if file.get("name") == job["name"]:
                return str(file.get("status", ""))
        return ""

    def _process_page(self, page, page_no: int, state: dict) -> bool:
        page_started_at = time.monotonic()
        if page.is_closed():
            raise RuntimeError("浏览器页面已关闭，请确认登录状态目录未被其他浏览器占用。")
        url = f"https://yuntu.pop136.com/patternlibrary/page_{page_no}/#anchor"
        self.log(f"进入第 {page_no} 页")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        cards_locator = page.locator('li[data-t="graphicitem"][data-index]')
        try:
            wait_for_full_card_page(page, cards_locator, page_no)
        except RuntimeError:
            def empty_loaded():
                return (
                    page_no == next_page(state)
                    and cards_locator.count() == 0
                    and re.search(r"共\s*0\s*个", page.locator("body").inner_text())
                    and page.evaluate(
                        "() => performance.getEntriesByType('resource').some(entry => "
                        "entry.name.includes('/patternlibrary/getList/') && entry.responseStatus === 200)"
                    )
                )

            if not empty_loaded():
                raise
            page.reload(wait_until="domcontentloaded", timeout=30_000)
            try:
                wait_for_full_card_page(page, cards_locator, page_no)
            except RuntimeError:
                if not empty_loaded():
                    raise
                (self.target / "_download_complete.flag").write_text(str(page_no), encoding="utf-8")
                self.log(f"第 {page_no} 页连续两次返回 0 个图案，已到最后一页")
                return True
        cards = cards_locator.evaluate_all(
            """items => items.map(item => ({
                id: item.dataset.id,
                index: Number(item.dataset.index),
                date: (item.querySelector('.dis-posion')?.innerText || '').trim()
            })).sort((a,b) => a.index-b.index)"""
        )
        selected_cards = cards_for_download(cards)

        while not self.stop_event.is_set():
            pending = [
                card for card in selected_cards
                if not record_finished_for_run(
                    self.target,
                    state["processed"].get(card["id"], {}),
                    card["id"],
                )
            ]
            if not pending:
                break
            batch = page_batch(pending)
            self._stream_page_downloads(page, page_no, batch, state)
            for card in batch:
                record = state["processed"].get(card["id"], {})
                files = record.get("files", [])
                record["completed"] = record_files_complete(self.target, record, card["id"])
                if record["completed"]:
                    record["completedAt"] = datetime.now().astimezone().isoformat()
            save_state(self.state_path, state)
            if any(
                not record_finished_for_run(
                    self.target,
                    state["processed"].get(card["id"], {}),
                    card["id"],
                )
                for card in batch
            ):
                batch_ids = {card["id"] for card in batch}
                verification_jobs = [
                    job for job in pending_state_jobs(self.target, state)
                    if job["id"] in batch_ids
                    and is_verification_error(self._job_error(state, job))
                ]
                if verification_jobs:
                    remember_browser_routes(state, verification_jobs)
                    self.log("CDN 拦截了非浏览器下载，切换到已登录 Chrome 下载链路")
                    self._download_browser_jobs(page.context, verification_jobs, state)
                    continue
                raise RuntimeError("本批次仍有失败项目；已保留断点，请稍后重试或生成诊断包")

        completed = 0
        handled = 0
        for card in selected_cards:
            record = state["processed"].get(card["id"], {})
            record["completed"] = record_files_complete(self.target, record, card["id"])
            completed += bool(record["completed"])
            handled += bool(record_finished_for_run(self.target, record, card["id"]))
        state["page"] = page_no
        elapsed_seconds = round(time.monotonic() - page_started_at, 1)
        state["pageStats"][str(page_no)] = {
            "items": len(cards), "yearItems": len(selected_cards), "completed": completed,
            "deferred": handled - completed,
            "remaining": len(selected_cards) - handled,
            "elapsedSeconds": elapsed_seconds,
        }
        if handled == len(selected_cards):
            state["lastCompletedPage"] = page_no
        save_state(self.state_path, state)
        self.log(
            f"第 {page_no} 页处理 {handled}/{len(selected_cards)}；"
            f"已完成 {completed}，超时保留 {handled - completed}；"
            f"耗时 {elapsed_seconds:.1f} 秒"
        )
        self.progress(handled, len(selected_cards), f"第 {page_no} 页完成")
        return False

    def _stream_page_downloads(self, page, page_no: int, cards: list[dict], state: dict) -> None:
        known = existing_names(self.target)
        scheduled: set[str] = set()
        results: queue.Queue = queue.Queue()
        workers: list[threading.Thread] = []
        browser_jobs: list[dict] = []
        concurrency_limit = int(state["concurrencyLimit"])
        limiter = threading.Semaphore(concurrency_limit)
        collected = 0
        finished = 0
        successful = 0
        failed = 0
        last_saved_finished = 0

        def download(job: dict) -> None:
            try:
                with limiter:
                    result = download_file(
                        job["url"], self.target / job["name"], self.stop_event,
                        job.get("cookie_header", ""),
                        lambda done, total: self.file_progress(job["name"], done, total),
                    )
                results.put((job, result, None))
            except Exception as error:
                results.put((job, None, error))

        def drain_results() -> int:
            nonlocal finished, successful, failed
            changed = 0
            while True:
                try:
                    job, result, error = results.get_nowait()
                except queue.Empty:
                    break
                if error is None:
                    self._set_file_status(state, job, result["status"], result.get("bytes"))
                    self.log(f"{job['name']}：{result['status']}")
                    if result["status"] != "paused":
                        successful += 1
                else:
                    status = "verification_required" if isinstance(error, HtmlChallenge) else (
                        "skipped_timeout" if isinstance(error, DownloadTimeout) else "failed"
                    )
                    self._set_file_status(state, job, status, error=str(error))
                    message = "需要人工验证" if status == "verification_required" else (
                        "下载超时，已跳过" if status == "skipped_timeout" else "下载失败"
                    )
                    self.log(f"{job['name']} {message}：{error}")
                    failed += 1
                finished += 1
                changed += 1
                self.progress(finished, max(1, len(workers)), job["name"])
            return changed

        def collect(card: dict) -> list[dict]:
            nonlocal collected
            if self.stop_event.is_set():
                return []
            record = state["processed"].setdefault(card["id"], {"index": card["index"], "files": []})
            record["files"] = []
            record.pop("status", None)
            jobs = []
            try:
                raw_files = self._collect_files(page, card, page_no)
                # 序号在「全部收集到的文件」上统一计算，再按后缀筛选，
                # 保证与既有库存的改名规则完全一致。
                names = unified_names(card["id"], raw_files)
                files = [
                    (file, name)
                    for file, name in zip(raw_files, names)
                    if should_download(file["name"])
                ]
                record["selection_checked"] = True
                for file, name in files:
                    # original 保留站点原名：改名迁移期间旧名与新名任一存在都视为「已有」。
                    record["files"].append(
                        {"name": name, "original": file["name"], "url": file["url"], "status": "queued"}
                    )
                    jobs.append(
                        {"id": card["id"], "name": name, "original": file["name"], "url": file["url"]}
                    )
                if not files:
                    self.log(f"作品 {card['id']} 只提供 PSD/EPS，已跳过")
            except DetailUnavailable as error:
                record["completed"] = False
                record["status"] = "detail_unavailable"
                record["error"] = str(error)
                self.log(f"作品 {card['id']} 详情暂不可用，本轮跳过：{error}")
            except Exception as error:
                record["completed"] = False
                record["error"] = str(error)
                self.log(f"作品 {card['id']} 读取失败：{error}")
            collected += 1
            drain_results()
            if should_checkpoint(collected):
                save_state(self.state_path, state)
            self.status(
                f"第 {page_no} 页：已收集 {collected}/{len(cards)} 个作品，"
                f"同时下载 {len(workers)} 个文件"
            )
            return jobs

        def submit(job: dict) -> None:
            key = job["name"].casefold()
            original = str(job.get("original") or "").casefold()
            if key in known or (original and original in known):
                self._set_file_status(state, job, "skipped_exists")
                return
            if key in scheduled:
                self._set_file_status(state, job, "duplicate_name")
                return
            scheduled.add(key)
            job["cookie_header"] = self._browser_cookie_header(page.context, job["url"])
            if use_browser_route(state, job["name"]):
                browser_jobs.append(job)
                return
            worker = threading.Thread(target=download, args=(job,), daemon=True)
            workers.append(worker)
            worker.start()

        stream_page_jobs(cards, collect, submit)
        save_state(self.state_path, state)
        self.log(
            f"流水线已调度 {len(workers)} 个文件；收集与下载同步进行；"
            f"稳定并发上限 {concurrency_limit}"
        )
        while any(worker.is_alive() for worker in workers):
            for worker in workers:
                worker.join(timeout=0.05)
            if drain_results() and finished - last_saved_finished >= 20:
                save_state(self.state_path, state)
                last_saved_finished = finished
        drain_results()
        if workers:
            state["concurrencyLimit"] = next_concurrency_limit(
                concurrency_limit, successful, failed, stable_concurrency_cap()
            )
            if state["concurrencyLimit"] != concurrency_limit:
                self.log(f"自适应并发调整：{concurrency_limit} -> {state['concurrencyLimit']}")
        save_state(self.state_path, state)
        if browser_jobs and not self.stop_event.is_set():
            self.log("已启用浏览器直连模式，跳过会被 CDN 拦截的 HTTP/curl 路径")
            self._download_browser_jobs(page.context, browser_jobs, state)

    def _locate_card(self, page, card: dict, page_no: int):
        selector = f'li[data-t="graphicitem"][data-id="{card["id"]}"]'
        card_locator = page.locator(selector)
        if card_locator.count() == 0:
            page.goto(
                f"https://yuntu.pop136.com/patternlibrary/page_{page_no}/#anchor",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            cards_locator = page.locator('li[data-t="graphicitem"][data-index]')
            wait_for_full_card_page(page, cards_locator, page_no)
            card_locator = page.locator(selector)
        if card_locator.count() == 0:
            raise RuntimeError(f"第 {page_no} 页未找到作品 {card['id']}")
        return card_locator

    def _collect_files(self, page, card: dict, page_no: int) -> list[dict]:
        detail_overlay = page.locator(".js-detail-frame")
        if detail_overlay.count():
            detail_overlay.evaluate(
                """element => {
                    element.style.display = 'none';
                    const frame = element.querySelector('iframe');
                    if (frame) frame.src = 'about:blank';
                }"""
            )
        card_locator = self._locate_card(page, card, page_no)
        card_locator.scroll_into_view_if_needed()
        card_locator.locator("a").first.click()
        detail_frame = None
        for _ in range(80):
            detail_frame = next(
                (
                    frame for frame in page.frames
                    if "/patternlibrary/detail/" in frame.url
                    and re.search(rf"[?&]id={re.escape(str(card['id']))}(?:&|$)", frame.url)
                ),
                None,
            )
            if detail_frame is not None:
                break
            page.wait_for_timeout(200)
        if detail_frame is None:
            raise RuntimeError("详情窗口未打开")
        page.locator(".js-detail-frame").evaluate("element => element.style.display='block'")
        body_text = detail_frame.locator("body").inner_text(timeout=10_000)
        if "404 Page Not Found" in body_text:
            raise DetailUnavailable("网站详情页返回 404")
        detail_frame.locator(".js-detail-down").wait_for(state="visible", timeout=20_000)
        detail_frame.locator(".js-detail-down").click()
        detail_frame.locator(".js-downimg-right").wait_for(state="visible", timeout=10_000)
        raw_files = detail_frame.locator(".js-download-btn").evaluate_all(
            """buttons => buttons.map(button => ({
                name: (button.parentElement?.querySelector('.d-name')?.innerText || '').trim(),
                relativeUrl: button.getAttribute('data-bp'),
                previewUrl: button.closest('.down-list-box')?.querySelector('.download-img img')?.getAttribute('src') || null
            }))"""
        )
        big_image = detail_frame.locator(".bigbox").get_attribute("src")
        files = []
        for item in raw_files:
            if not item.get("name") or not item.get("relativeUrl"):
                continue
            base = item.get("previewUrl") or big_image
            if not base:
                continue
            origin = f"{urlparse(base).scheme}://{urlparse(base).netloc}/"
            files.append({"name": item["name"].replace("\n", "").strip(), "url": urljoin(origin, item["relativeUrl"])})
        return files

    def _download_jobs(self, jobs: list[dict], state: dict, cookie_header: str = "") -> None:
        if not jobs:
            return
        known = existing_names(self.target)
        unique: dict[str, dict] = {}
        for job in jobs:
            key = job["name"].casefold()
            if key in known:
                self._set_file_status(state, job, "skipped_exists")
            elif key not in unique:
                unique[key] = job
            else:
                self._set_file_status(state, job, "duplicate_name")
        concurrency_limit = int(state["concurrencyLimit"])
        self.log(f"恢复 {len(unique)} 个文件；稳定并发上限 {concurrency_limit}")
        total_jobs = len(unique)
        finished_jobs = 0
        successful = 0
        failed = 0
        self.progress(0, total_jobs, "准备下载")
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(len(unique), concurrency_limit))
        ) as executor:
            for job in unique.values():
                self.log(f"{job['name']}：下载中")
            future_jobs = {
                executor.submit(
                    download_file,
                    job["url"],
                    self.target / job["name"],
                    self.stop_event,
                    cookie_header,
                    lambda done, total, name=job["name"]: self.file_progress(name, done, total),
                ): job
                for job in unique.values()
            }
            for future in concurrent.futures.as_completed(future_jobs):
                job = future_jobs[future]
                try:
                    result = future.result()
                    self._set_file_status(state, job, result["status"], result.get("bytes"))
                    self.log(f"{job['name']}：{result['status']}")
                    if result["status"] != "paused":
                        successful += 1
                except Exception as error:
                    status = "verification_required" if isinstance(error, HtmlChallenge) else (
                        "skipped_timeout" if isinstance(error, DownloadTimeout) else "failed"
                    )
                    self._set_file_status(state, job, status, error=str(error))
                    message = "需要人工验证" if status == "verification_required" else (
                        "下载超时，已跳过" if status == "skipped_timeout" else "下载失败"
                    )
                    self.log(f"{job['name']} {message}：{error}")
                    failed += 1
                finished_jobs += 1
                self.progress(finished_jobs, total_jobs, job["name"])
                if should_checkpoint(finished_jobs):
                    save_state(self.state_path, state)
        state["concurrencyLimit"] = next_concurrency_limit(
            concurrency_limit, successful, failed, stable_concurrency_cap()
        )
        if state["concurrencyLimit"] != concurrency_limit:
            self.log(f"自适应并发调整：{concurrency_limit} -> {state['concurrencyLimit']}")
        save_state(self.state_path, state)

    def _download_browser_jobs(self, context, jobs: list[dict], state: dict) -> None:
        """Use the signed-in Chrome renderer when the CDN blocks non-browser clients."""
        known = existing_names(self.target)
        unique: dict[str, dict] = {}
        for job in jobs:
            key = job["name"].casefold()
            if key in known:
                self._set_file_status(state, job, "skipped_exists")
            elif key not in unique:
                unique[key] = job
        if not unique:
            return
        concurrency_limit = int(state["concurrencyLimit"])
        batch_limit = browser_batch_size(concurrency_limit)
        self.log(f"Chrome 下载 {len(unique)} 个文件；稳定并发 {batch_limit}")
        successful = 0
        failed = 0
        finished = 0
        entries = list(unique.values())
        for start in range(0, len(entries), batch_limit):
            if self.stop_event.is_set():
                return
            active = []
            for index, job in enumerate(entries[start:start + batch_limit]):
                tab = self._browser_tabs[index]
                self.log(f"{job['name']}：下载中")
                deadline = time.monotonic() + FILE_DOWNLOAD_TIMEOUT_SECONDS
                try:
                    response = tab.goto(job["url"], wait_until="commit", timeout=45_000)
                    active.append((job, tab, response, deadline))
                except Exception as error:
                    active.append((job, tab, error, deadline))
            for job, tab, result, deadline in active:
                try:
                    if isinstance(result, Exception):
                        raise result
                    if result is None:
                        raise RuntimeError("Chrome 未返回下载响应")
                    if "text/html" in result.headers.get("content-type", "").casefold():
                        self._verification_page = tab
                        self._verification_url = job["url"]
                        raise HtmlChallenge("服务器返回网页验证，需在浏览器手动完成验证")
                    streamed_size = None
                    try:
                        body = result.body()
                    except Exception:
                        streamed_size = self._download_browser_stream(context, job, tab, deadline)
                    if time.monotonic() >= deadline:
                        raise DownloadTimeout("下载超过 2 分钟，已跳过")
                    destination = self.target / job["name"]
                    if streamed_size is None:
                        partial = self.target / f"{job['name']}.part"
                        partial.write_bytes(body)
                        if not is_valid_download(partial):
                            partial.unlink(missing_ok=True)
                            raise RuntimeError("Chrome 返回的内容不是有效文件")
                        os.replace(partial, destination)
                        streamed_size = destination.stat().st_size
                    self.file_progress(job["name"], streamed_size, streamed_size)
                    self._set_file_status(state, job, "downloaded", streamed_size)
                    self.log(f"{job['name']}：browser_downloaded")
                    successful += 1
                except Exception as error:
                    status = "verification_required" if isinstance(error, HtmlChallenge) else (
                        "skipped_timeout" if isinstance(error, DownloadTimeout) else "failed"
                    )
                    self._set_file_status(state, job, status, error=str(error))
                    message = "需要人工验证" if status == "verification_required" else (
                        "下载超时，已跳过" if status == "skipped_timeout" else "Chrome 下载失败"
                    )
                    self.log(f"{job['name']} {message}：{error}")
                    failed += 1
                finished += 1
                self.progress(finished, len(entries), job["name"])
                if should_checkpoint(finished):
                    save_state(self.state_path, state)
        state["concurrencyLimit"] = next_concurrency_limit(
            concurrency_limit, successful, failed, stable_concurrency_cap()
        )
        if state["concurrencyLimit"] != concurrency_limit:
            self.log(f"Chrome 下载自适应并发调整：{concurrency_limit} -> {state['concurrencyLimit']}")
        save_state(self.state_path, state)

    def _download_browser_stream(self, context, job: dict, tab, deadline: float) -> int:
        """Stream a large response before Chrome's inspector cache can evict it."""
        session = context.new_cdp_session(tab)
        partial = self.target / f"{job['name']}.part"
        captured = {"bytes": 0, "content_type": "", "error": None}

        def on_paused(event: dict) -> None:
            request_id = event["requestId"]
            if event.get("request", {}).get("url") != job["url"] or "responseStatusCode" not in event:
                session.send("Fetch.continueRequest", {"requestId": request_id})
                return
            try:
                headers = {
                    header["name"].casefold(): header["value"]
                    for header in event.get("responseHeaders", [])
                }
                captured["content_type"] = headers.get("content-type", "")
                try:
                    total = int(headers.get("content-length", ""))
                except ValueError:
                    total = None
                handle = session.send(
                    "Fetch.takeResponseBodyAsStream", {"requestId": request_id}
                )["stream"]
                with partial.open("wb") as output:
                    while True:
                        if time.monotonic() >= deadline:
                            raise DownloadTimeout("下载超过 2 分钟，已跳过")
                        chunk = session.send("IO.read", {"handle": handle, "size": 1024 * 1024})
                        data = chunk.get("data", "")
                        if data:
                            raw = (
                                base64.b64decode(data)
                                if chunk.get("base64Encoded")
                                else data.encode("latin1")
                            )
                            output.write(raw)
                            captured["bytes"] += len(raw)
                            self.file_progress(job["name"], captured["bytes"], total)
                        if chunk.get("eof"):
                            break
                session.send("IO.close", {"handle": handle})
                session.send(
                    "Fetch.failRequest",
                    {"requestId": request_id, "errorReason": "Aborted"},
                )
            except Exception as error:
                captured["error"] = error

        session.on("Fetch.requestPaused", on_paused)
        session.send(
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]},
        )
        try:
            try:
                tab.goto(job["url"], wait_until="commit", timeout=60_000)
            except Exception:
                pass
            if captured["error"]:
                raise captured["error"]
            if "text/html" in str(captured["content_type"]).casefold():
                partial.unlink(missing_ok=True)
                raise HtmlChallenge("服务器返回网页验证，需在浏览器手动完成验证")
            if not captured["bytes"] or not is_valid_download(partial):
                partial.unlink(missing_ok=True)
                raise RuntimeError("Chrome 流式下载未返回有效文件")
            destination = self.target / job["name"]
            os.replace(partial, destination)
            return destination.stat().st_size
        finally:
            try:
                session.detach()
            except Exception:
                pass

    @staticmethod
    def _set_file_status(state: dict, job: dict, status: str, size: int | None = None, error: str | None = None) -> None:
        record = state["processed"].get(job["id"], {})
        for file in record.get("files", []):
            if file.get("name") == job["name"]:
                file["status"] = status
                if size is not None:
                    file["bytes"] = size
                if error:
                    file["error"] = error
                elif status in {"downloaded", "skipped_exists"}:
                    file.pop("error", None)


def make_diagnostic_bundle(target: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging = target / f"_诊断_{stamp}"
    staging.mkdir(parents=True, exist_ok=True)
    for name in ("_download_state.json", "_download_log.txt", "_runner_log.txt"):
        source = target / name
        if source.exists():
            shutil.copy2(source, staging / name)
    (staging / "环境.txt").write_text(
        f"POP136 App {APP_VERSION}\n生成时间：{datetime.now().astimezone().isoformat()}\n"
        f"目标目录：{target}\n注意：诊断包不包含登录 Cookie 或浏览器资料。\n",
        encoding="utf-8",
    )
    archive = shutil.make_archive(str(staging), "zip", staging)
    shutil.rmtree(staging)
    return Path(archive)
