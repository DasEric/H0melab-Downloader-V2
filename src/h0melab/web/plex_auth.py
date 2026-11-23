"""Short-lived, server-side Plex PIN login; no account tokens in browser responses."""

import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import niquests

from . import db, plex, settings_store
from .settings_store import SettingsError

COOKIE = "h0melab_plex_login"
TTL = 600


def _schema(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS plex_login (id TEXT PRIMARY KEY, expires REAL NOT NULL, data TEXT NOT NULL)"
    )
    conn.execute("DELETE FROM plex_login WHERE expires <= ?", (time.time(),))


def _key(secret):
    if not isinstance(secret, str) or len(secret) != 64:
        raise SettingsError("Plex-Anmeldung abgelaufen. Bitte erneut anmelden.")
    return hashlib.sha256(secret.encode()).hexdigest()


def _cloud(method, path, client, token=None, **kwargs):
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": "H0melab Downloader",
        "X-Plex-Client-Identifier": client,
    }
    if token:
        headers["X-Plex-Token"] = token
    try:
        with niquests.Session() as session:
            response = session.request(
                method,
                "https://plex.tv/api/v2/" + path,
                headers=headers,
                timeout=15,
                allow_redirects=False,
                **kwargs,
            )
            if response.status_code not in (200, 201):
                raise SettingsError(
                    f"Plex-Anmeldung: HTTP {response.status_code}. Bitte erneut versuchen."
                )
            return response.json()
    except SettingsError:
        raise
    except Exception:
        raise SettingsError(
            "Plex-Anmeldedienst nicht erreichbar oder Antwort ungültig."
        ) from None


def start(previous=None):
    client = secrets.token_hex(16)
    pin = _cloud("POST", "pins", client, data={"strong": "true"})
    if (
        not isinstance(pin, dict)
        or type(pin.get("id")) is not int
        or not isinstance(pin.get("code"), str)
    ):
        raise SettingsError("Plex liefert keine gültige Anmeldung.")
    secret = secrets.token_hex(32)
    data = {"client": client, "pin": pin["id"], "code": pin["code"]}
    lifetime = pin.get("expiresIn", TTL)
    lifetime = min(TTL, max(1, lifetime)) if type(lifetime) is int else TTL
    with db.session() as conn:
        _schema(conn)
        if previous and len(previous) == 64:
            conn.execute("DELETE FROM plex_login WHERE id=?", (_key(previous),))
        if conn.execute("SELECT COUNT(*) FROM plex_login").fetchone()[0] >= 20:
            raise SettingsError(
                "Zu viele offene Plex-Anmeldungen. Bitte später erneut versuchen."
            )
        conn.execute(
            "INSERT INTO plex_login VALUES (?,?,?)",
            (_key(secret), time.time() + lifetime, json.dumps(data)),
        )
    url = "https://app.plex.tv/auth#?" + urlencode(
        {
            "clientID": client,
            "code": pin["code"],
            "context[device][product]": "H0melab Downloader",
        }
    )
    return secret, {"auth_url": url, "expires_in": lifetime}


def _load(secret):
    with db.session() as conn:
        _schema(conn)
        row = conn.execute(
            "SELECT data FROM plex_login WHERE id=?", (_key(secret),)
        ).fetchone()
    if row is None:
        raise SettingsError(
            "Plex-Anmeldung abgelaufen oder abgebrochen. Bitte erneut anmelden."
        )
    return json.loads(row["data"])


def _choices(data):
    return {
        "status": "ready",
        "servers": [
            {"id": str(i), "name": row["name"], "url": row["url"]}
            for i, row in enumerate(data["servers"])
        ],
    }


def poll(secret):
    data = _load(secret)
    if "servers" in data:
        return _choices(data)
    pin = _cloud(
        "GET", "pins/" + str(data["pin"]), data["client"], params={"code": data["code"]}
    )
    if (
        not isinstance(pin, dict)
        or pin.get("id") != data["pin"]
        or pin.get("code") != data["code"]
    ):
        raise SettingsError("Plex-Anmeldung stimmt nicht mit dieser Sitzung überein.")
    token = pin.get("authToken")
    if not token:
        return {"status": "pending"}
    if not isinstance(token, str):
        raise SettingsError("Ungültige Plex-Anmeldeantwort.")
    resources = _cloud(
        "GET",
        "resources",
        data["client"],
        token,
        params={"includeHttps": "1", "includeRelay": "0"},
    )
    if not isinstance(resources, list):
        raise SettingsError("Plex liefert keine gültige Serverliste.")
    servers = []
    for resource in resources:
        if (
            not isinstance(resource, dict)
            or resource.get("owned") is not True
            or "server" not in resource.get("provides", "").split(",")
        ):
            continue
        access = resource.get("accessToken")
        if not isinstance(access, str) or not access:
            continue
        connections = resource.get("connections", [])
        if not isinstance(connections, list):
            continue
        for connection in connections:
            if not isinstance(connection, dict) or connection.get("relay"):
                continue
            url = connection.get("uri")
            updates = {}
            try:
                plex.collect_settings({"url": url, "api_key": access}, updates)
            except SettingsError:
                continue
            if not updates[plex.KEYS["url"]]:
                continue
            servers.append(
                {
                    "name": str(resource.get("name") or "Plex Server"),
                    "url": updates[plex.KEYS["url"]],
                    "token": access,
                }
            )
    data = {"servers": servers}
    with db.session() as conn:
        _schema(conn)
        changed = conn.execute(
            "UPDATE plex_login SET data=? WHERE id=?", (json.dumps(data), _key(secret))
        ).rowcount
    if not changed:
        raise SettingsError("Plex-Anmeldung wurde beendet. Bitte erneut anmelden.")
    return _choices(data)


def select(secret, selection, url=None):
    data = _load(secret)
    if (
        not isinstance(selection, str)
        or not selection.isdecimal()
        or len(selection) > 5
    ):
        raise SettingsError("Bitte einen angebotenen Plex-Server auswählen.")
    servers = data.get("servers", [])
    index = int(selection)
    if index >= len(servers):
        raise SettingsError("Bitte einen angebotenen Plex-Server auswählen.")
    server = dict(servers[index])
    if url is not None:
        if not isinstance(url, str):
            raise SettingsError("Plex-Server-Adresse muss eine URL sein.")
        if url.strip():
            validated = {}
            plex.collect_settings({"url": url}, validated)
            server["url"] = validated[plex.KEYS["url"]]
    # Probe from the downloader before replacing any previously working settings.
    rows = plex.libraries({"url": server["url"], "api_key": server["token"]})
    with db.session() as conn:
        _schema(conn)
        if not conn.execute(
            "SELECT 1 FROM plex_login WHERE id=?", (_key(secret),)
        ).fetchone():
            raise SettingsError("Plex-Anmeldung wurde beendet. Bitte erneut anmelden.")
        settings_store.update_settings(
            {
                "plex": {
                    "url": server["url"],
                    "api_key": server["token"],
                    "movie_library_id": "",
                    "series_library_id": "",
                }
            }
        )
        conn.execute("DELETE FROM plex_login WHERE id=?", (_key(secret),))
    return {"status": "connected", "url": server["url"], "libraries": rows}


def cancel(secret):
    if secret and len(secret) == 64:
        with db.session() as conn:
            _schema(conn)
            conn.execute("DELETE FROM plex_login WHERE id=?", (_key(secret),))
    return {"status": "cancelled"}
