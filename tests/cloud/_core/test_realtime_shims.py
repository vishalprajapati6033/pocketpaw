"""Identity tests for the Phase 5 realtime shim.

The realtime subsystem moved from ``ee.cloud.realtime.*`` to
``ee.cloud._core.realtime.*``. The old paths remain as re-export shims.
These tests assert that imports through both paths resolve to the same
Python objects.
"""

from __future__ import annotations


def test_bus_module_identity() -> None:
    from ee.cloud._core.realtime import bus as core_bus
    from ee.cloud.realtime import bus as shim_bus

    assert shim_bus.EventBus is core_bus.EventBus
    assert shim_bus.InProcessBus is core_bus.InProcessBus
    assert shim_bus.get_bus is core_bus.get_bus
    assert shim_bus.set_bus is core_bus.set_bus
    assert shim_bus.get_resolver is core_bus.get_resolver
    assert shim_bus.set_resolver is core_bus.set_resolver


def test_emit_identity() -> None:
    from ee.cloud._core.realtime.emit import emit as core_emit
    from ee.cloud.realtime.emit import emit as shim_emit

    assert shim_emit is core_emit


def test_audience_identity() -> None:
    from ee.cloud._core.realtime.audience import AudienceResolver as core_AR
    from ee.cloud.realtime.audience import AudienceResolver as shim_AR

    assert shim_AR is core_AR


def test_events_identity() -> None:
    from ee.cloud._core.realtime import events as core_events
    from ee.cloud.realtime import events as shim_events

    # Sample several event classes
    for name in (
        "Event",
        "WorkspaceUpdated",
        "NotificationNew",
        "MessageNew",
        "GroupCreated",
    ):
        assert getattr(shim_events, name) is getattr(core_events, name), name


def test_event_bus_port_re_export_in_core_ports() -> None:
    from ee.cloud._core.ports import EventBus as port_EventBus
    from ee.cloud._core.realtime.bus import EventBus as bus_EventBus

    assert port_EventBus is bus_EventBus
