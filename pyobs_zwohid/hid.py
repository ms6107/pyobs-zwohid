# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Shared pyobs base module for ZWO USB HID astronomy devices."""

from __future__ import annotations

from typing import Any, ClassVar, Generic, TypeVar

from pyobs.mixins import MotionStatusMixin
from pyobs.modules import Module
from pyobs.utils import exceptions as exc
from pyzwohid import ZwoError, ZwoHidDevice, ZwoHidStatus

_DeviceT = TypeVar("_DeviceT", bound=ZwoHidDevice[Any])
_StatusT = TypeVar("_StatusT", bound=ZwoHidStatus)


class ZwoHidModule(Module, MotionStatusMixin, Generic[_DeviceT, _StatusT]):
    """Base `pyobs` module managing a persistent `pyzwohid` USB HID device handle.

    Subclasses define `DEVICE_CLS` (`Efw` or `Eaf`) and `MOTION_INTERFACE`
    (`"IFilters"` or `"IFocuser"`), and implement `_refresh_state(device)` (plus
    `_setup_device(device)` when additional startup validation or configuration is
    needed).
    """

    __module__ = "pyobs_zwohid"

    state: ClassVar[Any] = None
    capabilities: ClassVar[Any] = None
    DEVICE_CLS: type[_DeviceT]
    MOTION_INTERFACE: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Enforce at class definition time that concrete subclasses define `DEVICE_CLS` and `MOTION_INTERFACE`."""
        super().__init_subclass__(**kwargs)
        if not hasattr(cls, "DEVICE_CLS") or not cls.MOTION_INTERFACE:
            raise TypeError(f"{cls.__name__} must define DEVICE_CLS and non-empty MOTION_INTERFACE")

    def __init__(self, path: str | None = None, **kwargs: Any) -> None:
        """Initialize the base ZWO HID module.

        Args:
            path: Optional OS HID device path or persistent `udev` symlink
                (e.g. `"/dev/hidraw/by-path/..."`). If `None`, `open()` calls
                `await DEVICE_CLS.discover()` and opens the sole matching device.
            **kwargs: Additional keyword arguments forwarded to `Module`.

        Raises:
            ValueError: If `path` is an empty or whitespace-only string.
        """
        if path is not None and not path.strip():
            raise ValueError(f"path must be a non-empty string when specified, got {path!r}")
        super().__init__(motion_status_interfaces=[self.MOTION_INTERFACE], **kwargs)
        self._path = path
        self._device: _DeviceT | None = None
        self._register_exception(exc.MotionError, 3, timespan=600, callback=self._default_remote_error_callback)

    async def _open_device(self) -> _DeviceT:
        """Resolve `self._path` (or auto-discover a single device) and open the `pyzwohid` handle."""
        if self._path is not None:
            return await self.DEVICE_CLS.open(self._path)
        paths = await self.DEVICE_CLS.discover()
        if not paths:
            raise ZwoError(f"No {self.DEVICE_CLS.__name__} devices found")
        if len(paths) > 1:
            raise ZwoError(f"Multiple {self.DEVICE_CLS.__name__} devices found ({paths}); specify 'path' explicitly")
        return await self.DEVICE_CLS.open(paths[0])

    def _require_device(self) -> _DeviceT:
        """Return the open `_DeviceT` handle or raise `ModuleError` if the module is not open."""
        if self._device is None:
            raise exc.ModuleError(f"{type(self).__name__} is not open")
        return self._device

    async def _setup_device(self, device: _DeviceT) -> None:
        """Perform subclass-specific hardware setup and initial state publishing during `open()`."""
        await self._refresh_state(device)

    async def _refresh_state(self, device: _DeviceT) -> _StatusT:
        """Query `device`, publish `pyobs` interface states and motion status, and return the status snapshot."""
        raise NotImplementedError

    async def open(self) -> None:
        """Open the `pyobs` module, connect the persistent `pyzwohid` device, and publish initial state.

        If `_setup_device()` raises or is cancelled after the OS HID handle is opened,
        the handle is closed before re-raising so `self._device` remains `None` and no
        file descriptor is leaked.
        """
        await Module.open(self)
        await MotionStatusMixin.open(self)
        try:
            device = await self._open_device()
            try:
                await self._setup_device(device)
            except BaseException:
                await device.close()
                raise
        except BaseException:
            self._stop_background_tasks()
            self._opened = False
            raise
        self._device = device

    async def close(self) -> None:
        """Stop background tasks, close the `pyobs` module, and close the persistent `pyzwohid` handle."""
        try:
            await super().close()
        finally:
            device = self._device
            self._device = None
            if device is not None:
                await device.close()

    async def init(self, **kwargs: Any) -> None:
        """Initialize device (no-op; ZWO HID devices are initialized on `open()`)."""

    async def park(self, **kwargs: Any) -> None:
        """Park device (no-op; ZWO HID devices remain positioned and ready)."""

    async def reset_error(self, **kwargs: Any) -> bool:
        """Clear latched MCU hardware fault (`01 0f`), refresh state, and clear module error state."""
        device = self._require_device()
        await device.clear_error()
        await self._refresh_state(device)
        return await super().reset_error(**kwargs)


__all__ = ["ZwoHidModule"]
