# H0melab Soulseek service

A small standalone program behind the Music tab. It keeps one Soulseek session
alive using [aioslsk](https://github.com/JurgenR/aioslsk) and offers search,
downloads and Soulseek's own recommendations to the downloader over a local
HTTP API on `127.0.0.1`, protected by a random token.

**License:** this directory is licensed under the GNU General Public License,
version 3 or later (see `LICENSE`), because it uses aioslsk. The rest of
H0melab Downloader stays under its own license. The downloader never imports
this code; it only starts it as a separate process and talks to it over HTTP.

## How it runs

You never start it by hand. When the Music tab is used, the downloader:

1. creates a private virtual environment in its config folder
   (`<config>/soulseek/venv`),
2. installs the newest aioslsk from PyPI into it (and checks for updates
   once a day),
3. writes `service-config.json` (Soulseek account, ports, folders, token)
   and starts `service.py` with that environment's Python,
4. restarts it if it stops.

If no Soulseek username is set, one is generated. The Soulseek server creates
an account automatically on its first login.
