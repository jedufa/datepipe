"""Boundaries for the single-user, loopback-only source prototype."""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
import stat
import threading
from urllib.error import URLError
from urllib.request import urlopen as _urlopen


_KEY_LOCK = threading.Lock()


def outbound_urlopen(request, *args, **kwargs):
  """Make outbound HTTP an explicit opt-in, including key-free search."""
  if os.environ.get("DATEPIPE_ALLOW_NETWORK") != "1":
    raise URLError("Outbound HTTP is disabled for the local prototype")
  return _urlopen(request, *args, **kwargs)


def local_auth_secret(state_path: Path, name: str) -> bytes:
  """Keep fallback hashing keys private and stable across local restarts."""
  configured = os.environ.get(name)
  if configured:
    return configured.encode("utf-8")
  path = state_path.with_name(state_path.name + ".auth.key")
  with _KEY_LOCK:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
      fd = os.open(path, flags, 0o600)
    except FileExistsError:
      pass
    else:
      with os.fdopen(fd, "wb") as stream:
        stream.write(secrets.token_bytes(32))
        stream.flush()
        os.fsync(stream.fileno())
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
      info = os.fstat(stream.fileno())
      if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("Local auth key must be a private regular file")
      key = stream.read(33)
    if len(key) != 32:
      raise ValueError("Invalid local auth key")
  return hmac.new(key, name.encode("utf-8"), hashlib.sha256).digest()
