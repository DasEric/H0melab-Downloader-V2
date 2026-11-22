"""Native HTTP/HLS/FFmpeg transfer. Never calls the yt-dlp fallback."""

from xml.etree import ElementTree

import ffmpeg
import niquests

from ..common import common, hls
from .client import BroadcasterError, language
from .episode import LANGUAGES


def _manifest(url, headers):
    try:
        response = niquests.get(url, headers=headers, timeout=25)
        response.raise_for_status()
        return response.text
    except niquests.RequestException:
        raise BroadcasterError(
            "Playback manifest could not be loaded; retry the download"
        ) from None


def _fallback_inputs(selected, headers, code):
    """Bound FFmpeg fallback to finite, unprotected media and explicit tracks."""
    video_url, audio_url = selected.url, None
    text = _manifest(video_url, headers)
    if selected.protocol == "hls":
        if "#EXT-X-STREAM-INF" in text:
            variants, renditions = hls._parse_master_playlist(text, video_url)
            if not variants:
                raise BroadcasterError("HLS manifest has no video variants")
            variant = max(variants, key=lambda item: item.bandwidth)
            candidates = [
                r for r in renditions if r.group_id == variant.audio_group and r.uri
            ]
            matching = [r for r in candidates if language(r.language) == code]
            if candidates and not matching:
                raise BroadcasterError(
                    "HLS manifest does not offer the selected audio language"
                )
            if matching:
                audio_url = matching[0].uri
            video_url = variant.uri
            text = _manifest(video_url, headers)
        playlists = [text] + ([_manifest(audio_url, headers)] if audio_url else [])
        for playlist in playlists:
            if (
                not playlist.lstrip().startswith("#EXTM3U")
                or "#EXT-X-ENDLIST" not in playlist
            ):
                raise BroadcasterError(
                    "Only finite on-demand HLS playlists are supported"
                )
            for line in playlist.splitlines():
                if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
                    attrs = hls._parse_attributes(line)
                    if (
                        attrs.get("METHOD") not in ("NONE", "AES-128")
                        or attrs.get("KEYFORMAT", "identity") != "identity"
                    ):
                        raise BroadcasterError(
                            "Protected HLS playback is not supported"
                        )
    elif selected.protocol == "dash":
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError:
            raise BroadcasterError("Invalid DASH manifest") from None
        if root.tag.rsplit("}", 1)[-1] != "MPD" or root.get("type") == "dynamic":
            raise BroadcasterError("Only finite on-demand DASH manifests are supported")
        if any(
            node.tag.rsplit("}", 1)[-1] == "ContentProtection" for node in root.iter()
        ):
            raise BroadcasterError("Protected DASH playback is not supported")
        audio_sets = [
            node
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1] == "AdaptationSet"
            and (
                node.get("contentType") == "audio"
                or (node.get("mimeType") or "").startswith("audio/")
            )
        ]
        # FFmpeg's first audio must agree with the language-specific source URL.
        # Never relabel a different-language fallback as the requested language.
        if (
            audio_sets
            and audio_sets[0].get("lang")
            and language(audio_sets[0].get("lang")) != code
        ):
            raise BroadcasterError(
                "DASH default audio does not match the selected language"
            )
    kwargs = {
        "headers": "".join(f"{k}: {v}\r\n" for k, v in headers.items()),
        "rw_timeout": 30000000,
    }
    source = ffmpeg.input(video_url, **kwargs)
    audio = ffmpeg.input(audio_url, **kwargs) if audio_url else source
    return [source["v:0"], audio["a:0"]]


def download_broadcast(episode):
    selected = episode.selected_format()
    if (
        episode.selected_subtitle_language != "none"
        and not episode.selected_subtitle_track()
    ):
        raise BroadcasterError(f"{episode.name}: requested subtitles are not available")
    output = episode._episode_path
    code = LANGUAGES[episode.selected_language][0]
    if output.exists():
        existing = common.check_downloaded(output)
        if (
            existing["video_langs"]
            and code in existing["audio_langs"]
            and (
                episode.selected_subtitle_language == "none"
                or episode.selected_subtitle_language in existing["subtitle_langs"]
            )
        ):
            return output
        raise BroadcasterError(
            f"{episode.name}: an existing output has different tracks or is incomplete; "
            "choose a different destination or move the existing file first"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = output.with_suffix(".broadcast")
    temp = output.with_suffix(".broadcast.mkv")
    http_temp = output.with_suffix(".broadcast.mp4")
    headers = {"Referer": episode.url, "User-Agent": "Mozilla/5.0"}
    paused = False
    try:
        common._raise_for_queue_control()
        if selected.protocol == "http":
            common._download_http_file(
                http_temp,
                selected.url,
                headers=headers,
                label=episode.title,
                progress_end=90,
                keep_progress=True,
            )
            inputs = [ffmpeg.input(str(http_temp))]
        else:
            written = []
            if selected.protocol == "hls":
                try:
                    written = hls.download_hls_parallel(
                        selected.url,
                        prefix,
                        headers=headers,
                        preferred_audio_lang=code,
                        strict_audio=True,
                        label=episode.title,
                        concurrency=common._episode_hls_concurrency(),
                        progress_end=90,
                        keep_progress=True,
                    )
                except hls.HLSUnsupported as exc:
                    if "live playlist" in str(exc) or "encryption method" in str(exc):
                        raise BroadcasterError(
                            f"{episode.name}: live or protected HLS playback is not supported"
                        ) from None
                    # Only FFmpeg handles unsupported native-HLS features.
                    written = []
            if written:
                inputs = (
                    [
                        ffmpeg.input(str(written[0])).video,
                        ffmpeg.input(str(written[1])).audio,
                    ]
                    if len(written) > 1
                    else [ffmpeg.input(str(written[0]))]
                )
            else:
                inputs = _fallback_inputs(selected, headers, code)
        common._run_ffmpeg_with_progress(
            ffmpeg.output(
                *inputs, str(temp), c="copy", **{"metadata:s:a:0": f"language={code}"}
            ),
            label=episode.title,
        )
        verified = common.check_downloaded(temp)
        if (
            not verified["exists"]
            or not verified["video_langs"]
            or not verified["audio_langs"]
        ):
            raise BroadcasterError(
                f"{episode.name}: downloaded file failed media verification"
            )
        common._raise_for_queue_control()
        common._finalize_episode(temp, output, episode.title, owner=episode)
        return episode._episode_path
    except common.DownloadPaused:
        paused = True
        raise
    finally:
        temp.unlink(missing_ok=True)
        if not paused:
            http_temp.unlink(missing_ok=True)
            hls.cleanup_temp_files(prefix)
        common._clear_download_progress()
