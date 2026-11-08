# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Unit tests for pyobs_zwohid.eaf (ZwoEaf)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, ClassVar

import pytest
from pyobs.interfaces import FitsHeaderEntry, FocuserState, IFocuser, IMotion, IReady
from pyobs.utils import exceptions as exc
from pyobs.utils.enums import MotionStatus
from pyzwohid import Eaf, EafStatus, ZwoBusyError, ZwoError
from test_hid import DEV_PATH, FakeComm, FakeZwoHidDevice

from pyobs_zwohid import ZwoEaf


def _eaf_status(
    *,
    moving: bool = False,
    step: int = 30_000,
    max_step: int = 60_000,
    temperature_c: float | None = 21.5,
    backlash: int = 0,
    reverse: bool = False,
    beep: bool = False,
    max_force: int = 3,
    hand_controller_active: bool = False,
) -> EafStatus:
    return EafStatus(
        moving=moving,
        step=step,
        max_step=max_step,
        temperature_c=temperature_c,
        backlash=backlash,
        reverse=reverse,
        beep=beep,
        max_force=max_force,
        hand_controller_active=hand_controller_active,
    )


class FakeEafDevice(FakeZwoHidDevice, Eaf):
    """In-memory fake for `Eaf` inheriting base lifecycle from `FakeZwoHidDevice`."""

    PIDS: tuple[int, ...] = Eaf.PIDS  # type: ignore[assignment]
    instances: ClassVar[list[FakeEafDevice]] = []  # type: ignore[assignment]
    initial_status: ClassVar[EafStatus] = _eaf_status()

    def __init__(self, path: str, device: Any = None, firmware_version: tuple[int, int, int] = (3, 1, 9)) -> None:
        Eaf.__init__(self, path, device, firmware_version)
        self.closed = False
        self.clear_error_count = 0
        self.status_error: Exception | None = None
        self.eaf_status = self.initial_status
        self.move_calls: list[tuple[int, float]] = []
        self.move_error: Exception | None = None
        self.settled_step_override: int | None = None
        self.move_hold_event: asyncio.Event | None = None
        self.stop_calls: list[float] = []
        self.stop_error: Exception | None = None
        self.stopped_step: int = 25_000

    @classmethod
    def reset_eaf_fakes(
        cls,
        *,
        paths: list[str] | None = None,
        initial_status: EafStatus | None = None,
    ) -> None:
        cls.reset_fakes(paths=paths)
        cls.initial_status = _eaf_status() if initial_status is None else initial_status

    async def get_status(self) -> EafStatus:
        if self.status_error is not None:
            raise self.status_error
        return self.eaf_status

    async def move(self, step: int, *, timeout: float = 60.0) -> int:
        self.move_calls.append((step, timeout))
        if self.eaf_status.moving:
            raise ZwoBusyError("EAF is already moving")
        if not (0 <= step <= self.eaf_status.max_step):
            raise ValueError(f"step={step} outside allowed range [0, {self.eaf_status.max_step}]")
        if self.move_error is not None:
            raise self.move_error

        if self.move_hold_event is not None:
            self.eaf_status = replace(self.eaf_status, moving=True)
            await self.move_hold_event.wait()
            return self.eaf_status.step

        settled = step if self.settled_step_override is None else self.settled_step_override
        self.eaf_status = replace(self.eaf_status, moving=False, step=settled)
        return settled

    async def stop(self, *, timeout: float = 5.0) -> int:
        self.stop_calls.append(timeout)
        if self.stop_error is not None:
            raise self.stop_error
        if self.move_hold_event is not None and not self.move_hold_event.is_set():
            self.eaf_status = replace(self.eaf_status, moving=False, step=self.stopped_step)
            self.move_hold_event.set()
            return self.stopped_step
        self.eaf_status = replace(self.eaf_status, moving=False)
        return self.eaf_status.step


class _TestableZwoEaf(ZwoEaf):
    """ZwoEaf wired to FakeEafDevice and FakeComm for unit tests."""

    DEVICE_CLS = FakeEafDevice

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("comm", FakeComm())
        super().__init__(*args, **kwargs)


def test_init_validates_parameters() -> None:
    for bad_val in (0.0, -1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="step_size must be finite and > 0"):
            _TestableZwoEaf(step_size=bad_val)
        with pytest.raises(ValueError, match="move_timeout must be finite and > 0"):
            _TestableZwoEaf(move_timeout=bad_val)
        with pytest.raises(ValueError, match="stop_timeout must be finite and > 0"):
            _TestableZwoEaf(stop_timeout=bad_val)


@pytest.mark.asyncio
async def test_open_publishes_focuser_state_and_fits_headers() -> None:
    FakeEafDevice.reset_eaf_fakes(initial_status=_eaf_status(step=25_000, max_step=60_000))
    eaf = _TestableZwoEaf(step_size=0.002)
    await eaf.open()

    state = eaf.comm.get_own_state(IFocuser)
    assert isinstance(state, FocuserState)
    assert state.focus == pytest.approx(50.0)
    assert state.focus_offset == pytest.approx(0.0)
    assert eaf.motion_status() == MotionStatus.POSITIONED
    assert eaf.comm.get_own_state(IReady).ready is True
    assert eaf.comm.missing_published_state(eaf.interfaces) == []

    hdr = await eaf.get_fits_header_before()
    assert hdr == {
        "FOCUS": FitsHeaderEntry(50.0, "Focus value [mm]"),
        "FOCOFF": FitsHeaderEntry(0.0, "Focus offset [mm]"),
    }

    await eaf.close()


