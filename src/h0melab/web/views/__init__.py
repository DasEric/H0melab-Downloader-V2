"""Blueprint registration.

Pages live under `pages.*`, everything JSON under `api.*`. The `api.` prefix is
what the auth layer uses to decide CSRF exemption, so keep it.
"""

from flask import Blueprint

from . import (
    api_autosync,
    api_catalog,
    api_keys,
    api_library,
    api_media,
    api_queue,
    api_settings,
    api_upcoming,
    pages,
)

# Endpoints that need an admin account rather than just a login.
ADMIN_ENDPOINTS = {
    "pages.settings",
    "api.get_settings",
    "api.update_settings",
    "api.public_ip",
    "api.preview_schedule",
    "api.export_env",
    "api.path_preview",
    "api.get_custom_css",
    "api.update_custom_css",
    "api.get_custom_shader",
    "api.update_custom_shader",
    "api.discord_status",
    "api.add_custom_path",
    "api.update_custom_path",
    "api.delete_custom_path",
    "api.delete_library_item",
    "pages.autosync",
    "pages.upcoming",
    "api.upcoming_list",
    "api.upcoming_add",
    "api.upcoming_update",
    "api.upcoming_delete",
    "api.upcoming_search",
    "api.upcoming_browse",
    "api.upcoming_run",
    "api.autosync_status",
    "api.autosync_run",
    "api.list_series",
    "api.series_state",
    "api.add_series",
    "api.update_series",
    "api.delete_series",
    "api.list_exclusions",
    "api.add_exclusion",
    "api.delete_exclusion",
    "api.exclusion_state",
    "api.set_exclusion_state",
    "api.list_api_keys",
    "api.create_api_key",
    "api.delete_api_key",
}


def register_blueprints(app):
    app.register_blueprint(pages.bp)

    api = Blueprint("api", __name__, url_prefix="/api")
    for module in (
        api_media,
        api_catalog,
        api_queue,
        api_settings,
        api_library,
        api_autosync,
        api_keys,
        api_upcoming,
    ):
        module.register(api)
    app.register_blueprint(api)
