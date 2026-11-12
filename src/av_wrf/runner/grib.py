"""Read GRIB message metadata using the solver image's ecCodes tools."""

import json
import subprocess
from pathlib import Path


def inspect_grib(path: Path, keys: str) -> list[dict[str, object]]:
    """Read requested ecCodes keys per message, preserving file order."""
    try:
        proc = subprocess.run(
            ["grib_ls", "-j", "-p", keys, str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Install the ecCodes grib_ls tool to verify forcing") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"ecCodes rejected {path}: {exc.stderr.strip()}") from exc
    doc = json.loads(proc.stdout)
    messages = doc.get("messages") if isinstance(doc, dict) else None
    if (
        not messages
        or not isinstance(messages, list)
        or any(not isinstance(row, dict) for row in messages)
    ):
        raise ValueError(f"No readable GRIB2 messages in {path}")
    return messages


__all__ = ["inspect_grib"]