@pytest.mark.asyncio
async def test_set_focus_and_focus_offset_move_and_update_state() -> None:
    FakeEafDevice.reset_eaf_fakes(initial_status=_eaf_status(step=20_000, max_step=60_000))
    eaf = _TestableZwoEaf(step_size=0.001, move_timeout=45.0)
    await eaf.open()
    dev = FakeEafDevice.instances[0]

    await eaf.set_focus(31.25)
    assert dev.move_calls == [(31_250, 45.0)]
    state = eaf.comm.get_own_state(IFocuser)
    assert state.focus == pytest.approx(31.25)
    assert state.focus_offset == pytest.approx(0.0)
    assert eaf.motion_status() == MotionStatus.POSITIONED

    await eaf.set_focus_offset(-1.25)
    assert dev.move_calls[-1] == (30_000, 45.0)
    state = eaf.comm.get_own_state(IFocuser)
    assert state.focus == pytest.approx(31.25)
    assert state.focus_offset == pytest.approx(-1.25)

    # Subsequent set_focus(32.0) moves to 32.0 + (-1.25) = 30.75 mm -> step 30_750
    await eaf.set_focus(32.0)
    assert dev.move_calls[-1] == (30_750, 45.0)
    state = eaf.comm.get_own_state(IFocuser)
    assert state.focus == pytest.approx(32.0)
    assert state.focus_offset == pytest.approx(-1.25)

    await eaf.close()


@pytest.mark.asyncio
async def test_stop_motion_aborts_in_flight_move_and_handles_stop_errors() -> None:
    FakeEafDevice.reset_eaf_fakes(initial_status=_eaf_status(step=20_000, max_step=60_000))
    eaf = _TestableZwoEaf(step_size=0.001, stop_timeout=3.5)
    await eaf.open()
    dev = FakeEafDevice.instances[0]
    dev.move_hold_event = asyncio.Event()
    dev.stopped_step = 24_500

    move_task = asyncio.create_task(eaf.set_focus(40.0))
    await asyncio.sleep(0)

    await eaf.stop_motion()
    with pytest.raises(exc.AbortedError, match="Focus movement was aborted"):
        await move_task

    assert dev.stop_calls == [3.5]
    assert eaf.motion_status() == MotionStatus.POSITIONED
    assert eaf.comm.get_own_state(IFocuser).focus == pytest.approx(24.5)

    # Error during stop_motion -> MotionStatus.ERROR + MoveError
    dev.stop_error = ZwoError(f"Stop failed on {DEV_PATH}")
    with pytest.raises(exc.MoveError, match="Failed to stop focuser"):
        await eaf.stop_motion()
    assert eaf.motion_status() == MotionStatus.ERROR

    await eaf.close()


@pytest.mark.asyncio
async def test_set_focus_and_offset_error_paths() -> None:
    FakeEafDevice.reset_eaf_fakes(initial_status=_eaf_status(step=20_000, max_step=60_000))
    eaf = _TestableZwoEaf(step_size=0.001)
    await eaf.open()
    dev = FakeEafDevice.instances[0]

    # 1. Non-finite focus / offset -> InvalidArgumentError
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(exc.InvalidArgumentError, match="focus must be finite"):
            await eaf.set_focus(bad)
        with pytest.raises(exc.InvalidArgumentError, match="offset must be finite"):
            await eaf.set_focus_offset(bad)

    # 2. Out-of-bounds focus / offset -> InvalidArgumentError without mutating focus_offset
    with pytest.raises(exc.InvalidArgumentError, match="must be >= 0"):
        await eaf.set_focus(-0.1)
    with pytest.raises(exc.InvalidArgumentError, match="outside allowed range"):
        await eaf.set_focus(65.0)
    with pytest.raises(exc.InvalidArgumentError, match="outside allowed range"):
        await eaf.set_focus_offset(50.0)
    assert eaf.comm.get_own_state(IFocuser).focus_offset == pytest.approx(0.0)

    # 3. Device already moving -> DeviceBusyError (state stays SLEWING)
    dev.eaf_status = _eaf_status(moving=True, step=20_000)
    with pytest.raises(exc.DeviceBusyError, match="EAF is already moving"):
        await eaf.set_focus(25.0)
    assert eaf.motion_status() == MotionStatus.SLEWING
    with pytest.raises(exc.DeviceBusyError, match="EAF is already moving"):
        await eaf.set_focus_offset(1.0)
    dev.eaf_status = _eaf_status(moving=False, step=20_000)

    # 4. Incomplete move (torque stall) -> ERROR + MoveError + updated FocuserState
    dev.settled_step_override = 22_100
    with pytest.raises(exc.MoveError, match="settled at step 22100 instead of target step 30000"):
        await eaf.set_focus(30.0)
    assert eaf.motion_status() == MotionStatus.ERROR
    assert eaf.comm.get_own_state(IFocuser).focus == pytest.approx(22.1)
    dev.settled_step_override = None

    # 5. ZwoError / TimeoutError during move or status query -> ERROR + MoveError
    for raw_err in (
        ZwoError(f"EAF hardware fault on {DEV_PATH}"),
        TimeoutError("move(30000) timed out after 60.00s"),
    ):
        dev.move_error = raw_err
        with pytest.raises(exc.MoveError, match="Failed to move focuser"):
            await eaf.set_focus(30.0)
        assert eaf.motion_status() == MotionStatus.ERROR
        assert eaf.comm.get_own_state(IMotion).status == MotionStatus.ERROR
    dev.move_error = None

    dev.status_error = ZwoError(f"Disconnected on {DEV_PATH}")
    with pytest.raises(exc.MoveError, match="Failed to read focuser status"):
        await eaf.set_focus_offset(1.0)
    assert eaf.motion_status() == MotionStatus.ERROR

    await eaf.close()
