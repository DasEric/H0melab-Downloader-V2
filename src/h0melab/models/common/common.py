import getpass
import glob
import hashlib
import os
import platform
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import ffmpeg
import niquests

from ...autodeps import DependencyManager

try:
    from ...autodeps import get_player_path, get_syncplay_path
    from ...config import (
        INVERSE_LANG_LABELS,
        LANG_CODE_MAP,
        LANG_KEY_MAP,
        NAMING_TEMPLATE,
        PROVIDER_HEADERS_D,
        PROVIDER_HEADERS_W,
        Audio,
        Subtitles,
        get_video_codec,
        is_sto_host,
        logger,
    )
except ImportError:
    from h0melab.autodeps import get_player_path, get_syncplay_path
    from h0melab.config import (
        INVERSE_LANG_LABELS,
        LANG_CODE_MAP,
        LANG_KEY_MAP,
        NAMING_TEMPLATE,
        PROVIDER_HEADERS_D,
        PROVIDER_HEADERS_W,
        Audio,
        Subtitles,
        get_video_codec,
        is_sto_host,
        logger,
    )

# Precompile regex for forbidden filename characters
FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*]')

# Providers whose extractor exposes all HLS mirrors for download-time failover.
STREAM_CANDIDATE_PROVIDERS = frozenset({"MoflixClick"})


def clean_title(title: str) -> str:
    """Clean a string to make it safe for use as a filename."""
    return FORBIDDEN_CHARS.sub("", title).strip()


def _naming_template_uses_resolution():
    template = os.getenv("H0MELAB_NAMING_TEMPLATE", NAMING_TEMPLATE)
    return "{resolution}" in template or "%resolution%" in template


