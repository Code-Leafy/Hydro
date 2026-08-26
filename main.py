from __future__ import annotations

import argparse
import atexit
from html.parser import HTMLParser
import ipaddress
import mimetypes
import json
import logging
import os
import platform
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

class ColabProxyShapeNoiseFilter(logging.Filter):

    def filter(self, record: logging.LogRecord) -> bool:
        return "Unexpected exception finding object shape" not in record.getMessage()

logging.getLogger().addFilter(ColabProxyShapeNoiseFilter())

for _stale_proxy_name in ("request", "session", "g", "current_app"):
    globals().pop(_stale_proxy_name, None)

APP_NAME = "Hydro"
DEFAULT_PORT = 5000


def _scratch_root() -> Path:
    override = os.environ.get("SIGNAL_TEMP_DIR")
    return Path(override) if override else Path(tempfile.gettempdir())


TEMP_ROOT = _scratch_root() / "signal-shelf"
INSTANCE_FILE = _scratch_root() / "signal-shelf-colab-instance.json"
MAX_COOKIE_BYTES = 2 * 1024 * 1024
INSPECTION_TTL = 20 * 60
JOB_TTL = 45 * 60

def env_int(name: str, fallback: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, fallback))
    except (TypeError, ValueError):
        value = fallback
    return max(minimum, min(maximum, value))

def memory_gib() -> float:

    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 12.0

MAX_CONCURRENT_JOBS = env_int("SIGNAL_MAX_JOBS", 3 if memory_gib() >= 20 else 2, 1, 4)
FRAGMENT_WORKERS = env_int("SIGNAL_FRAGMENT_WORKERS", 8, 1, 16)

def requested_port() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args, _ = parser.parse_known_args()
    return args.port

def process_command(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "ignore").replace("\0", " ")
    except OSError:
        return ""

def process_executable_name(pid: int) -> str:

    try:
        return Path(os.readlink(f"/proc/{pid}/exe")).name.lower()
    except OSError:
        return ""

def process_is_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False

def stop_process(pid: Any, grace: float = 2.0) -> None:

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if pid <= 1 or pid == os.getpid() or not process_is_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + grace
    while time.time() < deadline and process_is_alive(pid):
        time.sleep(0.08)
    if process_is_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

def read_instance() -> dict[str, Any]:
    try:
        value = json.loads(INSTANCE_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}

def remove_instance_if_owned() -> None:
    state = read_instance()
    if state.get("runner_pid") == os.getpid():
        try:
            INSTANCE_FILE.unlink()
        except OSError:
            pass

def cleanup_previous_instance(port: int) -> None:

    state = read_instance()

    if state:
        stop_process(state.get("tunnel_pid"))
        old_runner = state.get("runner_pid")
        try:
            old_runner_id = int(old_runner)
        except (TypeError, ValueError):
            old_runner_id = 0
        if old_runner_id != os.getpid() and process_executable_name(old_runner_id).startswith("python") and "main.py" in process_command(old_runner_id):
            stop_process(old_runner_id)
        try:
            INSTANCE_FILE.unlink()
        except OSError:
            pass

    try:
        process_ids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        process_ids = []
    port_markers = (f"127.0.0.1:{port}", f"localhost:{port}", f"0.0.0.0:{port}")
    for pid in process_ids:
        if pid == os.getpid():
            continue
        command = process_command(pid)
        executable = process_executable_name(pid)
        command_lower = command.lower()
        is_matching_tunnel = "cloudflared" in executable and "tunnel" in command_lower and any(marker in command for marker in port_markers)
        is_old_launcher = executable.startswith("python") and re.search(r"(?:^|\s|/)1\.py(?:\s|$)", command) is not None
        if is_matching_tunnel or is_old_launcher:
            stop_process(pid)

def run_quiet(command: list[str], task: str) -> None:
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode:
        tail = (result.stdout or "").strip()[-1800:]
        raise RuntimeError(f"Could not {task}.\n{tail}")

def ensure_dependencies() -> bool:

    run_quiet(
        [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--upgrade", "flask>=3.0", "yt-dlp"],
        "install Flask and yt-dlp",
    )
    if shutil.which("ffmpeg"):
        return True
    can_use_apt = bool(shutil.which("apt-get")) and (not hasattr(os, "geteuid") or os.geteuid() == 0)
    if not can_use_apt:
        return False
    run_quiet(["apt-get", "update", "-qq"], "refresh Colab system packages")
    run_quiet(["apt-get", "install", "-y", "-qq", "ffmpeg"], "install FFmpeg")
    return bool(shutil.which("ffmpeg"))

def clear_colab_output() -> None:

    if "ipykernel" not in sys.modules:
        return
    try:
        from IPython.display import clear_output
        clear_output(wait=True)
    except Exception:
        pass

def cloudflared_path() -> str:
    existing = shutil.which("cloudflared")
    if existing:
        return existing
    architecture = platform.machine().lower()
    if architecture not in {"x86_64", "amd64"}:
        raise RuntimeError(f"Free Quick Tunnel setup supports Colab's Linux amd64 runtime; detected {architecture}.")
    binary = Path(tempfile.gettempdir()) / "signal-shelf-cloudflared"
    if not binary.exists() or binary.stat().st_size < 1_000_000:
        try:
            urllib.request.urlretrieve(
                "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
                binary,
            )
            binary.chmod(0o755)
        except Exception as exc:
            raise RuntimeError("Could not download the free Cloudflare tunnel helper from GitHub.") from exc
    return str(binary)

def prepare_runtime() -> None:

    global FFMPEG_READY
    cleanup_previous_instance(requested_port())
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    (TEMP_ROOT / "inspections").mkdir(exist_ok=True)
    (TEMP_ROOT / "jobs").mkdir(exist_ok=True)
    FFMPEG_READY = ensure_dependencies()
    clear_colab_output()


FFMPEG_READY: bool = False

try:
    from flask import Flask, Response, jsonify, send_file, stream_with_context
    import yt_dlp
except ImportError:
    ensure_dependencies()
    from flask import Flask, Response, jsonify, send_file, stream_with_context
    import yt_dlp

from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.serving import WSGIRequestHandler, make_server

class APIError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status

class QuietYTDLPLogger:

    def debug(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass

@dataclass
class Inspection:
    token: str
    url: str
    directory: Path
    created_at: float
    expires_at: float
    source_type: str
    title: str
    uploader: str
    site: str
    duration_seconds: int | None
    thumbnail_url: str | None = None
    direct_media_url: str | None = None
    direct_media_referer: str | None = None
    video_formats: dict[str, dict[str, Any]] = field(default_factory=dict)
    audio_formats: dict[str, dict[str, Any]] = field(default_factory=dict)
    image_formats: dict[str, dict[str, Any]] = field(default_factory=dict)
    playlist_count: int = 0
    playlist_preview: list[dict[str, str]] = field(default_factory=list)
    cookie_path: Path | None = None

@dataclass
class DownloadJob:
    job_id: str
    inspection_token: str
    directory: Path
    selection: dict[str, str]
    created_at: float
    expires_at: float
    status: str = "queued"
    stage: str = "Waiting for a transfer slot"
    progress: float | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed: int | None = None
    eta: int | None = None
    item_index: int | None = None
    item_total: int | None = None
    files_ready: int = 0
    error: str | None = None
    result_path: Path | None = None
    download_name: str | None = None
    revision: int = 0
    history: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

class TransferStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.inspections: dict[str, Inspection] = {}
        self.jobs: dict[str, DownloadJob] = {}
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)

    def _snapshot_unlocked(self, job: DownloadJob) -> dict[str, Any]:
        return {
            "id": job.job_id,
            "status": job.status,
            "stage": job.stage,
            "progress": round(job.progress, 1) if job.progress is not None else None,
            "downloaded_bytes": job.downloaded_bytes,
            "total_bytes": job.total_bytes,
            "speed": job.speed,
            "eta": job.eta,
            "item_index": job.item_index,
            "item_total": job.item_total,
            "files_ready": job.files_ready,
            "error": job.error,
            "ready": job.status == "complete" and bool(job.result_path),
            "download_name": job.download_name,
            "revision": job.revision,
        }

    def _record_unlocked(self, job: DownloadJob) -> None:

        job.revision += 1
        job.history.append((job.revision, self._snapshot_unlocked(job)))

        if len(job.history) > 420:
            del job.history[: len(job.history) - 420]
        self.changed.notify_all()

    def add_inspection(self, item: Inspection) -> None:
        with self.lock:
            self.inspections[item.token] = item

    def get_inspection(self, token: str) -> Inspection | None:
        with self.lock:
            item = self.inspections.get(token)
            return item if item and item.expires_at > time.time() else None

    def get_job(self, job_id: str) -> DownloadJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def create_job(self, inspection: Inspection, selection: dict[str, str]) -> DownloadJob:
        job_id = secrets.token_urlsafe(24)
        directory = TEMP_ROOT / "jobs" / job_id
        directory.mkdir(parents=True, exist_ok=False)
        now = time.time()
        job = DownloadJob(
            job_id=job_id,
            inspection_token=inspection.token,
            directory=directory,
            selection=selection,
            created_at=now,
            expires_at=now + JOB_TTL,
        )
        with self.lock:

            inspection.expires_at = max(inspection.expires_at, job.expires_at)
            self.jobs[job_id] = job
            self._record_unlocked(job)
        return job

    def update_job(self, job_id: str, **changes: Any) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            incoming_progress = changes.get("progress")
            if incoming_progress is not None:
                try:

                    changes["progress"] = max(float(job.progress or 0), float(incoming_progress))
                except (TypeError, ValueError):
                    pass
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = time.time()

            if changes.get("status") in {"complete", "failed"}:
                job.expires_at = job.updated_at + JOB_TTL
            self._record_unlocked(job)

    def snapshot(self, job: DownloadJob) -> dict[str, Any]:
        with self.lock:
            return self._snapshot_unlocked(job)

    def cleanup(self) -> None:
        now = time.time()
        old_inputs: list[Inspection] = []
        old_jobs: list[DownloadJob] = []
        with self.lock:

            active_input_tokens = {
                job.inspection_token for job in self.jobs.values() if job.status in {"queued", "running"}
            }
            for token, item in list(self.inspections.items()):
                if item.expires_at <= now and token not in active_input_tokens:
                    old_inputs.append(item)
                    self.inspections.pop(token, None)
            for job_id, job in list(self.jobs.items()):
                if job.status in {"complete", "failed"} and job.expires_at <= now:
                    old_jobs.append(job)
                    self.jobs.pop(job_id, None)
        for item in old_inputs:
            shutil.rmtree(item.directory, ignore_errors=True)
        for job in old_jobs:
            shutil.rmtree(job.directory, ignore_errors=True)

store = TransferStore()

def cleanup_worker() -> None:
    while True:
        time.sleep(60)
        store.cleanup()

threading.Thread(target=cleanup_worker, name="signal-shelf-cleanup", daemon=True).start()

VIDEO_PLAYLIST_PROFILES = [
    {"id": "best", "label": "Best available", "detail": "Highest available rendition for every item"},
    {"id": "4320", "label": "Up to 4320p", "detail": "8K ceiling where the source provides it"},
    {"id": "2160", "label": "Up to 2160p", "detail": "4K ceiling"},
    {"id": "1440", "label": "Up to 1440p", "detail": "2K / QHD ceiling"},
    {"id": "1080", "label": "Up to 1080p", "detail": "Full HD ceiling"},
    {"id": "720", "label": "Up to 720p", "detail": "HD ceiling"},
    {"id": "480", "label": "Up to 480p", "detail": "SD ceiling"},
    {"id": "360", "label": "Up to 360p", "detail": "Compact SD ceiling"},
    {"id": "240", "label": "Up to 240p", "detail": "Low-bandwidth ceiling"},
    {"id": "144", "label": "Up to 144p", "detail": "Minimum video ceiling"},
]
AUDIO_PLAYLIST_PROFILES = [
    {"id": "best", "label": "Best available", "detail": "Highest available source audio per item"},
    {"id": "320", "label": "Up to 320 kbps", "detail": "High-bitrate source ceiling"},
    {"id": "256", "label": "Up to 256 kbps", "detail": "High-bitrate source ceiling"},
    {"id": "192", "label": "Up to 192 kbps", "detail": "Standard high-quality ceiling"},
    {"id": "160", "label": "Up to 160 kbps", "detail": "Balanced source ceiling"},
    {"id": "128", "label": "Up to 128 kbps", "detail": "Compact source ceiling"},
    {"id": "96", "label": "Up to 96 kbps", "detail": "Low-bandwidth source ceiling"},
]
VIDEO_PROFILE_IDS = {row["id"] for row in VIDEO_PLAYLIST_PROFILES}
AUDIO_PROFILE_IDS = {row["id"] for row in AUDIO_PLAYLIST_PROFILES}
IMAGE_PLAYLIST_PROFILES = [
    {"id": "source", "label": "Original source", "detail": "Source image or media file for every item"},
]
IMAGE_PROFILE_IDS = {row["id"] for row in IMAGE_PLAYLIST_PROFILES}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "avif", "tiff", "svg"}

def as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None

def human_bytes(value: Any) -> str | None:
    size = as_int(value)
    if not size or size <= 0:
        return None
    amount = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit in {"B", "KB"} else f"{amount:.1f} {unit}"
        amount /= 1024
    return None

def human_duration(value: Any) -> str | None:
    seconds = as_int(value)
    if seconds is None or seconds < 0:
        return None
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"

def compact_codec(value: Any) -> str | None:
    codec = str(value or "")
    return codec.split(".")[0].upper() if codec and codec != "none" else None

def clean_short_text(value: Any, limit: int = 96) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")

def source_thumbnail(info: dict[str, Any]) -> str | None:

    direct = info.get("thumbnail")
    if isinstance(direct, str) and direct.strip():
        return direct
    for entry in list(info.get("entries") or []):
        if isinstance(entry, dict):
            thumbnail = entry.get("thumbnail")
            if isinstance(thumbnail, str) and thumbnail.strip():
                return thumbnail
    source_url = info.get("url")
    if str(info.get("ext") or "").lower() in IMAGE_EXTENSIONS and isinstance(source_url, str) and source_url.startswith(("http://", "https://")):
        return source_url
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    media_id = str(info.get("id") or "").strip()
    if "youtube" in extractor and media_id:

        return f"https://i.ytimg.com/vi/{media_id}/hqdefault.jpg"
    return None

