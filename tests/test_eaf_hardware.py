# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Opt-in real-hardware tests for ZwoEaf."""

from __future__ import annotations

import os

import pytest
from pyobs.interfaces import FitsHeaderEntry, FocuserState, IFocuser, IReady, ITemperatures, TemperaturesState
from pyobs.utils.enums import MotionStatus
from pyzwohid import Eaf
from test_hid import FakeComm

from pyobs_zwohid import ZwoEaf

pytestmark = pytest.mark.skipif(
    os.environ.get("PYZWOHID_RUN_HARDWARE_TESTS") != "1"
    and os.environ.get("PYZWOHID_RUN_HARDWARE_MOTION_TESTS") != "1",
    reason="Set PYZWOHID_RUN_HARDWARE_TESTS=1 to run real-hardware tests",
)


@pytest.mark.asyncio
async def test_eaf_hardware_readonly_open_and_state() -> None:
    paths = await Eaf.discover()
    if not paths:
        pytest.skip("No physical ZWO EAF discovered via USB HID")

    for path in paths:
        comm = FakeComm()
        eaf = ZwoEaf(path=path, step_size=0.001, poll_interval=60.0, comm=comm)
        await eaf.open()
        try:
            state = comm.get_own_state(IFocuser)
            assert isinstance(state, FocuserState)
            assert 0.0 <= state.focus <= 600.0
            assert state.focus_offset == pytest.approx(0.0)

            temp_state = comm.get_own_state(ITemperatures)
            assert isinstance(temp_state, TemperaturesState)
            for reading in temp_state.readings:
                assert reading.name == "Focus"
                assert -200.0 < reading.value < 355.35

            assert eaf.motion_status() == MotionStatus.POSITIONED
            assert comm.get_own_state(IReady).ready is True
            assert comm.missing_published_state(eaf.interfaces) == []

            hdr = await eaf.get_fits_header_before()
            assert hdr == {
                "FOCUS": FitsHeaderEntry(state.focus, "Focus value [mm]"),
                "FOCOFF": FitsHeaderEntry(0.0, "Focus offset [mm]"),
            }
        finally:
            await eaf.close()


@pytest.mark.skipif(
    os.environ.get("PYZWOHID_RUN_HARDWARE_MOTION_TESTS") != "1",
    reason="Set PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1 to run physical focuser motion tests",
)
@pytest.mark.asyncio
async def test_eaf_hardware_move_and_back() -> None:
    paths = await Eaf.discover()
    if not paths:
        pytest.skip("No physical ZWO EAF discovered via USB HID")

    for path in paths:
        async with await Eaf.open(path) as raw_eaf:
            raw_status = await raw_eaf.get_status()
            max_focus_mm = raw_status.max_step * 0.001

        comm = FakeComm()
        eaf = ZwoEaf(path=path, step_size=0.001, poll_interval=60.0, comm=comm)
        await eaf.open()
        try:
            start_focus = comm.get_own_state(IFocuser).focus
            delta = 0.020 if start_focus + 0.020 <= max_focus_mm else -0.020
            target_focus = round(start_focus + delta, 3)

            await eaf.set_focus(target_focus)
            assert comm.get_own_state(IFocuser).focus == pytest.approx(target_focus)
            assert eaf.motion_status() == MotionStatus.POSITIONED

            await eaf.set_focus(start_focus)
            assert comm.get_own_state(IFocuser).focus == pytest.approx(start_focus)
            assert eaf.motion_status() == MotionStatus.POSITIONED
        finally:
            await eaf.close()