def _read_container_resolution(path):
    """Read one local video stream's height from FFmpeg's container output."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    streams = re.findall(
        r"^\s*Stream #.*Video:.*?\b\d{2,5}x(\d{2,5})\b",
        result.stderr or "",
        re.MULTILINE,
    )
    return f"{streams[0]}p" if len(streams) == 1 else "unknown"


def _reset_naming_cache(self):
    suffixes = (
        "__base_folder",
        "__folder_path",
        "__file_name",
        "__episode_path",
        "__is_downloaded",
    )
    for name in vars(self):
        if name.endswith(suffixes):
            setattr(self, name, None)


def _set_naming_resolution(self, resolution):
    self._resolution = resolution
    _reset_naming_cache(self)


def _prepare_resolution_naming(self):
    """Start with unknown, or reuse a matching previously downloaded file."""
    if not _naming_template_uses_resolution():
        return
    _set_naming_resolution(self, "unknown")
    if self._episode_path.exists():
        return

    marker = "__H0MELAB_RESOLUTION__"
    _set_naming_resolution(self, marker)
    pattern = glob.escape(str(self._episode_path)).replace(marker, "*")
    candidates = [Path(path) for path in glob.glob(pattern)]
    for candidate in candidates:
        resolution = _read_container_resolution(candidate)
        _set_naming_resolution(self, resolution)
        if self._episode_path == candidate:
            return
    _set_naming_resolution(self, "unknown")


def _finalize_resolution_naming(self):
    """Rename a finished local container using its unambiguous resolution."""
    if not _naming_template_uses_resolution():
        return
    old_path = self._episode_path
    _set_naming_resolution(self, _read_container_resolution(old_path))
    new_path = self._episode_path
    if new_path != old_path:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old_path, new_path)


def _progress_file_name(self):
    """Hide post-processed resolution metadata from the live progress label."""
    name = self._file_name or ""
    resolution = str(getattr(self, "_resolution", "") or "")
    if not _naming_template_uses_resolution() or not resolution:
        return name
    index = name.rfind(resolution)
    if index < 0:
        return name
    start, end = index, index + len(resolution)
    separators = "._- "
    if start and name[start - 1] in separators:
        start -= 1
    elif end < len(name) and name[end] in separators:
        end += 1
    return name[:start] + name[end:]


def _quote_windows_cmd_arg(arg) -> str:
    """Quote one argument for safe cmd.exe copy/paste."""
    arg = str(arg)
    if not arg:
        return '""'

    escaped = ['"']
    backslashes = 0

    for char in arg:
        if char == "\\":
            backslashes += 1
            continue

        if char == '"':
            escaped.append("\\" * (backslashes * 2 + 1))
            escaped.append('"')
            backslashes = 0
            continue

        if backslashes:
            escaped.append("\\" * backslashes)
            backslashes = 0

        escaped.append(char)

    if backslashes:
        escaped.append("\\" * (backslashes * 2))

    escaped.append('"')
    return "".join(escaped)


def format_command_for_shell(cmd, windows: bool | None = None) -> str:
    """Format a subprocess argv list as a shell-safe copy/paste command."""
    if windows is None:
        windows = os.name == "nt"

    if windows:
        return " ".join(_quote_windows_cmd_arg(part) for part in cmd)

    return shlex.join([str(part) for part in cmd])


def check_downloaded(episode_path):
    result = {
        "exists": False,
        "video_langs": set(),
        "audio_langs": set(),
        "subtitle_langs": set(),
    }

    if not episode_path.exists():
        return result

    result["exists"] = True

    try:
        probe = ffmpeg.probe(episode_path)
    except ffmpeg.Error:
        return result

    streams = probe.get("streams", [])

    for s in streams:
        lang = s.get("tags", {}).get("language", "und")
        if s.get("codec_type") == "video":
            result["video_langs"].add(lang)
        elif s.get("codec_type") == "audio":
            result["audio_langs"].add(lang)
        elif s.get("codec_type") == "subtitle":
            result["subtitle_langs"].add(lang)

    return result


class ProviderData:
    """
    Container for provider URLs grouped by language settings.

    The internal structure is:

        dict[(Audio, Subtitles)][provider_name]

    Meaning:
    - The key is a tuple of (Audio, Subtitles)
    - The value is a dictionary mapping provider names to their URLs
    """

    def __init__(self, data):
        self._data = data

    def __str__(self):
        # return f"{self.__class__.__name__}({self._data!r})"
        lines = []

        for (audio, subtitles), providers in sorted(
            self._data.items(), key=lambda item: (item[0][0].value, item[0][1].value)
        ):
            header = f"{audio.value} audio"
            if subtitles != Subtitles.NONE:
                header += f" + {subtitles.value} subtitles"

            lines.append(header)

            for provider, url in providers.items():
                lines.append(f"  - {provider:<8} -> {url}")

            lines.append("")

        return "\n".join(lines).rstrip()

    def __repr__(self):
        return f"{self.__class__.__name__}({self._data!r})"

    # Accept a tuple directly
    def get(self, lang_tuple: tuple[Audio, Subtitles]):
        return self._data.get(lang_tuple, {})

    # Behave like a dictionary
    def __getitem__(self, lang_tuple: tuple[Audio, Subtitles]):
        return self._data[lang_tuple]


# -----------------------------------------------------------------------------
# Episode actions (moved from models/*/episode.py)
# -----------------------------------------------------------------------------


def _remove_empty_dirs(folder_path, base_folder, protected=None):
    """Remove folder_path and base_folder if they are empty directories.

    `protected` is never removed. With H0MELAB_MOVIE_FOLDER=0 a movie's
    "folder" *is* the download root, and a failed download must not delete it.
    """
    try:
        protected = Path(protected).resolve() if protected else None
    except OSError:
        protected = None

    for candidate in (folder_path, base_folder):
        try:
            if not candidate.is_dir():
                continue
            if protected is not None and candidate.resolve() == protected:
                continue
            if not any(candidate.iterdir()):
                candidate.rmdir()
        except OSError:
            pass


def _cleanup_episode_download(self):
    """Remove partial files left by an interrupted or failed episode download."""
    for suffix in (
        ".temp_full.mkv",
        ".temp_audio.mkv",
        ".temp_video.mkv",
        ".temp_full.mkv.part",
        ".temp_full.mkv.ytdl",
        ".temp_full.seg.ts",
        ".new.mkv",
        ".convert.mkv",
        ".convert.mp4",
    ):
        try:
            self._episode_path.with_suffix(suffix).unlink(missing_ok=True)
        except OSError:
            pass

    try:
        from .hls import cleanup_temp_files

        for suffix in (".temp_full.mkv", ".temp_audio.mkv", ".temp_video.mkv"):
            temp_path = self._episode_path.with_suffix(suffix)
            cleanup_temp_files(temp_path.with_suffix(".hlswork"))
    except (ImportError, OSError):
        pass


def _reset_provider_resolution_cache(self):
    for attr in list(vars(self)):
        if attr.endswith(("__redirect_url", "__provider_url", "__media_asset")):
            setattr(self, attr, None)


def _set_selected_provider(self, provider_name):
    descriptor = getattr(type(self), "selected_provider", None)
    if isinstance(descriptor, property) and descriptor.fset is not None:
        descriptor.fset(self, provider_name)
        return
    if getattr(self, "selected_provider", None) == provider_name:
        return
    raise AttributeError("selected_provider cannot be updated for fallback handling")


def _publish_queue_provider(provider_name):
    """Expose provider fallback changes to the web queue when one is active."""
    try:
        from ...playwright.captcha import _local

        queue_id = getattr(_local, "queue_id", None)
        if queue_id is None:
            return
        from ...web.db import update_queue_provider

        update_queue_provider(queue_id, provider_name)
    except Exception as exc:
        # CLI downloads and tests do not necessarily initialise the web DB.
        logger.debug(f"Could not publish active provider {provider_name}: {exc}")


def _get_provider_attempt_order(self):
    provider_order = []
    provider_method = getattr(self, "provider_attempt_order", None)
    if callable(provider_method):
        provider_order.extend(
            provider for provider in provider_method() if str(provider).strip()
        )

    if not provider_order:
        current_provider = getattr(self, "selected_provider", None)
        if current_provider:
            provider_order.append(current_provider)

    return tuple(dict.fromkeys(provider_order))


def _build_provider_failure_message(action_name, provider_errors):
    details = "; ".join(
        f"{provider}: {error}" for provider, error in provider_errors.items()
    )
    if len(provider_errors) == 1:
        return (
            f"{action_name} failed. No other supported provider is available "
            f"for this item. {details}"
        )
    return f"{action_name} failed for all providers. {details}"


def _build_player_header_args(headers):
    # mpv parses --http-header-fields as a comma-separated list, so values
    # containing commas (User-Agent's "(KHTML, like Gecko)", Accept-Language's
    # "en-US,en;q=0.5") get split into malformed header lines and CDNs reply
    # with "400 Bad Request" (issue #200). Use the dedicated single-value
    # options where they exist and append the rest one at a time, which
    # bypasses list splitting entirely.
    args = []
    for key, value in headers.items():
        key_lower = key.lower()
        if key_lower == "user-agent":
            args.append(f"--user-agent={value}")
        elif key_lower == "referer":
            args.append(f"--referrer={value}")
        else:
            args.append(f"--http-header-fields-append={key}: {value}")
    return args


def _build_blocking_player_command(player_path, stream_url):
    player_name = os.path.basename(str(player_path)).lower()
    if player_name.startswith("iina"):
        return [player_path, "--keep-running", stream_url]
    return [player_path, stream_url]


def _resolve_stream_url_with_fallback(self, action_name):
    provider_errors = {}

    for provider_name in _get_provider_attempt_order(self):
        try:
            _set_selected_provider(self, provider_name)
            _reset_provider_resolution_cache(self)
            return self.stream_url, provider_name
        except Exception as exc:
            provider_errors[provider_name] = exc
            logger.warning(
                f"{action_name} setup failed for provider {provider_name}: {exc}"
            )

    if provider_errors:
        raise RuntimeError(
            _build_provider_failure_message(action_name, provider_errors)
        ) from list(provider_errors.values())[-1]

    raise RuntimeError(f"{action_name} failed: no providers available")


# Thread-safe global for current ffmpeg download progress (used by web UI)
_ffmpeg_progress_lock = threading.Lock()
_ffmpeg_progress = {
    "percent": 0.0,
    "time": "",
    "speed": "",
    "bandwidth": "",
    "active": False,
    "queue_id": None,
    "episode_index": None,
}
_episode_context = threading.local()


def set_episode_download_context(queue_id, episode_index, hls_concurrency):
    """Freeze per-episode settings so UI changes only affect the next episode."""
    _episode_context.queue_id = queue_id
    _episode_context.episode_index = episode_index
    _episode_context.hls_concurrency = hls_concurrency
    with _ffmpeg_progress_lock:
        _ffmpeg_progress.update(
            percent=0.0,
            time="",
            speed="",
            bandwidth="",
            active=False,
            queue_id=queue_id,
            episode_index=episode_index,
        )


def clear_episode_download_context():
    for name in ("queue_id", "episode_index", "hls_concurrency"):
        if hasattr(_episode_context, name):
            delattr(_episode_context, name)
    with _ffmpeg_progress_lock:
        _ffmpeg_progress.update(
            percent=0.0,
            time="",
            speed="",
            bandwidth="",
            active=False,
            queue_id=None,
            episode_index=None,
        )


def _episode_hls_concurrency():
    value = getattr(_episode_context, "hls_concurrency", None)
    if value is not None:
        return value
    from .hls import get_concurrency

    return get_concurrency()


def get_ffmpeg_progress():
    """Return a snapshot of the current ffmpeg download progress."""
    with _ffmpeg_progress_lock:
        return dict(_ffmpeg_progress)


def _clear_download_progress():
    with _ffmpeg_progress_lock:
        _ffmpeg_progress.update(
            percent=0.0, time="", speed="", bandwidth="", active=False
        )


def _parse_ffmpeg_time(time_str):
    """Parse ffmpeg time string (HH:MM:SS.xx) to seconds."""
    try:
        parts = time_str.split(":")
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except (ValueError, IndexError):
        pass
    return 0.0


def _print_cli_progress(percent, time_str, speed_str, label=""):
    """Print a simple CLI progress bar without ANSI colors."""
    if not sys.stderr.isatty():
        return
    bar_width = 30
    filled = int(bar_width * percent / 100)
    bar = "#" * filled + "-" * (bar_width - filled)
    prefix = f"{label} - " if label else ""
    line = f"\r{prefix}[{bar}] {percent:5.1f}% | {time_str} | {speed_str}  "
    sys.stderr.write(line)
    sys.stderr.flush()


class DownloadCancelled(Exception):
    """Raised when we killed the download ourselves, not when it failed."""


class DownloadPaused(Exception):
    """Raised when a queue transfer stopped at a resumable checkpoint."""


def _raise_for_queue_control():
    """Raise the internal control exception requested by the current queue row."""
    try:
        from ...playwright.captcha import _local
        from ...web.db import queue_control_flags

        queue_id = getattr(_local, "queue_id", None)
        if queue_id is None:
            return
        _cancelled, forced, paused = queue_control_flags(queue_id)
        if forced:
            raise DownloadCancelled("Download cancelled")
        if paused:
            raise DownloadPaused("Download paused")
    except (DownloadCancelled, DownloadPaused):
        raise
    except Exception:
        # CLI downloads and tests do not necessarily initialise the web DB.
        return


def _run_ffmpeg_with_progress(
    node,
    overwrite_output=True,
    label="",
    progress_start=0.0,
    progress_end=100.0,
    keep_progress=False,
    before_output_args=None,
):
    """Run an ffmpeg node and stream its progress output cleanly.

    Includes stall detection: if FFmpeg stops making progress (same frame/time
    values) for STALL_TIMEOUT seconds the process is killed so the caller's
    retry logic can kick in.
    """

    STALL_TIMEOUT = (
        60  # 60 seconds without progress → kill (must exceed reconnect_delay_max=30)
    )

    debug_mode = os.getenv("H0MELAB_DEBUG_MODE", "0") == "1"
    is_tty = sys.stderr.isatty()

    # Regex to extract progress indicators from ffmpeg status lines
    _RE_FRAME = re.compile(r"frame=\s*(\d+)")
    _RE_TIME = re.compile(r"time=(\S+)")
    _RE_SPEED = re.compile(r"speed=\s*(\S+)")
    _RE_BITRATE = re.compile(r"bitrate=\s*(\S+)")
    _RE_DURATION = re.compile(r"Duration:\s*(\d+:\d+:\d+\.\d+)")
    progress_start = max(0.0, min(float(progress_start), 100.0))
    progress_end = max(progress_start, min(float(progress_end), 100.0))

    # Use shorter stats_period for smoother progress (1s in non-debug, 10s in debug)
    stats_period = "10" if debug_mode else "1"

    args = ffmpeg.compile(node, overwrite_output=overwrite_output)
    if before_output_args:
        output_index = -2 if args and args[-1] == "-y" else -1
        args[output_index:output_index] = [str(value) for value in before_output_args]
    if "-stats_period" not in args:
        args.insert(-1, "-stats_period")
        args.insert(-1, stats_period)

    process = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        universal_newlines=False,
    )

    # --- reader thread: reads stderr byte-by-byte and pushes complete lines ---
    line_queue = queue.Queue()

    def _reader():
        buf = bytearray()
        while True:
            char = process.stderr.read(1)
            if not char:
                # EOF – push whatever is left
                if buf:
                    line_queue.put(buf.decode("utf-8", errors="replace").strip())
                line_queue.put(None)  # sentinel
                return
            if char in (b"\r", b"\n"):
                if buf:
                    line_queue.put(buf.decode("utf-8", errors="replace").strip())
                    buf.clear()
            else:
                buf.extend(char)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # --- main loop: consume lines, log them, and watch for stalls ---
    stderr_lines = []  # collect non-progress stderr lines for error reporting
    last_frame = None
    last_time = None
    last_change = time.monotonic()
    total_duration = 0.0
    stopped = None

    with _ffmpeg_progress_lock:
        _ffmpeg_progress.update(
            percent=progress_start, time="", speed="", bandwidth="", active=True
        )

    try:
        while True:
            try:
                _raise_for_queue_control()
            except (DownloadCancelled, DownloadPaused) as exc:
                stopped = exc
                logger.info("[FFmpeg] %s requested, stopping.", str(exc))
                process.kill()
                break
            try:
                line_str = line_queue.get(timeout=1.0)
            except queue.Empty:
                # No new line within 1 s – just check the stall timer
                if time.monotonic() - last_change > STALL_TIMEOUT:
                    logger.warning(
                        "[FFmpeg] Stall detected – no progress for "
                        f"{STALL_TIMEOUT}s. Killing process."
                    )
                    process.kill()
                    break
                continue

            if line_str is None:
                # Reader thread finished (EOF)
                break

            # Log the line
            if line_str.startswith(("frame=", "size=")):
                # --- extract progress values ---
                cur_frame = None
                cur_time = None
                cur_time_str = ""
                cur_speed_str = ""
                cur_bitrate_str = ""
                m = _RE_FRAME.search(line_str)
                if m:
                    cur_frame = m.group(1)
                m = _RE_TIME.search(line_str)
                if m:
                    cur_time = m.group(1)
                    cur_time_str = m.group(1)
                m = _RE_SPEED.search(line_str)
                if m:
                    cur_speed_str = m.group(1)
                m = _RE_BITRATE.search(line_str)
                if m:
                    cur_bitrate_str = m.group(1)
                    if cur_bitrate_str.lower() == "n/a":
                        cur_bitrate_str = ""
                # Compute percentage
                raw_percent = 0.0
                if total_duration > 0 and cur_time_str:
                    elapsed = _parse_ffmpeg_time(cur_time_str)
                    raw_percent = min((elapsed / total_duration) * 100, 100.0)
                percent = progress_start + (
                    (progress_end - progress_start) * raw_percent / 100.0
                )

                # Update global progress for web UI
                with _ffmpeg_progress_lock:
                    _ffmpeg_progress.update(
                        percent=round(percent, 1),
                        time=cur_time_str,
                        speed=cur_speed_str,
                        # FFmpeg reports output-file size, not bytes received.
                        # Do not present that value as network throughput.
                        bandwidth="",
                        active=True,
                    )

                if debug_mode:
                    logger.info(f"[FFmpeg Progress] {line_str}")
                elif is_tty:
                    _print_cli_progress(percent, cur_time_str, cur_speed_str, label)

                # --- stall and force cancel detection ---
                if cur_frame != last_frame or cur_time != last_time:
                    last_frame = cur_frame
                    last_time = cur_time
                    last_change = time.monotonic()
                elif time.monotonic() - last_change > STALL_TIMEOUT:
                    logger.warning(
                        "[FFmpeg] Stall detected – no progress for "
                        f"{STALL_TIMEOUT}s. Killing process."
                    )
                    process.kill()
                    break

            elif line_str:
                # Try to capture total duration from ffmpeg header
                if total_duration == 0.0:
                    dm = _RE_DURATION.search(line_str)
                    if dm:
                        total_duration = _parse_ffmpeg_time(dm.group(1))

                logger.debug(f"[FFmpeg] {line_str}")
                stderr_lines.append(line_str)

        # Clear the progress line in CLI
        if not debug_mode and is_tty:
            sys.stderr.write("\r" + " " * 120 + "\r")
            sys.stderr.flush()

    except KeyboardInterrupt:
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        except OSError:
            pass
        reader_thread.join(timeout=5)
        raise
    finally:
        with _ffmpeg_progress_lock:
            _ffmpeg_progress.update(
                percent=0.0, time="", speed="", bandwidth="", active=False
            )

    reader_thread.join(timeout=5)
    process.wait()
    # We killed it on purpose, so the non-zero exit code and whatever ffmpeg
    # printed on its way out are not worth reporting.
    if stopped is not None:
        raise stopped
    if process.returncode != 0:
        detail = (
            "\n".join(stderr_lines[-20:])
            if stderr_lines
            else f"exit code {process.returncode}"
        )
        logger.warning(f"[FFmpeg] Process failed (rc={process.returncode}):\n{detail}")
        raise RuntimeError(f"ffmpeg error (rc={process.returncode}): {detail}")
    if keep_progress:
        with _ffmpeg_progress_lock:
            _ffmpeg_progress.update(
                percent=progress_end,
                time="",
                speed="",
                bandwidth="",
                active=True,
            )


def movie_folder_enabled():
    """Whether movies get their own folder instead of landing in the root."""
    return os.getenv("H0MELAB_MOVIE_FOLDER", "1") != "0"


def _requested_subtitle_language(owner):
    from .subtitles import normalize_subtitle_language

    return normalize_subtitle_language(
        getattr(owner, "selected_subtitle_language", "none")
    )


def _has_requested_subtitle(probe, wanted):
    from .subtitles import normalize_subtitle_language

    return wanted in {
        normalize_subtitle_language(language)
        for language in (probe.get("subtitle_langs") or set())
    }


def _embed_requested_subtitle(owner, episode_path, label="", progress_start=95.0):
    """Atomically add the requested external soft subtitle to a finished file."""
    wanted = _requested_subtitle_language(owner)
    if wanted == "none":
        return

    probe = check_downloaded(episode_path)
    if _has_requested_subtitle(probe, wanted):
        return

    track_getter = getattr(owner, "selected_subtitle_track", None)
    track = track_getter() if callable(track_getter) else None
    if track is None:
        raise ValueError("Requested German subtitles are not available")

    from .subtitles import download_subtitle

    target_ext = episode_path.suffix.lower()
    if target_ext not in (".mkv", ".mp4"):
        raise ValueError(
            f"Soft subtitles can only be remuxed into MKV or MP4, not {target_ext or 'this file'}"
        )

    subtitle_path = episode_path.with_name(
        f"{episode_path.stem}.subtitle{track.suffix}"
    )
    output_path = episode_path.with_name(
        f"{episode_path.stem}.subtitle-new{target_ext}"
    )
    try:
        download_subtitle(track, subtitle_path)
        existing_subtitles = 0
        try:
            existing_subtitles = sum(
                1
                for stream in ffmpeg.probe(str(episode_path)).get("streams", [])
                if stream.get("codec_type") == "subtitle"
            )
        except ffmpeg.Error:
            pass

        codec = "mov_text" if target_ext == ".mp4" else "srt"
        options = {
            "c": "copy",
            f"c:s:{existing_subtitles}": codec,
            f"metadata:s:s:{existing_subtitles}": "language=deu",
            f"disposition:s:{existing_subtitles}": "0",
        }
        metadata_args = [
            f"-metadata:s:s:{existing_subtitles}",
            "title=Deutsch",
        ]
        if target_ext == ".mp4":
            metadata_args.extend(
                [
                    f"-metadata:s:s:{existing_subtitles}",
                    "handler_name=Deutsch",
                ]
            )
        # ffmpeg-python emits one -map per input when both input nodes are passed,
        # preserving every stream in the existing file and adding the caption.
        node = ffmpeg.output(
            ffmpeg.input(str(episode_path)),
            ffmpeg.input(str(subtitle_path)),
            str(output_path),
            **options,
        )
        _run_ffmpeg_with_progress(
            node,
            label=label,
            progress_start=progress_start,
            progress_end=100.0,
            before_output_args=metadata_args,
        )
        verified = check_downloaded(output_path)
        if not _has_requested_subtitle(verified, wanted):
            raise RuntimeError("Remuxed file does not contain the requested subtitle track")
        os.replace(output_path, episode_path)
    finally:
        subtitle_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)


def _finalize_episode(
    temp_path, episode_path, label="", owner=None, progress_start=0.0
):
    """Move `temp_path` onto `episode_path`, remuxing when containers differ.

    The muxer always writes Matroska, so a naming template ending in `.mp4`
    used to produce MKV data inside an `.mp4` file. Remuxing here keeps the
    file honest. Stream copy is tried first; only a codec the target container
    cannot hold forces a re-encode.
    """
    source_ext = temp_path.suffix.lower().lstrip(".")
    target_ext = episode_path.suffix.lower().lstrip(".")

    if target_ext == source_ext or target_ext not in ("mkv", "mp4"):
        if owner is not None:
            _embed_requested_subtitle(owner, temp_path, label)
        os.replace(temp_path, episode_path)
        if owner is not None:
            _finalize_resolution_naming(owner)
        if progress_start:
            _clear_download_progress()
        return

    converted = episode_path.with_suffix(f".convert.{target_ext}")
    output_kwargs = {"c": "copy"}
    if target_ext == "mp4":
        output_kwargs["movflags"] = "+faststart"

    try:
        logger.debug(f"[REMUXING] {source_ext} -> {target_ext}")
        _run_ffmpeg_with_progress(
            ffmpeg.input(str(temp_path)).output(str(converted), **output_kwargs),
            label=label,
            progress_start=progress_start,
        )
    except RuntimeError:
        if target_ext != "mp4":
            raise
        logger.warning(
            "[REMUXING] stream copy into MP4 failed, re-encoding to H.264/AAC"
        )
        converted.unlink(missing_ok=True)
        _run_ffmpeg_with_progress(
            ffmpeg.input(str(temp_path)).output(
                str(converted),
                vcodec="libx264",
                preset="veryfast",
                crf=20,
                acodec="aac",
                audio_bitrate="192k",
                movflags="+faststart",
            ),
            label=label,
            progress_start=progress_start,
        )

    if owner is not None:
        _embed_requested_subtitle(owner, converted, label)
    os.replace(converted, episode_path)
    temp_path.unlink(missing_ok=True)
    if owner is not None:
        _finalize_resolution_naming(owner)


def _download_http_file(
    output_path,
    stream_url,
    *,
    headers=None,
    label="",
    progress_end=100.0,
    keep_progress=False,
):
    """Download one HTTP media file while publishing bytes and network speed."""
    output_path = Path(output_path)
    progress_end = max(0.0, min(float(progress_end), 100.0))
    response = None
    succeeded = False
    try:
        from ...config import DEFAULT_USER_AGENT

        request_headers = {"User-Agent": DEFAULT_USER_AGENT}
        request_headers.update(headers or {})
        offset = output_path.stat().st_size if output_path.exists() else 0
        if offset:
            request_headers["Range"] = f"bytes={offset}-"
        logger.debug(f"[DOWNLOADING] {label} via direct HTTP")
        response = niquests.get(
            stream_url,
            headers=request_headers,
            stream=True,
            timeout=30,
        )
        response.raise_for_status()

        if offset and response.status_code != 206:
            offset = 0

        try:
            total = int(response.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            total = 0
        downloaded = offset
        if total:
            total += offset
        last_ts = time.monotonic()
        last_bytes = offset
        last_cancel_check = float("-inf")

        with _ffmpeg_progress_lock:
            _ffmpeg_progress.update(
                percent=0.0, time="", speed="", bandwidth="", active=True
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "ab" if offset else "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)

                raw_percent = min(downloaded / total * 100, 100.0) if total else 0.0
                percent = raw_percent * progress_end / 100.0
                mb = downloaded / 1024 / 1024
                time_label = (
                    f"{mb:.1f}/{total / 1024 / 1024:.1f} MB"
                    if total
                    else f"{mb:.1f} MB"
                )

                now = time.monotonic()
                if now - last_cancel_check >= 0.5:
                    last_cancel_check = now
                    _raise_for_queue_control()
                elapsed = now - last_ts
                bandwidth = ""
                if elapsed >= 0.5:
                    rate = (downloaded - last_bytes) / elapsed / 1024 / 1024
                    bandwidth = f"{rate:.1f} MB/s"
                    last_ts = now
                    last_bytes = downloaded

                with _ffmpeg_progress_lock:
                    previous = _ffmpeg_progress.get("bandwidth", "")
                    _ffmpeg_progress.update(
                        percent=round(percent, 1),
                        time=time_label,
                        speed="",
                        bandwidth=bandwidth or previous,
                        active=True,
                    )

                if sys.stderr.isatty():
                    _print_cli_progress(percent, time_label, bandwidth, label)

        succeeded = True
        if keep_progress:
            with _ffmpeg_progress_lock:
                _ffmpeg_progress.update(percent=progress_end, active=True)

        if sys.stderr.isatty():
            sys.stderr.write("\r" + " " * 80 + "\r")
            sys.stderr.flush()
    except DownloadPaused:
        raise
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    finally:
        if response is not None:
            response.close()
        if not (succeeded and keep_progress):
            _clear_download_progress()


def _download_direct_http(episode_path, stream_url, file_name):
    """Download a video via direct HTTP (e.g. pixeldrain). Shared helper."""
    temp_file = episode_path.with_suffix(".temp_dl.mp4")
    ep_label = file_name or ""
    try:
        _download_http_file(temp_file, stream_url, label=ep_label, keep_progress=True)
        _finalize_episode(temp_file, episode_path, ep_label)
    finally:
        _clear_download_progress()


def _download_hls_stream(
    episode_path, stream_url, file_name, audio_lang="jpn", owner=None
):
    """Download a Hanime HLS stream with per-segment retries."""
    ep_label = file_name or ""
    temp_full = episode_path.with_suffix(".temp_full.mkv")
    temp_prefix = episode_path.with_suffix(".hanime_hls")

    try:
        logger.debug(f"[DOWNLOADING] {ep_label} via HLS stream")
        video_codec = get_video_codec()
        from ...config import DEFAULT_USER_AGENT, GLOBAL_SESSION
        from .hls import HLSUnsupported, cleanup_temp_files, download_hls_parallel

        headers = {
            "User-Agent": GLOBAL_SESSION.headers.get("User-Agent", DEFAULT_USER_AGENT),
            "Referer": "https://hanime.tv/",
            "Origin": "https://hanime.tv",
        }
        try:
            written = download_hls_parallel(
                stream_url,
                temp_prefix,
                headers=headers,
                preferred_audio_lang=audio_lang,
                label=ep_label,
                concurrency=_episode_hls_concurrency(),
            )
        except HLSUnsupported as exc:
            logger.debug(
                f"[HLS] parallel Hanime download unsupported ({exc}); using FFmpeg"
            )
            written = []

        if written:
            if len(written) > 1:
                node = ffmpeg.output(
                    ffmpeg.input(str(written[0])).video,
                    ffmpeg.input(str(written[1])).audio,
                    str(temp_full),
                    vcodec=video_codec,
                    acodec="copy",
                    **{"metadata:s:a:0": f"language={audio_lang}"},
                )
            else:
                node = ffmpeg.input(str(written[0])).output(
                    str(temp_full),
                    vcodec=video_codec,
                    acodec="copy",
                    **{"metadata:s:a:0": f"language={audio_lang}"},
                )
            _run_ffmpeg_with_progress(node, label=ep_label)
        else:
            _download_full_stream(
                stream_url,
                temp_full,
                {
                    "reconnect": 1,
                    "reconnect_streamed": 1,
                    "reconnect_delay_max": 30,
                    "allowed_extensions": "ALL",
                    "headers": "".join(
                        f"{key}: {value}\r\n" for key, value in headers.items()
                    ),
                },
                headers,
                {"metadata:s:a:0": f"language={audio_lang}"},
                video_codec,
                ep_label,
                audio_lang,
            )

        _finalize_episode(temp_full, episode_path, ep_label, owner=owner)
    except Exception:
        if temp_full.exists():
            temp_full.unlink()
        raise
    finally:
        try:
            cleanup_temp_files(temp_prefix)
        except (ImportError, NameError, UnboundLocalError):
            pass


def download_hanime(self):
    """Download through Hanime only, refreshing expired streams on failure."""
    if platform.system() == "Windows":
        manager = DependencyManager()
        manager.fetch_binary("ffmpeg")

    _prepare_resolution_naming(self)

    if self._episode_path.exists():
        logger.debug(f"[SKIPPED] {self._file_name} (already downloaded)")
        return

    os.makedirs(self._folder_path, exist_ok=True)
    try:
        stream_url = self.stream_url
    except Exception as exc:
        raise RuntimeError(f"Hanime download failed: {exc}") from exc

    last_error = None
    for attempt in range(1, 4):
        try:
            _download_hls_stream(
                self._episode_path,
                stream_url,
                _progress_file_name(self),
                owner=self,
            )
            return
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                logger.warning(
                    f"Hanime download attempt {attempt}/3 failed: {exc}; retrying with a fresh stream"
                )
                time.sleep(attempt)
                try:
                    stream_url = self.refresh_stream_url()
                except Exception as refresh_exc:
                    raise RuntimeError(
                        f"Hanime download failed: {refresh_exc}"
                    ) from refresh_exc

    raise RuntimeError(
        f"Hanime download failed after 3 attempts: {last_error}"
    ) from last_error


class _HLSManualUnsupported(Exception):
    """Raised when the manual HLS segment fetcher can't handle a playlist."""