class PublicMetaParser(HTMLParser):

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self._in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag.lower() == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            value = attributes.get("content") or ""
            if key and value and key not in self.meta:
                self.meta[key] = value
        elif tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)

def image_extension(url: str, content_type: str | None = None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix in IMAGE_EXTENSIONS:
        return suffix
    guessed = mimetypes.guess_extension(str(content_type or "").split(";", 1)[0])
    if guessed and guessed.lstrip(".").lower() in IMAGE_EXTENSIONS:
        return guessed.lstrip(".").lower()
    return "jpg"

def platform_oembed_image_info(page_url: str) -> dict[str, Any] | None:

    host = (urlparse(page_url).hostname or "").lower()
    if "pinterest." in host:
        endpoint = "https://www.pinterest.com/oembed.json?" + urlencode({"url": page_url})
    elif "reddit." in host:
        endpoint = "https://www.reddit.com/oembed?" + urlencode({"url": page_url, "format": "json"})
    else:
        return None
    request_object = urllib.request.Request(
        endpoint,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urllib.request.urlopen(request_object, timeout=12) as response:
            data = json.loads(response.read(512 * 1024).decode("utf-8", "replace"))
        image_url = data.get("thumbnail_url") or data.get("image")
        if not isinstance(image_url, str) or not image_url.startswith(("http://", "https://")):
            return None
        try:
            validate_remote_fetch_url(image_url)
        except APIError:
            return None
        title = clean_short_text(data.get("title") or "Public preview image", 180)
        author = clean_short_text(data.get("author_name") or host, 120)
        extension = image_extension(image_url)
        return {
            "id": f"oembed-image-{secrets.token_hex(6)}",
            "title": title or "Public preview image",
            "uploader": author or host,
            "extractor_key": "Public oEmbed image",
            "ext": extension,
            "url": image_url,
            "thumbnail": image_url,
            "formats": [],
            "_hydro_direct_media_url": image_url,
            "_hydro_direct_media_referer": page_url,
            "_hydro_public_preview": True,
        }
    except Exception:
        return None

def public_preview_image_info(page_url: str) -> dict[str, Any] | None:

    oembed = platform_oembed_image_info(page_url)
    if oembed:
        return oembed
    request_object = urllib.request.Request(
        page_url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request_object, timeout=14) as response:
            final_url = response.geturl()
            content_type = response.headers.get_content_type() if hasattr(response.headers, "get_content_type") else ""
            if str(content_type).startswith("image/"):
                image_url = final_url
                title = Path(urlparse(final_url).path).stem.replace("-", " ") or "Public image"
            else:
                raw_html = response.read(2 * 1024 * 1024)
                document = raw_html.decode("utf-8", "replace")
                parser = PublicMetaParser()
                parser.feed(document)
                image_candidate = (
                    parser.meta.get("og:image:secure_url")
                    or parser.meta.get("og:image")
                    or parser.meta.get("twitter:image")
                    or parser.meta.get("twitter:image:src")
                )
                if not image_candidate:
                    return None
                image_url = urljoin(final_url, image_candidate)
                title = clean_short_text(
                    parser.meta.get("og:title") or parser.meta.get("twitter:title") or " ".join(parser.title_parts) or "Public preview image",
                    180,
                )
        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"}:
            return None
        try:
            validate_remote_fetch_url(image_url)
        except APIError:
            return None
        extension = image_extension(image_url, content_type if 'content_type' in locals() else None)
        host = urlparse(page_url).hostname or "Public page"
        return {
            "id": f"public-image-{secrets.token_hex(6)}",
            "title": title or "Public preview image",
            "uploader": host,
            "extractor_key": "Public image preview",
            "ext": extension,
            "url": image_url,
            "thumbnail": image_url,
            "formats": [],
            "_hydro_direct_media_url": image_url,
            "_hydro_direct_media_referer": page_url,
            "_hydro_public_preview": True,
        }
    except Exception:
        return None

def validate_source_url(raw: Any) -> str:
    url = str(raw or "").strip()
    if not url:
        raise APIError("Paste a media link before reading it.")
    if len(url) > 2048:
        raise APIError("That link is unusually long. Use the canonical media-page or playlist URL.")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise APIError("Use a complete http or https media link.")
    if parsed.username or parsed.password:
        raise APIError("Links with embedded credentials are not accepted.")
    host = (parsed.hostname or "").rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise APIError("Local-network addresses are not accepted.")
    try:
        address = ipaddress.ip_address(host)
        if not address.is_global:
            raise APIError("Private-network addresses are not accepted.")
    except ValueError:
        pass
    return url

def validate_remote_fetch_url(raw: Any) -> str:
    url = str(raw or "").strip()
    if not url:
        raise APIError("That remote URL is missing.", 400)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise APIError("That remote URL is not supported.", 400)
    host = (parsed.hostname or "").rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
        if not address.is_global:
            raise APIError("Remote private-network addresses are not accepted.", 400)
    except ValueError:
        pass
    return url

def save_cookie_upload(directory: Path) -> Path | None:

    from flask import request as current_request
    upload = current_request.files.get("cookies")
    if not upload or not upload.filename:
        return None
    if not upload.filename.lower().endswith(".txt"):
        raise APIError("Optional session files must be named cookies.txt or end in .txt.")
    payload = upload.read(MAX_COOKIE_BYTES + 1)
    if not payload:
        raise APIError("The optional cookies.txt file is empty.")
    if len(payload) > MAX_COOKIE_BYTES:
        raise APIError("cookies.txt is too large. The maximum is 2 MB.", 413)
    if b"\x00" in payload:
        raise APIError("The optional cookie file is not valid plain text.")
    path = directory / "cookies.txt"
    path.write_bytes(payload)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path

def resolution_label(height: int, width: int, fps: int) -> str:
    named = {4320: "8K", 2160: "4K", 1440: "2K", 1080: "FHD", 720: "HD"}
    if height:
        result = f"{height}p"
        if height in named:
            result += f" · {named[height]}"
    elif width:
        result = f"{width}px wide"
    else:
        result = "Source resolution"
    if fps > 30:
        result += f" · {fps} fps"
    return result

def is_storyboard(raw: dict[str, Any]) -> bool:
    descriptor = " ".join(str(raw.get(key) or "") for key in ("format_note", "format", "format_id")).lower()
    return "storyboard" in descriptor or "thumbnail" in descriptor

def suitable_merge_container(extension: str, codec: str) -> str:
    lower = codec.lower()
    if extension in {"mp4", "m4v", "mov"} and any(marker in lower for marker in ("avc", "h264", "av01", "hev", "hvc")):
        return "MP4"
    return "MKV"

def normalize_formats(info: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:

    videos: list[dict[str, Any]] = []
    audios: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []

    def add_image(raw: dict[str, Any], format_id: str, source_direct: bool = False) -> None:
        extension = str(raw.get("ext") or "jpg").lower()
        width = as_int(raw.get("width")) or 0
        height = as_int(raw.get("height")) or 0
        size = as_int(raw.get("filesize")) or as_int(raw.get("filesize_approx")) or 0
        label = f"{width} × {height}" if width and height else f"{extension.upper()} image"
        images.append(
            {
                "id": format_id,
                "label": label,
                "width": width,
                "height": height,
                "container": extension.upper(),
                "filesize": human_bytes(size),
                "filesize_bytes": size,
                "source_direct": source_direct,
            }
        )

    for raw in info.get("formats") or []:
        if not isinstance(raw, dict) or is_storyboard(raw):
            continue
        format_id = str(raw.get("format_id") or "").strip()
        extension = str(raw.get("ext") or "bin").lower()
        vcodec = str(raw.get("vcodec") or "none")
        acodec = str(raw.get("acodec") or "none")
        if vcodec == "none" and acodec == "none":
            if format_id and extension in IMAGE_EXTENSIONS:
                add_image(raw, format_id)
            continue
        if not format_id:
            continue
        size = as_int(raw.get("filesize")) or as_int(raw.get("filesize_approx")) or 0
        if vcodec != "none":
            width = as_int(raw.get("width")) or 0
            height = as_int(raw.get("height")) or 0
            fps = as_int(raw.get("fps")) or 0

            videos.append(
                {
                    "id": format_id,
                    "label": resolution_label(height, width, fps),
                    "height": height,
                    "width": width,
                    "fps": fps,
                    "container": extension.upper(),
                    "video_codec": compact_codec(vcodec) or "VIDEO",
                    "audio_codec": compact_codec(acodec),
                    "has_audio": acodec != "none",
                    "filesize": human_bytes(size),
                    "filesize_bytes": size,
                    "merge_container": suitable_merge_container(extension, vcodec),
                }
            )
        elif acodec != "none":
            abr = as_int(raw.get("abr")) or as_int(raw.get("tbr")) or 0
            audios.append(
                {
                    "id": format_id,
                    "label": f"{abr} kbps" if abr else "Source audio",
                    "abr": abr,
                    "container": extension.upper(),
                    "audio_codec": compact_codec(acodec) or "AUDIO",
                    "filesize": human_bytes(size),
                    "filesize_bytes": size,
                }
            )

    top_extension = str(info.get("ext") or "").lower()
    if not images and top_extension in IMAGE_EXTENSIONS and info.get("url"):
        add_image(info, "__source_image__", source_direct=True)

    videos.sort(
        key=lambda item: (
            -item["height"], -item["fps"], -item["filesize_bytes"],
            0 if item["container"] == "MP4" else 1,
            0 if item["has_audio"] else 1,
        )
    )

    if not audios:
        for video in videos:
            if not video["has_audio"]:
                continue
            codec = str(video.get("audio_codec") or "AUDIO").upper()
            target = "m4a" if codec.startswith("MP4") or codec.startswith("AAC") else "opus" if codec.startswith("OPUS") else "m4a"
            audios.append(
                {
                    "id": video["id"],
                    "label": f"Audio from {video['label']}",
                    "abr": 0,
                    "container": target.upper(),
                    "audio_codec": codec,
                    "filesize": None,
                    "filesize_bytes": 0,
                    "derived_from_video": True,
                    "source_resolution": video["label"],
                    "extract_codec": target,
                }
            )
    audios.sort(key=lambda item: (-item["abr"], -item["filesize_bytes"], 0 if item["container"] == "M4A" else 1))
    images.sort(key=lambda item: (-(item["width"] * item["height"]), -item["filesize_bytes"], item["container"]))
    return videos, audios, images

def content_type_label(videos: list[dict[str, Any]], audios: list[dict[str, Any]], images: list[dict[str, Any]]) -> str:
    kinds = [name for name, values in (("Video", videos), ("Audio", audios), ("Image", images)) if values]
    return " + ".join(kinds) if kinds else "Media"

def metadata_options(cookie_path: Path | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,

        "extract_flat": "discard_in_playlist",
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "cachedir": False,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.8",
        },
        "logger": QuietYTDLPLogger(),
    }
    if cookie_path:
        options["cookiefile"] = str(cookie_path)
    return options

def friendly_error(error: Exception, action: str) -> str:
    message = str(error).lower()
    if any(marker in message for marker in ("private", "login", "sign in", "authentication is required", "account authentication", "login required")):
        return f"This source requires your own signed-in session. Add a valid optional cookies.txt file, then {action} again."
    if "403" in message or "blocked" in message or "forbidden" in message:
        return "This host blocked the temporary Colab connection. Try a public canonical link or use your own signed-in session when you are allowed to do so."
    if "no video could be found" in message:
        return "This post does not expose a downloadable video. If it is a public image post, Hydro will try its page-preview image; otherwise use a public media post or your own session."
    if "no video formats found" in message or "no formats found" in message:
        return "No downloadable stream was exposed by this page. Hydro can use a public page-preview image when one is available, but protected media needs access from the source."
    if "unsupported url" in message:
        return "This link is not supported by the installed downloader. Try the canonical media-page link."
    if "requested format is not available" in message:
        return "That source rendition changed. Read the link again, then choose a currently listed path."
    if "not available" in message or "unavailable" in message:
        return "This media is unavailable at the source. Check the link or choose another item."
    if "ffmpeg" in message:
        return "FFmpeg is needed for this merge or conversion. Re-run in a Colab runtime where setup completed."
    return f"Could not {action}. Check the link and try again."

def is_playlist_info(info: dict[str, Any]) -> bool:
    return str(info.get("_type") or "").lower() == "playlist"

def playlist_summary(info: dict[str, Any]) -> tuple[int, int | None, list[dict[str, str]]]:
    raw_entries = [entry for entry in list(info.get("entries") or []) if isinstance(entry, dict)]
    count = as_int(info.get("playlist_count")) or len(raw_entries)
    durations = [as_int(entry.get("duration")) for entry in raw_entries]
    known = [duration for duration in durations if duration is not None and duration >= 0]
    total_duration = sum(known) if known and len(known) == len(raw_entries) else None
    preview: list[dict[str, str]] = []
    for index, entry in enumerate(raw_entries[:6], start=1):
        preview.append(
            {
                "index": str(as_int(entry.get("playlist_index")) or index),
                "title": clean_short_text(entry.get("title") or "Untitled item", 72),
                "duration": human_duration(entry.get("duration")) or "—",
            }
        )
    return count, total_duration, preview

def profile_by_id(rows: list[dict[str, str]], profile_id: str) -> dict[str, str]:
    return next(row for row in rows if row["id"] == profile_id)

def export_options(inspection: Inspection, job: DownloadJob) -> dict[str, Any]:
    selection = job.selection
    kind = selection["kind"]
    is_playlist = inspection.source_type == "playlist"
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 4,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "concurrent_fragment_downloads": FRAGMENT_WORKERS,
        "cachedir": False,
        "noprogress": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.8",
        },
        "logger": QuietYTDLPLogger(),
    }
    if inspection.cookie_path and inspection.cookie_path.exists():
        options["cookiefile"] = str(inspection.cookie_path)

    if is_playlist:
        options["noplaylist"] = False
        options["ignoreerrors"] = True
        options["outtmpl"] = str(job.directory / "%(playlist_index)03d - %(title).150B [%(id)s].%(ext)s")
        profile_id = selection["profile_id"]
        if kind == "video":
            if profile_id == "best":
                options["format"] = "(bestvideo+bestaudio)/best"
            else:
                ceiling = int(profile_id)
                options["format"] = f"(bestvideo[height<={ceiling}]+bestaudio)/best[height<={ceiling}]"

            options["merge_output_format"] = "mkv"
        elif kind == "audio":
            if profile_id == "best":
                options["format"] = "bestaudio/best"
            else:
                ceiling = int(profile_id)
                options["format"] = f"bestaudio[abr<={ceiling}]/bestaudio/best"
            add_audio_postprocessor(options, selection["audio_output"], source_is_m4a=False)
        else:

            options["format"] = "best"
        return options

    options["noplaylist"] = True
    options["outtmpl"] = str(job.directory / "%(title).160B [%(id)s].%(ext)s")
    format_id = selection["format_id"]
    if kind == "video":
        video = inspection.video_formats[format_id]
        if video["has_audio"]:
            options["format"] = format_id
        elif video["merge_container"] == "MP4":
            options["format"] = f"{format_id}+bestaudio[ext=m4a]/bestaudio"
            options["merge_output_format"] = "mp4"
        else:
            options["format"] = f"{format_id}+bestaudio/best"
            options["merge_output_format"] = "mkv"
    elif kind == "audio":
        audio = inspection.audio_formats[format_id]
        options["format"] = format_id
        if audio.get("derived_from_video"):

            options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": audio.get("extract_codec") or "m4a"}]
        else:
            add_audio_postprocessor(options, selection["audio_output"], source_is_m4a=audio["container"] == "M4A")
    else:
        image = inspection.image_formats[format_id]

        if not image.get("source_direct"):
            options["format"] = format_id
    return options

