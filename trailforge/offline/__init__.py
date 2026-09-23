"""可携带的离线行动包（纯标准库实现）。

包是一份稳定的 UTF-8 JSON 文档，结构为 ``manifest`` + ``payload`` + ``checksum``。
哈希链为::

    payload 规范编码 -> SHA-256（写入 manifest.baseline_cursor.payload_sha256）
    manifest 规范编码 -> SHA-256（写入 checksum.manifest_sha256）
    每条离线条目单独计算 SHA-256（写入 manifest.entries[*].sha256）

哈希保证偶然损坏或未同步重算的改写会在写库前被发现；它提供完整性校验，
不提供密码学身份认证（任何人都能用相同算法重算）。
"""

from trailforge.offline.pack import (
    ENTRY_TYPES,
    EXPEDITION_WINDOW_ENTRY_TYPES,
    PACKAGE_FORMAT,
    SUPPORTED_VERSIONS,
    ActionPackIntegrityError,
    ActionPackValidationError,
    PackEntry,
    add_offline_entry,
    build_pack,
    canonical_bytes,
    load_pack,
    new_pack_id,
    save_pack,
    sha256_hex,
    verify_pack,
)

__all__ = [
    "ENTRY_TYPES",
    "EXPEDITION_WINDOW_ENTRY_TYPES",
    "PACKAGE_FORMAT",
    "SUPPORTED_VERSIONS",
    "ActionPackIntegrityError",
    "ActionPackValidationError",
    "PackEntry",
    "add_offline_entry",
    "build_pack",
    "canonical_bytes",
    "load_pack",
    "new_pack_id",
    "save_pack",
    "sha256_hex",
    "verify_pack",
]
