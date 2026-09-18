"""Pure unit tests for the optional AI knowledge enhancement policy.

These tests do not import the application, do not touch the database
and do not read any environment variables. They exercise the frozen
dataclass, the strict ``from_config`` validator and the per-module /
time / budget gates in isolation.
"""
import copy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from apps.api.services.knowledge_enhancement_policy import (
    ALLOWED_MODULES,
    DEFAULT_CALL_BUDGET,
    MAX_CALL_BUDGET,
    MIN_CALL_BUDGET,
    EnhancementPolicy,
)

UTC = timezone.utc
CST = timezone(timedelta(hours=8))


def _aware(year: int = 2026, month: int = 9, day: int = 18, tz=UTC) -> datetime:
    return datetime(year, month, day, tzinfo=tz)


@pytest.mark.parametrize("config", [False, 0, [], "", [("enabled", False)]])
def test_configuration_requires_mapping(config):
    with pytest.raises(ValueError):
        EnhancementPolicy.from_config(config)


def test_no_selected_module_does_not_schedule_work():
    policy = EnhancementPolicy(enabled=True, cost_acknowledged=True, effective_at=_aware())
    assert not policy.should_auto_enhance(_aware())
    assert not policy.can_enhance_historical()


@pytest.mark.parametrize("created", [None, "2026-09-18", True, 123])
def test_invalid_creation_time_is_not_eligible(created):
    policy = EnhancementPolicy(enabled=True, modules=frozenset({"chapter"}),
                               cost_acknowledged=True, effective_at=_aware())
    assert not policy.should_auto_enhance(created)


