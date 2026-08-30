"""Toast images, read from the Windows notification database.

The listener hands out only the text elements of a toast, so the images a notification
carries cannot be reached through it. Windows itself keeps the raw toast XML in a SQLite
database of its own, and the images it draws are local files referenced from that payload.
This module reads the payload and turns those references into file paths. A source Windows
had to fetch over the network is looked up in the record it keeps of its own downloads, so
nothing here goes to the network either. It never decodes an image: that is the GUI thread's
job, the same way app icons are already handled.
"""

import logging
import os
import sqlite3
import winreg
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

_LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", ""))
NOTIFICATION_DATABASE = _LOCAL_APP_DATA / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"
# Windows keeps the database open, so it is only ever opened read-only and released again
CONNECT_TIMEOUT = 0.5
# Toast images are small by design, anything past this is decoding cost with nothing to show
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# ms-appdata:///<root>/ names the per package folders that live under LOCALAPPDATA\Packages
APPDATA_ROOTS = {"local": "LocalState", "roaming": "RoamingState", "temp": "TempState"}
# Packaged resources are stored per display scale, so the plain name is often missing
SCALE_QUALIFIERS = ("", ".scale-200", ".scale-100", ".scale-150", ".scale-400")
# Windows notes every toast image it downloads here, which is what makes a remote source
# reachable without asking the network for it a second time
DOWNLOAD_RECORD = r"Software\Microsoft\Windows\CurrentVersion\PushNotifications\wpnidm"
DOWNLOAD_URL_PREFIX = "wpnidm:"

# (registry write time, url -> file), rebuilt only once Windows has touched the record
_downloads: tuple[int, dict[str, str]] | None = None


@dataclass(frozen=True, slots=True)
class ToastImages:
    """The images of a single toast, as local file paths.

    Plain types only: these travel from the listener thread to the GUI thread, like the
    notification they belong to.
    """

    app_logo: str = ""  # placement="appLogoOverride"
    app_logo_circle: bool = False  # hint-crop="circle"
    hero: str = ""  # placement="hero"
    inline: tuple[str, ...] = ()  # everything else, in payload order


def read_toast_images(senders: dict[int, str]) -> dict[int, ToastImages]:
    """Return the images of the given notifications, keyed by notification id.

    `senders` maps a notification id to the AUMID that sent it, which is needed both to
    resolve package relative sources and to confirm the row is the notification we asked
    for. Notifications without a usable image are left out of the result.
    """
    if not senders or not NOTIFICATION_DATABASE.is_file():
        return {}

    try:
        rows = _read_payloads(list(senders))
    except (sqlite3.Error, OSError) as e:
        logging.debug("Failed to read the notification database: %s", e)
        return {}

    images: dict[int, ToastImages] = {}
    for notification_id, payload, primary_id in rows:
        aumid = senders.get(notification_id, "")
        # The id is the only link between the listener and the database, so a row whose
        # sender disagrees is treated as somebody else's notification rather than trusted
        if aumid and primary_id and not _same_sender(aumid, primary_id):
            logging.debug("Notification %s is held for %s, not %s", notification_id, primary_id, aumid)
            continue
        parsed = _parse_payload(payload, aumid)
        if parsed is not None:
            images[notification_id] = parsed
    return images


def _same_sender(aumid: str, primary_id: str) -> bool:
    """Whether a stored row belongs to the sender the listener named.

    The two do not always spell it the same way. A browser files a website's notifications
    under an identifier of its own for that site, while the listener reports them as coming
    from the browser, so the names agree only as far as the package they share.
    """
    if aumid.casefold() == primary_id.casefold():
        return True
    family_name = _package_family_name(aumid)
    return bool(family_name) and family_name.casefold() == _package_family_name(primary_id).casefold()


def _read_payloads(notification_ids: list[int]) -> list[tuple[int, bytes, str]]:
    """Read the stored payloads in one query, without taking a write lock on the database."""
    placeholders = ",".join("?" * len(notification_ids))
    query = (
        "SELECT n.Id, n.Payload, h.PrimaryId FROM Notification n "
        "LEFT JOIN NotificationHandler h ON h.RecordId = n.HandlerId "
        f"WHERE n.Type = 'toast' AND n.PayloadType = 'Xml' AND n.Id IN ({placeholders})"
    )
    uri = f"file:{quote(NOTIFICATION_DATABASE.as_posix())}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=CONNECT_TIMEOUT)
    try:
        return [(row[0], row[1], row[2] or "") for row in connection.execute(query, notification_ids)]
    finally:
        connection.close()


def _parse_payload(payload: bytes | str, aumid: str) -> ToastImages | None:
    """Pull the image elements out of a toast payload, keeping the first of each placement."""
    try:
        root = ET.fromstring(payload.decode("utf-8") if isinstance(payload, bytes) else payload)
    except (ET.ParseError, UnicodeDecodeError, TypeError) as e:
        logging.debug("Unreadable toast payload: %s", e)
        return None

    app_logo = ""
    app_logo_circle = False
    hero = ""
    inline: list[str] = []

    for element in root.iter("image"):
        path = _resolve_src(element.get("src", ""), aumid)
        if not path:
            continue
        placement = (element.get("placement") or "").casefold()
        if placement == "applogooverride":
            if not app_logo:
                app_logo = path
                app_logo_circle = (element.get("hint-crop") or "").casefold() == "circle"
        elif placement == "hero":
            hero = hero or path
        else:
            inline.append(path)

    if not (app_logo or hero or inline):
        return None
    return ToastImages(app_logo, app_logo_circle, hero, tuple(inline))


