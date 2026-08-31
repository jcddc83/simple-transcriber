"""Silent, best-effort yt-dlp self-update for the packaged app.

The frozen desktop app bundles a snapshot of yt-dlp taken at build time. That
snapshot goes stale as YouTube changes how it serves videos, and downloads
eventually start failing until a new release is built. To avoid that, on launch
we check PyPI in the background for a newer yt-dlp and stage it in a per-user
folder; the *next* launch activates it (hot-swapping an already-imported module
mid-process is fragile, so we deliberately defer to the next start).

Everything here is best-effort and wrapped so any failure — offline, blocked by
a firewall, a corrupt download, an incompatible package — silently falls back to
the bundled yt-dlp. The user never sees an error and nothing blocks startup.

Trust: the wheel is fetched from PyPI over HTTPS and verified against the
SHA-256 digest PyPI publishes for it before it's ever put on the import path.
This is the same source `pip install -U yt-dlp` would use.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path

_PYPI_JSON = "https://pypi.org/pypi/yt-dlp/json"
_TIMEOUT = 15  # seconds for each network call
_UA = "SimpleTranscriber-ytdlp-updater"


def _data_dir() -> Path:
    """Per-user, writable folder for the staged/active yt-dlp overlay."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "SimpleTranscriber"


# Active copy the app imports from; and the copy staged by a prior run's download.
_OVERLAY = _data_dir() / "ytdlp"
_PENDING = _data_dir() / "ytdlp_pending"


def _valid(root: Path) -> bool:
    """True if `root` holds an importable yt_dlp package."""
    try:
        return (root / "yt_dlp" / "__init__.py").is_file() and \
               (root / "yt_dlp" / "version.py").is_file()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Activation — runs at startup, BEFORE `import yt_dlp`
# ---------------------------------------------------------------------------

def activate_overlay() -> None:
    """Promote any staged update, then load an overlay yt-dlp if one exists.

    MUST be called before the app imports yt_dlp. The overlay package is loaded
    explicitly from its files and registered in sys.modules, so it wins even
    over a PyInstaller-frozen yt_dlp (a plain sys.path entry would not, because
    the frozen importer runs first). On ANY problem we clear the half-registered
    module so the bundled yt_dlp imports normally instead — a bad overlay can
    never break the app.
    """
    try:
        _promote_pending()
    except Exception:
        pass

    if not _valid(_OVERLAY):
        return

    try:
        import importlib.util
        init = _OVERLAY / "yt_dlp" / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            "yt_dlp", str(init),
            submodule_search_locations=[str(_OVERLAY / "yt_dlp")],
        )
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules["yt_dlp"] = module
        spec.loader.exec_module(module)
    except Exception:
        # Overlay unusable — drop the partial import so the bundled copy wins.
        sys.modules.pop("yt_dlp", None)


def _promote_pending() -> None:
    """Move a previously staged update into place. Safe at startup because
    nothing has imported the overlay yet, so no files under it are locked
    (this is what lets it work on Windows)."""
    if not _valid(_PENDING):
        return
    if _OVERLAY.exists():
        shutil.rmtree(_OVERLAY, ignore_errors=True)
    _PENDING.replace(_OVERLAY)


# ---------------------------------------------------------------------------
# Background check + download — runs after startup, never blocks
# ---------------------------------------------------------------------------

def start_background_update(active_version: str) -> None:
    """Kick off a silent, non-blocking check for a newer yt-dlp.

    Only runs in the packaged (frozen) app, where yt-dlp can't otherwise be
    updated. Set SIMPLETRANSCRIBER_YTDLP_AUTOUPDATE=1 to force it from source
    for testing.
    """
    if not getattr(sys, "frozen", False) and \
            os.environ.get("SIMPLETRANSCRIBER_YTDLP_AUTOUPDATE") != "1":
        return
    threading.Thread(
        target=_update_worker, args=(active_version,), daemon=True
    ).start()


def _update_worker(active_version: str) -> None:
    try:
        latest, url, sha256 = _pypi_latest()
        if not latest or not url:
            return
        # Nothing to do if the running copy — or one already staged — is current.
        if _ver(latest) <= _ver(active_version):
            return
        staged = _staged_version()
        if staged and _ver(latest) <= _ver(staged):
            return
        _download_and_stage(url, sha256)
    except Exception:
        pass  # fully best-effort; never surface anything


def _pypi_latest() -> tuple[str | None, str | None, str | None]:
    """(version, wheel_url, sha256) for the newest yt-dlp on PyPI."""
    req = urllib.request.Request(_PYPI_JSON, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    version = (data.get("info") or {}).get("version")
    for f in data.get("urls", []):
        # yt-dlp ships a pure-python wheel (…-py3-none-any.whl); prefer it.
        if f.get("packagetype") == "bdist_wheel" and \
                str(f.get("filename", "")).endswith("-none-any.whl"):
            return version, f.get("url"), (f.get("digests") or {}).get("sha256")
    return version, None, None


def _staged_version() -> str | None:
    try:
        vf = _PENDING / "yt_dlp" / "version.py"
        if not vf.is_file():
            return None
        for line in vf.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("__version__"):
                return line.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        return None
    return None


def _ver(v: str) -> tuple:
    """yt-dlp uses date versions like 2025.09.05[.1]; compare numerically."""
    out = []
    for chunk in str(v).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _download_and_stage(url: str, sha256: str | None) -> None:
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp_zip = d / ".ytdlp_dl.tmp"
    tmp_dir = d / ".ytdlp_extract.tmp"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        h = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp, \
                open(tmp_zip, "wb") as out:
            while True:
                buf = resp.read(65536)
                if not buf:
                    break
                h.update(buf)
                out.write(buf)
        if sha256 and h.hexdigest() != sha256:
            return  # integrity check failed — discard the download

        shutil.rmtree(tmp_dir, ignore_errors=True)
        with zipfile.ZipFile(tmp_zip) as z:
            z.extractall(tmp_dir)
        if not _valid(tmp_dir):
            return

        # Atomically stage for the next launch to promote.
        if _PENDING.exists():
            shutil.rmtree(_PENDING, ignore_errors=True)
        tmp_dir.replace(_PENDING)
    finally:
        try:
            tmp_zip.unlink()
        except OSError:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
