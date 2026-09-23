from __future__ import annotations

import gzip
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_latest_backup(
    directory: Path,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=26),
) -> dict[str, Any]:
    """Verify the newest published dump, checksum, and successful-restore manifest."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    backups = sorted(directory.glob("fantasy-*.dump.gz"), reverse=True)
    if not backups:
        return {
            "status": "missing",
            "newest_path": None,
            "verified": False,
            "errors": ["No published database backup was found"],
        }

    backup = backups[0]
    checksum_path = Path(f"{backup}.sha256")
    manifest_path = Path(f"{backup}.manifest.json")
    modified = datetime.fromtimestamp(backup.stat().st_mtime, tz=UTC)
    errors: list[str] = []
    if current - modified > max_age:
        errors.append("Backup is older than the 26-hour recovery objective")

    manifest: dict[str, Any] = {}
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("manifest must be an object")
        manifest = loaded
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"Restore manifest is missing or invalid: {exc}")

    expected_checksum = ""
    try:
        expected_checksum = checksum_path.read_text(encoding="utf-8").split()[0].lower()
        if not SHA256_PATTERN.fullmatch(expected_checksum):
            raise ValueError("checksum is not SHA-256")
    except (OSError, IndexError, ValueError) as exc:
        errors.append(f"Checksum sidecar is missing or invalid: {exc}")

    actual_checksum = _sha256(backup)
    if expected_checksum and actual_checksum != expected_checksum:
        errors.append("Backup content does not match its checksum sidecar")
    if manifest:
        if manifest.get("backup") != backup.name:
            errors.append("Restore manifest names a different backup")
        if manifest.get("bytes") != backup.stat().st_size:
            errors.append("Restore manifest byte count does not match the backup")
        if manifest.get("sha256") != actual_checksum:
            errors.append("Restore manifest checksum does not match the backup")
        if manifest.get("verified") is not True:
            errors.append("Restore manifest does not record a successful verification")
        try:
            restored_tables = int(manifest.get("restored_table_count") or 0)
        except (TypeError, ValueError):
            restored_tables = 0
        if restored_tables < 10:
            errors.append("Restore manifest does not prove a complete isolated restore")

    try:
        with gzip.open(backup, "rb") as handle:
            for _ in iter(lambda: handle.read(1024 * 1024), b""):
                pass
    except (OSError, EOFError) as exc:
        errors.append(f"Compressed backup stream is invalid: {exc}")

    stale_only = len(errors) == 1 and errors[0].startswith("Backup is older")
    return {
        "status": "stale" if stale_only else "invalid" if errors else "healthy",
        "newest_path": backup.name,
        "modified_at": modified,
        "verified": not errors,
        "checksum_verified": bool(expected_checksum) and actual_checksum == expected_checksum,
        "restore_manifest_verified": bool(manifest) and manifest.get("verified") is True,
        "restored_table_count": manifest.get("restored_table_count") if manifest else None,
        "sha256": actual_checksum,
        "errors": errors,
    }