def _resolve_src(src: str, aumid: str) -> str:
    """Turn the src of a toast image into a local file path, or an empty string if it has none."""
    src = src.strip()
    scheme = src.split(":", 1)[0].casefold() if ":" in src else ""

    if not src:
        return ""
    if scheme in ("http", "https"):
        return _resolve_downloaded(src)
    if scheme == "file":
        return _verify(_file_url_to_path(src))
    if scheme == "ms-appdata":
        return _resolve_appdata(src, aumid)
    if scheme == "ms-appx":
        return _resolve_appx(src, aumid)
    return _verify(Path(os.path.expandvars(src)))


def _file_url_to_path(src: str) -> Path:
    """Turn a file: url into a path, whichever way the sender happened to write it.

    Senders are not consistent about this. A screenshot tool writes the separators as
    backslashes and percent encodes them along with the colon after the drive letter, which
    the standard conversion does not recognise as a drive at all and turns into a path that
    cannot be opened.
    """
    parsed = urlparse(src)
    path = unquote(parsed.path).replace("\\", "/")
    if parsed.netloc:
        return Path(f"//{parsed.netloc}{path}")
    # A drive letter arrives as /C:/..., which is not a path until the leading slash goes
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path)


def _resolve_appdata(src: str, aumid: str) -> str:
    """Resolve ms-appdata:///local/... against the sending package's own data folder."""
    family_name = _package_family_name(aumid)
    if not family_name or not _LOCAL_APP_DATA.name:
        return ""

    root_name, _, relative = urlparse(src).path.lstrip("/").partition("/")
    folder = APPDATA_ROOTS.get(root_name.casefold())
    if not folder or not relative:
        return ""

    root = _LOCAL_APP_DATA / "Packages" / family_name / folder
    return _verify(root / unquote(relative), root)


def _resolve_appx(src: str, aumid: str) -> str:
    """Resolve ms-appx:///Assets/... against the sending package's install folder."""
    root = _installed_path(_package_family_name(aumid))
    relative = unquote(urlparse(src).path.lstrip("/"))
    if root is None or not relative:
        return ""

    candidate = root / relative
    for qualifier in SCALE_QUALIFIERS:
        path = candidate if not qualifier else candidate.with_name(f"{candidate.stem}{qualifier}{candidate.suffix}")
        verified = _verify(path, root)
        if verified:
            return verified
    return ""


def _resolve_downloaded(url: str) -> str:
    """Find the copy Windows downloaded for a remote source, without fetching it again.

    Messaging apps send the sender's picture as an https URL, so this is the difference
    between showing a contact photo and showing the app icon. Windows has already fetched
    it to draw the toast, and notes where it put it. YASB never asks the network itself:
    requesting one of these would tell the sender when the menu was opened.
    """
    cached = _read_download_record().get(url, "")
    return _verify(Path(cached)) if cached else ""


def _read_download_record() -> dict[str, str]:
    """The url to file map Windows keeps, re-read only when it has changed."""
    global _downloads
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DOWNLOAD_RECORD) as key:
            entries, _, written = winreg.QueryInfoKey(key)
            if _downloads is None or _downloads[0] != written:
                _downloads = (written, _read_download_entries(key, entries))
    except OSError as e:
        logging.debug("No record of downloaded toast images: %s", e)
        return {}
    return _downloads[1]


def _read_download_entries(key, entries: int) -> dict[str, str]:
    downloads: dict[str, str] = {}
    for index in range(entries):
        try:
            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as entry:
                url = str(winreg.QueryValueEx(entry, "Url")[0])
                path = str(winreg.QueryValueEx(entry, "LocalPath")[0])
        except OSError:
            # An entry can be dropped while it is being enumerated, the rest are still good
            continue
        url = url.removeprefix(DOWNLOAD_URL_PREFIX)
        if url and path:
            downloads[url] = path
    return downloads


def _package_family_name(aumid: str) -> str:
    """The package family name of a packaged sender, which its AUMID is prefixed with."""
    return aumid.split("!")[0] if "!" in aumid else ""


@lru_cache(maxsize=32)
def _installed_path(family_name: str) -> Path | None:
    """Where a packaged app is installed, which is what ms-appx: is relative to."""
    if not family_name:
        return None
    try:
        from winrt.windows.management.deployment import PackageManager

        # The user scoped lookup is the one that works without elevation
        for package in PackageManager().find_packages_by_user_security_id_package_family_name("", family_name):
            installed_path = package.installed_path
            if installed_path:
                return Path(installed_path)
    except Exception as e:
        logging.debug("Failed to locate package %s: %s", family_name, e)
    return None


def _verify(path: Path, root: Path | None = None) -> str:
    """Accept a path only if it points at a readable file sized like a toast image.

    A package relative source is checked against the folder it was resolved from as well,
    since the payload is written by the sending app and can walk out of it.
    """
    try:
        resolved = path.resolve()
        if root is not None and not resolved.is_relative_to(root.resolve()):
            logging.debug("Toast image escapes its package folder: %s", path)
            return ""
        # A missing file is the normal case here, senders clean their temporary images up
        if not resolved.is_file() or resolved.stat().st_size > MAX_IMAGE_BYTES:
            return ""
    except (OSError, ValueError) as e:
        logging.debug("Unusable toast image path: %s", e)
        return ""
    return str(resolved)
