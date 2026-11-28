"""Browser-only application entry point."""

import os


def main():
    from .net_dns import install as install_dns
    from .web import start_web_ui

    # Before anything connects: the panel's DNS choice applies to every lookup
    install_dns()

    host = os.getenv("H0MELAB_WEB_HOST", "0.0.0.0")
    try:
        port = int(os.getenv("H0MELAB_WEB_PORT", "8080"))
    except ValueError:
        port = 8080
    open_browser = os.getenv("H0MELAB_OPEN_BROWSER", "0") == "1"
    start_web_ui(host=host, port=port, open_browser=open_browser)
    return 0
