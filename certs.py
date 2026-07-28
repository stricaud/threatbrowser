"""Keep the TLS CA bundle on a path that survives macOS temp purges.

PyInstaller --onefile extracts bundled data files (including certifi's
cacert.pem) into $TMPDIR/_MEIxxxx.  macOS periodically deletes files under
/var/folders/.../T/ that have not been accessed for a few days, so a
long-running server eventually loses its CA bundle while still holding the
now-dangling certifi path.  Every HTTPS fetch then fails with:

    Could not find a suitable TLS CA certificate bundle, invalid path:
    /var/folders/../T/_MEIxxxx/certifi/cacert.pem

install() copies the bundle once into the app's data directory and points
certifi / requests / httpx / curl_cffi at that copy.  The bundle bytes are also
held in memory, so ensure() and repair() can rewrite the file even after
_MEIPASS has been purged.
"""

import logging
import os
import ssl
import sys

log = logging.getLogger(__name__)

# How the missing bundle surfaces: requests (adapters.cert_verify), then the
# lower-level ssl/libcurl variants of the same "the CA file is gone" failure.
_CA_ERROR_MARKERS = (
    "could not find a suitable tls ca certificate bundle",
    "cannot open ca file",
    "ca cert file",
    "no such file or directory: cafile",
)

HELP_TEXT = (
    "The TLS certificate bundle was missing (macOS purges the app's temporary "
    "files after a few days). It has been rebuilt automatically — fetch again. "
    "If it keeps happening, open Settings → Repair TLS certificates."
)

CA_BUNDLE: str | None = None   # set by install()
_CA_BYTES: bytes | None = None  # in-memory copy, survives a _MEIPASS purge
_ORIGIN = "unknown"


def _meipass() -> str | None:
    return getattr(sys, "_MEIPASS", None)


def _bundle_dir() -> str:
    """A directory that macOS will not purge, and that is never inside _MEIPASS."""
    candidates = []
    if os.environ.get("TB_CERTS"):
        candidates.append(os.environ["TB_CERTS"])
    if os.environ.get("TB_DB"):
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(os.environ["TB_DB"])), "certs"))
    if os.environ.get("TB_CACHE"):
        candidates.append(os.path.join(os.environ["TB_CACHE"], "certs"))
    if not getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "certs"))

    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support", "ThreatBrowser")
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        base = os.path.join(base, "ThreatBrowser")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        base = os.path.join(base, "threatbrowser")
    candidates.append(os.path.join(base, "certs"))

    mei = _meipass()
    for path in candidates:
        full = os.path.abspath(path)
        # A bundle inside _MEIPASS (or anywhere under $TMPDIR) is exactly the
        # thing we are trying to escape.
        if mei and (full == mei or full.startswith(mei + os.sep)):
            continue
        return full
    return os.path.abspath(candidates[-1])


def _read_source_bundle() -> tuple[bytes, str]:
    """Read the freshest CA bundle we can find, before anything gets purged."""
    try:
        import certifi
        with open(certifi.where(), "rb") as fh:
            return fh.read(), "certifi"
    except Exception as exc:
        log.warning("certifi bundle unreadable (%s) — falling back", exc)

    try:  # curl_cffi ships its own copy
        import curl_cffi
        path = os.path.join(os.path.dirname(curl_cffi.__file__), "cacert.pem")
        with open(path, "rb") as fh:
            return fh.read(), "curl_cffi"
    except Exception:
        pass

    cafile = ssl.get_default_verify_paths().cafile
    if cafile and os.path.exists(cafile):
        with open(cafile, "rb") as fh:
            return fh.read(), "system"

    raise RuntimeError("no CA certificate bundle available on this system")


def _write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)  # atomic — a concurrent fetch never sees a half file


def _point_libraries_at(path: str) -> None:
    """Make every HTTP client in the process use `path` as its trust store."""
    # libcurl (curl_cffi) and anything reading the standard env vars.
    os.environ["SSL_CERT_FILE"] = path
    os.environ["REQUESTS_CA_BUNDLE"] = path
    os.environ["CURL_CA_BUNDLE"] = path

    # certifi.where() is what requests and httpx call; it still returns the dead
    # _MEIPASS path, so override it at the source.
    try:
        import certifi
        import certifi.core
        certifi.core.where = lambda: path
        certifi.where = lambda: path
    except Exception:
        pass

    # requests snapshots certifi.where() into a module constant at import time.
    try:
        import requests.adapters
        import requests.utils
        requests.utils.DEFAULT_CA_BUNDLE_PATH = path
        requests.adapters.DEFAULT_CA_BUNDLE_PATH = path
    except Exception:
        pass


def install() -> str | None:
    """Create the stable bundle and redirect all clients at it. Call once, early."""
    global CA_BUNDLE, _CA_BYTES, _ORIGIN
    try:
        _CA_BYTES, _ORIGIN = _read_source_bundle()
    except Exception as exc:
        log.error("Could not load a CA bundle: %s — HTTPS fetches may fail", exc)
        return None

    CA_BUNDLE = os.path.join(_bundle_dir(), "cacert.pem")
    try:
        ensure()
    except Exception as exc:
        log.error("Could not write CA bundle to %s: %s", CA_BUNDLE, exc)
        CA_BUNDLE = None
        return None

    _point_libraries_at(CA_BUNDLE)
    log.info("TLS CA bundle: %s (%d bytes, from %s)", CA_BUNDLE, len(_CA_BYTES), _ORIGIN)
    return CA_BUNDLE


def ensure() -> str | None:
    """Recreate the bundle if it went missing or was truncated. Cheap; call freely."""
    if not CA_BUNDLE or _CA_BYTES is None:
        return None
    try:
        if os.path.getsize(CA_BUNDLE) == len(_CA_BYTES):
            return CA_BUNDLE
    except OSError:
        pass
    _write(CA_BUNDLE, _CA_BYTES)
    log.warning("TLS CA bundle was missing — rewrote %s", CA_BUNDLE)
    return CA_BUNDLE


def repair() -> dict:
    """Force a rewrite and re-point every client. Backs the Settings button."""
    if CA_BUNDLE is None or _CA_BYTES is None:
        raise RuntimeError("no CA bundle was loaded at startup — restart ThreatBrowser")
    _write(CA_BUNDLE, _CA_BYTES)
    _point_libraries_at(CA_BUNDLE)
    return status()


def status() -> dict:
    return {
        "path": CA_BUNDLE,
        "exists": bool(CA_BUNDLE and os.path.exists(CA_BUNDLE)),
        "bytes": len(_CA_BYTES) if _CA_BYTES else 0,
        "origin": _ORIGIN,
    }


def is_ca_bundle_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CA_ERROR_MARKERS)


def describe_error(exc: BaseException) -> str:
    """Error string for the UI — CA-bundle failures get repair instructions."""
    msg = str(exc)
    if is_ca_bundle_error(exc):
        return f"{msg} — {HELP_TEXT}"
    return msg
