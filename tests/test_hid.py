# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Unit tests and shared fake device base for pyobs_zwohid.hid."""

from __future__ import annotations

from typing import Any, ClassVar, Self

import pytest
from pyobs.comm.dummy import DummyComm
from pyobs.events import Event
from pyobs.interfaces import IMotion, Interface, IReady, MotionState, ReadyState
from pyobs.utils import exceptions as exc
from pyobs.utils.enums import ModuleState, MotionStatus
from pyzwohid import ZwoError, ZwoHidDevice, ZwoHidStatus

from pyobs_zwohid import ZwoHidModule

DEV_PATH = "/dev/hidraw10"
SECOND_DEV_PATH = "/dev/hidraw11"


class FakeComm(DummyComm):
    """In-memory `DummyComm` storing published states, capabilities, and emitted events."""

    def __init__(self, name: str = "test") -> None:
        super().__init__(name=name)
        self.states: dict[type[Interface], Any] = {}
        self.capabilities: dict[type[Interface], Any] = {}
        self.events: list[Event] = []

    async def _set_state(self, interface: type[Interface], state: Any) -> None:
        self.states[interface] = state

    def _get_own_state(self, interface: type[Interface]) -> Any:
        return self.states.get(interface)

    async def _set_capabilities(self, interface: type[Interface], capabilities: Any) -> None:
        self.capabilities[interface] = capabilities

    def _get_own_capabilities(self, interface: type[Interface]) -> Any:
        return self.capabilities.get(interface)

    async def send_event(self, event: Event) -> None:
        self.events.append(event)
        await super().send_event(event)


class FakeZwoHidDevice(ZwoHidDevice[ZwoHidStatus]):
    """In-memory fake for `ZwoHidDevice` used by base and subclass module tests."""

    PIDS = (0x1F01,)

    discovered_paths: ClassVar[list[str]] = [DEV_PATH]
    open_error: ClassVar[Exception | None] = None
    instances: ClassVar[list[FakeZwoHidDevice]] = []

    def __init__(self, path: str) -> None:
        super().__init__(path, device=None)
        self.closed = False
        self.clear_error_count = 0
        self.status = ZwoHidStatus(moving=False)
        self.status_error: Exception | None = None

    @classmethod
    def reset_fakes(cls, *, paths: list[str] | None = None, open_error: Exception | None = None) -> None:
        """Reset class-level discovery/open state and recorded instances before a test."""
        cls.discovered_paths = [DEV_PATH] if paths is None else list(paths)
        cls.open_error = open_error
        cls.instances = []

    @classmethod
    async def discover(cls) -> list[str]:
        return list(cls.discovered_paths)

    @classmethod
    async def open(cls, path: str) -> Self:
        if cls.open_error is not None:
            raise cls.open_error
        instance = cls(path)
        cls.instances.append(instance)
        return instance

    async def close(self) -> None:
        self.closed = True

    async def clear_error(self) -> None:
        self.clear_error_count += 1

    async def get_status(self) -> ZwoHidStatus:
        if self.status_error is not None:
            raise self.status_error
        return self.status


class _DummyHidModule(ZwoHidModule[FakeZwoHidDevice, ZwoHidStatus], IMotion):
    """Concrete ZwoHidModule subclass for testing base lifecycle and fault recovery."""

    DEVICE_CLS = FakeZwoHidDevice
    MOTION_INTERFACE = "IMotion"

    def __init__(self, path: str | None = None, *, fail_setup: bool = False, **kwargs: Any) -> None:
        kwargs.setdefault("comm", FakeComm())
        super().__init__(path=path, **kwargs)
        self._fail_setup = fail_setup

    async def _setup_device(self, device: FakeZwoHidDevice) -> None:
        if self._fail_setup:
            raise RuntimeError("simulated setup failure")
        await super()._setup_device(device)

    async def _refresh_state(self, device: FakeZwoHidDevice) -> ZwoHidStatus:
        status = await device.get_status()
        motion_status = MotionStatus.SLEWING if status.moving else MotionStatus.POSITIONED
        await self._change_motion_status(motion_status, interface=self.MOTION_INTERFACE)
        return status

    async def stop_motion(self, device: str | None = None, **kwargs: Any) -> None:
        pass


