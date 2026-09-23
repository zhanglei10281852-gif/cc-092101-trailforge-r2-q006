from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

PACKAGE_FORMAT = "trailforge-action-pack"
SUPPORTED_VERSIONS = (1,)

ENTRY_CHECK_IN = "check_in_completion"
ENTRY_INCIDENT = "emergency_incident"
ENTRY_GEAR_CHECK = "gear_check"
ENTRY_TYPES = frozenset({ENTRY_CHECK_IN, ENTRY_INCIDENT, ENTRY_GEAR_CHECK})
# 条目时间必须落在活动集合/出发至结束窗口内的类型
EXPEDITION_WINDOW_ENTRY_TYPES = frozenset({ENTRY_CHECK_IN, ENTRY_INCIDENT})

_CHECK_IN_TYPES = {"assembly", "departure", "routine", "waypoint", "safe_return"}
_INCIDENT_TYPES = {
    "injury",
    "illness",
    "lost_person",
    "weather",
    "equipment",
    "delay",
    "other",
}
_RISK_LEVELS = {"low", "moderate", "high", "critical"}
_CHECKLIST_STATUSES = {"required", "packed", "verified", "missing", "waived"}
_MANIFEST_TOP_KEYS = {"format", "format_version", "pack_id", "expedition_id", "created_at",
                      "created_by", "baseline_cursor", "entries"}


class ActionPackValidationError(ValueError):
    """行动包内容不满足结构或领域规则。"""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path


class ActionPackIntegrityError(ValueError):
    """行动包校验摘要不匹配（损坏或被篡改）。"""


def canonical_bytes(obj: Any) -> bytes:
    """稳定的规范编码：UTF-8、键排序、无空白、不转义非 ASCII。"""

    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_pack_id() -> str:
    return f"pack-{uuid.uuid4().hex}"


