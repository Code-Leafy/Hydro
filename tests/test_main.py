"""Tests for Hydro's single-file Flask media downloader.

The suite is deterministic: yt_dlp.YoutubeDL is replaced with a fake extractor
and all scratch state lives under a sandboxed SIGNAL_TEMP_DIR, so nothing here
touches the network or the machine's real temp directory.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_ROOT.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="hydro-test-scratch-"))
os.environ["SIGNAL_TEMP_DIR"] = str(SCRATCH)
sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_store():
    """Every test starts from an empty store; teardown removes jobs/inspections."""
    with main.store.lock:
        main.store.jobs.clear()
        main.store.inspections.clear()
    yield
    with main.store.lock:
        main.store.jobs.clear()
        main.store.inspections.clear()
    for job_dir in (SCRATCH / "signal-shelf" / "jobs").glob("*"):
        shutil.rmtree(job_dir, ignore_errors=True)
    for inspection_dir in (SCRATCH / "signal-shelf" / "inspections").glob("*"):
        shutil.rmtree(inspection_dir, ignore_errors=True)


@pytest.fixture()
def client():
    return main.app.test_client()


def wait_for(predicate, timeout=10.0, interval=0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# URL validation (SSRF guards)
# ---------------------------------------------------------------------------

def test_validate_source_url_accepts_public_links():
    assert main.validate_source_url("https://www.youtube.com/watch?v=abc123") == "https://www.youtube.com/watch?v=abc123"
    assert main.validate_source_url("https://example.com/pin/12345/") == "https://example.com/pin/12345/"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "ftp://example.com/file.mp4",
        "file:///etc/passwd",
        "http://127.0.0.1/x",
        "http://10.0.0.5/x",
        "http://192.168.1.10/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/x",
        "http://localhost:5000/x",
        "http://myserver.local/x",
        "https://user:pass@example.com/x",
        "http://" + "a" * 3000,
    ],
)
def test_validate_source_url_rejects(url):
    with pytest.raises(main.APIError):
        main.validate_source_url(url)


def test_validate_remote_fetch_url():
    assert main.validate_remote_fetch_url("https://i.ytimg.com/vi/x/hqdefault.jpg") == "https://i.ytimg.com/vi/x/hqdefault.jpg"
    assert main.validate_remote_fetch_url("http://cdn.example.com/a.jpg") == "http://cdn.example.com/a.jpg"
    with pytest.raises(main.APIError):
        main.validate_remote_fetch_url("")
    with pytest.raises(main.APIError):
        main.validate_remote_fetch_url("file:///etc/passwd")
    with pytest.raises(main.APIError):
        main.validate_remote_fetch_url("http://127.0.0.1/secret")
    with pytest.raises(main.APIError):
        main.validate_remote_fetch_url("http://169.254.169.254/latest/meta-data/")


# ---------------------------------------------------------------------------
# Formatters and small helpers
# ---------------------------------------------------------------------------

def test_human_bytes():
    assert main.human_bytes(0) is None
    assert main.human_bytes(None) is None
    assert main.human_bytes(-5) is None
    assert main.human_bytes(500) == "500 B"
    assert main.human_bytes(2048) == "2 KB"
    assert main.human_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert main.human_bytes("1024") == "1 KB"


def test_human_duration():
    assert main.human_duration(59) == "0:59"
    assert main.human_duration(65) == "1:05"
    assert main.human_duration(3661) == "1:01:01"
    assert main.human_duration(-1) is None
    assert main.human_duration(None) is None


def test_resolution_label():
    assert main.resolution_label(2160, 3840, 30) == "2160p · 4K"
    assert main.resolution_label(1080, 1920, 60) == "1080p · FHD · 60 fps"
    assert main.resolution_label(0, 1280, 24) == "1280px wide"
    assert main.resolution_label(0, 0, 0) == "Source resolution"


def test_compact_codec():
    assert main.compact_codec("avc1.64001F") == "AVC1"
    assert main.compact_codec("mp4a.40.2") == "MP4A"
    assert main.compact_codec(None) is None
    assert main.compact_codec("none") is None


def test_friendly_error_mappings():
    assert "cookies" in main.friendly_error(Exception("Sign in to confirm you're not a bot"), "inspect")
    assert "blocked" in main.friendly_error(Exception("HTTP Error 403: Forbidden"), "inspect")
    assert "FFmpeg" in main.friendly_error(Exception("ffmpeg not found while merging"), "merge")
    assert "Read the link again" in main.friendly_error(Exception("requested format is not available"), "download")
    assert "Check the link" in main.friendly_error(Exception("some unrelated breakage"), "download")


# ---------------------------------------------------------------------------
# normalize_formats
# ---------------------------------------------------------------------------

FAKE_SINGLE_INFO = {
    "id": "abc123",
    "title": "Sample Video",
    "uploader": "Example Creator",
    "extractor_key": "YouTube",
    "ext": "mp4",
    "duration": 300,
    "thumbnail": "https://cdn.example.com/thumb.jpg",
    "formats": [
        {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42E01E", "acodec": "mp4a.40.2", "width": 640, "height": 360, "fps": 30, "filesize": 1_000_000, "format_note": "360p"},
        {"format_id": "22", "ext": "mp4", "vcodec": "avc1.64001F", "acodec": "mp4a.40.2", "width": 1280, "height": 720, "fps": 30, "filesize": 4_000_000, "format_note": "720p"},
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128, "filesize": 500_000, "format_note": "audio only"},
        {"format_id": "sb1", "ext": "jpg", "vcodec": "none", "acodec": "none", "width": 640, "height": 360, "format_note": "storyboard"},
    ],
}

FAKE_PLAYLIST_INFO = {
    "_type": "playlist",
    "id": "pl1",
    "title": "Sample Playlist",
    "uploader": "Example Creator",
    "extractor_key": "YouTube",
    "playlist_count": 2,
    "entries": [
        {"id": "item1", "title": "Item One", "duration": 100, "playlist_index": 1},
        {"id": "item2", "title": "Item Two", "duration": 200, "playlist_index": 2},
    ],
}


def test_normalize_formats_classifies_and_sorts():
    videos, audios, images = main.normalize_formats(FAKE_SINGLE_INFO)
    assert [v["id"] for v in videos] == ["22", "18"]  # height desc
    assert videos[0]["has_audio"] is True
    assert videos[0]["container"] == "MP4"
    assert [a["id"] for a in audios] == ["140"]
    assert images == []  # storyboard thumbnails are skipped


def test_normalize_formats_derives_audio_from_muxed_video():
    info = {
        "id": "x",
        "title": "t",
        "extractor_key": "Instagram",
        "ext": "mp4",
        "formats": [
            {"format_id": "h264", "ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "mp4a.40.2", "width": 720, "height": 1280, "fps": 30, "filesize": 2_000_000, "format_note": "muxed"},
        ],
    }
    videos, audios, images = main.normalize_formats(info)
    assert len(videos) == 1
    assert len(audios) == 1
    derived = audios[0]
    assert derived["derived_from_video"] is True
    assert derived["id"] == "h264"
    assert derived["extract_codec"] == "m4a"


def test_normalize_formats_direct_image_source():
    info = {"id": "img", "title": "t", "extractor_key": "SomeSite", "ext": "jpg", "url": "https://cdn.example.com/a.jpg", "formats": []}
    videos, audios, images = main.normalize_formats(info)
    assert videos == [] and audios == []
    assert len(images) == 1
    assert images[0]["source_direct"] is True


# ---------------------------------------------------------------------------
# export_options
# ---------------------------------------------------------------------------

def _inspection(**overrides):
    defaults = {
        "token": "t1",
        "url": "https://example.com/x",
        "directory": main.TEMP_ROOT / "inspections" / "t1",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
        "source_type": "single",
        "title": "T",
        "uploader": "U",
        "site": "S",
        "duration_seconds": 10,
    }
    defaults.update(overrides)
    return main.Inspection(**defaults)


def _job(inspection, selection):
    return main.DownloadJob(
        job_id="job1",
        inspection_token=inspection.token,
        directory=main.TEMP_ROOT / "jobs" / "job1",
        selection=selection,
        created_at=time.time(),
        expires_at=time.time() + main.JOB_TTL,
    )


def test_export_options_single_video_has_audio():
    inspection = _inspection(video_formats={"22": {"id": "22", "has_audio": True, "merge_container": "MP4", "container": "MP4"}})
    options = main.export_options(inspection, _job(inspection, {"kind": "video", "audio_output": "native", "format_id": "22"}))
    assert options["format"] == "22"
    assert "merge_output_format" not in options
    assert options["noplaylist"] is True


def test_export_options_single_video_merge_mp4():
    inspection = _inspection(video_formats={"137": {"id": "137", "has_audio": False, "merge_container": "MP4"}})
    options = main.export_options(inspection, _job(inspection, {"kind": "video", "audio_output": "native", "format_id": "137"}))
    assert options["format"] == "137+bestaudio[ext=m4a]/bestaudio"
    assert options["merge_output_format"] == "mp4"


def test_export_options_single_video_merge_mkv():
    inspection = _inspection(video_formats={"248": {"id": "248", "has_audio": False, "merge_container": "MKV"}})
    options = main.export_options(inspection, _job(inspection, {"kind": "video", "audio_output": "native", "format_id": "248"}))
    assert options["format"] == "248+bestaudio/best"
    assert options["merge_output_format"] == "mkv"


def test_export_options_audio_native_skips_postprocessor():
    inspection = _inspection(audio_formats={"140": {"id": "140", "container": "M4A", "audio_codec": "AAC"}})
    options = main.export_options(inspection, _job(inspection, {"kind": "audio", "audio_output": "native", "format_id": "140"}))
    assert "postprocessors" not in options


def test_export_options_audio_mp3_adds_postprocessor():
    inspection = _inspection(audio_formats={"140": {"id": "140", "container": "M4A", "audio_codec": "AAC"}})
    options = main.export_options(inspection, _job(inspection, {"kind": "audio", "audio_output": "mp3", "format_id": "140"}))
    processor = options["postprocessors"][0]
    assert processor["key"] == "FFmpegExtractAudio"
    assert processor["preferredcodec"] == "mp3"
    assert processor["preferredquality"] == "0"


def test_export_options_derived_audio():
    inspection = _inspection(audio_formats={"18": {"id": "18", "derived_from_video": True, "extract_codec": "m4a", "container": "M4A"}})
    options = main.export_options(inspection, _job(inspection, {"kind": "audio", "audio_output": "native", "format_id": "18"}))
    assert options["postprocessors"][0]["key"] == "FFmpegExtractAudio"
    assert options["postprocessors"][0]["preferredcodec"] == "m4a"


def test_export_options_playlist_profiles():
    inspection = _inspection(source_type="playlist", playlist_count=5)
    video = main.export_options(inspection, _job(inspection, {"kind": "video", "audio_output": "native", "profile_id": "1080"}))
    assert video["format"] == "(bestvideo[height<=1080]+bestaudio)/best[height<=1080]"
    assert video["merge_output_format"] == "mkv"
    assert video["noplaylist"] is False

    audio = main.export_options(inspection, _job(inspection, {"kind": "audio", "audio_output": "mp3", "profile_id": "320"}))
    assert audio["format"] == "bestaudio[abr<=320]/bestaudio/best"
    assert audio["postprocessors"][0]["preferredcodec"] == "mp3"

    image = main.export_options(inspection, _job(inspection, {"kind": "image", "audio_output": "native", "profile_id": "source"}))
    assert image["format"] == "best"


# ---------------------------------------------------------------------------
# playlist_summary / media_files / archive_playlist
# ---------------------------------------------------------------------------

def test_playlist_summary():
    info = {
        "_type": "playlist",
        "playlist_count": 2,
        "entries": [
            {"id": "a", "title": "First Item", "duration": 90, "playlist_index": 1},
            {"id": "b", "title": "Second Item", "duration": None, "playlist_index": 2},
        ],
    }
    count, total, preview = main.playlist_summary(info)
    assert count == 2
    assert total is None  # one item has an unknown duration
    assert preview[0]["index"] == "1"
    assert preview[0]["title"] == "First Item"

    complete = {"_type": "playlist", "entries": [{"id": "a", "title": "A", "duration": 90}, {"id": "b", "title": "B", "duration": 10}]}
    _count, total, _preview = main.playlist_summary(complete)
    assert total == 100


def test_media_files_ignores_partial_and_metadata():
    directory = SCRATCH / "signal-shelf" / "jobs" / "media-test"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "001 - A.mp4").write_bytes(b"a")
    (directory / "002 - B.mp4.part").write_bytes(b"b")
    (directory / "003 - C.vtt").write_bytes(b"c")
    (directory / "004 - D.json").write_bytes(b"{}")
    (directory / "playlist-other.zip").write_bytes(b"z")
    files = main.media_files(directory)
    assert [f.name for f in files] == ["001 - A.mp4"]


def test_archive_playlist_bundles_items():
    inspection = _inspection(source_type="playlist", title="My Playlist / With: Symbols?", playlist_count=3)
    job = main.store.create_job(inspection, {"kind": "video", "audio_output": "native", "profile_id": "best"})
    (job.directory / "001 - A.mp4").write_bytes(b"a")
    (job.directory / "002 - B.mp4").write_bytes(b"b")
    archive = main.archive_playlist(job, inspection)
    assert archive.exists()
    assert archive.name.startswith("playlist-")
    assert archive.suffix == ".zip"
    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist()) == ["001 - A.mp4", "002 - B.mp4"]


# ---------------------------------------------------------------------------
# TransferStore lifecycle (fake clock)
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_store_lifecycle_and_cleanup_expiry(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(main.time, "time", clock)
    inspection = _inspection(token="tok1", created_at=clock(), expires_at=clock() + main.INSPECTION_TTL)
    main.store.add_inspection(inspection)

    job = main.store.create_job(inspection, {"kind": "video", "audio_output": "native", "format_id": "22"})
    assert job.status == "queued"
    assert main.store.get_job(job.job_id) is job

    main.store.update_job(job.job_id, status="running", stage="Downloading", progress=50.0)
    assert main.store.get_job(job.job_id).progress == 50.0

    # progress is monotonic: a lower value is ignored
    main.store.update_job(job.job_id, progress=40.0)
    assert main.store.get_job(job.job_id).progress == 50.0
    main.store.update_job(job.job_id, progress=77.5)
    assert main.store.get_job(job.job_id).progress == 77.5

    # snapshot reflects the latest state with a rounded progress
    snapshot = main.store.snapshot(main.store.get_job(job.job_id))
    assert snapshot["status"] == "running"
    assert snapshot["progress"] == 77.5

    main.store.update_job(job.job_id, status="complete", progress=100.0, result_path=Path("f.mp4"), download_name="f.mp4")
    assert main.store.snapshot(main.store.get_job(job.job_id))["ready"] is True

    # after the job TTL expires, cleanup removes both the job and its inspection
    clock.advance(main.JOB_TTL + 60)
    main.store.cleanup()
    assert main.store.get_job(job.job_id) is None
    assert main.store.get_inspection("tok1") is None


def test_store_cleanup_keeps_active_jobs(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(main.time, "time", clock)
    inspection = _inspection(token="tok2", created_at=clock(), expires_at=clock() + main.INSPECTION_TTL)
    main.store.add_inspection(inspection)
    job = main.store.create_job(inspection, {"kind": "video", "audio_output": "native", "format_id": "22"})
    main.store.update_job(job.job_id, status="running", stage="Downloading", progress=30.0)

    # long past every TTL, but the job is still running: cleanup must keep the
    # inspection (and its files) because the token is pinned by an active job
    clock.advance(main.JOB_TTL + 60)
    main.store.cleanup()
    assert main.store.get_job(job.job_id) is not None
    with main.store.lock:
        assert "tok2" in main.store.inspections


# ---------------------------------------------------------------------------
# End-to-end flows through the Flask test client (yt-dlp mocked)
# ---------------------------------------------------------------------------

class FakeYoutubeDL:
    """Stand-in for yt_dlp.YoutubeDL: canned extraction, writes a fake file on download."""

    instances: list["FakeYoutubeDL"] = []
    extractor_error: Exception | None = None

    def __init__(self, options):
        self.options = options
        FakeYoutubeDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if FakeYoutubeDL.extractor_error and not download:
            raise FakeYoutubeDL.extractor_error
        if download:
            directory = Path(self.options["outtmpl"]).parent
            directory.mkdir(parents=True, exist_ok=True)
            if "playlist_index" in self.options.get("outtmpl", ""):
                (directory / "001 - Item One [item1].mp4").write_bytes(b"item-one")
                (directory / "002 - Item Two [item2].mp4").write_bytes(b"item-two")
                return {"id": "pl1", "title": "Sample Playlist", "ext": "mp4"}
            (directory / "Sample Video [abc123].mp4").write_bytes(b"fake-video-bytes")
            return {"id": "abc123", "title": "Sample Video", "ext": "mp4"}
        return FAKE_PLAYLIST_INFO if "playlist" in url else FAKE_SINGLE_INFO


@pytest.fixture()
def fake_ytdlp(monkeypatch):
    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(main, "FFMPEG_READY", True)
    FakeYoutubeDL.instances.clear()
    FakeYoutubeDL.extractor_error = None
    return FakeYoutubeDL


def test_e2e_single_video_download(client, fake_ytdlp):
    inspected = client.post("/api/inspect", data={"url": "https://example.com/watch?v=abc123"})
    assert inspected.status_code == 200
    payload = inspected.get_json()
    assert payload["type"] == "single"
    assert [f["id"] for f in payload["formats"]["video"]] == ["22", "18"]
    assert payload["formats"]["audio"][0]["id"] == "140"
    assert payload["formats"]["image"] == []
    assert payload["defaults"]["video"] == "22"  # preferred MP4
    assert payload["defaults"]["audio"] == "140"
    assert payload["defaults"]["image"] is None

    created = client.post("/api/jobs", json={"token": payload["token"], "kind": "video", "format_id": "22"})
    assert created.status_code == 202
    job_id = created.get_json()["id"]
    assert wait_for(lambda: main.store.get_job(job_id).status == "complete")
    job = main.store.get_job(job_id)
    assert job.result_path is not None and job.result_path.exists()
    assert job.result_path.read_bytes() == b"fake-video-bytes"
    assert job.download_name == "Sample Video [abc123].mp4"

    # SSE stream on a terminal job: exactly one progress frame + one done frame
    events = client.get(f"/api/jobs/{job_id}/events")
    assert events.status_code == 200
    data = events.get_data(as_text=True)
    assert data.count("event: progress") == 1
    assert data.count("event: done") == 1
    assert "fake-video-bytes" not in data
    assert "Sample Video [abc123].mp4" in data

    downloaded = client.get(f"/api/jobs/{job_id}/file")
    assert downloaded.status_code == 200
    assert downloaded.data == b"fake-video-bytes"


def test_e2e_playlist_zip(client, fake_ytdlp):
    inspected = client.post("/api/inspect", data={"url": "https://example.com/playlist?list=pl1"})
    assert inspected.status_code == 200
    payload = inspected.get_json()
    assert payload["type"] == "playlist"
    assert payload["playlist"]["count"] == 2
    assert payload["profiles"]["video"][0]["id"] == "best"
    assert payload["profiles"]["audio"][0]["id"] == "best"

    created = client.post("/api/jobs", json={"token": payload["token"], "kind": "video", "profile_id": "1080"})
    assert created.status_code == 202
    job_id = created.get_json()["id"]
    assert wait_for(lambda: main.store.get_job(job_id).status == "complete")
    job = main.store.get_job(job_id)
    assert job.result_path is not None
    assert job.result_path.name.startswith("playlist-") and job.result_path.suffix == ".zip"
    with zipfile.ZipFile(job.result_path) as bundle:
        assert sorted(bundle.namelist()) == ["001 - Item One [item1].mp4", "002 - Item Two [item2].mp4"]


def test_inspect_falls_back_to_public_image(client, monkeypatch):
    class RaisingYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            raise Exception("no video could be found")

    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", RaisingYoutubeDL)
    fake_image = {
        "id": "img-1",
        "title": "Pinned Image",
        "uploader": "Pinner",
        "extractor_key": "Public image preview",
        "ext": "jpg",
        "url": "https://cdn.example.com/pin.jpg",
        "thumbnail": "https://cdn.example.com/pin.jpg",
        "formats": [],
        "_hydro_direct_media_url": "https://cdn.example.com/pin.jpg",
        "_hydro_direct_media_referer": "https://pinterest.com/pin/1",
        "_hydro_public_preview": True,
    }
    monkeypatch.setattr(main, "public_preview_image_info", lambda url: dict(fake_image))
    response = client.post("/api/inspect", data={"url": "https://www.pinterest.com/pin/12345/"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["type"] == "single"
    assert len(payload["formats"]["image"]) == 1
    assert payload["formats"]["image"][0]["source_direct"] is True


def test_create_job_validation_without_ffmpeg(client, monkeypatch):
    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(main, "FFMPEG_READY", False)
    inspected = client.post("/api/inspect", data={"url": "https://example.com/watch?v=abc123"})
    payload = inspected.get_json()
    # MP3 conversion requires ffmpeg
    denied = client.post("/api/jobs", json={"token": payload["token"], "kind": "audio", "format_id": "140", "audio_output": "mp3"})
    assert denied.status_code == 503
    # a muxed video needs no merging, so it is allowed
    allowed = client.post("/api/jobs", json={"token": payload["token"], "kind": "video", "format_id": "18"})
    assert allowed.status_code == 202


def test_thumbnail_endpoint_rejects_private_url(client):
    directory = main.TEMP_ROOT / "inspections" / "ssrf-thumb"
    directory.mkdir(parents=True, exist_ok=True)
    inspection = _inspection(
        token="ssrf-thumb",
        directory=directory,
        thumbnail_url="http://169.254.169.254/latest/meta-data/",
    )
    main.store.add_inspection(inspection)
    try:
        response = client.get("/api/inspections/ssrf-thumb/thumbnail")
        assert response.status_code == 404
    finally:
        with main.store.lock:
            main.store.inspections.pop("ssrf-thumb", None)
        shutil.rmtree(directory, ignore_errors=True)


def test_public_image_job_rejects_private_url(client):
    directory = main.TEMP_ROOT / "inspections" / "ssrf-job"
    directory.mkdir(parents=True, exist_ok=True)
    inspection = _inspection(
        token="ssrf-job",
        directory=directory,
        direct_media_url="http://127.0.0.1:9/secret.png",
        direct_media_referer="https://example.com/x",
        image_formats={"__source_image__": {"id": "__source_image__", "source_direct": True, "label": "src", "container": "JPG"}},
    )
    main.store.add_inspection(inspection)
    job = None
    try:
        job = main.store.create_job(inspection, {"kind": "image", "audio_output": "native", "format_id": "__source_image__"})
        threading.Thread(target=main.run_download, args=(job.job_id,), daemon=True).start()
        assert wait_for(lambda: main.store.get_job(job.job_id).status == "failed")
        assert main.store.get_job(job.job_id).error is not None
    finally:
        with main.store.lock:
            main.store.inspections.pop("ssrf-job", None)
            if job:
                main.store.jobs.pop(job.job_id, None)
        shutil.rmtree(directory, ignore_errors=True)
        if job:
            shutil.rmtree(job.directory, ignore_errors=True)


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert isinstance(payload["ffmpeg"], bool)
    assert payload["parallel_jobs"] >= 1
    assert payload["fragment_workers"] >= 1


def test_home_renders_accessible_markup(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'for="source-url"' in html
    assert 'aria-controls="format-list"' in html
    assert 'role="tabpanel"' in html
    assert 'role="tablist"' in html
