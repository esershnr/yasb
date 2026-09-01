"""Lookups for packaged (Store) applications.

An AUMID is the only name a notification carries, and for a packaged sender it is also the
way to its install folder, which is what the resources it names are relative to.
"""

import logging
from functools import lru_cache
from pathlib import Path


def package_family_name(aumid: str) -> str:
    """The package family name of a packaged sender, which its AUMID is prefixed with."""
    return aumid.split("!")[0] if "!" in aumid else ""


@lru_cache(maxsize=32)
def installed_path(family_name: str) -> Path | None:
    """Where a packaged app is installed, which is what ms-appx: is relative to."""
    if not family_name:
        return None
    try:
        from winrt.windows.management.deployment import PackageManager

        # The user scoped lookup is the one that works without elevation
        for package in PackageManager().find_packages_by_user_security_id_package_family_name("", family_name):
            path = package.installed_path
            if path:
                return Path(path)
    except Exception as e:
        logging.debug("Failed to locate package %s: %s", family_name, e)
    return None
