"""Reading and writing the web UI settings.

Settings are exposed through environment variables to the rest of the app. Any
value saved in the panel is also written to a panel-owned file in the persistent
config directory and loaded with priority on the next start.
"""

import os
from datetime import timedelta

import niquests as requests
from dotenv import dotenv_values, load_dotenv

from ..config import (
    H0MELAB_CONFIG_DIR,
    LANG_LABELS,
    get_provider_fallback_order,
    parse_provider_order,
)
from ..env import modern_env_key, persist_env_values, sync_env_aliases
from ..logger import get_logger
from . import paths, schedule
from .media import SITE_KEYS, SITE_LABELS, SITES_OFF_BY_DEFAULT, WORKING_PROVIDERS

logger = get_logger(__name__)

# Keep panel choices separate from the generated/default .env. The regular
# .env is deliberately loaded without overriding real process variables, while
# a choice explicitly saved in the panel must beat image defaults after a
# restart (notably H0MELAB_DOWNLOAD_PATH in Docker).
PANEL_SETTINGS_PATH = H0MELAB_CONFIG_DIR / ".web-settings.env"
if PANEL_SETTINGS_PATH.exists():
    _legacy_panel = dotenv_values(PANEL_SETTINGS_PATH)
    _panel_v2 = {
        modern_env_key(key): value
        for key, value in _legacy_panel.items()
        if key.startswith("ANIWORLD_")
        and modern_env_key(key) not in _legacy_panel
        and value is not None
    }
    if _panel_v2:
        persist_env_values(PANEL_SETTINGS_PATH, _panel_v2)
load_dotenv(PANEL_SETTINGS_PATH, override=True)
sync_env_aliases()

UI_LANGUAGES = ("en", "de")
OUTPUT_FORMATS = ("mkv", "mp4")
DEFAULT_HLS_CONCURRENCY = 8
MIN_HLS_CONCURRENCY = 1
MAX_HLS_CONCURRENCY = 32
TMDB_KEY = "H0MELAB_TMDB_API_KEY"
UPCOMING_CHECKS_KEY = "H0MELAB_UPCOMING_CHECKS_PER_DAY"
DEFAULT_UPCOMING_CHECKS_PER_DAY = 1
MIN_UPCOMING_CHECKS_PER_DAY = 1
MAX_UPCOMING_CHECKS_PER_DAY = 24

MEDIA_LIBRARY_KEYS = {
    "ebook": "H0MELAB_EBOOK_PATH",
    "audiobook": "H0MELAB_AUDIOBOOK_PATH",
    "podcast": "H0MELAB_PODCAST_PATH",
}

# How Auto-Sync decides when to run: every so often, or at fixed times
AUTOSYNC_MODES = ("interval", "cron")
DEFAULT_AUTOSYNC_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_AUTOSYNC_CRON = "0 3 * * *"

DISCORD_MODES = ("standard", "advanced")
DISCORD_LANGUAGES = ("en", "de")

# Sent instead of the real token so it never leaves the server.
SECRET_PLACEHOLDER = "•" * 8

DISCORD_KEYS = {
    "enabled": "H0MELAB_DISCORD_BOT_ENABLED",
    "token": "H0MELAB_DISCORD_TOKEN",
    "owner_id": "H0MELAB_DISCORD_OWNER_ID",
    "mode": "H0MELAB_DISCORD_MODE",
    "request_role_id": "H0MELAB_DISCORD_REQUEST_ROLE_ID",
    "guild_id": "H0MELAB_DISCORD_GUILD_ID",
    "language": "H0MELAB_DISCORD_LANGUAGE",
    "announce_channel_id": "H0MELAB_DISCORD_ANNOUNCE_CHANNEL_ID",
}

_IP_LOOKUP_URLS = (
    "https://api.ipify.org?format=json",
    "https://ifconfig.me/all.json",
)


class SettingsError(ValueError):
    """Raised for an invalid settings payload."""


class SettingsPersistenceError(RuntimeError):
    """Raised when a valid panel change could not be stored durably."""


def _flag(key, default="0"):
    return os.environ.get(key, default) == "1"


def ui_language():
    lang = os.environ.get("H0MELAB_UI_LANGUAGE", "en").lower()
    return lang if lang in UI_LANGUAGES else "en"


def library_enabled():
    return _flag("H0MELAB_ENABLE_LIBRARY", "1")


