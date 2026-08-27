"""HTTP collectors shared compatibility helpers."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import certifi


def curl_ca_bundle() -> str:
    """Return a CA bundle path that Windows libcurl can read reliably.

    curl-cffi may fail to open certifi's bundle when the virtual environment is
    below a directory containing non-ASCII characters.  Copying the public CA
    bundle to the user's temporary directory keeps TLS verification enabled.
    """
    source = Path(certifi.where())
    try:
        str(source).encode("ascii")
        return str(source)
    except UnicodeEncodeError:
        pass

    target = Path(tempfile.gettempdir()) / "pachi-tool-cacert.pem"
    if not target.exists() or target.stat().st_size != source.stat().st_size:
        shutil.copyfile(source, target)
    return str(target)
