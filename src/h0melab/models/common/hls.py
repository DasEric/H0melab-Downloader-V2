"""Parallel HLS segment downloader.

FFmpeg consumes HLS segments largely in sequence, which can leave bandwidth
unused on hosters that limit an individual transfer. This module fetches a
bounded number of segments concurrently and writes them back in playlist order,
producing a file FFmpeg can then remux without any network access.

Anything the parser does not fully understand raises `HLSUnsupported` so the
caller can fall back to letting FFmpeg handle the stream directly.
"""

import hashlib
import json
import os
import re
import struct
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

import niquests

try:
    from ...config import DEFAULT_USER_AGENT, logger
except ImportError:
    from h0melab.config import DEFAULT_USER_AGENT, logger


class HLSUnsupported(Exception):
    """The playlist uses a feature this downloader cannot handle."""


DEFAULT_CONCURRENCY = 8
MAX_CONCURRENCY = 32
SEGMENT_RETRIES = 3
SEGMENT_TIMEOUT = 30

# Audio rendition LANGUAGE values seen in the wild, keyed by ffmpeg lang code
_LANG_PREFIXES = {
    "deu": ("de", "ger", "deu"),
    "eng": ("en", "eng"),
    "jpn": ("ja", "jp", "jpn"),
}

_LANG_NAME_HINTS = {
    "deu": ("german", "deutsch"),
    "eng": ("english",),
    "jpn": ("japanese", "japanisch"),
}

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def get_concurrency():
    """Read the configured segment concurrency, clamped to a sane range."""
    raw = os.getenv("H0MELAB_HLS_CONCURRENCY", str(DEFAULT_CONCURRENCY))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENCY
    return max(1, min(value, MAX_CONCURRENCY))


def _parse_attributes(line):
    """Parse the `KEY=VALUE,KEY="VALUE"` tail of an EXT-X tag."""
    attrs = {}
    for key, value in _ATTR_RE.findall(line):
        attrs[key] = value.strip('"')
    return attrs


class _Variant:
    __slots__ = ("audio_group", "bandwidth", "uri")

    def __init__(self, uri, bandwidth, audio_group):
        self.uri = uri
        self.bandwidth = bandwidth
        self.audio_group = audio_group


class _Rendition:
    __slots__ = ("group_id", "is_default", "language", "name", "uri")

    def __init__(self, uri, group_id, language, name, is_default):
        self.uri = uri
        self.group_id = group_id
        self.language = language
        self.name = name
        self.is_default = is_default


class _Key:
    __slots__ = ("iv", "method", "uri")

    def __init__(self, method, uri, iv):
        self.method = method
        self.uri = uri
        self.iv = iv


class _Segment:
    __slots__ = ("duration", "key", "sequence", "uri")

    def __init__(self, uri, key, sequence, duration):
        self.uri = uri
        self.key = key
        self.sequence = sequence
        self.duration = duration


class _MediaPlaylist:
    __slots__ = ("init_uri", "segments", "url")

    def __init__(self, url, segments, init_uri):
        self.url = url
        self.segments = segments
        self.init_uri = init_uri


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------

_thread_local = threading.local()