def add_audio_postprocessor(options: dict[str, Any], audio_output: str, source_is_m4a: bool) -> None:

    if audio_output == "native" or (audio_output == "m4a" and source_is_m4a):
        return
    postprocessor: dict[str, Any] = {"key": "FFmpegExtractAudio", "preferredcodec": audio_output}
    if audio_output == "mp3":
        postprocessor["preferredquality"] = "0"
    options["postprocessors"] = [postprocessor]

SKIP_OUTPUT_SUFFIXES = {
    ".part", ".ytdl", ".json", ".description", ".temp",
    ".vtt", ".srt", ".ass", ".lrc", ".nfo",
}

def media_files(directory: Path) -> list[Path]:
    files: list[Path] = []
    for path in directory.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in SKIP_OUTPUT_SUFFIXES or path.name.endswith(".part"):
            continue
        if path.suffix.lower() == ".zip" and path.name.startswith("playlist-"):
            continue
        files.append(path)
    return sorted(files, key=lambda path: str(path).lower())

def archive_playlist(job: DownloadJob, inspection: Inspection) -> Path:
    files = media_files(job.directory)
    if not files:
        raise RuntimeError("No playlist items could be downloaded.")
    stem = re.sub(r"[^\w .()\-]+", "_", inspection.title, flags=re.UNICODE).strip(" ._") or "playlist"
    archive_path = job.directory / f"playlist-{stem[:80]}.zip"
    store.update_job(job.job_id, stage=f"Packaging {len(files)} downloaded items", progress=99.0, files_ready=len(files))

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for file_path in files:
            archive.write(file_path, arcname=file_path.relative_to(job.directory))
    return archive_path

def download_public_image(job: DownloadJob, inspection: Inspection) -> Path:

    image_url = inspection.direct_media_url
    if not image_url:
        raise RuntimeError("No direct public image URL is available.")
    try:
        image_url = validate_remote_fetch_url(image_url)
    except APIError:
        raise RuntimeError("The source image URL is not reachable from this runtime.") from None
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
    }
    if inspection.direct_media_referer:
        headers["Referer"] = inspection.direct_media_referer
    store.update_job(job.job_id, stage="Fetching public image", progress=2.0, downloaded_bytes=0, total_bytes=None, speed=None, eta=None)
    request_object = urllib.request.Request(image_url, headers=headers)
    with urllib.request.urlopen(request_object, timeout=30) as response:
        content_type = response.headers.get_content_type() if hasattr(response.headers, "get_content_type") else ""
        total = as_int(response.headers.get("Content-Length"))
        if total and total > 512 * 1024 * 1024:
            raise RuntimeError("The source image is larger than the 512 MB notebook safety limit.")
        extension = image_extension(response.geturl(), content_type)
        stem = re.sub(r"[^\w .()\-]+", "_", inspection.title, flags=re.UNICODE).strip(" ._") or "image"
        output = job.directory / f"{stem[:140]}.{extension}"
        downloaded = 0
        started = time.monotonic()
        with output.open("wb") as target:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                target.write(chunk)
                downloaded += len(chunk)
                elapsed = max(time.monotonic() - started, 0.001)
                speed = int(downloaded / elapsed)
                progress = min(downloaded / total * 100, 99.0) if total else None
                remaining = int((total - downloaded) / speed) if total and speed else None
                store.update_job(
                    job.job_id,
                    stage="Downloading public image",
                    progress=progress,
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    speed=speed,
                    eta=max(remaining, 0) if remaining is not None else None,
                )
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("The public image download returned no file.")
    store.update_job(job.job_id, stage="Verifying image file", progress=99.0, downloaded_bytes=output.stat().st_size, total_bytes=total, eta=0)
    return output

def run_download(job_id: str) -> None:
    job = store.get_job(job_id)
    if not job:
        return
    inspection = store.get_inspection(job.inspection_token)
    if not inspection:
        store.update_job(job_id, status="failed", stage="Expired", error="The source catalog expired. Read the link again.")
        return

    with store.slots:
        store.update_job(job_id, status="running", stage="Opening the selected source path", progress=0)
        is_playlist = inspection.source_type == "playlist"

        last_hook_update = 0.0
        last_hook_progress: float | None = None

        def progress_hook(event: dict[str, Any]) -> None:
            nonlocal last_hook_update, last_hook_progress
            status = event.get("status")
            info = event.get("info_dict") if isinstance(event.get("info_dict"), dict) else {}
            item_index = as_int(info.get("playlist_index")) if is_playlist else None
            item_total = as_int(info.get("n_entries")) if is_playlist else None
            if is_playlist:
                item_index = item_index or 1
                item_total = item_total or inspection.playlist_count or None
            if status == "downloading":
                downloaded = as_int(event.get("downloaded_bytes")) or 0
                total = as_int(event.get("total_bytes")) or as_int(event.get("total_bytes_estimate"))
                item_progress = downloaded / total * 100 if total else None
                if is_playlist and item_total:
                    overall = ((item_index - 1) + ((item_progress or 0) / 100)) / item_total * 100
                    stage = f"Downloading item {item_index} of {item_total}"
                else:
                    overall = item_progress
                    stage = "Downloading source"
                now = time.monotonic()
                moved = abs((overall or 0.0) - (last_hook_progress or 0.0))
                throttled = overall is not None and moved < 1.0 and now - last_hook_update < 1.0
                if throttled:
                    return
                last_hook_update = now
                last_hook_progress = overall
                store.update_job(
                    job_id,
                    stage=stage,
                    progress=min(overall, 99.0) if overall is not None else None,
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    speed=as_int(event.get("speed")),
                    eta=as_int(event.get("eta")),
                    item_index=item_index,
                    item_total=item_total,
                )
            elif status == "finished":
                stage = f"Preparing item {item_index or 1} of {item_total}" if is_playlist and item_total else "Preparing the file"
                store.update_job(job_id, stage=stage, progress=99.0, eta=0, item_index=item_index, item_total=item_total)

        def postprocessor_hook(event: dict[str, Any]) -> None:
            if event.get("status") != "started":
                return
            info = event.get("info_dict") if isinstance(event.get("info_dict"), dict) else {}
            index = as_int(info.get("playlist_index"))
            total = as_int(info.get("n_entries")) or inspection.playlist_count
            processor = str(event.get("postprocessor") or "")
            action = "Processing selected media" if "Merger" in processor else "Finalizing export"
            if is_playlist and total:
                action += f" · item {index or 1} of {total}"
            store.update_job(job_id, stage=action, progress=99.0, item_index=index, item_total=total or None)

        try:
            if inspection.direct_media_url and job.selection["kind"] == "image":
                result = download_public_image(job, inspection)
                store.update_job(
                    job_id,
                    status="complete",
                    stage="Image file ready",
                    progress=100.0,
                    files_ready=1,
                    result_path=result,
                    download_name=result.name,
                    eta=0,
                )
                return

            options = export_options(inspection, job)
            options["progress_hooks"] = [progress_hook]
            options["postprocessor_hooks"] = [postprocessor_hook]
            store.update_job(job_id, stage="Resolving source manifest", progress=1.0)
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.extract_info(inspection.url, download=True)

            store.update_job(job_id, stage="Verifying downloaded file", progress=99.0)
            if is_playlist:
                result = archive_playlist(job, inspection)

                source_count = len(media_files(job.directory))
                store.update_job(
                    job_id,
                    status="complete",
                    stage=f"Playlist archive ready · {source_count} item(s)",
                    progress=100.0,
                    files_ready=source_count,
                    result_path=result,
                    download_name=result.name,
                    eta=0,
                )
            else:
                files = media_files(job.directory)
                if not files:
                    raise RuntimeError("Downloader finished without creating a media file.")
                result = max(files, key=lambda path: path.stat().st_size)
                store.update_job(
                    job_id,
                    status="complete",
                    stage="File ready",
                    progress=100.0,
                    files_ready=1,
                    result_path=result,
                    download_name=result.name,
                    eta=0,
                )
        except Exception as exc:
            store.update_job(
                job_id,
                status="failed",
                stage="Transfer stopped",
                error=friendly_error(exc, "retrieve that selected source path"),
            )

PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#000000">
  <title>Hydro — Universal Media Download Colab</title>
  <style>
    :root {
      --canvas: #000000;
      --bar: #050505;
      --panel: #0b0b0d;
      --panel-2: #111114;
      --panel-3: #18181c;
      --line: #29292e;
      --line-strong: #46464e;
      --text: #ffffff;
      --soft: #e7e7eb;
      --muted: #aaaab2;
      --faint: #707078;
      --accent: #4da8ff;
      --accent-strong: #2187ec;
      --accent-wash: #0c2948;
      --good: #68c6ff;
      --gold: #9fd7ff;
      --danger: #ff8585;
      --danger-wash: #35191d;
      --mono: ui-monospace, "SFMono-Regular", "Cascadia Mono", Consolas, monospace;
      --body: "Segoe UI", Arial, Helvetica, sans-serif;
      --display: "Arial Black", "Segoe UI Black", "Helvetica Neue", Arial, sans-serif;
      --radius: 9px;
      --radius-small: 6px;
    }
    * { box-sizing: border-box; }
    html { min-width: 320px; height: 100%; background: var(--canvas); }
    body { height: 100%; margin: 0; overflow: hidden; color: var(--text); background: var(--canvas); font-family: var(--body); -webkit-font-smoothing: antialiased; }
    button, input { font: inherit; }
    button { cursor: pointer; }
    button:disabled { cursor: not-allowed; opacity: .46; }
    [hidden] { display: none !important; }
    :focus-visible { outline: 2px solid var(--gold); outline-offset: 2px; }
    .skip { position: fixed; z-index: 50; top: -50px; left: 12px; padding: 8px 10px; color: var(--canvas); background: var(--gold); font: 700 11px var(--mono); text-decoration: none; }
    .skip:focus { top: 12px; }
    .visually-hidden { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }

    /* Desktop is a bounded application shell: panes, not a scrolling document. */
    .app { height: 100vh; height: 100dvh; display: grid; grid-template-rows: 62px minmax(0, 1fr); }
    .appbar { animation: bar-in .38s ease both; z-index: 10; display: grid; grid-template-columns: 178px minmax(320px, 1fr) auto; align-items: center; gap: 16px; min-width: 0; padding: 0 16px; border-bottom: 1px solid var(--line); background: var(--bar); }
    .brand { display: inline-flex; align-items: center; gap: 9px; min-width: 0; color: var(--text); text-decoration: none; }
    .brand-icon { width: 31px; height: 31px; flex: 0 0 auto; color: var(--accent); }
    .brand-word { display: grid; gap: 3px; min-width: 0; }
    .brand-word b { font: 900 19px/.9 var(--display); letter-spacing: -.55px; text-transform: uppercase; }
    .brand-word small { color: var(--faint); font: 800 8px/1 var(--mono); letter-spacing: .11em; text-transform: uppercase; }
    .read-form { min-width: 0; }
    .input-strip { display: grid; grid-template-columns: 28px minmax(0, 1fr) auto; align-items: stretch; height: 38px; overflow: hidden; border: 1px solid var(--line-strong); border-radius: var(--radius-small); background: var(--canvas); }
    .input-glyph { display: grid; place-items: center; color: var(--accent); border-right: 1px solid var(--line); font: 800 14px/1 var(--mono); }
    .input-strip input { min-width: 0; padding: 0 10px; border: 0; outline: 0; color: var(--text); background: transparent; font-size: 12px; }
    .input-strip input::placeholder { color: var(--faint); }
    .read { min-width: 104px; padding: 0 13px; border: 0; color: #000000; background: var(--accent); font: 800 9px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; }
    .read:hover:not(:disabled) { background: #88ccff; }
    .header-tools { display: flex; align-items: center; justify-content: flex-end; gap: 11px; }
    details.session { position: relative; }
    details.session summary { list-style: none; padding: 8px; border: 1px solid var(--line); border-radius: var(--radius-small); color: var(--muted); cursor: pointer; font: 700 9px/1 var(--mono); letter-spacing: .05em; text-transform: uppercase; white-space: nowrap; }
    details.session summary::-webkit-details-marker { display: none; }
    details.session[open] summary { color: var(--accent); border-color: var(--accent); }
    .session-popover { position: absolute; z-index: 20; top: calc(100% + 7px); right: 0; width: min(300px, 80vw); padding: 12px; border: 1px solid var(--line-strong); border-radius: var(--radius); background: var(--panel-2); }
    .session-popover p { margin: 0 0 10px; color: var(--muted); font-size: 10px; line-height: 1.45; }
    .session-file { display: flex; align-items: center; gap: 7px; color: var(--faint); font: 700 9px/1.2 var(--mono); }
    .session-file input { max-width: 150px; color: var(--muted); font-size: 10px; }

    .workspace { min-height: 0; display: grid; grid-template-columns: clamp(244px, 25vw, 322px) minmax(350px, 1fr) clamp(238px, 23vw, 292px); gap: 10px; padding: 10px; }
    .pane { min-width: 0; min-height: 0; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); animation: pane-in .42s ease both; }
    .workspace > .pane:nth-child(2) { animation-delay: .05s; }
    .workspace > .pane:nth-child(3) { animation-delay: .1s; }
    @keyframes bar-in { from { opacity: 0; transform: translateY(-5px); } to { opacity: 1; transform: translateY(0); } }
    @keyframes pane-in { from { opacity: 0; transform: translateY(7px); } to { opacity: 1; transform: translateY(0); } }
    .pane-label { display: flex; align-items: center; justify-content: space-between; min-height: 39px; padding: 0 13px; border-bottom: 1px solid var(--line); color: var(--muted); font: 800 9px/1 var(--mono); letter-spacing: .1em; text-transform: uppercase; }
    .pane-label b { color: var(--soft); font-weight: 800; }
    .status-line { display: inline-flex; align-items: center; gap: 5px; color: var(--good); font-size: 8px; }
    .status-line i { width: 5px; height: 5px; background: currentColor; }

    /* Left pane: full uncropped source preview and a tight metadata sheet. */
    .source-pane { display: flex; flex-direction: column; }
    .source-scroll { display: flex; flex: 1; flex-direction: column; min-height: 0; overflow-y: auto; scrollbar-color: var(--line-strong) var(--panel); }
    /* A fixed preview well prevents tall Pinterest/Reddit images from stretching the entire source pane. */
    .monitor { position: relative; display: grid; width: 100%; height: clamp(174px, 28vh, 250px); flex: 0 0 auto; min-height: 0; overflow: hidden; place-items: center; border-bottom: 1px solid var(--line); background: #000000; }
    .monitor img { display: block; width: 100%; height: 100%; min-width: 0; min-height: 0; max-width: 100%; max-height: 100%; object-fit: contain; object-position: center; background: #000000; }
    .monitor.has-preview img { animation: preview-in .32s ease both; }
    @keyframes preview-in { from { opacity: 0; transform: scale(.985); } to { opacity: 1; transform: scale(1); } }
    .monitor-empty { display: grid; place-items: center; gap: 10px; color: var(--faint); text-align: center; }
    .monitor-empty .empty-icon { width: 42px; height: 42px; color: var(--accent); }
    .monitor-empty span { max-width: 20ch; font: 800 9px/1.4 var(--mono); letter-spacing: .08em; text-transform: uppercase; }
    .monitor-tag { position: absolute; bottom: 8px; left: 8px; padding: 5px 6px; border: 1px solid #1e5a88; border-radius: 4px; color: #e2f4ff; background: #07192b; font: 700 8px/1 var(--mono); letter-spacing: .07em; text-transform: uppercase; }
    .source-copy { padding: 14px; }
    .source-copy h1 { display: -webkit-box; overflow: hidden; margin: 0; color: var(--text); font: 800 clamp(21px, 2.1vw, 31px)/.94 var(--display); letter-spacing: -.65px; text-transform: uppercase; -webkit-box-orient: vertical; -webkit-line-clamp: 3; }
    .source-copy p { display: -webkit-box; overflow: hidden; margin: 8px 0 0; color: var(--muted); font-size: 11px; line-height: 1.35; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
    .facts { display: grid; grid-template-columns: 1fr 1fr; overflow: hidden; margin: 15px 0 0; border-top: 1px solid var(--line); border-left: 1px solid var(--line); border-radius: var(--radius-small); }
    .facts div { min-width: 0; padding: 9px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
    /* Image information spans both columns so image-based sources have a wider cell. */
    .facts div:last-child { grid-column: 1 / -1; }
    .facts dt { color: var(--faint); font: 700 8px/1.2 var(--mono); letter-spacing: .08em; text-transform: uppercase; }
    .facts dd { overflow: hidden; margin: 5px 0 0; color: var(--soft); font: 700 10px/1.25 var(--mono); text-overflow: ellipsis; white-space: nowrap; }
    .queue-wrap { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 13px; }
    .queue-note { margin: 0; color: var(--gold); font-size: 10px; line-height: 1.45; }
    .queue { display: grid; gap: 1px; max-height: 160px; margin-top: 10px; overflow-y: auto; border-radius: var(--radius-small); background: var(--line); }
    .queue div { min-width: 0; padding: 8px; background: var(--panel-2); }
    .queue span { color: var(--accent); font: 700 8px/1.2 var(--mono); }
    .queue b { display: block; overflow: hidden; margin-top: 4px; color: var(--soft); font: 600 10px/1.2 var(--body); text-overflow: ellipsis; white-space: nowrap; }
    .queue small { display: block; margin-top: 4px; color: var(--faint); font: 700 8px/1 var(--mono); }

    /* Centre: one dense source-format table, its own scroll frame on desktop. */
    .catalog-pane { display: flex; flex-direction: column; }
    #catalog-content { display: flex; flex: 1; flex-direction: column; min-height: 0; }
    .empty-catalog { display: grid; flex: 1; min-height: 0; place-items: center; padding: 28px; text-align: center; }
    .empty-catalog-inner { max-width: 360px; }
    .empty-ruler { display: flex; justify-content: center; gap: 4px; margin-bottom: 18px; }
    .empty-ruler i { width: 18px; height: 4px; background: var(--line-strong); }
    .empty-ruler i:nth-child(2), .empty-ruler i:nth-child(5) { background: var(--accent); }
    .empty-catalog h2 { margin: 0; color: var(--text); font: 800 clamp(27px, 3.5vw, 44px)/.9 var(--display); letter-spacing: -1px; text-transform: uppercase; }
    .empty-catalog p { margin: 13px 0 0; color: var(--muted); font-size: 12px; line-height: 1.55; }
    .catalog-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; min-height: 58px; padding: 0 15px; border-bottom: 1px solid var(--line); }
    .catalog-head h2 { margin: 0; color: var(--text); font: 800 23px/.9 var(--display); letter-spacing: -.45px; text-transform: uppercase; }
    .catalog-head p { margin: 5px 0 0; color: var(--faint); font: 700 8px/1.2 var(--mono); letter-spacing: .06em; text-transform: uppercase; }
    .catalog-count { color: var(--muted); font: 700 9px/1.3 var(--mono); text-align: right; }
    .tabs { display: flex; flex: 0 0 auto; min-height: 43px; border-bottom: 1px solid var(--line); }
    .tab { min-width: 115px; padding: 0 15px; border: 0; border-right: 1px solid var(--line); color: var(--muted); background: transparent; font: 800 9px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; }
    .tab[aria-selected="true"] { color: var(--text); background: var(--panel-2); box-shadow: inset 0 -2px 0 var(--accent); }
    .tab span { color: var(--accent); }
    .table-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 14px; min-height: 31px; align-items: center; padding: 0 15px; border-bottom: 1px solid var(--line); color: var(--faint); font: 700 8px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; }
    .table-head span:last-child { text-align: right; }
    .format-list { flex: 1; min-height: 0; overflow-y: auto; scrollbar-color: var(--line-strong) var(--panel); }
    .format-folder { border-bottom: 1px solid var(--line); animation: folder-in .24s ease both; animation-delay: calc(min(var(--folder-index, 0), 12) * 28ms); }
    @keyframes folder-in { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: translateY(0); } }
    .folder-toggle { width: 100%; display: grid; grid-template-columns: 16px minmax(0, 1fr) auto; align-items: center; gap: 8px; min-height: 35px; padding: 0 15px; border: 0; color: var(--soft); background: var(--panel-2); text-align: left; }
    .folder-toggle:hover { background: var(--panel-3); }
    .folder-caret { width: 16px; height: 16px; color: var(--accent); overflow: visible; transform-origin: 50% 50%; transition: transform .28s cubic-bezier(.16,1,.3,1), color .2s ease; }
    .folder-caret path { stroke: currentColor; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }
    .folder-toggle:hover .folder-caret { color: var(--gold); }
    .folder-toggle[aria-expanded="false"] .folder-caret { transform: rotate(-90deg); }
    .folder-title { overflow: hidden; font: 900 10px/1 var(--mono); letter-spacing: .06em; text-overflow: ellipsis; text-transform: uppercase; white-space: nowrap; }
    .folder-count { color: var(--faint); font: 800 8px/1 var(--mono); letter-spacing: .05em; text-transform: uppercase; }
    .folder-content { background: var(--panel); }
    .format-row { width: 100%; display: grid; grid-template-columns: 14px minmax(0, 1fr) auto; align-items: center; gap: 11px; padding: 11px 15px; border: 0; border-bottom: 1px solid var(--line); color: var(--text); background: transparent; text-align: left; transition: background .15s ease, box-shadow .15s ease; content-visibility: auto; contain-intrinsic-size: auto 50px; }
    .format-row:last-child { border-bottom: 0; }
    .format-row:hover { background: var(--panel-2); }
    .format-row[aria-pressed="true"] { background: var(--accent-wash); box-shadow: inset 2px 0 0 var(--accent); }
    .selector { width: 12px; height: 12px; border: 1px solid var(--line-strong); }
    .format-row[aria-pressed="true"] .selector { border-color: var(--accent); background: var(--accent); box-shadow: inset 0 0 0 3px var(--accent-wash); }
    .format-main { display: block; overflow: hidden; color: var(--text); font: 800 14px/1.1 var(--display); letter-spacing: -.12px; text-overflow: ellipsis; text-transform: uppercase; white-space: nowrap; }
    .format-sub { display: block; overflow: hidden; margin-top: 4px; color: var(--muted); font: 600 9px/1.25 var(--mono); text-overflow: ellipsis; white-space: nowrap; }
    .format-right { display: grid; justify-items: end; gap: 4px; color: var(--faint); font: 700 8px/1.1 var(--mono); white-space: nowrap; }
    .format-tag { padding: 3px 4px; border: 1px solid currentColor; color: var(--good); font-size: 7px; letter-spacing: .05em; text-transform: uppercase; }
    .format-row[aria-pressed="true"] .format-tag { color: #a9ddff; }

    /* Right: a full-height transfer console with live flow telemetry. */
    .export-pane { display: flex; flex-direction: column; }
    .export-scroll { display: flex; flex: 1; flex-direction: column; min-height: 0; overflow-y: auto; padding: 14px; scrollbar-color: var(--line-strong) var(--panel); }
    .export-empty { display: grid; flex: 1; place-items: center; min-height: 0; color: var(--faint); text-align: center; font: 800 10px/1.5 var(--mono); }
    #export-content { display: flex; flex: 1; flex-direction: column; min-height: 0; }
    .selection-label { margin: 0; color: var(--accent); font: 900 8px/1 var(--mono); letter-spacing: .1em; text-transform: uppercase; }
    .selection-title { margin: 10px 0 0; color: var(--text); font: 900 28px/.92 var(--display); letter-spacing: -.5px; text-transform: uppercase; }
    .selection-detail { margin: 8px 0 0; color: var(--muted); font: 600 10px/1.45 var(--mono); }
    .export-facts { display: grid; gap: 0; margin: 18px 0 0; border-top: 1px solid var(--line); }
    .export-facts div { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--line); font: 700 8px/1.25 var(--mono); letter-spacing: .04em; text-transform: uppercase; }
    .export-facts span:first-child { color: var(--faint); }
    .export-facts span:last-child { max-width: 58%; color: var(--soft); text-align: right; }
    .project-link { display: flex; justify-content: space-between; gap: 10px; margin-top: 10px; padding: 9px; color: var(--accent); border: 1px solid var(--line); border-radius: var(--radius-small); font: 800 8px/1.2 var(--mono); letter-spacing: .05em; text-decoration: none; text-transform: uppercase; }
    .project-link:hover { color: var(--gold); }
    .project-link b { color: var(--faint); font-weight: 700; text-align: right; }
    .project-link--source { margin: auto 14px 14px; flex: 0 0 auto; }
    .compatibility { margin: 13px 0 0; color: var(--faint); font-size: 10px; line-height: 1.45; }
    .transfer { display: flex; flex: 1; flex-direction: column; min-height: 205px; margin-top: 16px; padding: 13px; border: 1px solid var(--line-strong); border-radius: var(--radius); background: var(--panel-2); }
    .transfer.is-done { border-color: var(--good); }
    .transfer.is-error { border-color: var(--danger); background: var(--danger-wash); }
    .transfer-top { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 10px; align-items: center; }
    .flow-orb { position: relative; display: grid; width: 31px; height: 31px; place-items: center; color: var(--accent); }
    .flow-orb svg { width: 100%; height: 100%; }
    .transfer[data-phase="working"] .flow-orb { animation: hydro-pulse 1.35s ease-in-out infinite; }
    .transfer[data-phase="working"] .flow-orb::after { content: ""; position: absolute; inset: -4px; border: 1px solid color-mix(in srgb, var(--accent) 55%, transparent); border-radius: 50%; animation: hydro-ring 1.35s ease-out infinite; }
    @keyframes hydro-pulse { 50% { transform: translateY(-2px) scale(1.05); } }
    @keyframes hydro-ring { from { opacity: .75; transform: scale(.7); } to { opacity: 0; transform: scale(1.45); } }
    .transfer-copy { min-width: 0; }
    .transfer-copy b { display: block; overflow: hidden; color: var(--soft); font: 900 11px/1.2 var(--display); letter-spacing: .05px; text-overflow: ellipsis; text-transform: uppercase; white-space: nowrap; }
    .transfer-copy span { display: block; overflow: hidden; margin-top: 3px; color: var(--faint); font: 600 8px/1.3 var(--mono); text-overflow: ellipsis; white-space: nowrap; }
    .transfer-percent { color: var(--accent); font: 900 14px/1 var(--mono); }
    .flow-track { height: 9px; margin-top: 15px; overflow: hidden; border: 1px solid var(--line); border-radius: 999px; background: #000; }
    .flow-track i { display: block; width: 0%; height: 100%; background: repeating-linear-gradient(135deg, var(--accent) 0 8px, var(--accent-strong) 8px 16px); background-size: 23px 23px; will-change: width; transition: width .72s cubic-bezier(.16,1,.3,1); }
    .transfer[data-phase="working"] .flow-track i { animation: water-flow .72s linear infinite; }
    @keyframes water-flow { to { background-position: 23px 0; } }
    .process-steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; margin-top: 13px; }
    .process-steps span { padding: 6px 3px; border: 1px solid var(--line); border-radius: 4px; color: var(--faint); background: var(--panel); font: 800 7px/1 var(--mono); letter-spacing: .05em; text-align: center; text-transform: uppercase; }
    .process-steps span.is-active { color: var(--text); border-color: var(--accent); background: var(--accent-wash); }
    .process-steps span.is-done { color: #000; border-color: var(--good); background: var(--good); }
    .transfer-metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; overflow: hidden; margin: auto 0 0; border: 1px solid var(--line); border-radius: var(--radius-small); background: var(--line); }
    .transfer-metrics div { min-width: 0; padding: 8px; background: var(--panel); }
    .transfer-metrics dt { color: var(--faint); font: 700 7px/1 var(--mono); letter-spacing: .06em; text-transform: uppercase; }
    .transfer-metrics dd { overflow: hidden; margin: 4px 0 0; color: var(--soft); font: 800 9px/1.2 var(--mono); text-overflow: ellipsis; white-space: nowrap; }
    .manual { display: inline-block; margin-top: 9px; color: var(--good); font: 800 8px/1.2 var(--mono); letter-spacing: .04em; }
    .export-button { width: 100%; min-height: 43px; margin-top: 13px; border: 0; border-radius: var(--radius-small); color: #000; background: var(--accent); font: 900 9px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; }
    .export-button:hover:not(:disabled) { background: #88ccff; }
    .export-button:disabled { background: var(--line-strong); color: var(--muted); }

    .notice { position: fixed; z-index: 40; right: 14px; bottom: 14px; width: min(380px, calc(100vw - 28px)); padding: 12px 13px; border: 1px solid var(--danger); border-radius: var(--radius); color: #ffc5c1; background: var(--danger-wash); font-size: 11px; line-height: 1.45; }

    @media (max-width: 880px) {
      html, body { height: auto; min-height: 100%; overflow: auto; }
      .app { height: auto; min-height: 100vh; display: block; }
      .appbar { position: sticky; top: 0; grid-template-columns: 160px minmax(0, 1fr) auto; min-height: 60px; }
      .workspace { grid-template-columns: minmax(0, 1fr) minmax(230px, .54fr); height: auto; min-height: 0; }
      .source-pane { grid-column: 1 / -1; max-height: none; }
      .source-scroll { display: grid; grid-template-columns: minmax(210px, .42fr) minmax(0, 1fr); overflow: visible; }
      .monitor { height: clamp(200px, 32vw, 320px); border-right: 1px solid var(--line); border-bottom: 0; }
      .project-link--source { grid-column: 1 / -1; margin: 0 14px 14px; }
      .catalog-pane, .export-pane { min-height: 570px; }
      .queue { max-height: 130px; }
    }
    @media (max-width: 650px) {
      .appbar { grid-template-columns: 1fr auto; gap: 8px; padding: 9px 10px; }
      .brand { grid-column: 1; }
      .read-form { grid-row: 2; grid-column: 1 / -1; }
      .header-tools { grid-column: 2; grid-row: 1; gap: 7px; }
      .input-strip { height: 38px; }
      .workspace { grid-template-columns: 1fr; padding: 8px; gap: 8px; }
      .source-scroll { display: block; }
      .monitor { height: min(72vw, 360px); border-right: 0; border-bottom: 1px solid var(--line); }
      .project-link--source { margin: 0 14px 14px; }
      .catalog-pane, .export-pane { min-height: auto; }
      .format-list { max-height: min(45vh, 410px); }
      .export-scroll { min-height: 300px; }
      .source-copy h1 { font-size: 27px; }
      .tab { flex: 1; min-width: 0; padding: 0 8px; }
      .format-row { grid-template-columns: 13px minmax(0, 1fr); }
      .format-right { grid-column: 2; justify-items: start; grid-template-columns: auto auto; gap: 7px; }
      .notice { position: sticky; right: auto; bottom: auto; width: auto; margin: 8px; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; scroll-behavior: auto !important; }
    }
  </style>
</head>
<body>
  <a class="skip" href="#source-url">Skip to media link</a>
  <div class="app">
    <header class="appbar">
      <a class="brand" href="/" aria-label="Hydro — Universal Media Download Colab home"><svg class="brand-icon" viewBox="0 0 44 44" fill="none" aria-hidden="true"><path d="M22 3.5C17.9 10.3 8.5 18.2 8.5 27.1A13.5 13.5 0 0 0 35.5 27.1C35.5 18.2 26.1 10.3 22 3.5Z" fill="currentColor"/><path d="M22 14.5v12m-4.5-4 4.5 4 4.5-4M15.5 32h13" stroke="#000" stroke-width="2.2" stroke-linecap="square" stroke-linejoin="miter"/></svg><span class="brand-word"><b>Hydro</b><small>Universal Media Colab</small></span></a>
      <form class="read-form" id="source-form" novalidate><label class="visually-hidden" for="source-url">Media link to inspect</label><div class="input-strip"><span class="input-glyph" aria-hidden="true">⌁</span><input id="source-url" type="url" inputmode="url" autocomplete="url" placeholder="YouTube, Instagram, X, Pinterest, Reddit, or a direct media link…" required><button class="read" id="read-button" type="submit">Inspect link</button></div></form>
      <div class="header-tools"><details class="session"><summary>Session</summary><div class="session-popover"><p>Optional only. Public links work without a cookie file. Use your own cookies.txt only when a source asks for signed-in access.</p><label class="session-file"><input id="cookie-file" type="file" accept=".txt,text/plain"><output id="cookie-name">No file</output></label></div></details></div>
    </header>

    <main class="workspace" aria-label="Media format application">
      <aside class="pane source-pane" aria-label="Selected source preview and details">
        <div class="source-scroll">
          <div class="monitor" id="monitor"><div class="monitor-empty" id="monitor-empty"><svg class="empty-icon" viewBox="0 0 44 44" fill="none" aria-hidden="true"><path d="M22 3.5C17.9 10.3 8.5 18.2 8.5 27.1A13.5 13.5 0 0 0 35.5 27.1C35.5 18.2 26.1 10.3 22 3.5Z" fill="currentColor"/><path d="M22 14.5v12m-4.5-4 4.5 4 4.5-4M15.5 32h13" stroke="#000" stroke-width="2.2" stroke-linecap="square" stroke-linejoin="miter"/></svg><span>Preview appears here</span></div><img id="source-thumbnail" alt="Source preview thumbnail" decoding="async" fetchpriority="high" hidden><span class="monitor-tag" id="monitor-tag" hidden>Source preview</span></div>
          <div class="source-copy">
            <h1 id="source-title">No source loaded</h1>
            <p id="source-byline">Inspect a link to see its preview, publisher, runtime, and available source paths.</p>
            <dl class="facts"><div><dt>Platform</dt><dd id="source-site">—</dd></div><div><dt>Runtime</dt><dd id="source-runtime">—</dd></div><div><dt>Video</dt><dd id="source-video-count">—</dd></div><div><dt>Audio</dt><dd id="source-audio-count">—</dd></div><div><dt>Images</dt><dd id="source-image-count">—</dd></div></dl>
            <section class="queue-wrap" id="playlist-wrap" hidden><p class="queue-note"><strong>Playlist delivery:</strong> a quality ceiling is selected for every item, then the available files return in one ZIP.</p><div class="queue" id="queue-preview" aria-label="Playlist items"></div></section>
          </div>
          <!-- Replace this href with the real repository URL when the project is published. -->
          <a class="project-link project-link--source" href="https://github.com/your-username/hydro" target="_blank" rel="noreferrer"><span>GitHub project</span><b>Set link later ↗</b></a>
        </div>
      </aside>

      <section class="pane catalog-pane" aria-labelledby="catalog-title">
        <div class="empty-catalog" id="catalog-empty"><div class="empty-catalog-inner"><div class="empty-ruler" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i></div><h2>Inspect first.<br>Choose exactly.</h2><p>Paste a public media link above. This app inspects the source before it downloads anything.</p></div></div>
        <div id="catalog-content" hidden>
          <div class="catalog-head"><div><h2 id="catalog-title">Source paths</h2><p id="catalog-mode">Exact formats reported by the source</p></div><span class="catalog-count" id="catalog-count">—</span></div>
          <div class="tabs" role="tablist" aria-label="Format type"><button class="tab" id="tab-video" type="button" role="tab" aria-controls="format-list" aria-selected="true">Video <span id="video-count">0</span></button><button class="tab" id="tab-audio" type="button" role="tab" aria-controls="format-list" aria-selected="false">Audio <span id="audio-count">0</span></button><button class="tab" id="tab-image" type="button" role="tab" aria-controls="format-list" aria-selected="false">Image <span id="image-count">0</span></button></div>
          <div class="table-head"><span id="table-label">Available video formats</span><span id="table-note">Source size</span></div>
          <div class="format-list" id="format-list" role="tabpanel" aria-labelledby="tab-video" aria-label="Available source formats"></div>
        </div>
      </section>

      <aside class="pane export-pane" aria-labelledby="export-pane-label">
        <div class="pane-label"><b id="export-pane-label">Export</b><span id="export-state">Waiting</span></div>
        <div class="export-scroll">
          <div class="export-empty" id="export-empty">Choose a video, audio, or image path<br>to prepare an export.</div>
          <div id="export-content" hidden>
            <p class="selection-label" id="selection-kind">Video selection</p>
            <h2 class="selection-title" id="selection-title">—</h2>
            <p class="selection-detail" id="selection-detail">—</p>
            <div class="export-facts"><div><span>Delivery</span><span id="delivery-value">—</span></div><div><span>Estimate</span><span id="estimate-value">—</span></div></div>
            <p class="compatibility" id="compatibility-note">—</p>
            <section class="transfer" id="transfer" data-phase="idle" aria-live="polite"><div class="transfer-top"><span class="flow-orb" aria-hidden="true"><svg viewBox="0 0 44 44" fill="none"><path d="M22 3.5C17.9 10.3 8.5 18.2 8.5 27.1A13.5 13.5 0 0 0 35.5 27.1C35.5 18.2 26.1 10.3 22 3.5Z" fill="currentColor"/><path d="M22 15v12m-4.5-4 4.5 4 4.5-4" stroke="#000" stroke-width="2.2" stroke-linecap="square" stroke-linejoin="miter"/></svg></span><div class="transfer-copy"><b id="transfer-stage">Ready to transfer</b><span id="transfer-detail">Choose download when you are ready.</span></div><b class="transfer-percent" id="transfer-percent">0%</b></div><div class="flow-track" aria-hidden="true"><i id="transfer-bar"></i></div><div class="process-steps" aria-label="Transfer stages"><span id="step-fetch">Fetch</span><span id="step-process">Process</span><span id="step-package">Package</span></div><dl class="transfer-metrics"><div><dt>Item</dt><dd id="metric-item">Ready</dd></div><div><dt>Data</dt><dd id="metric-data">—</dd></div><div><dt>Rate</dt><dd id="metric-rate">—</dd></div><div><dt>Time</dt><dd id="metric-time">—</dd></div></dl><a class="manual" id="manual-download" hidden>Save file manually</a></section>
          </div>
          <button class="export-button" id="export-button" type="button" disabled>Choose a source path</button>
        </div>
      </aside>
    </main>
  </div>
  <div class="notice" id="error-note" role="alert" hidden></div>

  <script>
  (() => {
    const state = { token: null, type: null, source: null, formats: { video: [], audio: [], image: [] }, profiles: { video: [], audio: [], image: [] }, video: null, audio: null, image: null, active: 'video', openFolders: { video: new Set(), audio: new Set(), image: new Set() }, eventSource: null, terminalJob: null, progressQueue: [], progressPlaying: false, visualProgress: 0 };
    const $ = (selector) => document.querySelector(selector);
    const setText = (selector, value) => { const node = $(selector); if (node) node.textContent = value || '—'; };
    const form = $('#source-form');
    const readButton = $('#read-button');
    const formatList = $('#format-list');

    function bytes(value) {
      if (!value || Number(value) <= 0) return 'size unknown';
      const units = ['B', 'KB', 'MB', 'GB', 'TB']; let amount = Number(value); let unit = 0;
      while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
      return `${amount >= 10 || unit < 2 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`;
    }
    function eta(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return 'estimating time';
      const seconds = Math.max(0, Math.round(Number(value))); const mins = Math.floor(seconds / 60); return mins ? `${mins}m ${seconds % 60}s left` : `${seconds}s left`;
    }
    function showError(message) { $('#error-note').textContent = message; $('#error-note').hidden = false; }
    function clearError() { $('#error-note').hidden = true; $('#error-note').textContent = ''; }
    function items(kind) { return state.type === 'playlist' ? state.profiles[kind] : state.formats[kind]; }
    function chosen(kind = state.active) { const id = state[kind]; return items(kind).find((item) => item.id === id) || null; }

    function resetMonitor() {
      $('#monitor').classList.remove('has-preview'); $('#source-thumbnail').hidden = true; $('#source-thumbnail').removeAttribute('src'); $('#monitor-empty').hidden = false; $('#monitor-tag').hidden = true;
      setText('#source-title', 'Inspecting source…'); setText('#source-byline', 'Fetching current metadata and available paths.'); setText('#source-site', '—'); setText('#source-runtime', '—'); setText('#source-video-count', '—'); setText('#source-audio-count', '—'); setText('#source-image-count', '—'); $('#playlist-wrap').hidden = true;
    }
    function setStepStates(phase, stage = '') {
      const fetch = $('#step-fetch'); const process = $('#step-process'); const pack = $('#step-package');
      [fetch, process, pack].forEach((node) => node.className = '');
      if (phase === 'done') { [fetch, process, pack].forEach((node) => node.classList.add('is-done')); return; }
      if (phase === 'error') { fetch.classList.add('is-active'); return; }
      const lower = String(stage).toLowerCase();
      if (phase === 'ready') { fetch.classList.add('is-active'); return; }
      if (lower.includes('packag') || lower.includes('archive')) { fetch.classList.add('is-done'); process.classList.add('is-done'); pack.classList.add('is-active'); }
      else if (lower.includes('merg') || lower.includes('process') || lower.includes('final') || lower.includes('prepar') || lower.includes('verif')) { fetch.classList.add('is-done'); process.classList.add('is-active'); }
      else { fetch.classList.add('is-active'); }
    }
    function readyTransferBoard(kind = state.active) {
      const transfer = $('#transfer'); transfer.hidden = false; transfer.classList.remove('is-done', 'is-error'); transfer.dataset.phase = 'ready';
      setText('#transfer-stage', 'Ready to transfer'); setText('#transfer-detail', `Selected ${kind} path is ready on demand.`); setText('#transfer-percent', '0%'); $('#transfer-bar').style.width = '0%';
      setText('#metric-item', 'Ready'); setText('#metric-data', 'Source path'); setText('#metric-rate', '—'); setText('#metric-time', '—'); setStepStates('ready'); $('#manual-download').hidden = true; $('#manual-download').removeAttribute('href');
    }
    function clearTransfer() {
      if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
      state.terminalJob = null; state.progressQueue = []; state.progressPlaying = false; state.visualProgress = 0; readyTransferBoard(); $('#export-button').disabled = false;
    }
    function setReading(reading) { readButton.disabled = reading; readButton.textContent = reading ? 'Inspecting…' : 'Inspect link'; }

    function renderQueue(entries) {
      const queue = $('#queue-preview'); queue.replaceChildren();
      entries.forEach((entry) => { const row = document.createElement('div'); const no = document.createElement('span'); no.textContent = `ITEM ${entry.index}`; const title = document.createElement('b'); title.textContent = entry.title; const duration = document.createElement('small'); duration.textContent = entry.duration; row.append(no, title, duration); queue.append(row); });
    }
    function loadThumbnail(url) {
      const image = $('#source-thumbnail'); const empty = $('#monitor-empty'); const tag = $('#monitor-tag'); const monitor = $('#monitor');
      if (!url) { monitor.classList.remove('has-preview'); image.hidden = true; empty.hidden = false; tag.hidden = true; return; }
      image.onload = () => { image.hidden = false; empty.hidden = true; tag.hidden = false; monitor.classList.remove('has-preview'); requestAnimationFrame(() => monitor.classList.add('has-preview')); };
      image.onerror = () => { monitor.classList.remove('has-preview'); image.hidden = true; empty.hidden = false; tag.hidden = true; };
      image.src = url;
    }
    function renderSource(payload) {
      state.type = payload.type;
      state.source = payload.source;
      state.formats = { video: [], audio: [], image: [], ...(payload.formats || {}) };
      state.profiles = { video: [], audio: [], image: [], ...(payload.profiles || {}) };
      const source = payload.source; const isPlaylist = payload.type === 'playlist';
      setText('#source-title', source.title); setText('#source-byline', source.uploader || 'Unknown source'); setText('#source-site', source.site || 'Media source'); setText('#source-runtime', source.duration || (isPlaylist ? 'Varies by item' : 'Not reported'));
      setText('#source-video-count', isPlaylist ? 'Profile mode' : `${state.formats.video.length} paths`);
      setText('#source-audio-count', isPlaylist ? 'Profile mode' : `${state.formats.audio.length} paths`);
      setText('#source-image-count', isPlaylist ? 'Source mode' : `${state.formats.image.length} paths`);
      const monitorTag = $('#monitor-tag'); monitorTag.textContent = String(source.content_type || (isPlaylist ? 'Playlist' : 'Media')).toUpperCase();
      $('#playlist-wrap').hidden = !isPlaylist; if (isPlaylist) renderQueue(payload.playlist.preview || []); loadThumbnail(source.thumbnail);
    }

    function folderKey(kind, item) {
      if (state.type === 'playlist') return `playlist-${kind}`;
      if (kind === 'video') { const height = Number(item.height || 0); return height ? `video-${height}` : 'video-other'; }
      if (kind === 'audio') { if (item.derived_from_video) return `audio-source-${item.source_resolution || item.id}`; const bitrate = Number(item.abr || 0); return bitrate ? `audio-${bitrate}` : 'audio-other'; }
      return `image-${String(item.container || 'source').toLowerCase()}`;
    }
    function folderLabel(kind, item) {
      if (state.type === 'playlist') return kind === 'video' ? 'Video quality ceilings' : kind === 'audio' ? 'Audio quality ceilings' : 'Image source profile';
      if (kind === 'video') { const height = Number(item.height || 0); return height ? `${height}p formats` : 'Other video formats'; }
      if (kind === 'audio') { if (item.derived_from_video) return `${item.source_resolution || 'Muxed'} source audio`; const bitrate = Number(item.abr || 0); return bitrate ? `${bitrate} kbps formats` : 'Other audio formats'; }
      return `${item.container || 'Source'} image formats`;
    }
    function folderGroups(kind) {
      const groups = new Map();
      items(kind).forEach((item) => {
        const key = folderKey(kind, item);
        if (!groups.has(key)) groups.set(key, { key, label: folderLabel(kind, item), items: [] });
        groups.get(key).items.push(item);
      });
      return [...groups.values()];
    }
    function selectItem(kind, item, group) {
      state[kind] = item.id;
      state.openFolders[kind].add(group.key);
      if (!state.eventSource) state.terminalJob = null;
      formatList.querySelectorAll('.format-row').forEach((row) => row.setAttribute('aria-pressed', String(row.dataset.id === item.id)));
      updateExport();
    }
    function makeDirectRow(kind, item, group) {
      const row = document.createElement('button'); row.type = 'button'; row.className = 'format-row'; row.dataset.id = item.id; row.setAttribute('aria-pressed', String(item.id === state[kind]));
      const mark = document.createElement('i'); mark.className = 'selector'; mark.setAttribute('aria-hidden', 'true');
      const copy = document.createElement('span'); const main = document.createElement('strong'); main.className = 'format-main'; main.textContent = item.label; const sub = document.createElement('small'); sub.className = 'format-sub';
      if (kind === 'video') sub.textContent = [item.container, item.video_codec, item.audio_codec || 'VIDEO'].filter(Boolean).join(' · ');
      else if (kind === 'audio') sub.textContent = item.derived_from_video ? `${item.container} · ${item.audio_codec} · embedded track` : `${item.container} · ${item.audio_codec}`;
      else sub.textContent = [item.container, item.width && item.height ? `${item.width} × ${item.height}` : 'source image'].filter(Boolean).join(' · ');
      copy.append(main, sub);
      const right = document.createElement('span'); right.className = 'format-right';
      if (kind !== 'video') { const tag = document.createElement('b'); tag.className = 'format-tag'; tag.textContent = kind === 'audio' ? 'audio' : 'image'; right.append(tag); }
      const size = document.createElement('span'); size.textContent = item.filesize || 'unknown'; right.append(size);
      row.append(mark, copy, right); row.addEventListener('click', () => selectItem(kind, item, group)); return row;
    }
    function makeProfileRow(kind, item, group) {
      const row = document.createElement('button'); row.type = 'button'; row.className = 'format-row'; row.dataset.id = item.id; row.setAttribute('aria-pressed', String(item.id === state[kind]));
      const mark = document.createElement('i'); mark.className = 'selector'; mark.setAttribute('aria-hidden', 'true'); const copy = document.createElement('span'); const main = document.createElement('strong'); main.className = 'format-main'; main.textContent = item.label; const sub = document.createElement('small'); sub.className = 'format-sub'; sub.textContent = item.detail; copy.append(main, sub); const right = document.createElement('span'); right.className = 'format-right'; const tag = document.createElement('b'); tag.className = 'format-tag'; tag.textContent = kind === 'video' ? 'video cap' : kind === 'audio' ? 'audio cap' : 'source'; right.append(tag); row.append(mark, copy, right); row.addEventListener('click', () => selectItem(kind, item, group)); return row;
    }
    function makeFolderChevron() {
      const ns = 'http://www.w3.org/2000/svg'; const icon = document.createElementNS(ns, 'svg'); icon.setAttribute('class', 'folder-caret'); icon.setAttribute('viewBox', '0 0 24 24'); icon.setAttribute('aria-hidden', 'true');
      const path = document.createElementNS(ns, 'path'); path.setAttribute('d', 'M6 9l6 6 6-6'); path.setAttribute('fill', 'none'); icon.append(path); return icon;
    }
    function makeFolder(kind, group, index) {
      const folder = document.createElement('section'); folder.className = 'format-folder'; folder.style.setProperty('--folder-index', String(index));
      const isOpen = state.openFolders[kind].has(group.key); const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'folder-toggle'; toggle.setAttribute('aria-expanded', String(isOpen));
      const caret = makeFolderChevron(); const label = document.createElement('b'); label.className = 'folder-title'; label.textContent = group.label; const count = document.createElement('span'); count.className = 'folder-count'; count.textContent = `${group.items.length} format${group.items.length === 1 ? '' : 's'}`; toggle.append(caret, label, count);
      const content = document.createElement('div'); content.className = 'folder-content'; content.hidden = !isOpen; group.items.forEach((item) => content.append(state.type === 'playlist' ? makeProfileRow(kind, item, group) : makeDirectRow(kind, item, group)));
      toggle.addEventListener('click', () => { const next = content.hidden; content.hidden = !next; toggle.setAttribute('aria-expanded', String(next)); if (next) state.openFolders[kind].add(group.key); else state.openFolders[kind].delete(group.key); });
      folder.append(toggle, content); return folder;
    }
    function renderList() {
      const kind = state.active; const groups = folderGroups(kind); formatList.replaceChildren(...groups.map((group, index) => makeFolder(kind, group, index)));
      const directLabel = kind === 'video' ? 'Video resolution folders' : kind === 'audio' ? 'Audio bitrate folders' : 'Image format folders';
      const profileLabel = kind === 'video' ? 'Video quality ceiling' : kind === 'audio' ? 'Audio quality ceiling' : 'Image source profile';
      setText('#table-label', state.type === 'playlist' ? profileLabel : directLabel); setText('#table-note', state.type === 'playlist' ? 'Closest match per item' : 'Open a folder');
    }
    function updateTabs() {
      const kinds = ['video', 'audio', 'image'];
      kinds.forEach((kind) => { const total = items(kind).length; setText(`#${kind}-count`, String(total)); const tab = $(`#tab-${kind}`); tab.disabled = total === 0; tab.setAttribute('aria-selected', String(state.active === kind)); tab.tabIndex = state.active === kind ? 0 : -1; });
      const panel = $('#format-list'); if (panel) panel.setAttribute('aria-labelledby', `tab-${state.active}`);
    }
    function updateExport() {
      const kind = state.active; const item = chosen(kind); if (!item) { $('#export-empty').hidden = false; $('#export-content').hidden = true; $('#export-button').disabled = true; return; }
      $('#export-empty').hidden = true; $('#export-content').hidden = false; $('#export-button').disabled = false; setText('#selection-kind', `${kind} selection`); setText('#selection-title', item.label);
      if (kind === 'video') {
        if (state.type === 'playlist') { setText('#selection-detail', item.detail); setText('#delivery-value', 'ZIP archive'); setText('#estimate-value', 'Per-item source size'); setText('#compatibility-note', 'Each playlist item receives the closest available rendition at or below this ceiling.'); setText('#export-button', 'Build video playlist archive'); }
        else { setText('#selection-detail', [item.container, item.video_codec, item.audio_codec || 'video'].filter(Boolean).join(' · ')); setText('#delivery-value', `${item.has_audio ? item.container : item.merge_container} delivery`); setText('#estimate-value', item.filesize || 'Not reported'); setText('#compatibility-note', 'Optimized source video delivery preserves the selected rendition.'); setText('#export-button', 'Download video'); }
      } else if (kind === 'audio') {
        if (state.type === 'playlist') { setText('#selection-detail', item.detail); setText('#delivery-value', 'ZIP archive'); setText('#estimate-value', 'Per-item source size'); setText('#compatibility-note', 'The selected source-quality profile is applied to each available playlist item.'); setText('#export-button', 'Build audio playlist archive'); }
        else if (item.derived_from_video) { setText('#selection-detail', `${item.container} · ${item.audio_codec} · ${item.source_resolution || 'source'} track`); setText('#delivery-value', 'Extracted audio'); setText('#estimate-value', 'Derived from selected playable stream'); setText('#compatibility-note', 'This social-media source exposes audio inside its video stream, so Hydro extracts a clean audio file automatically.'); setText('#export-button', 'Download audio'); }
        else { setText('#selection-detail', `${item.container} · ${item.audio_codec}`); setText('#delivery-value', 'Source audio'); setText('#estimate-value', item.filesize || 'Not reported'); setText('#compatibility-note', 'The selected source audio stream is delivered without an output-format selector.'); setText('#export-button', 'Download audio'); }
      } else {
        if (state.type === 'playlist') { setText('#selection-detail', item.detail); setText('#delivery-value', 'ZIP archive'); setText('#estimate-value', 'Per-item source size'); setText('#compatibility-note', 'Original image source files are collected from each available playlist item.'); setText('#export-button', 'Build image playlist archive'); }
        else { setText('#selection-detail', [item.container, item.width && item.height ? `${item.width} × ${item.height}` : 'source image'].filter(Boolean).join(' · ')); setText('#delivery-value', `${item.container} source image`); setText('#estimate-value', item.filesize || 'Not reported'); setText('#compatibility-note', 'The source image is preserved without re-encoding.'); setText('#export-button', 'Download image'); }
      }
      if (!state.eventSource && !state.terminalJob) readyTransferBoard(kind);
    }
    function activate(kind) { if (!items(kind).length) return; state.active = kind; if (!state.eventSource) state.terminalJob = null; updateTabs(); renderList(); updateExport(); }
    function renderCatalog(payload) {
      state.token = payload.token; state.video = payload.defaults.video; state.audio = payload.defaults.audio; state.image = payload.defaults.image; state.openFolders = { video: new Set(), audio: new Set(), image: new Set() }; state.visualProgress = 0; renderSource(payload);
      ['video', 'audio', 'image'].forEach((kind) => { const value = chosen(kind); if (value) state.openFolders[kind].add(folderKey(kind, value)); });
      const activeKind = ['video', 'audio', 'image'].find((kind) => items(kind).length > 0) || 'video'; state.active = activeKind; $('#catalog-empty').hidden = true; $('#catalog-content').hidden = false; setText('#catalog-title', state.type === 'playlist' ? 'Queue quality' : 'Source paths'); setText('#catalog-mode', state.type === 'playlist' ? `${payload.playlist.count} items · profile applies to each entry` : 'Formats sorted into resolution, bitrate, and image folders'); setText('#catalog-count', state.type === 'playlist' ? 'One ZIP delivery' : `${items('video').length + items('audio').length + items('image').length} paths`); updateTabs(); renderList(); updateExport();
    }
    async function payloadOf(response) { const data = await response.json().catch(() => ({})); if (!response.ok) throw new Error(data.error || 'The request could not be completed.'); return data; }

    form.addEventListener('submit', async (event) => {
      event.preventDefault(); const url = $('#source-url').value.trim(); if (!url) { showError('Paste a media page or playlist link first.'); $('#source-url').focus(); return; }
      clearError(); clearTransfer(); resetMonitor(); $('#catalog-empty').hidden = false; $('#catalog-content').hidden = true; $('#export-empty').hidden = false; $('#export-content').hidden = true; $('#export-button').disabled = true; setText('#export-state', 'Inspecting'); setReading(true);
      const data = new FormData(); data.append('url', url); if ($('#cookie-file').files[0]) data.append('cookies', $('#cookie-file').files[0]);
      try { const response = await fetch('/api/inspect', { method: 'POST', body: data }); renderCatalog(await payloadOf(response)); }
      catch (error) { setText('#source-title', 'No source loaded'); setText('#source-byline', 'Try a direct public media or playlist URL.'); showError(error.message || 'Could not inspect that link.'); }
      finally { setReading(false); }
    });
    $('#tab-video').addEventListener('click', () => activate('video')); $('#tab-audio').addEventListener('click', () => activate('audio')); $('#tab-image').addEventListener('click', () => activate('image'));
    const tabButtons = ['video', 'audio', 'image'].map((kind) => $(`#tab-${kind}`));
    function tabKeydown(event) {
      const enabled = tabButtons.filter((tab) => !tab.disabled);
      const index = enabled.indexOf(document.activeElement);
      if (index === -1) return;
      let next = null;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = enabled[(index + 1) % enabled.length];
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = enabled[(index - 1 + enabled.length) % enabled.length];
      else if (event.key === 'Home') next = enabled[0];
      else if (event.key === 'End') next = enabled[enabled.length - 1];
      if (next) { event.preventDefault(); activate(next.id.replace('tab-', '')); next.focus(); }
    }
    tabButtons.forEach((tab) => tab.addEventListener('keydown', tabKeydown));
    $('#cookie-file').addEventListener('change', () => setText('#cookie-name', $('#cookie-file').files[0] ? $('#cookie-file').files[0].name : 'No file'));

    function updateTransfer(job) {
      const transfer = $('#transfer'); const rawProgress = job.progress === null || job.progress === undefined ? null : Math.round(job.progress); if (rawProgress !== null) state.visualProgress = Math.max(state.visualProgress || 0, rawProgress); const progress = rawProgress === null ? null : state.visualProgress; const stage = job.stage || 'Preparing transfer';
      transfer.classList.remove('is-done', 'is-error'); transfer.dataset.phase = 'working'; setText('#export-state', job.status === 'running' ? 'Flowing' : job.status); setText('#transfer-stage', stage);
      const detail = []; if (job.item_index && job.item_total) detail.push(`item ${job.item_index}/${job.item_total}`); if (job.downloaded_bytes) detail.push(`${bytes(job.downloaded_bytes)} received`); if (job.total_bytes) detail.push(`of ${bytes(job.total_bytes)}`); if (job.speed) detail.push(`${bytes(job.speed)}/s`); if (job.eta !== null && job.eta !== undefined) detail.push(eta(job.eta));
      setText('#transfer-detail', detail.length ? detail.join(' · ') : 'Opening the selected source path.'); setText('#transfer-percent', progress === null ? '…' : `${progress}%`); $('#transfer-bar').style.width = `${Math.max(2, progress === null ? 7 : progress)}%`;
      setText('#metric-item', job.item_index && job.item_total ? `${job.item_index} / ${job.item_total}` : 'Single'); setText('#metric-data', job.downloaded_bytes ? `${bytes(job.downloaded_bytes)}${job.total_bytes ? ` / ${bytes(job.total_bytes)}` : ''}` : 'Connecting'); setText('#metric-rate', job.speed ? `${bytes(job.speed)}/s` : '—'); setText('#metric-time', job.eta !== null && job.eta !== undefined ? eta(job.eta) : 'Estimating'); setStepStates('working', stage);
    }
    function finishTransfer(job) {
      if (state.terminalJob === job.id) return; state.terminalJob = job.id; state.progressQueue = []; state.progressPlaying = false; if (state.eventSource) { state.eventSource.close(); state.eventSource = null; } $('#export-button').disabled = false; const transfer = $('#transfer');
      if (job.status === 'complete') {
        state.visualProgress = 100; transfer.classList.remove('is-error'); transfer.classList.add('is-done'); transfer.dataset.phase = 'done'; setStepStates('done'); setText('#export-state', 'Ready'); setText('#transfer-stage', job.stage || 'File ready'); setText('#transfer-detail', 'Your browser download should begin now.'); setText('#transfer-percent', '100%'); $('#transfer-bar').style.width = '100%'; setText('#metric-item', job.files_ready ? `${job.files_ready} ready` : 'Ready'); setText('#metric-data', job.download_name || 'File assembled'); setText('#metric-rate', 'Complete'); setText('#metric-time', 'Now'); const manual = $('#manual-download'); manual.href = `/api/jobs/${encodeURIComponent(job.id)}/file`; manual.hidden = false; setTimeout(() => { const link = document.createElement('a'); link.href = manual.href; link.click(); }, 250);
      } else {
        transfer.classList.remove('is-done'); transfer.classList.add('is-error'); transfer.dataset.phase = 'error'; setStepStates('error'); setText('#export-state', 'Stopped'); setText('#transfer-stage', 'Transfer stopped'); setText('#transfer-detail', job.error || 'Inspect the link again or choose another path.'); setText('#transfer-percent', '—'); $('#transfer-bar').style.width = '2%'; setText('#metric-item', 'Stopped'); setText('#metric-data', 'No file delivered'); setText('#metric-rate', '—'); setText('#metric-time', 'Try again');
      }
    }
    function playProgressQueue() {
      if (!state.progressQueue.length) { state.progressPlaying = false; return; }
      state.progressPlaying = true;
      const job = state.progressQueue.shift(); updateTransfer(job);
      const terminal = job.status === 'complete' || job.status === 'failed';
      if (terminal) { setTimeout(() => finishTransfer(job), 180); return; }
      // Yield to the renderer between recorded hook snapshots so fast files still
      // visibly pass through real fetch/process/package states.
      setTimeout(playProgressQueue, state.progressQueue.length ? 170 : 0);
    }
    function queueTransferUpdate(job) {
      const terminal = job.status === 'complete' || job.status === 'failed';
      const last = state.progressQueue[state.progressQueue.length - 1];
      const moved = last && last.progress !== null && last.progress !== undefined && job.progress !== null && job.progress !== undefined ? Math.abs(job.progress - last.progress) : 99;
      if (last && !terminal && last.status !== 'complete' && last.status !== 'failed' && last.stage === job.stage && moved < 8) state.progressQueue[state.progressQueue.length - 1] = job;
      else if (!last || last.revision !== job.revision) { if (state.progressQueue.length >= 48 && !terminal) state.progressQueue[state.progressQueue.length - 1] = job; else state.progressQueue.push(job); }
      if (!state.progressPlaying) playProgressQueue();
    }
    function follow(jobId) {
      if (state.eventSource) state.eventSource.close();
      const source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`); state.eventSource = source;
      source.addEventListener('progress', (event) => queueTransferUpdate(JSON.parse(event.data)));
      source.addEventListener('done', (event) => queueTransferUpdate(JSON.parse(event.data)));
      const releaseSource = () => { if (state.eventSource === source) { state.eventSource = null; source.close(); } $('#export-button').disabled = false; };
      source.addEventListener('closed', releaseSource);
      source.onerror = () => { if (source.readyState === EventSource.CLOSED) releaseSource(); };
    }
    async function beginExport() {
      const kind = state.active; const item = chosen(kind); if (!state.token || !item) { showError('Choose a source path first.'); return; }
      clearError(); clearTransfer(); $('#export-button').disabled = true; const transfer = $('#transfer'); transfer.dataset.phase = 'working'; setStepStates('working', 'Scheduling export'); setText('#export-state', 'Queued'); setText('#transfer-stage', 'Scheduling export'); setText('#transfer-detail', 'Opening the selected source path.'); setText('#transfer-percent', '…'); $('#transfer-bar').style.width = '7%'; setText('#metric-item', state.type === 'playlist' ? 'Queue' : 'Single'); setText('#metric-data', 'Connecting'); setText('#metric-rate', '—'); setText('#metric-time', 'Estimating');
      const request = { token: state.token, kind, audio_output: 'native' }; if (state.type === 'playlist') request.profile_id = item.id; else request.format_id = item.id;
      try { const response = await fetch('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request) }); follow((await payloadOf(response)).id); }
      catch (error) { finishTransfer({ id: `failed-${Date.now()}`, status: 'failed', error: error.message }); }
    }
    $('#export-button').addEventListener('click', beginExport);
  })();
  </script>
</body>
</html>"""

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_COOKIE_BYTES + 32 * 1024

app.config["PROPAGATE_EXCEPTIONS"] = False
app.logger.disabled = True
app.logger.propagate = False
logging.getLogger("werkzeug").disabled = True

@app.get("/")
def home() -> Response:
    response = Response(PAGE, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response

@app.get("/healthz")
def healthz() -> Response:
    return jsonify(
        {
            "ok": True,
            "runtime": "Google Colab-compatible",
            "ffmpeg": FFMPEG_READY,
            "parallel_jobs": MAX_CONCURRENT_JOBS,
            "fragment_workers": FRAGMENT_WORKERS,
        }
    )

@app.get("/api/inspections/<token>/thumbnail")
def inspection_thumbnail(token: str) -> Response:

    inspection = store.get_inspection(token)
    remote_url = inspection.thumbnail_url if inspection else None
    if not remote_url:
        raise APIError("That preview is unavailable or its source catalog expired.", 404)
    try:
        remote_url = validate_remote_fetch_url(remote_url)
    except APIError:
        raise APIError("That preview is unavailable or its source catalog expired.", 404) from None
    parsed = urlparse(remote_url)
    try:
        request_object = urllib.request.Request(
            remote_url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(request_object, timeout=12) as remote:
            payload = remote.read(8 * 1024 * 1024 + 1)
            content_type = remote.headers.get_content_type() if hasattr(remote.headers, "get_content_type") else "image/jpeg"
        if len(payload) > 8 * 1024 * 1024:
            raise APIError("The source preview image is too large to display.", 413)
        if not str(content_type).startswith("image/"):
            content_type = "image/jpeg"
        response = Response(payload, mimetype=content_type)
        response.headers["Cache-Control"] = "private, max-age=900"
        return response
    except APIError:
        raise
    except Exception as exc:
        raise APIError("The source preview could not be loaded. The download options still work.", 502) from exc

@app.post("/api/inspect")
def inspect_source() -> Response:
    from flask import request as current_request
    url = validate_source_url(current_request.form.get("url"))
    token = secrets.token_urlsafe(24)
    directory = TEMP_ROOT / "inspections" / token
    directory.mkdir(parents=True, exist_ok=False)
    try:
        cookie_path = save_cookie_upload(directory)
        try:
            with yt_dlp.YoutubeDL(metadata_options(cookie_path)) as downloader:
                info = downloader.extract_info(url, download=False)
        except Exception as extractor_error:

            info = public_preview_image_info(url)
            if info is None:
                raise extractor_error
        if not isinstance(info, dict):
            raise APIError("The source did not return a media item. Use a direct media-page or playlist URL.")

        title = clean_short_text(info.get("title") or "Untitled media", 180) or "Untitled media"
        uploader = clean_short_text(info.get("uploader") or info.get("channel") or info.get("creator") or "Unknown source", 120)
        site = clean_short_text(str(info.get("extractor_key") or info.get("extractor") or "Media source").replace("_", " "), 40)
        thumbnail = source_thumbnail(info)
        direct_media_url = info.get("_hydro_direct_media_url") if isinstance(info.get("_hydro_direct_media_url"), str) else None
        direct_media_referer = info.get("_hydro_direct_media_referer") if isinstance(info.get("_hydro_direct_media_referer"), str) else None

        thumbnail_endpoint = f"/api/inspections/{token}/thumbnail" if thumbnail else None
        if is_playlist_info(info):
            count, duration, preview = playlist_summary(info)
            if not count:
                raise APIError("This playlist did not report any accessible items.")
            inspection = Inspection(
                token=token,
                url=url,
                directory=directory,
                created_at=time.time(),
                expires_at=time.time() + INSPECTION_TTL,
                source_type="playlist",
                title=title,
                uploader=uploader,
                site=site,
                duration_seconds=duration,
                thumbnail_url=thumbnail,
                playlist_count=count,
                playlist_preview=preview,
                cookie_path=cookie_path,
            )
            store.add_inspection(inspection)
            return jsonify(
                {
                    "token": token,
                    "type": "playlist",
                    "source": {
                        "title": title,
                        "uploader": uploader,
                        "site": site,
                        "duration": human_duration(duration),
                        "thumbnail": thumbnail_endpoint,
                        "content_type": "Playlist",
                    },
                    "playlist": {"count": count, "preview": preview},
                    "formats": {"video": [], "audio": [], "image": []},
                    "profiles": {"video": VIDEO_PLAYLIST_PROFILES, "audio": AUDIO_PLAYLIST_PROFILES, "image": IMAGE_PLAYLIST_PROFILES},
                    "defaults": {"video": "best", "audio": "best", "image": "source"},
                }
            )

        videos, audios, images = normalize_formats(info)
        if not videos and not audios and not images and not direct_media_url:

            fallback_info = public_preview_image_info(url)
            if fallback_info:
                info = fallback_info
                title = clean_short_text(info.get("title") or "Public preview image", 180) or "Public preview image"
                uploader = clean_short_text(info.get("uploader") or "Public page", 120)
                site = clean_short_text(str(info.get("extractor_key") or "Public image preview"), 40)
                thumbnail = source_thumbnail(info)
                direct_media_url = info.get("_hydro_direct_media_url") if isinstance(info.get("_hydro_direct_media_url"), str) else None
                direct_media_referer = info.get("_hydro_direct_media_referer") if isinstance(info.get("_hydro_direct_media_referer"), str) else None
                thumbnail_endpoint = f"/api/inspections/{token}/thumbnail" if thumbnail else None
                videos, audios, images = normalize_formats(info)
        if not videos and not audios and not images:
            raise APIError("No downloadable video, audio, or image paths were reported for this item.")
        inspection = Inspection(
            token=token,
            url=url,
            directory=directory,
            created_at=time.time(),
            expires_at=time.time() + INSPECTION_TTL,
            source_type="single",
            title=title,
            uploader=uploader,
            site=site,
            duration_seconds=as_int(info.get("duration")),
            thumbnail_url=thumbnail,
            direct_media_url=direct_media_url,
            direct_media_referer=direct_media_referer,
            video_formats={item["id"]: item for item in videos},
            audio_formats={item["id"]: item for item in audios},
            image_formats={item["id"]: item for item in images},
            cookie_path=cookie_path,
        )
        store.add_inspection(inspection)
        video_rows = list(inspection.video_formats.values())
        audio_rows = list(inspection.audio_formats.values())
        image_rows = list(inspection.image_formats.values())
        preferred_video = next((item["id"] for item in video_rows if item["container"] == "MP4"), None)
        preferred_audio = next((item["id"] for item in audio_rows if item["container"] == "M4A"), None)
        return jsonify(
            {
                "token": token,
                "type": "single",
                "source": {
                    "title": title,
                    "uploader": uploader,
                    "site": site,
                    "duration": human_duration(inspection.duration_seconds),
                    "thumbnail": thumbnail_endpoint,
                    "content_type": content_type_label(video_rows, audio_rows, image_rows),
                },
                "formats": {"video": video_rows, "audio": audio_rows, "image": image_rows},
                "profiles": {"video": [], "audio": [], "image": []},
                "defaults": {
                    "video": preferred_video or (video_rows[0]["id"] if video_rows else None),
                    "audio": preferred_audio or (audio_rows[0]["id"] if audio_rows else None),
                    "image": image_rows[0]["id"] if image_rows else None,
                },
            }
        )
    except APIError as error:
        shutil.rmtree(directory, ignore_errors=True)
        return jsonify({"error": error.message}), error.status
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        return jsonify({"error": friendly_error(exc, "inspect this link")}), 400

@app.post("/api/jobs")
def create_job() -> Response:
    from flask import request as current_request
    data = current_request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIError("Send a selected video, audio, or image path as JSON.")
    token = str(data.get("token") or "")
    kind = str(data.get("kind") or "")
    audio_output = str(data.get("audio_output") or "native")
    inspection = store.get_inspection(token)
    if not inspection:
        raise APIError("This catalog expired. Read the source link again.", 410)
    if kind not in {"video", "audio", "image"}:
        raise APIError("Choose a video, audio, or image delivery.")
    if audio_output not in {"native", "m4a", "mp3"}:
        raise APIError("Choose source audio, M4A, or MP3 output.")
    if kind != "audio":
        audio_output = "native"
    if kind == "audio" and audio_output != "native" and not FFMPEG_READY:
        raise APIError("FFmpeg is unavailable in this runtime, so MP3/M4A conversion cannot run.", 503)
    if inspection.source_type == "playlist" and kind == "video" and not FFMPEG_READY:
        raise APIError("FFmpeg is unavailable in this runtime, so playlist video streams cannot be merged safely.", 503)

    selection: dict[str, str] = {"kind": kind, "audio_output": audio_output}
    if inspection.source_type == "playlist":
        profile_id = str(data.get("profile_id") or "")
        allowed = {"video": VIDEO_PROFILE_IDS, "audio": AUDIO_PROFILE_IDS, "image": IMAGE_PROFILE_IDS}[kind]
        if profile_id not in allowed:
            raise APIError("That playlist quality profile is not in the current catalog.")
        selection["profile_id"] = profile_id
    else:
        format_id = str(data.get("format_id") or "")
        allowed = {"video": inspection.video_formats, "audio": inspection.audio_formats, "image": inspection.image_formats}[kind]
        if format_id not in allowed:
            raise APIError("That source path is no longer in the current catalog. Read the link again.")
        if kind == "video" and not FFMPEG_READY and not inspection.video_formats[format_id]["has_audio"]:
            raise APIError("FFmpeg is unavailable in this runtime, so video-only paths cannot be merged with audio.", 503)
        if kind == "audio" and not FFMPEG_READY and inspection.audio_formats[format_id].get("derived_from_video"):
            raise APIError("FFmpeg is unavailable in this runtime, so this embedded social-media audio track cannot be extracted.", 503)
        selection["format_id"] = format_id

    job = store.create_job(inspection, selection)
    threading.Thread(target=run_download, args=(job.job_id,), name=f"signal-transfer-{job.job_id[:7]}", daemon=True).start()
    return jsonify(store.snapshot(job)), 202

@app.get("/api/jobs/<job_id>/events")
def job_events(job_id: str) -> Response:
    if not store.get_job(job_id):
        raise APIError("That transfer is no longer available.", 404)

    @stream_with_context
    def events() -> Any:

        last_revision = 0
        terminal_at_open = False
        with store.changed:
            opening_job = store.jobs.get(job_id)
            if opening_job:
                terminal_at_open = opening_job.status in {"complete", "failed"}
        while True:
            heartbeat = False
            with store.changed:
                job = store.jobs.get(job_id)
                if not job:
                    pending: list[tuple[int, dict[str, Any]]] = []
                    terminal = True
                    missing = True
                else:
                    pending = [(revision, snapshot) for revision, snapshot in job.history if revision > last_revision]
                    terminal = job.status in {"complete", "failed"}
                    missing = False
                    if terminal_at_open:
                        pending = pending[-1:] if pending else []
                    if not pending and not terminal:
                        heartbeat = not store.changed.wait(timeout=12)
            if missing:
                yield "event: closed\ndata: {}\n\n"
                return
            if pending:
                for revision, snapshot in pending:
                    last_revision = revision
                    packed = json.dumps(snapshot, separators=(",", ":"))
                    yield f"event: progress\ndata: {packed}\n\n"
                if terminal:
                    final = pending[-1][1]
                    yield f"event: done\ndata: {json.dumps(final, separators=(',', ':'))}\n\n"
                    return
            elif terminal:

                job = store.get_job(job_id)
                if not job:
                    yield "event: closed\ndata: {}\n\n"
                    return
                final = store.snapshot(job)
                yield f"event: progress\ndata: {json.dumps(final, separators=(',', ':'))}\n\n"
                yield f"event: done\ndata: {json.dumps(final, separators=(',', ':'))}\n\n"
                return
            elif heartbeat:
                yield ": keep-alive\n\n"

    response = Response(events(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response

@app.get("/api/jobs/<job_id>/file")
def download_result(job_id: str) -> Response:
    job = store.get_job(job_id)
    if not job or job.status != "complete" or not job.result_path or not job.result_path.exists():
        raise APIError("That file is not ready or has expired. Read the source and transfer it again.", 404)
    response = send_file(job.result_path, as_attachment=True, download_name=job.download_name, conditional=True)
    response.headers["Cache-Control"] = "private, no-store"
    return response

@app.errorhandler(APIError)
def api_error(error: APIError) -> tuple[Response, int]:
    return jsonify({"error": error.message}), error.status

@app.errorhandler(RequestEntityTooLarge)
def too_large(_: RequestEntityTooLarge) -> tuple[Response, int]:
    return jsonify({"error": "The optional cookies.txt upload is too large. The limit is 2 MB."}), 413

@app.errorhandler(404)
def not_found(_: Exception) -> Response:
    from flask import request as current_request
    if current_request.path.startswith("/api/"):
        return jsonify({"error": "That API route was not found."}), 404
    return Response("Not found", status=404, mimetype="text/plain")

OWN_TUNNEL: subprocess.Popen[str] | None = None
OWN_SERVER: Any | None = None
OWN_PORT: int | None = None

def write_instance(port: int, tunnel_pid: int) -> None:
    payload = {"runner_pid": os.getpid(), "tunnel_pid": tunnel_pid, "port": port, "started_at": time.time()}
    temporary = INSTANCE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload))
    temporary.replace(INSTANCE_FILE)

def stop_own_instance() -> None:
    global OWN_TUNNEL, OWN_SERVER
    if OWN_SERVER:
        try:
            OWN_SERVER.shutdown()
        except Exception:
            pass
        OWN_SERVER = None
    if OWN_TUNNEL and OWN_TUNNEL.poll() is None:
        OWN_TUNNEL.terminate()
        try:
            OWN_TUNNEL.wait(timeout=4)
        except subprocess.TimeoutExpired:
            OWN_TUNNEL.kill()
    remove_instance_if_owned()

atexit.register(stop_own_instance)

class SilentRequestHandler(WSGIRequestHandler):

    def log_request(self, _code: Any = "-", _size: Any = "-") -> None:
        pass

    def log_error(self, _format: str, *args: Any) -> None:
        pass

    def log_message(self, _format: str, *args: Any) -> None:
        pass

def available_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("0.0.0.0", 0))
        return int(probe.getsockname()[1])

def port_is_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False

def start_server(port: int) -> int:

    global OWN_SERVER
    active_port = port
    if not port_is_free(active_port):
        active_port = available_local_port()
        print(f"• Port {port} belongs to an older notebook app; Hydro moved to private local port {active_port}.", flush=True)
    try:
        server = make_server("0.0.0.0", active_port, app, threaded=True, request_handler=SilentRequestHandler)
    except (OSError, SystemExit) as exc:

        active_port = available_local_port()
        try:
            server = make_server("0.0.0.0", active_port, app, threaded=True, request_handler=SilentRequestHandler)
        except (OSError, SystemExit) as retry_error:
            raise RuntimeError("Hydro could not reserve a local web port. Restart the Colab runtime, then run main.py again.") from retry_error
    OWN_SERVER = server
    threading.Thread(target=server.serve_forever, name="hydro-web", daemon=True).start()
    return active_port

def launch_quick_tunnel(port: int) -> subprocess.Popen[str]:
    binary = cloudflared_path()
    process = subprocess.Popen(
        [binary, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    if not process.stdout:
        raise RuntimeError("Cloudflare tunnel did not expose a readable output stream.")
    url_queue: queue.Queue[str] = queue.Queue(maxsize=1)
    log_queue: queue.Queue[str] = queue.Queue()

    def read_tunnel_output() -> None:
        for line in process.stdout:
            clean = line.strip()
            if clean:
                log_queue.put(clean)
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line, flags=re.I)
            if match:
                try:
                    url_queue.put_nowait(match.group(0))
                except queue.Full:
                    pass

    threading.Thread(target=read_tunnel_output, name="signal-tunnel-log", daemon=True).start()
    deadline = time.time() + 55
    public_url: str | None = None
    while time.time() < deadline:
        if process.poll() is not None:
            lines: list[str] = []
            while not log_queue.empty():
                lines.append(log_queue.get_nowait())
            raise RuntimeError("Cloudflare Quick Tunnel stopped before publishing a URL.\n" + "\n".join(lines[-8:]))
        try:
            public_url = url_queue.get(timeout=.5)
            break
        except queue.Empty:
            continue
    if not public_url:
        process.terminate()
        raise RuntimeError("Cloudflare Quick Tunnel did not return a URL in time. Re-run the Colab cell.")

    print("\n┌──────────────────────────────────────────────────────────────────┐")
    print("│  HYDRO IS READY                                                   │")
    print("├──────────────────────────────────────────────────────────────────┤")
    print(f"│  {public_url:<64} │")
    print("├──────────────────────────────────────────────────────────────────┤")
    print(f"│  Colab tuning: {MAX_CONCURRENT_JOBS} transfer jobs · {FRAGMENT_WORKERS} fragment lanes{' ' * max(0, 20 - len(str(MAX_CONCURRENT_JOBS)) - len(str(FRAGMENT_WORKERS)))}│")
    print("│  Keep this cell running. Re-running main.py closes this old tunnel. │")
    print("└──────────────────────────────────────────────────────────────────┘\n")
    return process

def main() -> None:
    global OWN_TUNNEL, OWN_PORT
    parser = argparse.ArgumentParser(description="Run Hydro in a free Google Colab session.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-tunnel", action="store_true", help="Run Flask only for local troubleshooting.")

    args, _kernel_args = parser.parse_known_args()
    prepare_runtime()
    print(f"{APP_NAME} · starting free Colab runtime (FFmpeg: {'ready' if FFMPEG_READY else 'unavailable'})", flush=True)
    active_port = start_server(args.port)
    OWN_PORT = active_port
    if args.no_tunnel:
        print(f"Local server ready at http://127.0.0.1:{active_port}. Press Ctrl+C to stop.", flush=True)
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            return
    OWN_TUNNEL = launch_quick_tunnel(active_port)
    write_instance(active_port, OWN_TUNNEL.pid)
    try:
        while OWN_TUNNEL.poll() is None:
            time.sleep(1)
        raise RuntimeError("The temporary Cloudflare tunnel stopped. Re-run main.py to create a fresh free URL.")
    except KeyboardInterrupt:
        print("\nHydro stopped. The temporary tunnel is closing.", flush=True)

if __name__ == "__main__":
    main()
