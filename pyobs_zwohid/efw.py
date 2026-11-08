# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""pyobs filter wheel module for ZWO Electronic Filter Wheels (EFW)."""

from __future__ import annotations

import logging
import math
from typing import Any

from pyobs.events import FilterChangedEvent
from pyobs.interfaces import FiltersCapabilities, FilterState, IFilters, IFitsHeaderBefore
from pyobs.mixins import FilterHeaderMixin
from pyobs.modules import timeout
from pyobs.utils import exceptions as exc
from pyobs.utils.enums import MotionStatus
from pyzwohid import Efw, EfwStatus, ZwoBusyError, ZwoError

from .hid import ZwoHidModule

log = logging.getLogger(__name__)


class ZwoEfw(FilterHeaderMixin, ZwoHidModule[Efw, EfwStatus], IFilters, IFitsHeaderBefore):
    """`pyobs` module for ZWO Electronic Filter Wheels (EFW)."""

    __module__ = "pyobs_zwohid"

    DEVICE_CLS = Efw
    MOTION_INTERFACE = "IFilters"

    def __init__(
        self,
        path: str | None = None,
        filters: list[str] | None = None,
        unidirectional: bool = False,
        move_timeout: float = 30.0,
        **kwargs: Any,
    ) -> None:
        """Initialize the ZWO EFW module.

        Args:
            path: Optional OS HID device path or persistent `udev` symlink.
                If `None`, `open()` auto-discovers a single EFW device.
            filters: Optional list of filter names mapped 1-to-1 to 1-based slots
                `1..slot_count`. If `None`, defaults to `["1", ..., str(slot_count)]`
                after reading `slot_count` from hardware during `open()`.
            unidirectional: If `True`, rotate forward-only (CW) on slot changes;
                if `False` (default), use shortest-path bidirectional rotation.
            move_timeout: Maximum time in seconds to wait for a slot move to settle.
            **kwargs: Additional keyword arguments forwarded to `ZwoHidModule`.

        Raises:
            ValueError: If `move_timeout` is not finite and positive, or if `filters`
                is empty, contains blank names, or contains duplicate names.
        """
        if not math.isfinite(move_timeout) or move_timeout <= 0:
            raise ValueError(f"move_timeout must be finite and > 0, got {move_timeout}")
        if filters is not None:
            if not filters:
                raise ValueError("filters must not be empty when specified")
            if any(not name.strip() for name in filters):
                raise ValueError(f"filters must contain non-empty names, got {filters!r}")
            if len(set(filters)) != len(filters):
                raise ValueError(f"filters must not contain duplicate names, got {filters!r}")

        super().__init__(path=path, **kwargs)
        self._configured_filters: tuple[str, ...] | None = tuple(filters) if filters is not None else None
        self._filters: tuple[str, ...] = ()
        self._slot_to_filter: dict[int, str] = {}
        self._filter_to_slot: dict[str, int] = {}
        self._unidirectional = unidirectional
        self._move_timeout = move_timeout

    async def _publish_status(self, status: EfwStatus) -> EfwStatus:
        """Publish `IMotion`, `IReady`, and `IFilters` states for `status`."""
        motion_status = MotionStatus.SLEWING if status.moving else MotionStatus.POSITIONED
        await self._change_motion_status(motion_status, interface=self.MOTION_INTERFACE)
        filter_name = self._slot_to_filter.get(status.slot, "")
        await self.comm.set_state(IFilters, FilterState(filter=filter_name))
        return status

    async def _setup_device(self, device: Efw) -> None:
        """Wait for power-on motion to settle, validate filter count, and publish capabilities/state."""
        status = await device.get_status()
        if status.moving:
            status = await device._wait_until_settled(self._move_timeout, "open()")

        if self._configured_filters is None:
            filters = tuple(str(slot) for slot in range(1, status.slot_count + 1))
        else:
            if len(self._configured_filters) != status.slot_count:
                raise ValueError(
                    f"Configured {len(self._configured_filters)} filters {list(self._configured_filters)!r}, "
                    f"but EFW at {device.path!r} reported {status.slot_count} slots"
                )
            filters = self._configured_filters

        self._filters = filters
        self._slot_to_filter = {slot: name for slot, name in enumerate(filters, start=1)}
        self._filter_to_slot = {name: slot for slot, name in enumerate(filters, start=1)}

        await self.comm.set_capabilities(IFilters, FiltersCapabilities(filters=list(filters)))
        await self._publish_status(status)

    async def _refresh_state(self, device: Efw) -> EfwStatus:
        """Query `device.get_status()` and publish updated motion and filter states."""
        return await self._publish_status(await device.get_status())

    @timeout(60)
    async def set_filter(self, filter_name: str, **kwargs: Any) -> None:
        """Move the filter wheel to `filter_name`.

        Args:
            filter_name: Configured name of the target filter.
            **kwargs: Additional RPC keyword arguments.

        Raises:
            InvalidArgumentError: If `filter_name` is not a configured filter.
            DeviceBusyError: If the filter wheel is already moving.
            MoveError: If the filter wheel move fails or times out.
        """
        target_slot = self._filter_to_slot.get(filter_name)
        if target_slot is None:
            raise exc.InvalidArgumentError(f"Unknown filter {filter_name!r}; available filters: {list(self._filters)}")

        device = self._require_device()
        log.info("Moving filter wheel to %s (slot %d)...", filter_name, target_slot)
        await self._change_motion_status(MotionStatus.SLEWING, interface=self.MOTION_INTERFACE)
        try:
            settled_slot = await device.move_to_slot(
                target_slot,
                unidirectional=self._unidirectional,
                timeout=self._move_timeout,
            )
        except ZwoBusyError as err:
            await self._refresh_state(device)
            raise exc.DeviceBusyError(str(err)) from err
        except (ZwoError, TimeoutError) as err:
            await self._change_motion_status(MotionStatus.ERROR, interface=self.MOTION_INTERFACE)
            raise exc.MoveError(f"Failed to move filter wheel to {filter_name!r} (slot {target_slot}): {err}") from err

        settled_name = self._slot_to_filter[settled_slot]
        await self._change_motion_status(MotionStatus.POSITIONED, interface=self.MOTION_INTERFACE)
        await self.comm.set_state(IFilters, FilterState(filter=settled_name))
        await self.comm.send_event(FilterChangedEvent(current=settled_name))
        log.info("Filter wheel positioned at %s (slot %d).", settled_name, settled_slot)

    async def stop_motion(self, device: str | None = None, **kwargs: Any) -> None:
        """Stop filter wheel motion (no-op; ZWO EFW protocol does not support aborting slot motion)."""


__all__ = ["ZwoEfw"]
