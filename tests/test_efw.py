# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Unit tests for pyobs_zwohid.efw (ZwoEfw)."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pyobs.events import FilterChangedEvent
from pyobs.interfaces import FiltersCapabilities, FilterState, FitsHeaderEntry, IFilters, IMotion, IReady
from pyobs.utils import exceptions as exc
from pyobs.utils.enums import MotionStatus
from pyzwohid import Efw, EfwStatus, ZwoBusyError, ZwoError
from test_hid import DEV_PATH, FakeComm, FakeZwoHidDevice

from pyobs_zwohid import ZwoEfw


class FakeEfwDevice(FakeZwoHidDevice, Efw):
    """In-memory fake for `Efw` inheriting base lifecycle from `FakeZwoHidDevice`."""

    PIDS: tuple[int, ...] = Efw.PIDS  # type: ignore[assignment]
    instances: ClassVar[list[FakeEfwDevice]] = []  # type: ignore[assignment]
    initial_statuses: ClassVar[list[EfwStatus]] = []

    def __init__(self, path: str, device: Any = None) -> None:
        super().__init__(path)
        self.efw_status = EfwStatus(moving=False, slot_count=5, slot=1)
        self.pending_statuses: list[EfwStatus] = list(self.initial_statuses)
        self.move_to_slot_calls: list[tuple[int, bool, float]] = []
        self.move_to_slot_error: Exception | None = None

    @classmethod
    def reset_efw_fakes(
        cls,
        *,
        paths: list[str] | None = None,
        initial_statuses: list[EfwStatus] | None = None,
    ) -> None:
        cls.reset_fakes(paths=paths)
        cls.initial_statuses = [] if initial_statuses is None else list(initial_statuses)

    async def get_status(self) -> EfwStatus:
        if self.status_error is not None:
            raise self.status_error
        if self.pending_statuses:
            self.efw_status = self.pending_statuses.pop(0)
        return self.efw_status

    async def move_to_slot(
        self,
        slot: int,
        *,
        unidirectional: bool = False,
        timeout: float = 30.0,
    ) -> int:
        self.move_to_slot_calls.append((slot, unidirectional, timeout))
        if self.efw_status.moving:
            raise ZwoBusyError("EFW is already moving")
        if self.move_to_slot_error is not None:
            raise self.move_to_slot_error
        self.efw_status = EfwStatus(moving=False, slot_count=self.efw_status.slot_count, slot=slot)
        return slot


class _TestableZwoEfw(ZwoEfw):
    """ZwoEfw wired to FakeEfwDevice and FakeComm for unit tests."""

    DEVICE_CLS = FakeEfwDevice

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("comm", FakeComm())
        super().__init__(*args, **kwargs)


def test_init_validates_filters_and_move_timeout() -> None:
    for bad_timeout in (0.0, -1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="move_timeout must be finite and > 0"):
            _TestableZwoEfw(move_timeout=bad_timeout)

    with pytest.raises(ValueError, match="filters must not be empty"):
        _TestableZwoEfw(filters=[])

    with pytest.raises(ValueError, match="filters must contain non-empty names"):
        _TestableZwoEfw(filters=["L", "  ", "R"])

    with pytest.raises(ValueError, match="filters must not contain duplicate names"):
        _TestableZwoEfw(filters=["L", "R", "L"])


@pytest.mark.asyncio
async def test_open_publishes_explicit_filters_and_fits_header() -> None:
    FakeEfwDevice.reset_efw_fakes(
        initial_statuses=[EfwStatus(moving=False, slot_count=5, slot=2)],
    )
    efw = _TestableZwoEfw(filters=["L", "R", "G", "B", "Ha"])
    await efw.open()

    caps = efw.comm.get_own_capabilities(IFilters)
    assert caps == FiltersCapabilities(filters=["L", "R", "G", "B", "Ha"])

    state = efw.comm.get_own_state(IFilters)
    assert isinstance(state, FilterState)
    assert state.filter == "R"
    assert efw.motion_status() == MotionStatus.POSITIONED
    assert efw.comm.get_own_state(IReady).ready is True
    assert efw.comm.missing_published_state(efw.interfaces) == []

    hdr = await efw.get_fits_header_before()
    assert hdr == {"FILTER": FitsHeaderEntry("R", "Current filter")}

    await efw.stop_motion()
    await efw.close()