def test_subclass_requires_device_cls_and_motion_interface() -> None:
    with pytest.raises(TypeError, match="must define DEVICE_CLS and non-empty MOTION_INTERFACE"):

        class _MissingAttrsModule(ZwoHidModule[FakeZwoHidDevice, ZwoHidStatus]):
            pass

    with pytest.raises(TypeError, match="must define DEVICE_CLS and non-empty MOTION_INTERFACE"):

        class _MissingInterfaceModule(ZwoHidModule[FakeZwoHidDevice, ZwoHidStatus]):
            DEVICE_CLS = FakeZwoHidDevice


def test_init_rejects_empty_or_whitespace_path() -> None:
    for bad_path in ("", "   "):
        with pytest.raises(ValueError, match="path must be a non-empty string"):
            _DummyHidModule(path=bad_path)


@pytest.mark.asyncio
async def test_open_with_explicit_path_and_close() -> None:
    FakeZwoHidDevice.reset_fakes(paths=[DEV_PATH, SECOND_DEV_PATH])
    module = _DummyHidModule(path=SECOND_DEV_PATH)

    await module.open()
    assert len(FakeZwoHidDevice.instances) == 1
    dev = FakeZwoHidDevice.instances[0]
    assert dev.path == SECOND_DEV_PATH
    assert not dev.closed
    assert module.motion_status() == MotionStatus.POSITIONED
    motion_state = module.comm.get_own_state(IMotion)
    assert isinstance(motion_state, MotionState)
    assert motion_state.status == MotionStatus.POSITIONED
    ready_state = module.comm.get_own_state(IReady)
    assert isinstance(ready_state, ReadyState)
    assert ready_state.ready is True

    await module.init()
    await module.park()
    await module.stop_motion()

    await module.close()
    assert dev.closed
    await module.close()  # idempotent second close


@pytest.mark.asyncio
async def test_open_discovers_single_device_and_rejects_zero_or_multiple() -> None:
    FakeZwoHidDevice.reset_fakes(paths=[DEV_PATH])
    module = _DummyHidModule()
    await module.open()
    assert len(FakeZwoHidDevice.instances) == 1
    assert FakeZwoHidDevice.instances[0].path == DEV_PATH
    await module.close()

    FakeZwoHidDevice.reset_fakes(paths=[])
    with pytest.raises(ZwoError, match="No FakeZwoHidDevice devices found"):
        await _DummyHidModule().open()

    FakeZwoHidDevice.reset_fakes(paths=[DEV_PATH, SECOND_DEV_PATH])
    with pytest.raises(ZwoError, match="Multiple FakeZwoHidDevice devices found"):
        await _DummyHidModule().open()


@pytest.mark.asyncio
async def test_open_closes_device_handle_on_setup_failure() -> None:
    FakeZwoHidDevice.reset_fakes(paths=[DEV_PATH])
    module = _DummyHidModule(fail_setup=True)

    with pytest.raises(RuntimeError, match="simulated setup failure"):
        await module.open()

    assert len(FakeZwoHidDevice.instances) == 1
    assert FakeZwoHidDevice.instances[0].closed
    with pytest.raises(exc.ModuleError, match="is not open"):
        module._require_device()
    await module.close()


@pytest.mark.asyncio
async def test_reset_error_clears_mcu_error_and_refreshes_state() -> None:
    FakeZwoHidDevice.reset_fakes(paths=[DEV_PATH])
    module = _DummyHidModule()
    await module.open()
    dev = FakeZwoHidDevice.instances[0]

    await module._change_motion_status(MotionStatus.ERROR, interface="IMotion")
    await module.set_state(ModuleState.ERROR, "Hardware fault")
    assert await module.get_state() == ModuleState.ERROR
    assert module.motion_status() == MotionStatus.ERROR

    assert await module.reset_error() is True
    assert dev.clear_error_count == 1
    assert module.motion_status() == MotionStatus.POSITIONED
    assert await module.get_state() == ModuleState.READY
    assert await module.get_error_string() == ""
    await module.close()


@pytest.mark.asyncio
async def test_unimplemented_refresh_state_raises_and_closes_handle() -> None:
    class _UnimplementedRefreshModule(ZwoHidModule[FakeZwoHidDevice, ZwoHidStatus], IMotion):
        DEVICE_CLS = FakeZwoHidDevice
        MOTION_INTERFACE = "IMotion"

        async def stop_motion(self, device: str | None = None, **kwargs: Any) -> None:
            pass

    FakeZwoHidDevice.reset_fakes(paths=[DEV_PATH])
    module = _UnimplementedRefreshModule()
    with pytest.raises(NotImplementedError):
        await module.open()
    assert FakeZwoHidDevice.instances[0].closed
    await module.close()