def autosync_enabled():
    return _flag("H0MELAB_ENABLE_AUTOSYNC")


def autosync_new_only():
    """Queue only the episodes from the feed instead of filling the gaps.

    Off by default so upgrading does not silently change what AutoSync does.
    """
    return _flag("H0MELAB_AUTOSYNC_NEW_ONLY")


# ---------------------------------------------------------------------------
# When Auto-Sync runs
#
# A broken value in the environment must never take the worker down with it, so
# every reader below falls back to the default and says so in the log. The
# settings page rejects bad input up front, this is for a hand-edited .env.
# ---------------------------------------------------------------------------
def autosync_mode():
    mode = os.environ.get("H0MELAB_AUTOSYNC_MODE", "").strip().lower()
    return mode if mode in AUTOSYNC_MODES else "interval"


def autosync_interval_seconds():
    """Seconds between two runs in interval mode."""
    raw = os.environ.get("H0MELAB_AUTOSYNC_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_AUTOSYNC_INTERVAL_SECONDS
    try:
        return schedule.parse_interval(raw)
    except schedule.ScheduleError as exc:
        logger.warning("Ignoring H0MELAB_AUTOSYNC_INTERVAL=%r: %s", raw, exc)
        return DEFAULT_AUTOSYNC_INTERVAL_SECONDS


def autosync_interval():
    """The same interval written the way it is stored, e.g. "24h"."""
    return schedule.format_interval(autosync_interval_seconds())


def autosync_cron():
    """The cron expression for fixed times, normalised."""
    raw = os.environ.get("H0MELAB_AUTOSYNC_CRON", "").strip()
    if not raw:
        return DEFAULT_AUTOSYNC_CRON
    try:
        return schedule.parse(raw).expression
    except schedule.ScheduleError as exc:
        logger.warning("Ignoring H0MELAB_AUTOSYNC_CRON=%r: %s", raw, exc)
        return DEFAULT_AUTOSYNC_CRON


def autosync_cron_schedule():
    """Parsed fixed times, or None when Auto-Sync runs on an interval."""
    if autosync_mode() != "cron":
        return None
    return schedule.parse(autosync_cron())


def autosync_schedule_description(language=None):
    """One line for the UI: "Every day at 22:00", "Every 6 hours"."""
    language = language or ui_language()
    fixed = autosync_cron_schedule()
    if fixed is not None:
        return fixed.describe(language)
    return schedule.describe_interval(autosync_interval_seconds(), language)


# ---------------------------------------------------------------------------
# Sites
#
# Every site can be switched off, which takes its tab off the home page along
# with the rows it fills there. Some sites start off (see media.py), the
# rest start on, and all of them follow the same H0MELAB_ENABLE_<SITE> name.
# ---------------------------------------------------------------------------
def site_env_key(site):
    return f"H0MELAB_ENABLE_{site.upper()}"


def site_enabled(site):
    if site not in SITE_KEYS:
        return False
    return _flag(site_env_key(site), "0" if site in SITES_OFF_BY_DEFAULT else "1")


def enabled_sites():
    """{site key: on or off} for every site there is."""
    return {site: site_enabled(site) for site in SITE_KEYS}


def htv_enabled():
    return site_enabled("htv")


def burningseries_enabled():
    """Off by default: the site is geo-blocked and behind Google reCAPTCHA."""
    return site_enabled("burningseries")


def kinox_enabled():
    """Off by default: every download needs a captcha solved by hand."""
    return site_enabled("kinox")


def english_sub_disabled():
    return _flag("H0MELAB_DISABLE_ENGLISH_SUB")


def default_language():
    language = os.environ.get("H0MELAB_LANGUAGE", "German Dub")
    return language if language in LANG_LABELS.values() else "German Dub"


def media_library_path(media_kind):
    """Return the existing library root selected for a non-video media kind."""
    if media_kind not in MEDIA_LIBRARY_KEYS:
        raise SettingsError(f"Invalid media kind: {media_kind}")
    configured = os.environ.get(MEDIA_LIBRARY_KEYS[media_kind], "").strip()
    if configured:
        return paths.expand(configured)
    defaults = {"ebook": "Books", "audiobook": "Audiobooks", "podcast": "Podcasts"}
    return paths.default_download_path() / defaults[media_kind]


def media_library_paths():
    return {kind: str(media_library_path(kind)) for kind in MEDIA_LIBRARY_KEYS}


def hls_concurrency():
    """Parallel HLS connections used for the next episode that starts."""
    raw = os.environ.get("H0MELAB_HLS_CONCURRENCY", str(DEFAULT_HLS_CONCURRENCY))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid H0MELAB_HLS_CONCURRENCY=%r", raw)
        return DEFAULT_HLS_CONCURRENCY
    if not MIN_HLS_CONCURRENCY <= value <= MAX_HLS_CONCURRENCY:
        logger.warning("Ignoring out-of-range H0MELAB_HLS_CONCURRENCY=%r", raw)
        return DEFAULT_HLS_CONCURRENCY
    return value


def tmdb_settings():
    """Return secret metadata only; the key itself never leaves the server."""
    return {"key_set": bool(os.environ.get(TMDB_KEY, "").strip())}


def upcoming_checks_per_day():
    raw = os.environ.get(UPCOMING_CHECKS_KEY, DEFAULT_UPCOMING_CHECKS_PER_DAY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_UPCOMING_CHECKS_PER_DAY
    return value if MIN_UPCOMING_CHECKS_PER_DAY <= value <= MAX_UPCOMING_CHECKS_PER_DAY else DEFAULT_UPCOMING_CHECKS_PER_DAY


def upcoming_interval():
    return timedelta(seconds=86400 / upcoming_checks_per_day())


def _collect_tmdb(payload, updates):
    if not isinstance(payload, dict):
        raise SettingsError("tmdb must be an object")
    if "api_key" in payload:
        value = str(payload["api_key"]).strip()
        if value != SECRET_PLACEHOLDER:
            updates[TMDB_KEY] = value


# ---------------------------------------------------------------------------
# Naming template (drives the output container)
# ---------------------------------------------------------------------------
def _naming_template():
    from ..config import NAMING_TEMPLATE

    return os.environ.get("H0MELAB_NAMING_TEMPLATE", NAMING_TEMPLATE)


def output_format():
    """Container implied by the naming template's file extension."""
    last = _naming_template().rstrip('"').split("/")[-1]
    if "." in last:
        extension = last.rsplit(".", 1)[1].strip().strip('"').lower()
        if extension:
            return extension
    return "mkv"


def _template_with_extension(extension):
    template = _naming_template()
    quoted = template.startswith('"') and template.endswith('"')
    if quoted:
        template = template[1:-1]
    parts = template.split("/")
    parts[-1] = parts[-1].rsplit(".", 1)[0] + f".{extension}"
    rebuilt = "/".join(parts)
    return f'"{rebuilt}"' if quoted else rebuilt


# ---------------------------------------------------------------------------
# Where a download lands
#
# Not a second implementation of the naming rules: the episode path is built by
# the downloader itself, a real AniworldEpisode handed a stand-in series, so the
# box on the settings page cannot say one thing while the disk gets another.
#
# The stand-in carries what a real page gives, which is why the title is the
# whole cleaned title and the year is a range rather than one year.
#
# Movies never go through the naming template. Every movie site writes
# "Title (Year)", inside a folder of the same name unless that is switched off,
# and takes only the file extension from the template.
# ---------------------------------------------------------------------------
_PREVIEW_URL = (
    "https://aniworld.to/anime/stream/konosuba-gods-blessing-on-this-wonderful-world"
    "/staffel-1/episode-3"
)
_PREVIEW_TITLE = "KonoSuba God\u2019s blessing on this wonderful world!"
_PREVIEW_YEARS = "2016-2025"
_PREVIEW_IMDB = "tt5370118"
_PREVIEW_RESOLUTION = "1080p"
_PREVIEW_MOVIE = ("Your Name", "2016")


def _preview_episode(root, language):
    """A real episode object, built without touching the network."""
    from types import SimpleNamespace

    from ..models.h0melab_to.episode import AniworldEpisode

    episode = AniworldEpisode(
        url=_PREVIEW_URL,
        series=SimpleNamespace(
            title_cleaned=_PREVIEW_TITLE,
            release_year=_PREVIEW_YEARS,
            imdb=_PREVIEW_IMDB,
        ),
        season=SimpleNamespace(season_number=1),
        episode_number=3,
        selected_path=str(root),
        selected_language=language,
    )
    # What the downloader sets once it has probed the finished file, so a
    # template using {resolution} previews the name the file ends up with
    episode._resolution = _PREVIEW_RESOLUTION
    return episode


def preview_paths(download_path=None):
    """The full path a movie and an episode would be written to."""
    from ..models.common.common import movie_folder_enabled

    root = (
        paths.expand(download_path) if download_path else paths.default_download_path()
    )
    language = default_language()
    if paths.lang_separation_enabled():
        root = root / paths.lang_folder_for(language)

    try:
        episode = _preview_episode(root, language)
        episode_path = str(episode._episode_path)
        extension = episode._file_extension
    except KeyError as exc:
        # The downloader raises the same way on the first download, so saying
        # it here is the whole point of having a preview
        return {
            "error": f"The naming template uses {{{exc.args[0]}}}, "
            "which is not one of the placeholders a download can fill in"
        }
    except Exception as exc:
        # A preview must never take the settings page down with it
        logger.warning("Could not work out the download path preview: %s", exc)
        return {"error": "Could not work this out from the naming template"}

    title, year = _PREVIEW_MOVIE
    movie_name = f"{title} ({year})"
    folder = root / movie_name if movie_folder_enabled() else root
    return {
        "episode": episode_path,
        "movie": str(folder / f"{movie_name}.{extension}"),
    }


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def discord_settings():
    return {
        "enabled": _flag(DISCORD_KEYS["enabled"]),
        "token_set": bool(os.environ.get(DISCORD_KEYS["token"], "").strip()),
        "owner_id": os.environ.get(DISCORD_KEYS["owner_id"], ""),
        "mode": os.environ.get(DISCORD_KEYS["mode"], "standard"),
        "request_role_id": os.environ.get(DISCORD_KEYS["request_role_id"], ""),
        "guild_id": os.environ.get(DISCORD_KEYS["guild_id"], ""),
        "language": os.environ.get(DISCORD_KEYS["language"], "en"),
        "announce_channel_id": os.environ.get(DISCORD_KEYS["announce_channel_id"], ""),
    }


def _collect_discord(payload, updates):
    if not isinstance(payload, dict):
        raise SettingsError("discord must be an object")

    if "enabled" in payload:
        updates[DISCORD_KEYS["enabled"]] = "1" if payload["enabled"] else "0"

    if "token" in payload:
        token = str(payload["token"]).strip()
        # The UI echoes the placeholder back when the field was left alone
        if token != SECRET_PLACEHOLDER:
            updates[DISCORD_KEYS["token"]] = token

    if "mode" in payload:
        mode = str(payload["mode"]).strip().lower()
        if mode not in DISCORD_MODES:
            raise SettingsError(f"Invalid discord mode: {mode}")
        updates[DISCORD_KEYS["mode"]] = mode

    if "language" in payload:
        language = str(payload["language"]).strip().lower()
        if language not in DISCORD_LANGUAGES:
            raise SettingsError(f"Invalid discord language: {language}")
        updates[DISCORD_KEYS["language"]] = language

    for field in ("owner_id", "request_role_id", "guild_id", "announce_channel_id"):
        if field in payload:
            value = str(payload[field]).strip()
            if value and not value.isdigit():
                raise SettingsError(f"Invalid discord {field}: must be a numeric ID")
            updates[DISCORD_KEYS[field]] = value


def _persist_settings(updates):
    if not updates:
        return
    try:
        from ..env import persist_env_values

        # Write v2 keys while retaining v1 aliases during the transition. This
        # keeps rollback possible and lets an older container read a volume
        # after a test upgrade without losing settings.
        compatible = dict(updates)
        compatible.update({modern_env_key(key): value for key, value in updates.items()})
        persist_env_values(PANEL_SETTINGS_PATH, compatible)
    except OSError as exc:
        raise SettingsPersistenceError(
            "The settings could not be written to the persistent config directory"
        ) from exc


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------
def read_settings():
    return {
        "download_path": str(paths.default_download_path()),
        "media_library_paths": media_library_paths(),
        "lang_separation": paths.lang_separation_enabled(),
        "disable_english_sub": english_sub_disabled(),
        **{f"enable_{site}": state for site, state in enabled_sites().items()},
        "enable_library": library_enabled(),
        "enable_autosync": autosync_enabled(),
        "autosync_new_only": autosync_new_only(),
        "autosync_mode": autosync_mode(),
        "autosync_interval": autosync_interval(),
        "autosync_interval_seconds": autosync_interval_seconds(),
        "autosync_cron": autosync_cron(),
        "autosync_schedule": autosync_schedule_description(),
        "path_preview": preview_paths(),
        "movie_folder": _flag("H0MELAB_MOVIE_FOLDER", "1"),
        "ui_language": ui_language(),
        "output_format": output_format(),
        "hls_concurrency": hls_concurrency(),
        "hls_concurrency_min": MIN_HLS_CONCURRENCY,
        "hls_concurrency_max": MAX_HLS_CONCURRENCY,
        "upcoming_checks_per_day": upcoming_checks_per_day(),
        "upcoming_checks_per_day_min": MIN_UPCOMING_CHECKS_PER_DAY,
        "upcoming_checks_per_day_max": MAX_UPCOMING_CHECKS_PER_DAY,
        "provider_fallback_order": list(get_provider_fallback_order(WORKING_PROVIDERS)),
        "available_providers": list(WORKING_PROVIDERS),
        "available_ui_languages": list(UI_LANGUAGES),
        "available_output_formats": list(OUTPUT_FORMATS),
        "available_autosync_modes": list(AUTOSYNC_MODES),
        "available_sites": [
            {
                "key": site,
                "label": SITE_LABELS[site],
                "default_on": site not in SITES_OFF_BY_DEFAULT,
            }
            for site in SITE_KEYS
        ],
        "discord": discord_settings(),
        "tmdb": tmdb_settings(),
    }


_BOOL_SETTINGS = {
    "lang_separation": "H0MELAB_LANG_SEPARATION",
    "disable_english_sub": "H0MELAB_DISABLE_ENGLISH_SUB",
    "enable_library": "H0MELAB_ENABLE_LIBRARY",
    "enable_autosync": "H0MELAB_ENABLE_AUTOSYNC",
    "autosync_new_only": "H0MELAB_AUTOSYNC_NEW_ONLY",
    "movie_folder": "H0MELAB_MOVIE_FOLDER",
    **{f"enable_{site}": site_env_key(site) for site in SITE_KEYS},
}


def _collect_provider_order(raw, updates):
    if isinstance(raw, (list, tuple)):
        requested = [str(item).strip() for item in raw]
    else:
        requested = [item.strip() for item in str(raw).split(",")]
    requested = [item for item in requested if item]

    if not requested:
        raise SettingsError("provider_fallback_order cannot be empty")
    unknown = sorted({p for p in requested if p not in WORKING_PROVIDERS})
    if unknown:
        raise SettingsError(
            "Invalid provider_fallback_order entries: " + ", ".join(unknown)
        )
    if len(set(requested)) != len(requested):
        raise SettingsError("provider_fallback_order contains duplicates")

    updates["H0MELAB_PROVIDER_FALLBACK_ORDER"] = ",".join(
        parse_provider_order(",".join(requested), allowed_providers=WORKING_PROVIDERS)
    )


def _check_a_site_is_left(data):
    """Refuse the change that would leave the home page with nothing on it.

    Only a payload that touches a site is checked: a .env with everything off
    is the user's business, and must not block every other setting on the page.
    """
    if not any(f"enable_{site}" in data for site in SITE_KEYS):
        return

    wanted = {
        site: bool(data[f"enable_{site}"])
        if f"enable_{site}" in data
        else site_enabled(site)
        for site in SITE_KEYS
    }
    if not any(wanted.values()):
        raise SettingsError("At least one site has to stay enabled")


def _collect_autosync_schedule(data, updates):
    """Validate the Auto-Sync schedule fields, both stored the way they parse."""
    if "autosync_mode" in data:
        mode = str(data["autosync_mode"]).strip().lower()
        if mode not in AUTOSYNC_MODES:
            raise SettingsError(f"Invalid autosync_mode: {mode}")
        updates["H0MELAB_AUTOSYNC_MODE"] = mode

    if "autosync_interval" in data:
        try:
            seconds = schedule.parse_interval(data["autosync_interval"])
        except schedule.ScheduleError as exc:
            raise SettingsError(str(exc)) from None
        updates["H0MELAB_AUTOSYNC_INTERVAL"] = schedule.format_interval(seconds)

    if "autosync_cron" in data:
        # Plain language is accepted here and comes back out as cron
        try:
            parsed = schedule.parse(str(data["autosync_cron"]))
        except schedule.ScheduleError as exc:
            raise SettingsError(str(exc)) from None
        updates["H0MELAB_AUTOSYNC_CRON"] = parsed.expression


def update_settings(data):
    """Apply a settings payload. Raises SettingsError on invalid input.

    Returns True when the Discord config changed, so the caller can restart the bot.
    """
    updates = {}

    if "download_path" in data:
        updates["H0MELAB_DOWNLOAD_PATH"] = str(data["download_path"]).strip()

    if "media_library_paths" in data:
        values = data["media_library_paths"]
        if not isinstance(values, dict):
            raise SettingsError("media_library_paths must be an object")
        for media_kind, env_key in MEDIA_LIBRARY_KEYS.items():
            if media_kind not in values:
                continue
            value = str(values[media_kind]).strip()
            if not value:
                raise SettingsError(f"{media_kind} library path cannot be empty")
            updates[env_key] = value

    for field, key in _BOOL_SETTINGS.items():
        if field in data:
            updates[key] = "1" if data[field] else "0"

    if "ui_language" in data:
        language = str(data["ui_language"]).strip().lower()
        if language not in UI_LANGUAGES:
            raise SettingsError(f"Invalid ui_language: {language}")
        updates["H0MELAB_UI_LANGUAGE"] = language

    if "output_format" in data:
        fmt = str(data["output_format"]).strip().lower().lstrip(".")
        if fmt not in OUTPUT_FORMATS:
            raise SettingsError(f"Invalid output_format: {fmt}")
        updates["H0MELAB_NAMING_TEMPLATE"] = _template_with_extension(fmt)

    if "provider_fallback_order" in data:
        _collect_provider_order(data["provider_fallback_order"], updates)

    if "hls_concurrency" in data:
        raw = data["hls_concurrency"]
        if isinstance(raw, bool):
            raise SettingsError("hls_concurrency must be an integer")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise SettingsError("hls_concurrency must be an integer") from None
        if str(raw).strip() != str(value) or not MIN_HLS_CONCURRENCY <= value <= MAX_HLS_CONCURRENCY:
            raise SettingsError(
                f"hls_concurrency must be between {MIN_HLS_CONCURRENCY} and {MAX_HLS_CONCURRENCY}"
            )
        updates["H0MELAB_HLS_CONCURRENCY"] = str(value)

    if "upcoming_checks_per_day" in data:
        raw = data["upcoming_checks_per_day"]
        if isinstance(raw, bool):
            raise SettingsError("upcoming_checks_per_day must be an integer")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise SettingsError("upcoming_checks_per_day must be an integer") from None
        if str(raw).strip() != str(value) or not MIN_UPCOMING_CHECKS_PER_DAY <= value <= MAX_UPCOMING_CHECKS_PER_DAY:
            raise SettingsError(
                f"upcoming_checks_per_day must be between {MIN_UPCOMING_CHECKS_PER_DAY} and {MAX_UPCOMING_CHECKS_PER_DAY}"
            )
        updates[UPCOMING_CHECKS_KEY] = str(value)

    _collect_autosync_schedule(data, updates)
    _check_a_site_is_left(data)

    discord_changed = "discord" in data
    if discord_changed:
        _collect_discord(data["discord"], updates)
    if "tmdb" in data:
        _collect_tmdb(data["tmdb"], updates)

    # Persist first. If the disk is read-only/full, the API must not claim a
    # successful save or leave a runtime-only change that vanishes on restart.
    _persist_settings(updates)
    for key, value in updates.items():
        os.environ[key] = value

    return discord_changed


# ---------------------------------------------------------------------------
# Exporting what is running
#
# Panel changes already persist. This snapshot is still useful as a backup or
# as a starting point for a separately managed deployment configuration.
#
# Secrets are left out on purpose. The Discord token is already kept in the
# panel-owned settings file, and nothing sensitive should end up in an export
# that may be copied into a downloads folder.
# ---------------------------------------------------------------------------
def _env_sections():
    """(heading, [(key, value)]) in the order they should be written."""
    discord = discord_settings()
    return [
        (
            "General",
            [
                ("H0MELAB_DOWNLOAD_PATH", str(paths.default_download_path())),
                ("H0MELAB_UI_LANGUAGE", ui_language()),
            ],
        ),
        (
            "Downloads",
            [
                ("H0MELAB_NAMING_TEMPLATE", _naming_template()),
                (
                    "H0MELAB_PROVIDER_FALLBACK_ORDER",
                    ",".join(get_provider_fallback_order(WORKING_PROVIDERS)),
                ),
                ("H0MELAB_HLS_CONCURRENCY", str(hls_concurrency())),
                (UPCOMING_CHECKS_KEY, str(upcoming_checks_per_day())),
                *( (env_key, str(media_library_path(kind))) for kind, env_key in MEDIA_LIBRARY_KEYS.items() ),
                (
                    "H0MELAB_LANG_SEPARATION",
                    _one_or_zero(paths.lang_separation_enabled()),
                ),
                ("H0MELAB_DISABLE_ENGLISH_SUB", _one_or_zero(english_sub_disabled())),
                (
                    "H0MELAB_MOVIE_FOLDER",
                    _one_or_zero(_flag("H0MELAB_MOVIE_FOLDER", "1")),
                ),
            ],
        ),
        (
            "Sites",
            [
                (site_env_key(site), _one_or_zero(state))
                for site, state in enabled_sites().items()
            ],
        ),
        (
            "Library and Auto-Sync",
            [
                ("H0MELAB_ENABLE_LIBRARY", _one_or_zero(library_enabled())),
                ("H0MELAB_ENABLE_AUTOSYNC", _one_or_zero(autosync_enabled())),
                ("H0MELAB_AUTOSYNC_NEW_ONLY", _one_or_zero(autosync_new_only())),
                ("H0MELAB_AUTOSYNC_MODE", autosync_mode()),
                ("H0MELAB_AUTOSYNC_INTERVAL", autosync_interval()),
                ("H0MELAB_AUTOSYNC_CRON", autosync_cron()),
            ],
        ),
        (
            "Discord bot (the token is not included in this export)",
            [
                (DISCORD_KEYS["enabled"], _one_or_zero(discord["enabled"])),
                (DISCORD_KEYS["owner_id"], discord["owner_id"]),
                (DISCORD_KEYS["mode"], discord["mode"]),
                (DISCORD_KEYS["language"], discord["language"]),
                (DISCORD_KEYS["request_role_id"], discord["request_role_id"]),
                (DISCORD_KEYS["guild_id"], discord["guild_id"]),
                (DISCORD_KEYS["announce_channel_id"], discord["announce_channel_id"]),
            ],
        ),
    ]


def _one_or_zero(state):
    return "1" if state else "0"


def _env_value(value):
    """Quote the way the shipped .env.example does, only where it is needed."""
    value = "" if value is None else str(value)
    if value and value[0] in "\"'" and value[-1] == value[0]:
        return value
    return f'"{value}"' if any(ch in value for ch in " \t#") else value


def export_env():
    """The running settings as the text of a .env file."""
    lines = [
        "# H0melab Downloader settings, exported from the web UI.",
        "#",
        "# These are the values this instance is running with right now. Panel",
        "# changes are already stored persistently; this file is a portable backup",
        "# or a starting point for a separately managed deployment configuration.",
        "#",
        "# Passwords and tokens are deliberately not in here: the Discord bot token,",
        "# TMDB key, OIDC client secret and any admin password stay where they are.",
    ]
    for heading, entries in _env_sections():
        lines.append("")
        lines.append(f"# ===== {heading} =====")
        lines.extend(
            f"{modern_env_key(key)}={_env_value(value)}" for key, value in entries
        )
    lines.extend(
        [
            "",
            "# ===== v1 rollback aliases (read-only compatibility) =====",
            "# These aliases let the same export boot one final v1 container during rollback.",
        ]
    )
    for _heading, entries in _env_sections():
        lines.extend(
            f"{key}={_env_value(value)}"
            for key, value in entries
            if key.startswith("H0MELAB_")
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public IP lookup (only run when the user presses reveal)
# ---------------------------------------------------------------------------
def fetch_public_ip():
    last_error = None
    for url in _IP_LOOKUP_URLS:
        try:
            response = requests.get(
                url, headers={"User-Agent": "H0melab Downloader"}, timeout=5
            )
            response.raise_for_status()
            payload = response.json()
            ip = (payload.get("ip") or payload.get("ip_addr") or "").strip()
            if ip:
                return {"ip": ip, "source": url}
            last_error = "No IP address returned by upstream service"
        except requests.RequestException as exc:
            last_error = str(exc)
        except ValueError as exc:
            last_error = f"Invalid response: {exc}"
    raise RuntimeError(last_error or "Failed to resolve public IP")