@pytest.mark.asyncio
async def test_open_defaults_filter_names_to_slot_numbers() -> None:
    FakeEfwDevice.reset_efw_fakes(
        initial_statuses=[EfwStatus(moving=False, slot_count=7, slot=4)],
    )
    efw = _TestableZwoEfw()
    await efw.open()

    caps = efw.comm.get_own_capabilities(IFilters)
    assert caps == FiltersCapabilities(filters=["1", "2", "3", "4", "5", "6", "7"])
    assert efw.comm.get_own_state(IFilters).filter == "4"
    await efw.close()


@pytest.mark.asyncio
async def test_open_rejects_filter_count_mismatch_and_closes_handle() -> None:
    FakeEfwDevice.reset_efw_fakes(
        initial_statuses=[EfwStatus(moving=False, slot_count=5, slot=1)],
    )
    efw = _TestableZwoEfw(filters=["L", "R", "G"])
    with pytest.raises(ValueError, match="Configured 3 filters.*reported 5 slots"):
        await efw.open()

    assert len(FakeEfwDevice.instances) == 1
    assert FakeEfwDevice.instances[0].closed
    await efw.close()


@pytest.mark.asyncio
async def test_open_waits_for_power_on_calibration_to_settle() -> None:
    FakeEfwDevice.reset_efw_fakes(
        initial_statuses=[
            EfwStatus(moving=True, slot_count=-1, slot=-1),
            EfwStatus(moving=False, slot_count=5, slot=1),
        ],
    )
    efw = _TestableZwoEfw(filters=["L", "R", "G", "B", "Ha"])
    await efw.open()

    assert efw.comm.get_own_state(IFilters).filter == "L"
    assert efw.motion_status() == MotionStatus.POSITIONED
    await efw.close()


@pytest.mark.asyncio
async def test_open_times_out_if_power_on_motion_never_settles() -> None:
    FakeEfwDevice.reset_efw_fakes(
        initial_statuses=[EfwStatus(moving=True, slot_count=-1, slot=-1)],
    )
    efw = _TestableZwoEfw(move_timeout=0.02)
    with pytest.raises(TimeoutError, match="open\\(\\) timed out"):
        await efw.open()

    assert FakeEfwDevice.instances[0].closed
    await efw.close()


@pytest.mark.asyncio
async def test_set_filter_moves_wheel_updates_state_and_emits_event() -> None:
    FakeEfwDevice.reset_efw_fakes(
        initial_statuses=[EfwStatus(moving=False, slot_count=5, slot=1)],
    )
    comm = FakeComm()
    efw = _TestableZwoEfw(
        filters=["L", "R", "G", "B", "Ha"],
        unidirectional=True,
        move_timeout=15.0,
        comm=comm,
    )
    await efw.open()
    dev = FakeEfwDevice.instances[0]
    comm.events.clear()

    await efw.set_filter("Ha")
    assert dev.move_to_slot_calls == [(5, True, 15.0)]
    assert efw.comm.get_own_state(IFilters).filter == "Ha"
    assert efw.motion_status() == MotionStatus.POSITIONED
    filter_events = [e for e in comm.events if isinstance(e, FilterChangedEvent)]
    assert len(filter_events) == 1
    assert filter_events[0].filter == "Ha"
    await efw.close()


@pytest.mark.asyncio
async def test_set_filter_error_paths() -> None:
    FakeEfwDevice.reset_efw_fakes(
        initial_statuses=[EfwStatus(moving=False, slot_count=5, slot=1)],
    )
    efw = _TestableZwoEfw(filters=["L", "R", "G", "B", "Ha"])
    await efw.open()
    dev = FakeEfwDevice.instances[0]

    # 1. Invalid filter name
    with pytest.raises(exc.InvalidArgumentError, match="Unknown filter 'OIII'"):
        await efw.set_filter("OIII")

    # 2. Device already moving according to hardware status -> DeviceBusyError (not ERROR)
    dev.efw_status = EfwStatus(moving=True, slot_count=5, slot=-1)
    with pytest.raises(exc.DeviceBusyError, match="EFW is already moving"):
        await efw.set_filter("B")
    assert efw.motion_status() == MotionStatus.SLEWING
    dev.efw_status = EfwStatus(moving=False, slot_count=5, slot=1)

    # 3. ZwoError / TimeoutError from underlying controller -> ERROR + MoveError
    for raw_err in (
        ZwoError(f"EFW hardware error on {DEV_PATH}"),
        TimeoutError("move_to_slot(4) timed out after 30.00s"),
    ):
        dev.move_to_slot_error = raw_err
        with pytest.raises(exc.MoveError, match="Failed to move filter wheel to 'B'"):
            await efw.set_filter("B")
        assert efw.motion_status() == MotionStatus.ERROR
        assert efw.comm.get_own_state(IMotion).status == MotionStatus.ERROR

    await efw.close()