def _full_on_config() -> dict:
    return {
        "enabled": True,
        "modules": ["chapter", "graph"],
        "call_budget": 16,
        "cost_acknowledged": True,
        "effective_at": "2026-09-18T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Defaults, immutability and structural safety
# ---------------------------------------------------------------------------


def test_default_is_disabled_and_empty():
    policy = EnhancementPolicy()
    assert policy.enabled is False
    assert policy.modules == frozenset()
    assert policy.call_budget == DEFAULT_CALL_BUDGET
    assert policy.cost_acknowledged is False
    assert policy.effective_at is None


def test_from_empty_config_is_disabled():
    policy = EnhancementPolicy.from_config({})
    assert policy.enabled is False
    assert policy.modules == frozenset()
    assert policy.call_budget == DEFAULT_CALL_BUDGET
    assert policy.cost_acknowledged is False
    assert policy.effective_at is None


def test_from_none_config_is_disabled():
    policy = EnhancementPolicy.from_config(None)
    assert policy.enabled is False


def test_policy_is_frozen_dataclass():
    policy = EnhancementPolicy()
    with pytest.raises(FrozenInstanceError):
        policy.enabled = True  # type: ignore[misc]


def test_constants_exposed():
    assert ALLOWED_MODULES == frozenset({"chapter", "graph", "chunking", "overview"})
    assert MIN_CALL_BUDGET == 1
    assert MAX_CALL_BUDGET == 32
    assert DEFAULT_CALL_BUDGET == 8


def test_policy_does_not_gate_basic_understanding():
    """The switch must not expose any gate over the basic understanding flow.

    The only public methods are the ones that describe the *optional*
    modules. If a future change adds a ``should_understand`` style method,
    the test fails and forces a review.
    """
    policy = EnhancementPolicy()
    public_callables = {
        name
        for name in dir(policy)
        if not name.startswith("_")
        and callable(getattr(policy, name))
        and name != "from_config"
    }
    assert public_callables == {
        "is_module_enabled",
        "active_modules",
        "should_auto_enhance",
        "can_enhance_historical",
    }


# ---------------------------------------------------------------------------
# "Off" must not change the other settings of the original config
# ---------------------------------------------------------------------------


def test_off_preserves_modules_budget_and_effective_at():
    on = EnhancementPolicy.from_config(_full_on_config())
    off = EnhancementPolicy.from_config({**_full_on_config(), "enabled": False})
    assert on.enabled is True
    assert off.enabled is False
    assert off.modules == on.modules == frozenset({"chapter", "graph"})
    assert off.call_budget == on.call_budget == 16
    assert off.cost_acknowledged == on.cost_acknowledged is True
    assert off.effective_at == on.effective_at
    assert off.effective_at.tzinfo is not None


def test_from_config_does_not_mutate_input_dict():
    config = _full_on_config()
    original = copy.deepcopy(config)
    _ = EnhancementPolicy.from_config(config)
    assert config == original


def test_from_config_does_not_mutate_modules_list():
    modules = ["chapter", "graph", "chapter"]
    config = {"modules": modules}
    original = copy.deepcopy(modules)
    _ = EnhancementPolicy.from_config(config)
    assert modules == original


def test_default_construction_does_not_share_module_state():
    a = EnhancementPolicy(modules=frozenset({"chapter"}))
    b = EnhancementPolicy(modules=frozenset({"graph"}))
    assert a.modules == frozenset({"chapter"})
    assert b.modules == frozenset({"graph"})


# ---------------------------------------------------------------------------
# Edge time: created_at vs effective_at
# ---------------------------------------------------------------------------


def test_auto_enhance_at_exact_effective_at_is_true():
    effective = _aware()
    policy = EnhancementPolicy(
        enabled=True,
        modules=frozenset({"chapter"}),
        call_budget=8,
        cost_acknowledged=True,
        effective_at=effective,
    )
    assert policy.should_auto_enhance(effective) is True


def test_auto_enhance_one_microsecond_before_effective_at_is_false():
    effective = _aware()
    policy = EnhancementPolicy(
        enabled=True,
        modules=frozenset({"chapter"}),
        call_budget=8,
        cost_acknowledged=True,
        effective_at=effective,
    )
    assert policy.should_auto_enhance(effective - timedelta(microseconds=1)) is False


def test_auto_enhance_one_microsecond_after_effective_at_is_true():
    effective = _aware()
    policy = EnhancementPolicy(
        enabled=True,
        modules=frozenset({"chapter"}),
        call_budget=8,
        cost_acknowledged=True,
        effective_at=effective,
    )
    assert policy.should_auto_enhance(effective + timedelta(microseconds=1)) is True


def test_auto_enhance_off_is_always_false():
    policy = EnhancementPolicy(enabled=False, modules=frozenset({"chapter"}))
    assert policy.should_auto_enhance(_aware()) is False
    assert policy.should_auto_enhance(_aware(2027, 1, 1)) is False


def test_auto_enhance_compares_across_timezones():
    policy = EnhancementPolicy(
        enabled=True,
        modules=frozenset({"chapter"}),
        call_budget=8,
        cost_acknowledged=True,
        effective_at=_aware(tz=CST),  # 2026-09-18 00:00 +08:00
    )
    # Same instant expressed in UTC
    same_in_utc = datetime(2026, 9, 17, 16, 0, 0, tzinfo=UTC)
    one_second_later_utc = datetime(2026, 9, 17, 16, 0, 1, tzinfo=UTC)
    one_second_earlier_utc = datetime(2026, 9, 17, 15, 59, 59, tzinfo=UTC)
    assert policy.should_auto_enhance(same_in_utc) is True
    assert policy.should_auto_enhance(one_second_later_utc) is True
    assert policy.should_auto_enhance(one_second_earlier_utc) is False


def test_auto_enhance_rejects_naive_created_at():
    policy = EnhancementPolicy(
        enabled=True,
        modules=frozenset({"chapter"}),
        call_budget=8,
        cost_acknowledged=True,
        effective_at=_aware(),
    )
    assert policy.should_auto_enhance(datetime(2026, 9, 18)) is False


# ---------------------------------------------------------------------------
# Budget: defaults, in-range, out-of-range, bool rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("budget", [1, 8, 32])
def test_call_budget_in_range_accepted(budget):
    policy = EnhancementPolicy.from_config({"call_budget": budget})
    assert policy.call_budget == budget


@pytest.mark.parametrize("budget", [0, -1, 33, 100, 10_000])
def test_call_budget_out_of_range_rejected(budget):
    with pytest.raises(ValueError):
        EnhancementPolicy.from_config({"call_budget": budget})


def test_call_budget_rejects_bool_true():
    with pytest.raises(ValueError, match="bool"):
        EnhancementPolicy.from_config({"call_budget": True})


def test_call_budget_rejects_bool_false():
    with pytest.raises(ValueError, match="bool"):
        EnhancementPolicy.from_config({"call_budget": False})


def test_call_budget_rejects_string():
    with pytest.raises(ValueError):
        EnhancementPolicy.from_config({"call_budget": "8"})


def test_call_budget_rejects_float():
    with pytest.raises(ValueError):
        EnhancementPolicy.from_config({"call_budget": 8.0})


def test_call_budget_default_is_eight():
    policy = EnhancementPolicy()
    assert policy.call_budget == 8
    assert policy.call_budget == DEFAULT_CALL_BUDGET


# ---------------------------------------------------------------------------
# Modules: explicit whitelist
# ---------------------------------------------------------------------------


def test_modules_accept_every_allowed_name():
    policy = EnhancementPolicy.from_config(
        {"modules": ["chapter", "graph", "chunking"]}
    )
    assert policy.modules == frozenset({"chapter", "graph", "chunking"})


def test_modules_dedup_repeated_entries():
    policy = EnhancementPolicy.from_config(
        {"modules": ["chapter", "graph", "chapter", "graph"]}
    )
    assert policy.modules == frozenset({"chapter", "graph"})


def test_modules_accept_tuple_input():
    policy = EnhancementPolicy.from_config({"modules": ("chapter", "chunking")})
    assert policy.modules == frozenset({"chapter", "chunking"})


def test_modules_reject_unknown_name():
    with pytest.raises(ValueError, match="未知 module"):
        EnhancementPolicy.from_config({"modules": ["chapter", "novel_character"]})


def test_modules_reject_non_string_item():
    with pytest.raises(ValueError):
        EnhancementPolicy.from_config({"modules": [123]})


def test_modules_reject_non_list_input():
    with pytest.raises(ValueError):
        EnhancementPolicy.from_config({"modules": {"chapter"}})


def test_direct_construction_rejects_unknown_module():
    with pytest.raises(ValueError, match="未知 module"):
        EnhancementPolicy(modules=frozenset({"novel_character"}))


# ---------------------------------------------------------------------------
# Bad config: unknown keys, bool/int swaps, naive datetimes
# ---------------------------------------------------------------------------


def test_unknown_config_keys_rejected():
    with pytest.raises(ValueError, match="未知"):
        EnhancementPolicy.from_config({"mystery_key": 1})


def test_extra_known_key_alongside_unknown_rejected():
    with pytest.raises(ValueError, match="未知"):
        EnhancementPolicy.from_config(
            {
                **_full_on_config(),
                "experimental": True,
            }
        )


def test_enabled_must_be_bool_not_int():
    with pytest.raises(ValueError, match="bool"):
        EnhancementPolicy.from_config({"enabled": 1})
    with pytest.raises(ValueError, match="bool"):
        EnhancementPolicy.from_config({"enabled": 0})


def test_cost_acknowledged_must_be_bool_not_int():
    with pytest.raises(ValueError, match="bool"):
        EnhancementPolicy.from_config({"cost_acknowledged": 1})


def test_effective_at_naive_string_rejected():
    with pytest.raises(ValueError, match="时区"):
        EnhancementPolicy.from_config({"effective_at": "2026-09-18T00:00:00"})


def test_effective_at_garbage_string_rejected():
    with pytest.raises(ValueError):
        EnhancementPolicy.from_config({"effective_at": "not a date"})


def test_effective_at_non_string_non_datetime_rejected():
    with pytest.raises(ValueError):
        EnhancementPolicy.from_config({"effective_at": 1234567890})


def test_effective_at_timezone_aware_string_accepted():
    policy = EnhancementPolicy.from_config(
        {"effective_at": "2026-09-18T00:00:00+08:00"}
    )
    assert policy.effective_at == _aware(tz=CST)
    assert policy.effective_at.tzinfo is not None


def test_effective_at_naive_datetime_in_constructor_rejected():
    with pytest.raises(ValueError, match="时区"):
        EnhancementPolicy(
            enabled=True,
            modules=frozenset({"chapter"}),
            call_budget=8,
            cost_acknowledged=True,
            effective_at=datetime(2026, 9, 18),
        )


# ---------------------------------------------------------------------------
# Enablement consistency
# ---------------------------------------------------------------------------


def test_enabled_without_cost_acknowledged_rejected():
    with pytest.raises(ValueError, match="cost_acknowledged"):
        EnhancementPolicy(
            enabled=True,
            modules=frozenset({"chapter"}),
            call_budget=8,
            cost_acknowledged=False,
            effective_at=_aware(),
        )


def test_enabled_without_effective_at_rejected():
    with pytest.raises(ValueError, match="effective_at"):
        EnhancementPolicy(
            enabled=True,
            modules=frozenset({"chapter"}),
            call_budget=8,
            cost_acknowledged=True,
            effective_at=None,
        )


def test_enabled_full_payload_accepted():
    policy = EnhancementPolicy.from_config(_full_on_config())
    assert policy.enabled is True
    assert policy.cost_acknowledged is True
    assert policy.effective_at is not None
    assert policy.call_budget == 16
    assert policy.modules == frozenset({"chapter", "graph"})


# ---------------------------------------------------------------------------
# Historical explicit request is judged independently
# ---------------------------------------------------------------------------


def test_historical_off_is_false():
    assert EnhancementPolicy(enabled=False).can_enhance_historical() is False


def test_historical_requires_cost_acknowledged():
    policy = EnhancementPolicy(
        enabled=False,
        modules=frozenset({"chapter"}),
        call_budget=8,
        cost_acknowledged=False,
        effective_at=_aware(),
    )
    assert policy.can_enhance_historical() is False


def test_historical_fully_enabled_is_true():
    policy = EnhancementPolicy(
        enabled=True,
        modules=frozenset({"chapter"}),
        call_budget=8,
        cost_acknowledged=True,
        effective_at=_aware(),
    )
    assert policy.can_enhance_historical() is True


def test_historical_does_not_depend_on_document_created_at():
    policy = EnhancementPolicy(
        enabled=True,
        modules=frozenset({"chapter"}),
        call_budget=8,
        cost_acknowledged=True,
        effective_at=_aware(),
    )
    very_old = datetime(2000, 1, 1, tzinfo=UTC)
    very_new = datetime(2099, 1, 1, tzinfo=UTC)
    assert policy.can_enhance_historical() is True
    # The boolean gate is independent of the document's created_at; the
    # caller is responsible for picking which historical documents to submit.
    assert policy.should_auto_enhance(very_old) is False
    assert policy.should_auto_enhance(very_new) is True


# ---------------------------------------------------------------------------
# Per-module checks
# ---------------------------------------------------------------------------


def test_is_module_enabled_off_is_false_for_every_module():
    policy = EnhancementPolicy(enabled=False, modules=frozenset({"chapter"}))
    for module in ALLOWED_MODULES:
        assert policy.is_module_enabled(module) is False


def test_is_module_enabled_only_listed_modules_are_active():
    policy = EnhancementPolicy(
        enabled=True,
        modules=frozenset({"chapter"}),
        call_budget=8,
        cost_acknowledged=True,
        effective_at=_aware(),
    )
    assert policy.is_module_enabled("chapter") is True
    assert policy.is_module_enabled("graph") is False
    assert policy.is_module_enabled("chunking") is False


def test_is_module_enabled_rejects_unknown_name():
    policy = EnhancementPolicy(
        enabled=True,
        modules=frozenset({"chapter"}),
        call_budget=8,
        cost_acknowledged=True,
        effective_at=_aware(),
    )
    assert policy.is_module_enabled("novel_character") is False


def test_active_modules_off_is_empty():
    policy = EnhancementPolicy(enabled=False, modules=frozenset({"chapter", "graph"}))
    assert policy.active_modules() == frozenset()


def test_active_modules_on_returns_listed_set():
    policy = EnhancementPolicy(
        enabled=True,
        modules=frozenset({"chapter", "graph"}),
        call_budget=8,
        cost_acknowledged=True,
        effective_at=_aware(),
    )
    assert policy.active_modules() == frozenset({"chapter", "graph"})