_HLS_MEDIA_EXTS = (".ts", ".m4s", ".mp4", ".m4a", ".m4v", ".aac", ".mp3", ".mov")


def _fetch_hls_segment(session, seg_url, headers, hosts, timeout=90):
    """Download one HLS segment, failing over across mirror hosts.

    cineby serves every segment from a rotating pool of mirror hosts that all
    return byte-identical content, so a single host's transient failure (e.g. a
    Cloudflare 522 origin timeout) no longer has to abort the whole download — we
    retry the same path on the other mirrors (two passes, short backoff) before
    giving up. Segments on a single host still just retry that host.
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(seg_url)
    ordered = [parsed.netloc] + [h for h in hosts if h and h != parsed.netloc]
    last_exc = None
    for attempt in range(2):
        for host in ordered:
            url = urlunparse(parsed._replace(netloc=host))
            try:
                resp = session.get(url, headers=headers, timeout=timeout)
                resp.raise_for_status()
                return resp.content
            except Exception as exc:
                last_exc = exc
        if attempt == 0:
            time.sleep(1.0)
    raise last_exc


def _hls_uris(playlist, base_url):
    from urllib.parse import urljoin

    return [
        urljoin(base_url, line.strip())
        for line in playlist.splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _download_hls_manual(m3u8_url, headers, temp_ts, label=""):
    """Fetch an HLS media playlist's segments over plain HTTP into `temp_ts`.

    Some hosters (cineby) disguise their segments with non-media extensions
    (`.jpg`, `.css`, `.txt`) served from rotating hosts. Strict FFmpeg builds
    refuse those through the HLS demuxer even with `-allowed_extensions ALL`, so
    we bypass the demuxer entirely: download each segment ourselves (they are
    plain MPEG-TS) and concatenate them, then let FFmpeg remux the local file.

    Raises `_HLSManualUnsupported` for playlists this simple path shouldn't take
    over (a master with no usable variant, AES-encrypted segments, or ordinary
    `.ts` segments that FFmpeg already handles well).
    """
    session = niquests.Session()
    req_headers = {"Accept-Encoding": "identity"}
    req_headers.update(headers or {})

    resp = session.get(m3u8_url, headers=req_headers, timeout=30)
    resp.raise_for_status()
    playlist = resp.text
    if "#EXTM3U" not in playlist:
        raise _HLSManualUnsupported("not an m3u8 playlist")

    # Master playlist → follow the last (usually highest-quality) variant.
    if "#EXT-X-STREAM-INF" in playlist:
        variants = _hls_uris(playlist, m3u8_url)
        if not variants:
            raise _HLSManualUnsupported("master playlist with no variants")
        m3u8_url = variants[-1]
        resp = session.get(m3u8_url, headers=req_headers, timeout=30)
        resp.raise_for_status()
        playlist = resp.text

    if "#EXT-X-KEY" in playlist:
        raise _HLSManualUnsupported("encrypted playlist")
    if "#EXT-X-MAP" in playlist or "#EXT-X-BYTERANGE" in playlist:
        raise _HLSManualUnsupported("fragmented or byte-range playlist")

    segments = _hls_uris(playlist, m3u8_url)
    if not segments:
        raise _HLSManualUnsupported("no segments")

    def _is_standard(url):
        path = url.split("?", 1)[0].lower()
        return path.endswith(_HLS_MEDIA_EXTS)

    if all(_is_standard(url) for url in segments):
        raise _HLSManualUnsupported("standard segments (leave to ffmpeg)")

    # Pool of interchangeable mirror hosts to fail a segment over to (see
    # _fetch_hls_segment): every host the playlist uses, plus the playlist's own.
    from urllib.parse import urlparse

    seg_hosts = list(
        dict.fromkeys(
            [urlparse(u).netloc for u in segments] + [urlparse(m3u8_url).netloc]
        )
    )

    total = len(segments)
    ep = os.path.splitext(label)[0] if label else ""
    logger.debug(f"[DOWNLOADING] {ep} via manual HLS ({total} segments)")
    started = time.monotonic()
    received_bytes = 0
    try:
        with open(temp_ts, "wb") as out:
            for index, seg_url in enumerate(segments, start=1):
                data = _fetch_hls_segment(session, seg_url, req_headers, seg_hosts)
                # This path concatenates MPEG-TS packets. Some hosters serve
                # fMP4 fragments or an HTML error page under a disguised URL;
                # those bytes cannot become a valid .ts file.
                if (
                    len(data) < 188
                    or data[0] != 0x47
                    or (len(data) >= 376 and data[188] != 0x47)
                ):
                    raise _HLSManualUnsupported("segment is not MPEG-TS")
                out.write(data)
                received_bytes += len(data)
                elapsed = time.monotonic() - started
                bandwidth = (
                    f"{received_bytes / elapsed / 1024 / 1024:.1f} MB/s"
                    if elapsed > 0
                    else ""
                )
                percent = round(index / total * 100, 1)
                with _ffmpeg_progress_lock:
                    _ffmpeg_progress.update(
                        percent=percent,
                        time=f"{index}/{total} segments",
                        speed="",
                        bandwidth=bandwidth,
                        active=True,
                    )
    except Exception:
        try:
            temp_ts.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    finally:
        with _ffmpeg_progress_lock:
            _ffmpeg_progress.update(
                percent=0.0, time="", speed="", bandwidth="", active=False
            )


def _parallel_hls_enabled(owner, stream_url):
    """Whether the shared fast HLS path should handle this episode.

    MoflixClick may return an HLS playlist under a .txt URL. Its extractor
    verifies the playlist content before this point, so that URL is eligible
    too. Setting concurrency to one is the supported global opt-out.
    """
    from urllib.parse import urlparse

    is_m3u8 = ".m3u8" in (stream_url or "").split("?", 1)[0].lower()
    source_host = (urlparse(getattr(owner, "url", "") or "").hostname or "").lower()
    owner_module = type(owner).__module__.lower()
    is_moflix = "moflix_stream" in owner_module or source_host.startswith(
        "moflix-stream."
    )
    if not is_m3u8 and not (
        is_moflix and getattr(owner, "selected_provider", None) == "MoflixClick"
    ):
        return False
    try:
        return _episode_hls_concurrency() > 1
    except ImportError:
        return False


def _try_parallel_hls(
    stream_url,
    temp_prefix,
    headers,
    audio_code,
    ep_label,
    *,
    include_audio,
    progress_end,
):
    """Use the bounded parallel fetcher, falling back safely on incompatibility."""
    from .hls import HLSUnsupported, cleanup_temp_files, download_hls_parallel

    try:
        return download_hls_parallel(
            stream_url,
            temp_prefix,
            headers=headers,
            preferred_audio_lang=audio_code,
            label=ep_label,
            include_audio=include_audio,
            progress_end=progress_end,
            keep_progress=True,
            concurrency=_episode_hls_concurrency(),
        )
    except DownloadPaused:
        raise
    except DownloadCancelled:
        raise
    except HLSUnsupported as exc:
        logger.debug(f"[HLS] parallel download unsupported ({exc}); falling back")
        cleanup_temp_files(temp_prefix)
        _clear_download_progress()
        return None
    except Exception as exc:
        # Compatibility wins over acceleration. Unsupported tags, unusual
        # authentication and transient worker failures all retain the proven
        # FFmpeg/manual path.
        logger.warning(f"[HLS] parallel download unavailable ({exc}); falling back")
        cleanup_temp_files(temp_prefix)
        _clear_download_progress()
        return None


def _download_full_stream(
    stream_url,
    temp_full,
    input_kwargs,
    headers,
    stream_metadata,
    video_codec,
    ep_label,
    audio_code,
    parallel_hls=False,
    direct_http=False,
):
    """Fetch audio+video into `temp_full`.

    Suitable HLS streams first use the bounded parallel segment fetcher. Signed
    direct files can be staged over HTTP so the UI gets byte-accurate progress.
    Returns True when a staged path with reserved finalisation progress was used.
    """
    # The reference downloader uses yt-dlp as its primary transfer engine.  It
    # keeps a full fragment window busy and handles retries/resume without the
    # in-order head-of-line wait of our compatibility HLS fetcher.
    try:
        from .transfer import download_with_ytdlp

        def _ytdlp_progress(data):
            _raise_for_queue_control()
            status = data.get("status")
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            percent = min(95.0, (downloaded / total * 95.0) if total else 0.0)
            speed = data.get("speed") or 0
            bandwidth = f"{speed / 1024 / 1024:.1f} MB/s" if speed else ""
            with _ffmpeg_progress_lock:
                _ffmpeg_progress.update(
                    percent=percent,
                    time="",
                    speed="",
                    bandwidth=bandwidth,
                    active=status != "finished",
                )

        download_with_ytdlp(
            stream_url,
            temp_full,
            headers,
            _episode_hls_concurrency(),
            _ytdlp_progress,
            preferred_audio_lang=audio_code,
        )
        with _ffmpeg_progress_lock:
            _ffmpeg_progress.update(percent=95.0, active=True)
        return True
    except DownloadPaused:
        raise
    except DownloadCancelled:
        temp_full.unlink(missing_ok=True)
        raise
    except Exception as exc:
        cause = exc
        while cause is not None:
            if isinstance(cause, DownloadPaused) or "Download paused" in str(cause):
                raise DownloadPaused("Download paused") from exc
            if isinstance(cause, DownloadCancelled) or "Download cancelled" in str(cause):
                temp_full.unlink(missing_ok=True)
                temp_full.with_name(temp_full.name + ".part").unlink(missing_ok=True)
                temp_full.with_name(temp_full.name + ".ytdl").unlink(missing_ok=True)
                raise DownloadCancelled("Download cancelled") from exc
            cause = getattr(cause, "__cause__", None)
        logger.debug(
            f"[TRANSFER] primary yt-dlp transfer unavailable ({exc}); using compatibility path"
        )
        temp_full.unlink(missing_ok=True)
        _clear_download_progress()

    if direct_http:
        direct_source = temp_full.with_suffix(".direct.mp4")
        paused = False
        try:
            _download_http_file(
                direct_source,
                stream_url,
                headers=headers,
                label=ep_label,
                progress_end=90.0,
                keep_progress=True,
            )
            _run_ffmpeg_with_progress(
                ffmpeg.input(str(direct_source)).output(
                    str(temp_full),
                    vcodec=video_codec,
                    acodec="copy",
                    **stream_metadata,
                ),
                label=ep_label,
                progress_start=90.0,
                progress_end=95.0,
                keep_progress=True,
            )
            return True
        except DownloadPaused:
            paused = True
            raise
        finally:
            if not paused:
                direct_source.unlink(missing_ok=True)

    parallel_remux_failed = False
    if parallel_hls:
        from .hls import cleanup_temp_files

        temp_prefix = temp_full.with_suffix(".hlswork")
        written = _try_parallel_hls(
            stream_url,
            temp_prefix,
            headers,
            audio_code,
            ep_label,
            include_audio=True,
            progress_end=85.0,
        )
        if written:
            try:
                if len(written) > 1:
                    node = ffmpeg.output(
                        ffmpeg.input(str(written[0])).video,
                        ffmpeg.input(str(written[1])).audio,
                        str(temp_full),
                        vcodec=video_codec,
                        acodec="copy",
                        **stream_metadata,
                    )
                else:
                    node = ffmpeg.input(str(written[0])).output(
                        str(temp_full),
                        vcodec=video_codec,
                        acodec="copy",
                        **stream_metadata,
                    )
                _run_ffmpeg_with_progress(
                    node,
                    label=ep_label,
                    progress_start=85.0,
                    progress_end=95.0,
                    keep_progress=True,
                )
                return True
            except RuntimeError as exc:
                logger.warning(
                    f"[HLS] parallel remux failed ({exc}); retrying the HLS playlist"
                )
                temp_full.unlink(missing_ok=True)
                parallel_remux_failed = True
            finally:
                cleanup_temp_files(temp_prefix)

    if not parallel_remux_failed and ".m3u8" in stream_url.split("?", 1)[0].lower():
        temp_ts = temp_full.with_suffix(".seg.ts")
        try:
            _download_hls_manual(stream_url, headers, temp_ts, ep_label)
        except _HLSManualUnsupported as exc:
            logger.debug(f"[HLS] manual fetch not used ({exc}); using FFmpeg")
        else:
            try:
                _run_ffmpeg_with_progress(
                    ffmpeg.input(str(temp_ts)).output(
                        str(temp_full),
                        vcodec=video_codec,
                        acodec="copy",
                        **stream_metadata,
                    ),
                    label=ep_label,
                )
                return False
            except RuntimeError as exc:
                logger.warning(
                    f"[HLS] local segment remux failed ({exc}); retrying the HLS playlist with FFmpeg"
                )
                temp_full.unlink(missing_ok=True)
            finally:
                temp_ts.unlink(missing_ok=True)

    _run_ffmpeg_with_progress(
        ffmpeg.input(stream_url, **input_kwargs).output(
            str(temp_full),
            vcodec=video_codec,
            acodec="copy",
            **stream_metadata,
        ),
        label=ep_label,
    )
    return False


def download(self):
    """Download required audio/video streams for an episode (AniWorld + serienstream.to) with retry logic."""
    if platform.system() == "Windows":
        manager = DependencyManager()
        manager.fetch_binary("ffmpeg")

    max_retries = 3
    provider_order = _get_provider_attempt_order(self)
    provider_errors = {}
    _prepare_resolution_naming(self)

    for provider_index, provider_name in enumerate(provider_order):
        _set_selected_provider(self, provider_name)
        _publish_queue_provider(provider_name)

        stream_candidates = None
        if provider_name in STREAM_CANDIDATE_PROVIDERS:
            candidates_method = getattr(self, "stream_url_candidates", None)
            if callable(candidates_method):
                try:
                    stream_candidates = tuple(candidates_method())
                    if not stream_candidates:
                        raise ValueError("No usable HLS mirrors")
                except Exception as exc:
                    provider_errors[provider_name] = exc
                    logger.warning(
                        f"Could not resolve HLS mirrors for {provider_name}: {exc}"
                    )
                    continue
        # Do not retry the same failed Moflix URL. Try the next player mirror
        # instead, then the next supported provider if one exists.
        provider_retries = (
            len(stream_candidates)
            if stream_candidates is not None
            else (1 if provider_name in STREAM_CANDIDATE_PROVIDERS else max_retries)
        )
        for attempt in range(1, provider_retries + 1):
            try:
                _reset_provider_resolution_cache(self)
                stream_url = (
                    stream_candidates[attempt - 1]
                    if stream_candidates is not None
                    else self.stream_url
                )
                headers = PROVIDER_HEADERS_D.get(provider_name, {})
                check = check_downloaded(self._episode_path)
                input_kwargs = {
                    "reconnect": 1,
                    "reconnect_streamed": 1,
                    "reconnect_delay_max": 30,  # wait up to 30s for connection recovery
                }
                # Cineby (and some other hosters) disguise their HLS segments with
                # non-.ts extensions like .jpg; ffmpeg 7+ refuses those by default
                # ("not in allowed_segment_extensions"), so allow every segment
                # extension for m3u8 inputs.
                if ".m3u8" in (stream_url or "").split("?", 1)[0].lower() or (
                    provider_name == "MoflixClick"
                ):
                    input_kwargs["allowed_extensions"] = "ALL"
                if headers:
                    header_list = [f"{k}: {v}" for k, v in headers.items()]
                    input_kwargs["headers"] = "\r\n".join(header_list) + "\r\n"

                # Covers every host in config.STO_DOMAINS/STO_IP
                is_serienstream = is_sto_host(getattr(self, "url", "") or "")

                if is_serienstream and hasattr(self, "_normalize_language"):
                    audio_enum, sub_enum = self._normalize_language(
                        self.selected_language
                    )
                    audio_code = {"German": "deu", "English": "eng"}.get(
                        getattr(audio_enum, "value", None)
                    )
                    if not audio_code:
                        raise ValueError(
                            f"Unsupported audio language for serienstream.to: {audio_enum}"
                        )
                    wants_clean_video = True
                    sub_video_code = None
                else:
                    selected_key = INVERSE_LANG_LABELS[self.selected_language]
                    audio_enum, sub_enum = LANG_KEY_MAP[selected_key]

                    audio_code = LANG_CODE_MAP[audio_enum]
                    wants_clean_video = sub_enum == Subtitles.NONE
                    sub_video_code = (
                        None if wants_clean_video else LANG_CODE_MAP[sub_enum]
                    )

                has_video = bool(check["video_langs"])
                has_audio = audio_code in check["audio_langs"]
                requested_subtitle = _requested_subtitle_language(self)
                has_subtitle = _has_requested_subtitle(check, requested_subtitle)

                need_audio = not has_audio
                need_subtitle = requested_subtitle != "none" and not has_subtitle
                if not has_video:
                    need_video = True
                elif not wants_clean_video:
                    need_video = sub_video_code not in check["video_langs"]
                else:
                    need_video = False

                if not need_audio and not need_video and not need_subtitle:
                    logger.debug(f"[SKIPPED] {self._file_name}")
                    return

                if not need_audio and not need_video and need_subtitle:
                    logger.debug("[REMUXING] adding requested German subtitles")
                    _embed_requested_subtitle(
                        self,
                        self._episode_path,
                        _progress_file_name(self),
                        progress_start=0.0,
                    )
                    return

                os.makedirs(self._folder_path, exist_ok=True)

                ep_label = _progress_file_name(self)

                full_stream_needed = need_audio and need_video

                parallel_hls = _parallel_hls_enabled(self, stream_url)

                temp_audio = self._episode_path.with_suffix(".temp_audio.mkv")
                temp_video = self._episode_path.with_suffix(".temp_video.mkv")
                temp_full = self._episode_path.with_suffix(".temp_full.mkv")

                if full_stream_needed:
                    logger.debug(
                        f"[DOWNLOADING] full preset (audio + video together) via {provider_name}"
                    )

                    stream_metadata = {"metadata:s:a:0": f"language={audio_code}"}
                    if (not wants_clean_video) and sub_video_code:
                        stream_metadata["metadata:s:v:0"] = f"language={sub_video_code}"

                    video_codec = get_video_codec()
                    used_parallel = _download_full_stream(
                        stream_url,
                        temp_full,
                        input_kwargs,
                        headers,
                        stream_metadata,
                        video_codec,
                        ep_label,
                        audio_code,
                        parallel_hls=parallel_hls,
                        direct_http=provider_name == "Veev",
                    )

                    if self._episode_path.exists():
                        inputs = [
                            ffmpeg.input(str(self._episode_path)),
                            ffmpeg.input(str(temp_full)),
                        ]
                        output_path = self._episode_path.with_suffix(".new.mkv")
                        _run_ffmpeg_with_progress(
                            ffmpeg.output(*inputs, str(output_path), c="copy"),
                            progress_start=95.0 if used_parallel else 0.0,
                            progress_end=98.0 if used_parallel else 100.0,
                            keep_progress=used_parallel,
                        )
                        _finalize_episode(
                            output_path,
                            self._episode_path,
                            ep_label,
                            owner=self,
                            progress_start=98.0 if used_parallel else 0.0,
                        )
                    else:
                        _finalize_episode(
                            temp_full,
                            self._episode_path,
                            ep_label,
                            owner=self,
                            progress_start=95.0 if used_parallel else 0.0,
                        )

                    if temp_full.exists():
                        temp_full.unlink()
                    return

                if need_audio:
                    logger.debug(f"[DOWNLOADING] audio stream via {provider_name}")
                    audio_done = False
                    used_parallel = False
                    if parallel_hls:
                        from .hls import cleanup_temp_files

                        temp_prefix = temp_audio.with_suffix(".hlswork")
                        result = _try_parallel_hls(
                            stream_url,
                            temp_prefix,
                            headers,
                            audio_code,
                            ep_label,
                            include_audio=True,
                            progress_end=85.0,
                        )
                        if result:
                            audio_path = result[1] if len(result) > 1 else None
                            audio_src = audio_path or result[0]
                            try:
                                _run_ffmpeg_with_progress(
                                    ffmpeg.input(str(audio_src)).output(
                                        str(temp_audio),
                                        acodec="copy",
                                        map="0:a:0?",
                                        **{"metadata:s:a:0": f"language={audio_code}"},
                                    ),
                                    label=ep_label,
                                    progress_start=85.0,
                                    progress_end=95.0,
                                    keep_progress=True,
                                )
                                audio_done = True
                                used_parallel = True
                            finally:
                                cleanup_temp_files(temp_prefix)
                    if not audio_done:
                        _run_ffmpeg_with_progress(
                            ffmpeg.input(stream_url, **input_kwargs).output(
                                str(temp_audio),
                                acodec="copy",
                                map="0:a:0?",
                                **{"metadata:s:a:0": f"language={audio_code}"},
                            ),
                            label=ep_label,
                        )

                if need_video:
                    logger.debug(f"[DOWNLOADING] video stream via {provider_name}")
                    video_codec = get_video_codec()
                    video_done = False
                    used_parallel = False
                    if parallel_hls:
                        from .hls import cleanup_temp_files

                        temp_prefix = temp_video.with_suffix(".hlswork")
                        result = _try_parallel_hls(
                            stream_url,
                            temp_prefix,
                            headers,
                            audio_code,
                            ep_label,
                            include_audio=False,
                            progress_end=85.0,
                        )
                        if result:
                            try:
                                _run_ffmpeg_with_progress(
                                    ffmpeg.input(str(result[0])).output(
                                        str(temp_video),
                                        vcodec=video_codec,
                                        map="0:v:0?",
                                        **(
                                            {}
                                            if wants_clean_video
                                            else {
                                                "metadata:s:v:0": (
                                                    f"language={sub_video_code}"
                                                )
                                            }
                                        ),
                                    ),
                                    label=ep_label,
                                    progress_start=85.0,
                                    progress_end=95.0,
                                    keep_progress=True,
                                )
                                video_done = True
                                used_parallel = True
                            finally:
                                cleanup_temp_files(temp_prefix)
                    if not video_done:
                        _run_ffmpeg_with_progress(
                            ffmpeg.input(stream_url, **input_kwargs).output(
                                str(temp_video),
                                vcodec=video_codec,
                                map="0:v:0?",
                                **(
                                    {}
                                    if wants_clean_video
                                    else {
                                        "metadata:s:v:0": f"language={sub_video_code}"
                                    }
                                ),
                            ),
                            label=ep_label,
                        )

                logger.debug("[MUXING] combining streams")
                inputs = (
                    [ffmpeg.input(str(self._episode_path))]
                    if self._episode_path.exists()
                    else []
                )

                if need_audio:
                    inputs.append(ffmpeg.input(str(temp_audio)))
                if need_video:
                    inputs.append(ffmpeg.input(str(temp_video)))

                output_path = self._episode_path.with_suffix(".new.mkv")
                _run_ffmpeg_with_progress(
                    ffmpeg.output(*inputs, str(output_path), c="copy"),
                    progress_start=95.0 if used_parallel else 0.0,
                    progress_end=98.0 if used_parallel else 100.0,
                    keep_progress=used_parallel,
                )
                _finalize_episode(
                    output_path,
                    self._episode_path,
                    ep_label,
                    owner=self,
                    progress_start=98.0 if used_parallel else 0.0,
                )

                for f in (temp_audio, temp_video):
                    if f.exists():
                        f.unlink()

                return

            except KeyboardInterrupt:
                _cleanup_episode_download(self)
                _remove_empty_dirs(
                    self._folder_path,
                    self._base_folder,
                    protected=getattr(self, "selected_path", None),
                )
                raise

            except DownloadCancelled:
                # The user stopped this, so clean up and get out instead of
                # logging a failure and trying the next provider.
                _cleanup_episode_download(self)
                _remove_empty_dirs(
                    self._folder_path,
                    self._base_folder,
                    protected=getattr(self, "selected_path", None),
                )
                raise

            except DownloadPaused:
                # Keep resumable transfer artefacts and retry the same episode
                # after the queue item is resumed.
                raise

            except Exception as e:
                _cleanup_episode_download(self)

                try:
                    from ...playwright.captcha import _local
                    from ...web.db import is_queue_force_cancelled

                    qid = getattr(_local, "queue_id", None)
                    if qid is not None and is_queue_force_cancelled(qid):
                        _remove_empty_dirs(
                            self._folder_path,
                            self._base_folder,
                            protected=getattr(self, "selected_path", None),
                        )
                        raise
                except Exception as inner_e:
                    if inner_e is e:
                        raise

                provider_errors[provider_name] = e
                logger.warning(
                    f"Download attempt {attempt}/{provider_retries} failed for provider "
                    f"{provider_name}: {e}"
                )
                if attempt < provider_retries:
                    logger.debug(f"Retrying download with provider {provider_name}...")
                    continue

                next_provider = None
                if provider_index + 1 < len(provider_order):
                    next_provider = provider_order[provider_index + 1]
                if next_provider:
                    logger.warning(
                        f"Falling back from provider {provider_name} to "
                        f"{next_provider} for {getattr(self, 'url', 'episode')}"
                    )

    _remove_empty_dirs(
        self._folder_path,
        self._base_folder,
        protected=getattr(self, "selected_path", None),
    )
    if provider_errors:
        raise RuntimeError(
            _build_provider_failure_message("Download", provider_errors)
        ) from list(provider_errors.values())[-1]
    raise RuntimeError("Download failed: no providers available")


def watch(self):
    """Watch the current episode with provider headers."""

    print(f"[WATCHING] {self._file_name}")

    player_path = str(get_player_path())
    provider_order = _get_provider_attempt_order(self)
    provider_errors = {}

    # AniSkip: AniWorld only; ignore for serienstream.to
    aniskip_enabled = os.getenv("H0MELAB_ANISKIP", "0") == "1"
    if aniskip_enabled and hasattr(self, "skip_times"):
        skip_times = self.skip_times
    else:
        skip_times = None

    if skip_times:
        from ...aniskip import build_mpv_flags, setup_aniskip

        setup_aniskip()
        skip_flags = build_mpv_flags(skip_times).split()
        logger.debug(f"[SKIP TIMES FOUND]: {skip_flags}")
    else:
        skip_flags = []

    base_args = [
        "--no-ytdl",
        "--fs",
        "--quiet",
        f"--force-media-title={self._file_name}",
    ]

    for provider_index, provider_name in enumerate(provider_order):
        _set_selected_provider(self, provider_name)
        max_retries = 3

        for attempt in range(1, max_retries + 1):
            try:
                _reset_provider_resolution_cache(self)
                stream_url = self.stream_url
                headers = PROVIDER_HEADERS_W.get(provider_name, {})
                cmd = _build_blocking_player_command(player_path, stream_url)

                if skip_flags:
                    cmd.extend(skip_flags)

                cmd.extend(base_args)

                if headers:
                    cmd.extend(_build_player_header_args(headers))

                print(format_command_for_shell(cmd))
                process = subprocess.run(cmd, check=False)
                if process.returncode != 0:
                    raise RuntimeError(f"player exited with code {process.returncode}")
                return
            except Exception as e:
                provider_errors[provider_name] = e
                logger.warning(
                    f"Watch attempt {attempt}/{max_retries} failed for provider "
                    f"{provider_name}: {e}"
                )
                if attempt < max_retries:
                    logger.debug(f"Retrying watch with provider {provider_name}...")
                    continue

                next_provider = None
                if provider_index + 1 < len(provider_order):
                    next_provider = provider_order[provider_index + 1]
                if next_provider:
                    logger.warning(
                        f"Falling back from provider {provider_name} to "
                        f"{next_provider} for {getattr(self, 'url', 'episode')}"
                    )

    if provider_errors:
        raise RuntimeError(
            _build_provider_failure_message("Watch", provider_errors)
        ) from list(provider_errors.values())[-1]
    raise RuntimeError("Watch failed: no providers available")


def syncplay(self):
    """Syncplay an episode (AniWorld + serienstream.to)."""

    print(f"[Syncplaying] {self._file_name}")

    # TODO: implement IINA support for syncplay (Syncplay may not detect IINA binary reliably)
    # Force mpv for now (get_player_path() reads this env var)
    os.environ["H0MELAB_USE_IINA"] = "0"

    stream_url, provider_name = _resolve_stream_url_with_fallback(self, "Syncplay")

    syncplay_host = os.getenv("H0MELAB_SYNCPLAY_HOST") or "syncplay.pl:8998"
    syncplay_password = os.getenv("H0MELAB_SYNCPLAY_PASSWORD")

    # getpass.getuser() is usually fine, but can fail in some environments
    syncplay_username = os.getenv("H0MELAB_SYNCPLAY_USERNAME")

    if not syncplay_username:
        try:
            syncplay_username = getpass.getuser()
        except Exception:
            syncplay_username = "H0melab-Downloader"

    room = "AniWorld"
    file_name = self._file_name.replace(" ", "_")

    if syncplay_password:
        # Log what we're using to derive the room (helps debugging)
        logger.debug(f"{room}-{file_name}-{syncplay_password}")
        room += (
            "-"
            + hashlib.sha256(f"-{file_name}-{syncplay_password}".encode()).hexdigest()
        )
    else:
        logger.debug(f"{room}-{file_name}")
        room += f"-{file_name}"

    syncplay_room = os.getenv("H0MELAB_SYNCPLAY_ROOM") or room

    logger.debug(room)

    cmd = [
        str(get_syncplay_path()),
        "--no-gui",
        "--no-store",
        "--host",
        syncplay_host,
        "--room",
        syncplay_room,
        "--name",
        syncplay_username,
        "--player-path",
        str(get_player_path()),
        stream_url,
        # "/Users/phoenixthrush/Downloads/Caramelldansen.webm",
    ]

    # MPV flags come after this
    cmd.append("--")

    aniskip_enabled = os.getenv("H0MELAB_ANISKIP", "0") == "1"
    skip_times = None
    if aniskip_enabled and hasattr(self, "skip_times"):
        skip_times = self.skip_times

    if skip_times:
        from ...aniskip import build_mpv_flags, setup_aniskip

        setup_aniskip()
        skip_flags = build_mpv_flags(skip_times).split()
        cmd.extend(skip_flags)
        logger.debug(f"[SKIP TIMES FOUND]: {skip_flags}")

    cmd.extend(
        ["--no-ytdl", "--fs", "--quiet", f"--force-media-title={self._file_name}"]
    )

    headers = PROVIDER_HEADERS_W.get(provider_name, {})

    if headers:
        cmd.extend(_build_player_header_args(headers))

    print(format_command_for_shell(cmd))
    logger.debug("\n" + format_command_for_shell(cmd))
    subprocess.run(cmd, check=False)


if __name__ == "__main__":
    from h0melab.models import AniworldEpisode

    ep = AniworldEpisode(
        "https://aniworld.to/anime/stream/highschool-dxd/staffel-1/episode-1"
    )

    ep.syncplay()
