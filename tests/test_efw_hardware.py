# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Opt-in real-hardware tests for ZwoEfw."""

from __future__ import annotations

import os

import pytest
from pyobs.events import FilterChangedEvent
from pyobs.interfaces import FiltersCapabilities, FilterState, FitsHeaderEntry, IFilters, IReady
from pyobs.utils.enums import MotionStatus
from pyzwohid import Efw
from test_hid import FakeComm

from pyobs_zwohid import ZwoEfw

pytestmark = pytest.mark.skipif(
    os.environ.get("PYZWOHID_RUN_HARDWARE_TESTS") != "1"
    and os.environ.get("PYZWOHID_RUN_HARDWARE_MOTION_TESTS") != "1",
    reason="Set PYZWOHID_RUN_HARDWARE_TESTS=1 to run real-hardware tests",
)


@pytest.mark.asyncio
async def test_efw_hardware_readonly_open_and_state() -> None:
    paths = await Efw.discover()
    if not paths:
        pytest.skip("No physical ZWO EFW discovered via USB HID")

    for path in paths:
        comm = FakeComm()
        efw = ZwoEfw(path=path, comm=comm)
        await efw.open()
        try:
            caps = comm.get_own_capabilities(IFilters)
            assert isinstance(caps, FiltersCapabilities)
            assert 1 <= len(caps.filters) <= 16

            state = comm.get_own_state(IFilters)
            assert isinstance(state, FilterState)
            assert state.filter in caps.filters
            assert efw.motion_status() == MotionStatus.POSITIONED
            assert comm.get_own_state(IReady).ready is True
            assert comm.missing_published_state(efw.interfaces) == []

            hdr = await efw.get_fits_header_before()
            assert hdr == {"FILTER": FitsHeaderEntry(state.filter, "Current filter")}
        finally:
            await efw.close()


@pytest.mark.skipif(
    os.environ.get("PYZWOHID_RUN_HARDWARE_MOTION_TESTS") != "1",
    reason="Set PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1 to run physical filter wheel motion tests",
)
@pytest.mark.asyncio
async def test_efw_hardware_move_next_filter_and_back() -> None:
    paths = await Efw.discover()
    if not paths:
        pytest.skip("No physical ZWO EFW discovered via USB HID")

    for path in paths:
        comm = FakeComm()
        efw = ZwoEfw(path=path, comm=comm)
        await efw.open()
        try:
            caps = comm.get_own_capabilities(IFilters)
            assert len(caps.filters) > 1
            start_filter = comm.get_own_state(IFilters).filter
            start_idx = caps.filters.index(start_filter)
            next_filter = caps.filters[(start_idx + 1) % len(caps.filters)]

            comm.events.clear()
            await efw.set_filter(next_filter)
            assert comm.get_own_state(IFilters).filter == next_filter
            assert efw.motion_status() == MotionStatus.POSITIONED
            filter_events = [e for e in comm.events if isinstance(e, FilterChangedEvent)]
            assert filter_events and filter_events[-1].filter == next_filter

            await efw.set_filter(start_filter)
            assert comm.get_own_state(IFilters).filter == start_filter
            assert efw.motion_status() == MotionStatus.POSITIONED
        finally:
            await efw.close()