def _session():
    """One niquests session per worker thread."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = niquests.Session()
        _thread_local.session = session
    from ...config import GLOBAL_SESSION

    session.cookies.update(GLOBAL_SESSION.cookies)
    return session


def _default_headers(headers):
    merged = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        merged.update(headers)
    # Segment payloads are media bytes. Disabling content encoding keeps the
    # byte counter identical to what was actually transferred and avoids doing
    # pointless compression work in every worker.
    merged.setdefault("Accept-Encoding", "identity")
    return merged


def _fetch_text(url, headers):
    resp = _session().get(url, headers=headers, timeout=SEGMENT_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _fetch_bytes(url, headers, on_bytes=None, check_cancelled=None):
    """Fetch a URL, retrying transient failures and reporting wire bytes."""
    last_error = None
    for attempt in range(SEGMENT_RETRIES):
        response = None
        try:
            if check_cancelled:
                check_cancelled()
            response = _session().get(
                url, headers=headers, timeout=SEGMENT_TIMEOUT, stream=True
            )
            response.raise_for_status()
            chunks = []
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                if check_cancelled:
                    check_cancelled()
                chunks.append(chunk)
                if on_bytes:
                    on_bytes(len(chunk))
            content = b"".join(chunks)
            if not content:
                raise ValueError("empty response body")
            return content
        except Exception as err:
            if isinstance(err, _common().DownloadCancelled):
                raise
            last_error = err
            if attempt < SEGMENT_RETRIES - 1:
                time.sleep(2**attempt)
        finally:
            if response is not None:
                response.close()
    raise RuntimeError(f"failed to fetch {url}: {last_error}") from last_error


# -----------------------------------------------------------------------------
# Playlist parsing
# -----------------------------------------------------------------------------


def _parse_master_playlist(text, base_url):
    """Return (variants, renditions) from a master playlist."""
    variants = []
    renditions = []
    lines = [line.strip() for line in text.splitlines()]

    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = _parse_attributes(line)
            if attrs.get("TYPE") != "AUDIO":
                continue
            uri = attrs.get("URI")
            renditions.append(
                _Rendition(
                    uri=urljoin(base_url, uri) if uri else None,
                    group_id=attrs.get("GROUP-ID", ""),
                    language=attrs.get("LANGUAGE", ""),
                    name=attrs.get("NAME", ""),
                    is_default=attrs.get("DEFAULT", "").upper() == "YES",
                )
            )
        elif line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _parse_attributes(line)
            # The URI is on the next non-comment line
            uri = None
            for candidate in lines[index + 1 :]:
                if candidate and not candidate.startswith("#"):
                    uri = candidate
                    break
            if not uri:
                continue
            try:
                bandwidth = int(attrs.get("BANDWIDTH", "0"))
            except ValueError:
                bandwidth = 0
            variants.append(
                _Variant(
                    uri=urljoin(base_url, uri),
                    bandwidth=bandwidth,
                    audio_group=attrs.get("AUDIO", ""),
                )
            )

    return variants, renditions


def _select_audio_rendition(renditions, group_id, preferred_lang):
    """Pick the audio rendition matching `preferred_lang`, else the default.

    Returns None when the variant carries its audio inline, which is the case
    whenever it declares no AUDIO group — the renditions then belong to other
    variants and must not be mixed in.
    """
    if not group_id:
        return None

    candidates = [
        rendition
        for rendition in renditions
        if rendition.uri and rendition.group_id == group_id
    ]
    if not candidates:
        return None

    prefixes = _LANG_PREFIXES.get(preferred_lang, ())
    hints = _LANG_NAME_HINTS.get(preferred_lang, ())

    if prefixes:
        for rendition in candidates:
            language = (rendition.language or "").lower()
            if language and language.startswith(prefixes):
                return rendition
        for rendition in candidates:
            name = (rendition.name or "").lower()
            if any(hint in name for hint in hints):
                return rendition

    for rendition in candidates:
        if rendition.is_default:
            return rendition

    return candidates[0]


def rendition_languages(master_text, base_url=""):
    """Return the ffmpeg lang codes present as *separate* audio renditions.

    Given an HLS master playlist, report which of the languages this downloader
    can select (``_LANG_PREFIXES``) appear as their own ``#EXT-X-MEDIA:TYPE=AUDIO``
    rendition — matched the exact same way ``_select_audio_rendition`` matches
    them, so "detected as available" and "selectable at download time" never
    disagree. Used by cineby to decide whether e.g. a German dub exists before
    offering it.
    """
    _, renditions = _parse_master_playlist(master_text, base_url)
    found = set()
    for code, prefixes in _LANG_PREFIXES.items():
        hints = _LANG_NAME_HINTS.get(code, ())
        for rendition in renditions:
            language = (rendition.language or "").lower()
            name = (rendition.name or "").lower()
            if (language and language.startswith(prefixes)) or any(
                hint in name for hint in hints
            ):
                found.add(code)
                break
    return found


def _parse_media_playlist(text, base_url):
    """Return the finite media segments and their optional init segment."""
    if "#EXT-X-ENDLIST" not in text:
        raise HLSUnsupported("live playlist (no EXT-X-ENDLIST)")

    segments = []
    init_uri = None
    current_key = None
    sequence = 0
    duration = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1])
            except ValueError:
                sequence = 0

        elif line.startswith("#EXT-X-BYTERANGE"):
            raise HLSUnsupported("byte-range segments")

        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                duration = None

        elif line.startswith("#EXT-X-MAP:"):
            attrs = _parse_attributes(line)
            uri = attrs.get("URI")
            if not uri:
                raise HLSUnsupported("EXT-X-MAP without URI")
            if "BYTERANGE" in attrs:
                raise HLSUnsupported("byte-range init segment")
            resolved = urljoin(base_url, uri)
            if init_uri is not None and resolved != init_uri:
                raise HLSUnsupported("playlist changes its init segment")
            init_uri = resolved

        elif line.startswith("#EXT-X-KEY:"):
            attrs = _parse_attributes(line)
            method = attrs.get("METHOD", "NONE").upper()
            if method == "NONE":
                current_key = None
            elif method == "AES-128":
                uri = attrs.get("URI")
                if not uri:
                    raise HLSUnsupported("AES-128 key without URI")
                current_key = _Key(method, urljoin(base_url, uri), attrs.get("IV"))
            else:
                raise HLSUnsupported(f"encryption method {method}")

        elif not line.startswith("#"):
            segments.append(
                _Segment(
                    uri=urljoin(base_url, line),
                    key=current_key,
                    sequence=sequence,
                    duration=duration,
                )
            )
            sequence += 1
            duration = None

    if not segments:
        raise HLSUnsupported("playlist contains no segments")

    return segments, init_uri


# -----------------------------------------------------------------------------
# Decryption
# -----------------------------------------------------------------------------


def _decrypt_segment(data, key_bytes, iv_bytes):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()

    # HLS pads each segment with PKCS7, but not every encoder does.
    if plain:
        pad = plain[-1]
        if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
            return plain[:-pad]
    return plain


def _resolve_iv(key, sequence):
    if key.iv:
        raw = key.iv.lower().removeprefix("0x")
        return bytes.fromhex(raw.zfill(32))
    return b"\x00" * 8 + struct.pack(">Q", sequence)


# -----------------------------------------------------------------------------
# Progress reporting
# -----------------------------------------------------------------------------


def _common():
    """Resolve the sibling module lazily — it imports this one at load time."""
    from . import common

    return common


def _publish_progress(**fields):
    module = _common()
    with module._ffmpeg_progress_lock:
        module._ffmpeg_progress.update(**fields)


class _ProgressTracker:
    """Thread-safe progress based on completed media time and received bytes."""

    def __init__(self, playlists, label, progress_end=100.0):
        self.total = sum(len(item.segments) for item in playlists)
        durations = [
            segment.duration
            for item in playlists
            for segment in item.segments
            if segment.duration is not None
        ]
        self._use_duration = len(durations) == self.total
        self._total_work = sum(durations) if self._use_duration else float(self.total)
        self._done_work = 0.0
        self._progress_end = max(0.0, min(float(progress_end), 100.0))
        self.label = label
        self.done = 0
        self.bytes_received = 0
        self._common = _common()
        self._lock = threading.Lock()
        self._samples = deque([(time.monotonic(), 0)])
        self._last_publish = 0.0
        self._last_cancel_check = 0.0
        self._bandwidth = ""
        self._queue_id = None
        try:
            from ...playwright.captcha import _local

            self._queue_id = getattr(_local, "queue_id", None)
        except Exception:
            pass

    def check_cancelled(self):
        """Raise the downloader's control exception for a forced stop or pause."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_cancel_check < 0.5:
                return
            self._last_cancel_check = now
        if self._queue_id is None:
            return
        try:
            from ...web.db import queue_control_flags

            _cancelled, forced, paused = queue_control_flags(self._queue_id)
            if forced:
                raise self._common.DownloadCancelled("Download cancelled")
            if paused:
                raise self._common.DownloadPaused("Download paused")
        except (self._common.DownloadCancelled, self._common.DownloadPaused):
            raise
        except Exception:
            # CLI use and tests do not necessarily initialise the web database.
            return

    def received(self, size):
        """Record actual response-body bytes from any worker thread."""
        now = time.monotonic()
        with self._lock:
            self.bytes_received += size
            self._samples.append((now, self.bytes_received))
            # Keep at least two samples so even the first received chunk can
            # produce a rate after a slow connection setup.
            while len(self._samples) > 2 and now - self._samples[0][0] > 3.0:
                self._samples.popleft()
            started, start_bytes = self._samples[0]
            elapsed = now - started
            if elapsed > 0:
                rate = (self.bytes_received - start_bytes) / elapsed
                if rate > 0:
                    self._bandwidth = f"{rate / 1024 / 1024:.1f} MB/s"
            should_publish = now - self._last_publish >= 0.25
            if should_publish:
                self._last_publish = now
        if should_publish:
            self._publish()

    def complete(self, segment):
        with self._lock:
            self.done += 1
            self._done_work += (
                segment.duration if self._use_duration else 1.0
            )
        self._publish(force=True)

    def restore(self, segments):
        """Seed progress for segments already verified by a resume checkpoint."""
        with self._lock:
            for segment in segments:
                self.done += 1
                self._done_work += (
                    segment.duration if self._use_duration else 1.0
                )
        self._publish(force=True)

    def finish(self):
        with self._lock:
            self.done = self.total
            self._done_work = self._total_work
        self._publish(force=True)

    def _publish(self, force=False):
        with self._lock:
            fraction = (
                self._done_work / self._total_work if self._total_work > 0 else 0.0
            )
            percent = round(min(fraction, 1.0) * self._progress_end, 1)
            counter = f"{self.done}/{self.total}"
            bandwidth = self._bandwidth

        with self._common._ffmpeg_progress_lock:
            self._common._ffmpeg_progress.update(
                percent=percent,
                time=f"{counter} segments",
                speed="",
                bandwidth=bandwidth,
                active=True,
            )

        if sys.stderr.isatty():
            self._common._print_cli_progress(
                percent, counter, bandwidth, self.label
            )


