from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone

from app.backup_health import verify_latest_backup


def _backup(tmp_path):
    path = tmp_path / "fantasy-20260909T120000Z.dump.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b"verified postgres dump")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(f"{path.name}.sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    path.with_name(f"{path.name}.manifest.json").write_text(
        json.dumps(
            {
                "backup": path.name,
                "bytes": path.stat().st_size,
                "restored_table_count": 22,
                "sha256": digest,
                "verified": True,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_backup_health_requires_checksum_and_restore_manifest(tmp_path) -> None:
    path = _backup(tmp_path)
    observed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    result = verify_latest_backup(tmp_path, now=observed)
    assert result["status"] == "healthy"
    assert result["checksum_verified"] is True
    assert result["restore_manifest_verified"] is True
    assert result["restored_table_count"] == 22


def test_backup_health_rejects_corrupted_or_unverified_publication(tmp_path) -> None:
    path = _backup(tmp_path)
    with path.open("ab") as handle:
        handle.write(b"corruption")
    observed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    result = verify_latest_backup(tmp_path, now=observed)
    assert result["status"] == "invalid"
    assert result["verified"] is False
    assert any("checksum" in error.lower() for error in result["errors"])
