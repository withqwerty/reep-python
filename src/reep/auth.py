"""Find, store and remove the Reep API key.

The SDK looks for the key in this order:

1. the ``REEP_API_KEY`` environment variable;
2. the operating system keyring, when the optional ``keyring`` package is installed;
3. ``credentials.json`` in the SDK config directory (file mode 600).

The SDK sends the key only to the Reep API, as ``Authorization: Bearer <key>``.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from reep.config import Settings

__all__ = [
    "KEYRING_SERVICE",
    "KEYRING_USERNAME",
    "delete_key",
    "key_source",
    "load_key",
    "mask",
    "save_key",
]

logger = logging.getLogger(__name__)

#: The keyring service name.
KEYRING_SERVICE = "reep"
#: The keyring user name.
KEYRING_USERNAME = "api-key"

_FILE = "credentials.json"


def _keyring() -> Any | None:
    try:
        return importlib.import_module("keyring")
    except ImportError:
        return None


def _keyring_get() -> str | None:
    module = _keyring()
    if module is None:
        return None
    try:
        value = module.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except Exception as exc:
        logger.debug("Keyring lookup failed: %s", exc)
        return None
    return value if isinstance(value, str) and value else None


def _file_path(settings: Settings) -> Path:
    return settings.config_dir / _FILE


def _file_get(settings: Settings) -> str | None:
    path = _file_path(settings)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read %s: %s", path, exc)
        return None
    value = data.get("api_key") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def key_source(settings: Settings) -> str | None:
    """Return where the API key comes from: ``env``, ``keyring``, ``file`` or ``None``.

    Args:
        settings: The settings.

    Returns:
        The source, or ``None`` when there is no key.
    """
    if settings.api_key:
        return "env"
    if _keyring_get():
        return "keyring"
    if _file_get(settings):
        return "file"
    return None


def load_key(settings: Settings) -> str | None:
    """Return the API key, or ``None`` when there is none.

    Args:
        settings: The settings.

    Returns:
        The key.
    """
    return settings.api_key or _keyring_get() or _file_get(settings)


def save_key(key: str, settings: Settings, *, use_keyring: bool = True) -> str:
    """Store the API key.

    Args:
        key: The key.
        settings: The settings.
        use_keyring: Store the key in the keyring when the ``keyring`` package is
            installed and has a working backend.

    Returns:
        Where the key went: ``"keyring"`` or the path of the credentials file.
    """
    key = key.strip()
    if use_keyring:
        module = _keyring()
        if module is not None:
            try:
                module.set_password(KEYRING_SERVICE, KEYRING_USERNAME, key)
            except Exception as exc:
                logger.info("Keyring storage failed (%s); using the file", exc)
            else:
                return "keyring"
    path = _file_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"api_key": key}, fh)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return str(path)


def delete_key(settings: Settings) -> list[str]:
    """Remove the stored API key from the keyring and the credentials file.

    The ``REEP_API_KEY`` environment variable is not changed.

    Args:
        settings: The settings.

    Returns:
        Where a key was removed from.
    """
    removed: list[str] = []
    module = _keyring()
    if module is not None and _keyring_get():
        try:
            module.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
        except Exception as exc:
            logger.warning("Keyring removal failed: %s", exc)
        else:
            removed.append("keyring")
    path = _file_path(settings)
    if path.is_file():
        path.unlink()
        removed.append(str(path))
    return removed


def mask(key: str) -> str:
    """Return a key with all but its last four characters hidden.

    Args:
        key: The key.

    Returns:
        The masked key, for example ``"…k3f9"``.
    """
    return f"…{key[-4:]}" if len(key) > 8 else "…"