def _require_aware(value: str | datetime, *, path: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ActionPackValidationError(
                f"invalid ISO-8601 datetime: {value!r}", path=path
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ActionPackValidationError("datetime must include timezone information", path=path)
    return parsed


def _require_str(
    value: Any,
    *,
    path: str,
    field: str,
    min_len: int = 1,
    max_len: int = 10000,
) -> str:
    if not isinstance(value, str):
        raise ActionPackValidationError(f"{field} must be a string", path=path)
    text = value.strip()
    if not min_len <= len(text) <= max_len:
        raise ActionPackValidationError(
            f"{field} length must be between {min_len} and {max_len}", path=path
        )
    return text


def _coordinates(data: dict[str, Any], *, path: str) -> None:
    lat = data.get("latitude")
    lon = data.get("longitude")
    if (lat is None) != (lon is None):
        raise ActionPackValidationError(
            "latitude and longitude must be provided together", path=path
        )
    if lat is not None and not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
        raise ActionPackValidationError("coordinates out of range", path=path)


def _require_positive_int(data: dict[str, Any], fields: tuple[str, ...], *, path: str) -> None:
    for field in fields:
        value = data.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ActionPackValidationError(
                f"{field} must be a positive integer", path=path
            )


def _validate_check_in(data: dict[str, Any], *, path: str) -> None:
    _require_positive_int(data, ("expedition_id", "user_id"), path=path)
    if data.get("check_in_type") not in _CHECK_IN_TYPES:
        raise ActionPackValidationError("unknown check_in_type", path=path)
    # 时间字段必须是带时区的 ISO-8601；早到签到（早于 due_at）是合法的。
    _require_aware(data["due_at"], path=f"{path}.due_at")
    _require_aware(data["checked_in_at"], path=f"{path}.checked_in_at")
    if not isinstance(data.get("is_safe"), bool):
        raise ActionPackValidationError("is_safe must be a boolean", path=path)
    if not isinstance(data.get("note", ""), str) or len(data.get("note", "")) > 4000:
        raise ActionPackValidationError("note must be a string up to 4000 chars", path=path)
    _coordinates(data, path=path)


def _validate_incident(data: dict[str, Any], *, path: str) -> None:
    _require_positive_int(data, ("expedition_id", "reported_by"), path=path)
    client_ref = data.get("client_ref")
    if not isinstance(client_ref, str) or not (8 <= len(client_ref) <= 80):
        raise ActionPackValidationError(
            "client_ref must be a stable 8-80 char identifier (e.g. a UUID)", path=path
        )
    if any(char.isspace() for char in client_ref):
        raise ActionPackValidationError("client_ref must not contain whitespace", path=path)
    if data.get("incident_type") not in _INCIDENT_TYPES:
        raise ActionPackValidationError("unknown incident_type", path=path)
    if data.get("risk_level") not in _RISK_LEVELS:
        raise ActionPackValidationError("unknown risk_level", path=path)
    _require_aware(data["occurred_at"], path=f"{path}.occurred_at")
    _require_str(data.get("description"), path=path, field="description", max_len=10000)
    if not isinstance(data.get("actions_taken", ""), str) or len(
        data.get("actions_taken", "")
    ) > 10000:
        raise ActionPackValidationError(
            "actions_taken must be a string up to 10000 chars", path=path
        )
    _coordinates(data, path=path)


def _validate_gear_check(data: dict[str, Any], *, path: str) -> None:
    _require_positive_int(
        data, ("expedition_id", "user_id", "catalog_id"), path=path
    )
    quantity = data.get("quantity")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 0 <= quantity <= 100000:
        raise ActionPackValidationError("quantity must be between 0 and 100000", path=path)
    if data.get("status") not in _CHECKLIST_STATUSES:
        raise ActionPackValidationError("unknown checklist status", path=path)
    verified_by = data.get("verified_by")
    if verified_by is not None and (
        not isinstance(verified_by, int) or isinstance(verified_by, bool) or verified_by <= 0
    ):
        raise ActionPackValidationError("verified_by must be a positive integer", path=path)
    if data["status"] == "verified" and verified_by is None:
        raise ActionPackValidationError("verified status requires verified_by", path=path)
    if not isinstance(data.get("notes", ""), str) or len(data.get("notes", "")) > 2000:
        raise ActionPackValidationError("notes must be a string up to 2000 chars", path=path)


def validate_entry(entry_type: str, data: dict[str, Any]) -> None:
    """只依赖包内信息的条目校验（离线电脑可独立运行）。"""

    if not isinstance(data, dict):
        raise ActionPackValidationError("entry data must be an object", path="entry")
    if entry_type == ENTRY_CHECK_IN:
        _validate_check_in(data, path=f"entries.{ENTRY_CHECK_IN}")
    elif entry_type == ENTRY_INCIDENT:
        _validate_incident(data, path=f"entries.{ENTRY_INCIDENT}")
    elif entry_type == ENTRY_GEAR_CHECK:
        _validate_gear_check(data, path=f"entries.{ENTRY_GEAR_CHECK}")
    else:
        raise ActionPackValidationError(f"unknown entry type: {entry_type!r}", path="entries")


def _validate_entry_window(
    pack: dict[str, Any], entry_type: str, data: dict[str, Any]
) -> None:
    """对照包内基线活动时间窗校验条目（离线电脑无需联网即可判断）。"""

    if entry_type not in EXPEDITION_WINDOW_ENTRY_TYPES:
        return
    expedition = pack.get("payload", {}).get("expedition")
    if not isinstance(expedition, dict):
        # 由 verify_pack/build_pack 保证 payload 结构；缺窗时不在此处拦截。
        return
    meeting_at = _require_aware(
        expedition["meeting_at"], path="payload.expedition.meeting_at"
    )
    if entry_type == ENTRY_CHECK_IN:
        checked_in_at = _require_aware(
            data["checked_in_at"], path="entries.check_in.checked_in_at"
        )
        if checked_in_at < meeting_at:
            raise ActionPackValidationError(
                "checked_in_at cannot be earlier than the expedition meeting time",
                path="entries.check_in",
            )
    elif entry_type == ENTRY_INCIDENT:
        occurred_at = _require_aware(
            data["occurred_at"], path="entries.incident.occurred_at"
        )
        if occurred_at < meeting_at:
            raise ActionPackValidationError(
                "occurred_at cannot be earlier than the expedition meeting time",
                path="entries.incident",
            )
    # 不硬性拒绝晚于活动结束的记录：救援事件可能在返程后补录。


class PackEntry:
    """离线条目视图，提供稳定指纹。"""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    @property
    def entry_id(self) -> str:
        return str(self.raw["entry_id"])

    @property
    def type(self) -> str:
        return str(self.raw["type"])

    @property
    def data(self) -> dict[str, Any]:
        return self.raw["data"]

    @property
    def fingerprint(self) -> str:
        return sha256_hex(canonical_bytes({"type": self.type, "data": self.data}))


def build_pack(
    *,
    pack_id: str,
    expedition_id: int,
    created_by: int,
    created_at: datetime,
    baseline_cursor: dict[str, Any],
    payload: dict[str, Any],
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """组装并密封一份行动包（计算整条哈希链）。"""

    payload_hash = sha256_hex(canonical_bytes(payload))
    cursor = dict(baseline_cursor)
    cursor["payload_sha256"] = payload_hash
    manifest = {
        "format": PACKAGE_FORMAT,
        "format_version": SUPPORTED_VERSIONS[0],
        "pack_id": pack_id,
        "expedition_id": expedition_id,
        "created_at": created_at.isoformat(),
        "created_by": created_by,
        "baseline_cursor": cursor,
        "entries": [],
    }
    pack = {"manifest": manifest, "payload": payload, "checksum": {}}
    for raw_entry in entries or []:
        add_offline_entry(pack, raw_entry["type"], raw_entry["data"])
    _seal_manifest(pack)
    verify_pack(pack)
    return pack


def _seal_manifest(pack: dict[str, Any]) -> None:
    manifest = pack["manifest"]
    pack["checksum"] = {
        "algorithm": "sha256",
        "manifest_sha256": sha256_hex(canonical_bytes(manifest)),
    }


def add_offline_entry(
    pack: dict[str, Any], entry_type: str, data: dict[str, Any]
) -> dict[str, Any]:
    """在离线端向行动包追加一条记录并重算摘要。"""

    manifest = pack.get("manifest")
    if not isinstance(manifest, dict):
        raise ActionPackValidationError("pack is missing a manifest")
    expedition_id = manifest.get("expedition_id")
    validate_entry(entry_type, data)
    if data["expedition_id"] != expedition_id:
        raise ActionPackValidationError(
            f"entry expedition_id {data['expedition_id']} does not match pack expedition "
            f"{expedition_id}",
            path="entries",
        )
    _validate_entry_window(pack, entry_type, data)
    entry = {
        "entry_id": uuid.uuid4().hex,
        "type": entry_type,
        "data": _normalize_datetimes(entry_type, data),
    }
    entry["sha256"] = sha256_hex(
        canonical_bytes({"type": entry["type"], "data": entry["data"]})
    )
    seen = [item["entry_id"] for item in manifest["entries"]]
    if entry["entry_id"] in seen:
        raise ActionPackValidationError("duplicate entry_id in pack", path="entries")
    manifest["entries"].append(entry)
    _seal_manifest(pack)
    return entry


def _normalize_datetimes(entry_type: str, data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    fields = {
        ENTRY_CHECK_IN: ("due_at", "checked_in_at"),
        ENTRY_INCIDENT: ("occurred_at",),
    }.get(entry_type, ())
    for field in fields:
        parsed = _require_aware(normalized[field], path=f"entries.{entry_type}.{field}")
        normalized[field] = parsed.isoformat()
    return normalized


def verify_pack(pack: Any) -> None:
    """完整校验：结构、逐条哈希、payload 哈希、manifest 哈希。"""

    if not isinstance(pack, dict):
        raise ActionPackIntegrityError("action pack must be a JSON object")
    manifest, payload, checksum = (
        pack.get("manifest"),
        pack.get("payload"),
        pack.get("checksum"),
    )
    if not isinstance(manifest, dict) or not isinstance(payload, dict) or not isinstance(
        checksum, dict
    ):
        raise ActionPackIntegrityError("pack must contain manifest, payload and checksum objects")
    if set(manifest) != _MANIFEST_TOP_KEYS:
        raise ActionPackIntegrityError(
            f"manifest keys mismatch: {sorted(set(manifest) ^ _MANIFEST_TOP_KEYS)}"
        )
    if manifest.get("format") != PACKAGE_FORMAT:
        raise ActionPackIntegrityError(f"unsupported format: {manifest.get('format')!r}")
    if manifest.get("format_version") not in SUPPORTED_VERSIONS:
        raise ActionPackIntegrityError(
            f"unsupported format version: {manifest.get('format_version')!r}"
        )
    if checksum.get("algorithm") != "sha256":
        raise ActionPackIntegrityError("unsupported checksum algorithm")
    if not isinstance(manifest.get("entries"), list):
        raise ActionPackIntegrityError("manifest.entries must be a list")

    # manifest 哈希：用不含不可信输入重算的方式验证
    claimed_manifest_hash = checksum.get("manifest_sha256")
    manifest_copy = {key: value for key, value in manifest.items()}
    actual_manifest_hash = sha256_hex(canonical_bytes(manifest_copy))
    if claimed_manifest_hash != actual_manifest_hash:
        raise ActionPackIntegrityError(
            "manifest checksum mismatch: pack is corrupted or was modified without resealing"
        )

    cursor = manifest.get("baseline_cursor")
    if not isinstance(cursor, dict) or not isinstance(cursor.get("payload_sha256"), str):
        raise ActionPackIntegrityError("baseline_cursor.payload_sha256 is missing")
    payload_hash = sha256_hex(canonical_bytes(payload))
    if cursor["payload_sha256"] != payload_hash:
        raise ActionPackIntegrityError("payload checksum mismatch")

    expedition_id = manifest.get("expedition_id")
    fingerprints: set[str] = set()
    entry_ids: set[str] = set()
    for index, raw_entry in enumerate(manifest["entries"]):
        path = f"manifest.entries[{index}]"
        required_keys = {"entry_id", "type", "sha256", "data"}
        if not isinstance(raw_entry, dict) or set(raw_entry) != required_keys:
            raise ActionPackIntegrityError(f"{path}: malformed entry")
        entry_id, entry_type, claimed = (
            raw_entry["entry_id"],
            raw_entry["type"],
            raw_entry["sha256"],
        )
        if entry_id in entry_ids:
            raise ActionPackIntegrityError(f"{path}: duplicate entry_id")
        entry_ids.add(entry_id)
        data = raw_entry["data"]
        if not isinstance(data, dict) or data.get("expedition_id") != expedition_id:
            raise ActionPackIntegrityError(f"{path}: expedition_id does not match manifest")
        actual = sha256_hex(canonical_bytes({"type": entry_type, "data": data}))
        if claimed != actual:
            raise ActionPackIntegrityError(f"{path}: entry checksum mismatch")
        if actual in fingerprints:
            raise ActionPackIntegrityError(f"{path}: duplicate entry content in pack")
        fingerprints.add(actual)
        validate_entry(entry_type, data)


def load_pack(source: str | Path | BinaryIO | dict[str, Any]) -> dict[str, Any]:
    """读取并校验行动包。接受文件路径、可读二进制流或已解析的 dict。"""

    if isinstance(source, dict):
        pack = source
    elif isinstance(source, (str, Path)):
        raw = Path(source).read_bytes()
        try:
            pack = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ActionPackIntegrityError(f"pack is not valid UTF-8 JSON: {exc}") from exc
    else:
        try:
            pack = json.loads(source.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise ActionPackIntegrityError(f"pack is not valid UTF-8 JSON: {exc}") from exc
    verify_pack(pack)
    return pack


def save_pack(pack: dict[str, Any], target: str | Path | BinaryIO) -> None:
    """把行动包以稳定的 UTF-8 JSON 写盘（键排序、缩进两空格、末尾换行）。"""

    verify_pack(pack)
    encoded = (
        json.dumps(pack, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    if isinstance(target, (str, Path)):
        Path(target).write_bytes(encoded)
    else:
        target.write(encoded)
