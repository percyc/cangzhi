"""Optional AI knowledge enhancement policy.

This module only governs the *optional* chapter understanding, entity
graph and assisted chunking modules. The basic AI understanding
(summary, category, tags), configured OCR, rule-based chunker,
full-text and vector retrieval are never controlled by this switch
and must remain available when the policy is closed or absent.

Closing the switch stops future automatic enhancement, but it does
not delete existing artifacts, rebuild the current index, or
silently cancel running tasks. The default is disabled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

ALLOWED_MODULES: frozenset[str] = frozenset({"chapter", "graph", "chunking"})
DEFAULT_CALL_BUDGET = 8
MIN_CALL_BUDGET = 1
MAX_CALL_BUDGET = 32
POLICY_VERSION = "enhancement-policy:v1"

__all__ = [
    "ALLOWED_MODULES",
    "DEFAULT_CALL_BUDGET",
    "EnhancementPolicy",
    "MAX_CALL_BUDGET",
    "MIN_CALL_BUDGET",
    "POLICY_VERSION",
]


def _is_aware(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None


def _coerce_bool(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} 必须是 bool")


def _coerce_int(value, name: str, min_value: int, max_value: int) -> int:
    # ``bool`` is a subclass of ``int`` in Python; reject it explicitly so
    # that values like ``True`` cannot masquerade as ``1`` for budget fields.
    if isinstance(value, bool):
        raise ValueError(f"{name} 不接受 bool")
    if not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if value < min_value or value > max_value:
        raise ValueError(f"{name} 必须在 {min_value}..{max_value} 范围内")
    return value


def _coerce_modules(value) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError("modules 必须是字符串列表")
    cleaned: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise ValueError("module 名称必须是字符串")
        if item not in ALLOWED_MODULES:
            raise ValueError(
                f"未知 module: {item}; 允许: {sorted(ALLOWED_MODULES)}"
            )
        cleaned.add(item)
    return frozenset(cleaned)


def _coerce_effective_at(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"effective_at 必须是 ISO 8601 字符串或 datetime: {value!r}"
            ) from exc
    raise ValueError("effective_at 必须是 datetime 或 ISO 8601 字符串")


@dataclass(frozen=True)
class EnhancementPolicy:
    """Frozen, validated policy for optional AI knowledge enhancement.

    Defaults to disabled. Enabling requires ``cost_acknowledged=True`` and
    a timezone-aware ``effective_at``. New documents whose creation time is
    ``>=`` ``effective_at`` are auto-enhanced; historical documents need an
    explicit request and are judged independently by
    :meth:`can_enhance_historical`.
    """

    enabled: bool = False
    modules: frozenset[str] = field(default_factory=frozenset)
    call_budget: int = DEFAULT_CALL_BUDGET
    cost_acknowledged: bool = False
    effective_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled 必须是 bool")
        if not isinstance(self.modules, frozenset):
            raise TypeError("modules 必须是 frozenset[str]")
        for module in self.modules:
            if module not in ALLOWED_MODULES:
                raise ValueError(f"未知 module: {module}")
        if isinstance(self.call_budget, bool) or not isinstance(self.call_budget, int):
            raise TypeError("call_budget 必须是整数")
        if not MIN_CALL_BUDGET <= self.call_budget <= MAX_CALL_BUDGET:
            raise ValueError(
                f"call_budget 必须在 {MIN_CALL_BUDGET}..{MAX_CALL_BUDGET} 范围内"
            )
        if not isinstance(self.cost_acknowledged, bool):
            raise TypeError("cost_acknowledged 必须是 bool")
        if self.effective_at is not None:
            if not isinstance(self.effective_at, datetime):
                raise TypeError("effective_at 必须是 datetime")
            if not _is_aware(self.effective_at):
                raise ValueError("effective_at 必须是带时区的时间")
        if self.enabled:
            if not self.cost_acknowledged:
                raise ValueError("开启知识增强必须确认 cost_acknowledged=true")
            if self.effective_at is None:
                raise ValueError("开启知识增强必须提供 effective_at")

    @classmethod
    def from_config(cls, config: dict | None) -> "EnhancementPolicy":
        """Build a policy from a raw config dict with strict validation.

        Rejects unknown keys, ``bool`` values for ``int`` fields, budgets
        outside ``1..32``, naive datetimes, and module names outside the
        whitelist. The input dict is never mutated.
        """
        if config is not None and not isinstance(config, dict):
            raise ValueError("增强配置必须是对象")
        raw = dict(config or {})
        known = {
            "enabled",
            "modules",
            "call_budget",
            "cost_acknowledged",
            "effective_at",
        }
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"未知的增强配置项: {sorted(unknown)}")

        enabled = _coerce_bool(raw.get("enabled", False), "enabled")
        modules = _coerce_modules(raw.get("modules"))
        call_budget = _coerce_int(
            raw.get("call_budget", DEFAULT_CALL_BUDGET),
            "call_budget",
            MIN_CALL_BUDGET,
            MAX_CALL_BUDGET,
        )
        cost_acknowledged = _coerce_bool(
            raw.get("cost_acknowledged", False), "cost_acknowledged"
        )
        effective_at = _coerce_effective_at(raw.get("effective_at"))

        return cls(
            enabled=enabled,
            modules=modules,
            call_budget=call_budget,
            cost_acknowledged=cost_acknowledged,
            effective_at=effective_at,
        )

    def is_module_enabled(self, module: str) -> bool:
        """Return whether a specific optional module is currently active."""
        if not self.enabled:
            return False
        if module not in ALLOWED_MODULES:
            return False
        return module in self.modules

    def active_modules(self) -> frozenset[str]:
        """Return the set of optional modules that are currently active."""
        if not self.enabled:
            return frozenset()
        return self.modules

    def should_auto_enhance(self, document_created_at: datetime) -> bool:
        """Whether a new document should be auto-enhanced.

        True only when the policy is enabled, ``effective_at`` is set and
        timezone-aware, and the document's creation time is on or after
        ``effective_at``. The edge ``created_at == effective_at`` is
        treated as auto-enhanced.
        """
        if not self.enabled or not self.modules:
            return False
        if self.effective_at is None:
            return False
        if not isinstance(document_created_at, datetime):
            return False
        if not _is_aware(document_created_at):
            return False
        return document_created_at >= self.effective_at

    def can_enhance_historical(self) -> bool:
        """Whether historical documents may be enhanced on explicit request.

        Historical enhancement is a separate, manually triggered flow. The
        document's creation time does not affect this judgment; the caller
        is responsible for selecting which documents to submit and for
        surfacing the cost implication to the user.
        """
        return self.enabled and self.cost_acknowledged and bool(self.modules)