# -----------------------------------------------------------------------------
# Download
# -----------------------------------------------------------------------------


def _load_media_playlist(playlist_url, headers):
    text = _fetch_text(playlist_url, headers)
    if "#EXT-X-STREAM-INF" in text:
        raise HLSUnsupported("expected a media playlist, got a master playlist")
    segments, init_uri = _parse_media_playlist(text, playlist_url)
    return _MediaPlaylist(playlist_url, segments, init_uri)


def _download_playlist(playlist, headers, temp_prefix, suffix, tracker, concurrency):
    """Fetch every segment of a media playlist, in order, into one file.

    Returns the path written. The extension reflects the segment container so
    FFmpeg picks the right demuxer: `.mp4` for fMP4 (an EXT-X-MAP init segment
    is present), `.ts` for MPEG-TS.
    """
    output_path = temp_prefix.with_suffix(
        f"{suffix}{'.mp4' if playlist.init_uri else '.ts'}"
    )
    checkpoint_path = output_path.with_name(output_path.name + ".resume.json")
    fingerprint = hashlib.sha256(
        "\n".join(
            [playlist.url, playlist.init_uri or ""]
            + [segment.uri for segment in playlist.segments]
        ).encode("utf-8")
    ).hexdigest()

    completed = 0
    if output_path.exists() and checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if (
                checkpoint.get("fingerprint") != fingerprint
                or checkpoint.get("size") != output_path.stat().st_size
            ):
                raise ValueError("stale HLS checkpoint")
            completed = max(
                0,
                min(int(checkpoint.get("completed", 0)), len(playlist.segments)),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            output_path.unlink(missing_ok=True)
            checkpoint_path.unlink(missing_ok=True)
            completed = 0

    def _save_checkpoint(done):
        payload = {
            "fingerprint": fingerprint,
            "completed": done,
            "size": output_path.stat().st_size,
        }
        temporary = checkpoint_path.with_name(checkpoint_path.name + ".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, checkpoint_path)

    key_cache = {}
    key_cache_lock = threading.Lock()

    def _key_bytes(uri):
        with key_cache_lock:
            if uri in key_cache:
                return key_cache[uri]
        data = _fetch_bytes(
            uri,
            headers,
            on_bytes=tracker.received,
            check_cancelled=tracker.check_cancelled,
        )
        if len(data) != 16:
            raise HLSUnsupported(f"AES key has {len(data)} bytes, expected 16")
        with key_cache_lock:
            key_cache[uri] = data
        return data

    def _fetch_segment(segment):
        data = _fetch_bytes(
            segment.uri,
            headers,
            on_bytes=tracker.received,
            check_cancelled=tracker.check_cancelled,
        )
        if segment.key is None:
            return data
        return _decrypt_segment(
            data,
            _key_bytes(segment.key.uri),
            _resolve_iv(segment.key, segment.sequence),
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if completed:
        tracker.restore(playlist.segments[:completed])
    if completed == len(playlist.segments):
        return output_path

    with open(output_path, "ab" if completed else "wb") as handle:
        if playlist.init_uri and not completed:
            handle.write(
                _fetch_bytes(
                    playlist.init_uri,
                    headers,
                    on_bytes=tracker.received,
                    check_cancelled=tracker.check_cancelled,
                )
            )
            handle.flush()
            _save_checkpoint(0)

        if concurrency == 1:
            for index, segment in enumerate(
                playlist.segments[completed:], start=completed + 1
            ):
                chunk = _fetch_segment(segment)
                handle.write(chunk)
                handle.flush()
                _save_checkpoint(index)
                tracker.complete(segment)
            return output_path

        # Keep a bounded window of in-flight segments so memory stays flat
        # regardless of how many segments the playlist has.
        window = concurrency * 2
        pending = deque()
        next_index = completed

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            while next_index < len(playlist.segments) and len(pending) < window:
                segment = playlist.segments[next_index]
                pending.append(
                    (next_index, segment, pool.submit(_fetch_segment, segment))
                )
                next_index += 1

            while pending:
                index, segment, future = pending.popleft()
                chunk = future.result()
                handle.write(chunk)
                handle.flush()
                _save_checkpoint(index + 1)
                tracker.complete(segment)
                if next_index < len(playlist.segments):
                    segment = playlist.segments[next_index]
                    pending.append(
                        (next_index, segment, pool.submit(_fetch_segment, segment))
                    )
                    next_index += 1

    return output_path


def download_hls_parallel(
    stream_url,
    temp_prefix,
    headers=None,
    preferred_audio_lang=None,
    label="",
    include_audio=True,
    progress_end=100.0,
    keep_progress=False,
    concurrency=None,
):
    """Download an HLS stream into local files ready for an FFmpeg remux.

    Returns a list of paths: `[video]` for a muxed stream, or
    `[video, audio]` when the playlist carries audio as a separate rendition.

    Raises `HLSUnsupported` when the playlist needs features this downloader
    does not implement — callers should fall back to plain FFmpeg then.
    """
    concurrency = get_concurrency() if concurrency is None else int(concurrency)
    concurrency = max(1, min(concurrency, MAX_CONCURRENCY))
    if concurrency == 1:
        raise HLSUnsupported("parallel HLS download disabled")

    temp_prefix = Path(temp_prefix)
    headers = _default_headers(headers)

    master_text = _fetch_text(stream_url, headers)
    if not master_text.lstrip().startswith("#EXTM3U"):
        raise HLSUnsupported("response is not an m3u8 playlist")

    video_playlist = stream_url
    audio_playlist = None

    if "#EXT-X-STREAM-INF" in master_text:
        variants, renditions = _parse_master_playlist(master_text, stream_url)
        if not variants:
            raise HLSUnsupported("master playlist has no variants")

        variant = max(variants, key=lambda item: item.bandwidth)
        video_playlist = variant.uri

        rendition = (
            _select_audio_rendition(
                renditions, variant.audio_group, preferred_audio_lang
            )
            if include_audio
            else None
        )
        if rendition is not None:
            audio_playlist = rendition.uri
            logger.debug(
                f"[HLS] separate audio rendition: {rendition.name or rendition.language}"
            )

    video_media = _load_media_playlist(video_playlist, headers)
    audio_media = (
        _load_media_playlist(audio_playlist, headers)
        if include_audio and audio_playlist
        else None
    )
    playlists = [video_media] + ([audio_media] if audio_media else [])
    tracker = _ProgressTracker(playlists, label, progress_end=progress_end)
    written = []
    succeeded = False
    try:
        _publish_progress(percent=0.0, time="", speed="", bandwidth="", active=True)
        written.append(
            _download_playlist(
                video_media, headers, temp_prefix, ".hls_video", tracker, concurrency
            )
        )

        if audio_media:
            written.append(
                _download_playlist(
                    audio_media, headers, temp_prefix, ".hls_audio", tracker, concurrency
                )
            )

        tracker.finish()
        succeeded = True
        return written
    except _common().DownloadPaused:
        # The output and checkpoint describe only fully flushed segments.
        raise
    except Exception:
        cleanup_temp_files(temp_prefix)
        raise
    finally:
        if not (succeeded and keep_progress):
            _publish_progress(
                percent=0.0, time="", speed="", bandwidth="", active=False
            )


def cleanup_temp_files(temp_prefix):
    """Remove any partial files a previous HLS attempt may have left behind."""
    temp_prefix = Path(temp_prefix)
    for suffix in (
        ".hls_video.ts",
        ".hls_video.mp4",
        ".hls_audio.ts",
        ".hls_audio.mp4",
        ".hls_video.ts.resume.json",
        ".hls_video.mp4.resume.json",
        ".hls_audio.ts.resume.json",
        ".hls_audio.mp4.resume.json",
        ".hls_video.ts.resume.json.tmp",
        ".hls_video.mp4.resume.json.tmp",
        ".hls_audio.ts.resume.json.tmp",
        ".hls_audio.mp4.resume.json.tmp",
    ):
        temp_prefix.with_suffix(suffix).unlink(missing_ok=True)
