"""Handing a notification back to the app that sent it, the way the shell does.

Clicking a notification in the Notification Center does not just raise the sending app: it
gives the app the launch string of that particular toast, which is how a screenshot
notification opens the screenshot and a browser notification opens the page behind it.
The listener API has nothing for this, so what the shell does is done here instead.

There are two ways in, and which one a toast wants is written in its payload. A toast that
asks for protocol activation names a URI, which is opened like any other link. Everything
else goes to the sender's toast activator, a COM class the app registers for exactly this
call, either in the registry for a plain desktop app or in the manifest for a packaged one.
A sender that has neither cannot be handed anything, and the caller is told so.
"""

import ctypes
import ctypes.wintypes as wt
import logging
import winreg
import xml.etree.ElementTree as ET
from ctypes import POINTER, WINFUNCTYPE, byref, c_void_p
from functools import lru_cache

from core.utils.win32.aumid import GUID
from core.utils.win32.packages import installed_path, package_family_name

ole32 = ctypes.WinDLL("ole32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

CoInitializeEx = ole32.CoInitializeEx
CoInitializeEx.argtypes = [c_void_p, ctypes.c_ulong]
CoInitializeEx.restype = ctypes.c_long

CoCreateInstance = ole32.CoCreateInstance
CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, ctypes.c_ulong, POINTER(GUID), POINTER(c_void_p)]
CoCreateInstance.restype = ctypes.c_long

ShellExecuteW = shell32.ShellExecuteW
ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int]
ShellExecuteW.restype = wt.HINSTANCE

AllowSetForegroundWindow = user32.AllowSetForegroundWindow
AllowSetForegroundWindow.argtypes = [wt.DWORD]
AllowSetForegroundWindow.restype = wt.BOOL

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_LOCAL_SERVER = 0x4
SW_SHOWNORMAL = 1
# ShellExecute reports failure as a value that would be a meaningless instance handle
SHELL_EXECUTE_MIN_SUCCESS = 32
# Let whichever process the notification goes to take the foreground, which it is only
# allowed to do because the click that got us here happened in a window of ours
ASFW_ANY = wt.DWORD(-1)

IID_INotificationActivationCallback = GUID("53E31837-6600-4A81-9395-75CFFE746F94")
# Where a plain desktop app registers the class the shell calls back into. A packaged app
# declares it in its manifest instead, under this element
CUSTOM_ACTIVATOR = "CustomActivator"
ACTIVATION_ELEMENT = "ToastNotificationActivation"
ACTIVATION_ATTRIBUTE = "ToastActivatorCLSID"
# Toasts whose activation the shell handles itself, with nothing to hand the sender
SHELL_ACTIVATION_TYPES = ("system",)


class INotificationActivationCallbackVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))),
        ("AddRef", WINFUNCTYPE(ctypes.c_ulong, c_void_p)),
        ("Release", WINFUNCTYPE(ctypes.c_ulong, c_void_p)),
        # Activate(LPCWSTR appUserModelId, LPCWSTR invokedArgs, NOTIFICATION_USER_INPUT_DATA*, ULONG)
        (
            "Activate",
            WINFUNCTYPE(ctypes.c_long, c_void_p, wt.LPCWSTR, wt.LPCWSTR, c_void_p, ctypes.c_ulong),
        ),
    ]


class INotificationActivationCallback(ctypes.Structure):
    _fields_ = [("lpVtbl", POINTER(INotificationActivationCallbackVtbl))]


def activate_toast(aumid: str, launch: str, activation_type: str) -> bool:
    """Act on a notification the way clicking it in the Notification Center does.

    Returns whether the sender was actually handed the notification, so a caller can fall
    back to merely raising it for a sender that registered no way of taking one.
    """
    activation_type = activation_type.casefold()
    if activation_type in SHELL_ACTIVATION_TYPES:
        return False
    if activation_type == "protocol":
        return _open_protocol(launch)
    return _invoke_activator(aumid, launch)


def _open_protocol(launch: str) -> bool:
    """Open the URI a toast names, which is what protocol activation amounts to."""
    if not launch:
        return False
    AllowSetForegroundWindow(ASFW_ANY)
    result = ShellExecuteW(None, "open", launch, None, None, SW_SHOWNORMAL)
    if result and result > SHELL_EXECUTE_MIN_SUCCESS:
        return True
    logging.debug("Failed to open the notification protocol %s (%s)", launch, result)
    return False


def _invoke_activator(aumid: str, launch: str) -> bool:
    """Call the sender's toast activator with the launch string of the notification."""
    clsid = toast_activator_clsid(aumid)
    if not clsid:
        return False

    # An apartment of some kind is needed before the class can be created, and the caller
    # is not always one that has been in COM before
    CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    activator = c_void_p()
    hr = CoCreateInstance(
        byref(GUID(clsid)),
        None,
        CLSCTX_LOCAL_SERVER,
        byref(IID_INotificationActivationCallback),
        byref(activator),
    )
    if hr != 0 or not activator.value:
        logging.debug("The toast activator of %s could not be created (0x%08X)", aumid, hr & 0xFFFFFFFF)
        return False

    callback = ctypes.cast(activator, POINTER(INotificationActivationCallback))
    try:
        AllowSetForegroundWindow(ASFW_ANY)
        # No user input: the menu shows a notification, it does not offer its input fields
        hr = callback.contents.lpVtbl.contents.Activate(activator, aumid, launch, None, 0)
        if hr != 0:
            logging.debug("%s refused the notification (0x%08X)", aumid, hr & 0xFFFFFFFF)
            return False
        return True
    except Exception:
        logging.exception("Failed to hand a notification back to %s", aumid)
        return False
    finally:
        try:
            callback.contents.lpVtbl.contents.Release(activator)
        except Exception:
            pass


@lru_cache(maxsize=64)
def toast_activator_clsid(aumid: str) -> str:
    """The class the sender registered to be called back on, or an empty string if none."""
    if not aumid:
        return ""
    return _registered_activator(aumid) or _declared_activator(aumid)


def _registered_activator(aumid: str) -> str:
    """What a plain desktop app writes under its AUMID when it registers for toasts."""
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"AppUserModelId\{aumid}") as key:
            return str(winreg.QueryValueEx(key, CUSTOM_ACTIVATOR)[0]).strip()
    except OSError:
        return ""


def _declared_activator(aumid: str) -> str:
    """What a packaged app declares in its manifest instead of registering one."""
    root = installed_path(package_family_name(aumid))
    if root is None:
        return ""
    manifest = root / "AppxManifest.xml"
    try:
        tree = ET.parse(manifest)
    except (ET.ParseError, OSError) as e:
        logging.debug("Unreadable manifest for %s: %s", aumid, e)
        return ""

    # The element is namespaced by the manifest schema it came from, which has changed
    # more than once, so it is matched on its local name
    for element in tree.iter():
        _, _, name = element.tag.rpartition("}")
        if name == ACTIVATION_ELEMENT:
            clsid = (element.get(ACTIVATION_ATTRIBUTE) or "").strip()
            if clsid:
                return clsid
    return ""
