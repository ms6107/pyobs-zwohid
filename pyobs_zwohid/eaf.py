# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""pyobs focuser module for ZWO Electronic Automatic Focusers (EAF)."""

from __future__ import annotations

import logging
import math
from typing import Annotated, Any

from pyobs.interfaces import FocuserState, IFitsHeaderBefore, IFocuser
from pyobs.mixins import FocuserHeaderMixin
from pyobs.modules import timeout
from pyobs.utils import exceptions as exc
from pyobs.utils.enums import MotionStatus, Unit
from pyzwohid import Eaf, EafStatus, ZwoBusyError, ZwoError

from .hid import ZwoHidModule

log = logging.getLogger(__name__)


class ZwoEaf(FocuserHeaderMixin, ZwoHidModule[Eaf, EafStatus], IFocuser, IFitsHeaderBefore):
    """`pyobs` module for ZWO Electronic Automatic Focusers (EAF)."""

    __module__ = "pyobs_zwohid"

    DEVICE_CLS = Eaf
    MOTION_INTERFACE = "IFocuser"

    def __init__(
        self,
        path: str | None = None,
        step_size: float = 0.001,
        move_timeout: float = 60.0,
        stop_timeout: float = 5.0,
        max_step: int | None = None,
        backlash: int | None = None,
        reverse: bool | None = None,
        beep: bool | None = None,
        max_force: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the ZWO EAF focuser module.

        Args:
            path: Optional OS HID device path or persistent `udev` symlink.
                If `None`, `open()` auto-discovers a single EAF device.
            step_size: Conversion factor in millimeters per focuser step (`mm/step`).
                Defaults to `0.001` (`1 µm/step`).
            move_timeout: Maximum time in seconds to wait for a focus move to settle.
            stop_timeout: Maximum time in seconds to wait for `stop_motion()` to settle.
            max_step: Optional maximum step limit (`>= 1`). If `None`, keeps the
                focuser's current `max_step`.
            backlash: Optional backlash compensation in steps (`0..255`). If `None`,
                keeps the focuser's current `backlash`.
            reverse: Optional motor direction reversal flag. If `None`, keeps the
                focuser's current direction.
            beep: Optional motion buzzer flag. If `None`, keeps the focuser's
                current buzzer setting.
            max_force: Optional motor torque / holding force level (`1..5`). If `None`,
                keeps the focuser's current `max_force`.
            **kwargs: Additional keyword arguments forwarded to `ZwoHidModule`.

        Raises:
            ValueError: If `step_size`, `move_timeout`, or `stop_timeout` is not
                finite and positive, or if `max_step`, `backlash`, or `max_force` is
                outside its valid domain.
        """
        if not math.isfinite(step_size) or step_size <= 0:
            raise ValueError(f"step_size must be finite and > 0, got {step_size}")
        if not math.isfinite(move_timeout) or move_timeout <= 0:
            raise ValueError(f"move_timeout must be finite and > 0, got {move_timeout}")
        if not math.isfinite(stop_timeout) or stop_timeout <= 0:
            raise ValueError(f"stop_timeout must be finite and > 0, got {stop_timeout}")
        if max_step is not None and max_step < 1:
            raise ValueError(f"max_step must be >= 1, got {max_step}")
        if backlash is not None and not (0 <= backlash <= 255):
            raise ValueError(f"backlash must be in [0, 255], got {backlash}")
        if max_force is not None and not (1 <= max_force <= 5):
            raise ValueError(f"max_force must be in [1, 5], got {max_force}")

        super().__init__(path=path, **kwargs)
        self._step_size = step_size
        self._move_timeout = move_timeout
        self._stop_timeout = stop_timeout
        self._max_step = max_step
        self._backlash = backlash
        self._reverse = reverse
        self._beep = beep
        self._max_force = max_force
        self._focus_offset = 0.0
        self._aborted = False

    def _step_to_focus(self, step: int) -> float:
        """Convert hardware `step` to base focus in millimeters (`step * step_size - focus_offset`)."""
        return step * self._step_size - self._focus_offset

    async def _publish_settled_step(self, step: int) -> None:
        """Publish `MotionStatus.POSITIONED` and `FocuserState` for `step`."""
        await self._change_motion_status(MotionStatus.POSITIONED, interface=self.MOTION_INTERFACE)
        await self.comm.set_state(
            IFocuser,
            FocuserState(focus=self._step_to_focus(step), focus_offset=self._focus_offset),
        )

    async def _publish_status(self, status: EafStatus) -> EafStatus:
        """Publish `IMotion`, `IReady`, and `IFocuser` states for `status`."""
        motion_status = MotionStatus.SLEWING if status.moving else MotionStatus.POSITIONED
        await self._change_motion_status(motion_status, interface=self.MOTION_INTERFACE)
        await self.comm.set_state(
            IFocuser,
            FocuserState(focus=self._step_to_focus(status.step), focus_offset=self._focus_offset),
        )
        return status

    def _needs_configuration(self, status: EafStatus) -> bool:
        """Return `True` if any configured hardware parameter differs from `status`."""
        return (
            (self._max_step is not None and status.max_step != self._max_step)
            or (self._backlash is not None and status.backlash != self._backlash)
            or (self._reverse is not None and status.reverse != self._reverse)
            or (self._beep is not None and status.beep != self._beep)
            or (self._max_force is not None and status.max_force != self._max_force)
        )

    async def _setup_device(self, device: Eaf) -> None:
        """Read initial status from the EAF, apply differing hardware settings, and publish states."""
        status = await device.get_status()
        if self._needs_configuration(status):
            await device.configure(
                max_step=self._max_step,
                backlash=self._backlash,
                reverse=self._reverse,
                beep=self._beep,
                max_force=self._max_force,
            )
            status = await device.get_status()
        await self._publish_status(status)

    async def _refresh_state(self, device: Eaf) -> EafStatus:
        """Query `device.get_status()` and publish updated motion and focuser states."""
        return await self._publish_status(await device.get_status())

    async def _move_to_total_mm(self, total_mm: float) -> None:
        """Move the focuser to `total_mm` (`focus + focus_offset`) in millimeters."""
        device = self._require_device()
        target_step = round(total_mm / self._step_size)
        if target_step < 0:
            raise exc.InvalidArgumentError(f"Target focus {total_mm:.4f} mm (step {target_step}) must be >= 0")

        self._aborted = False
        log.info("Moving focuser to %.4f mm (step %d)...", total_mm, target_step)
        await self._change_motion_status(MotionStatus.SLEWING, interface=self.MOTION_INTERFACE)
        try:
            settled_step = await device.move(target_step, timeout=self._move_timeout)
        except ValueError as err:
            await self._refresh_state(device)
            raise exc.InvalidArgumentError(str(err)) from err
        except ZwoBusyError as err:
            await self._refresh_state(device)
            raise exc.DeviceBusyError(str(err)) from err
        except (ZwoError, TimeoutError) as err:
            await self._change_motion_status(MotionStatus.ERROR, interface=self.MOTION_INTERFACE)
            raise exc.MoveError(f"Failed to move focuser to {total_mm:.4f} mm (step {target_step}): {err}") from err

        if self._aborted:
            await self._publish_settled_step(settled_step)
            raise exc.AbortedError("Focus movement was aborted.")

        if settled_step != target_step:
            await self.comm.set_state(
                IFocuser,
                FocuserState(focus=self._step_to_focus(settled_step), focus_offset=self._focus_offset),
            )
            await self._change_motion_status(MotionStatus.ERROR, interface=self.MOTION_INTERFACE)
            raise exc.MoveError(f"Focuser settled at step {settled_step} instead of target step {target_step}")

        await self._publish_settled_step(settled_step)
        log.info("Focuser positioned at %.4f mm (step %d).", total_mm, settled_step)

    @timeout(120)
    async def set_focus(self, focus: Annotated[float, Unit.MM], **kwargs: Any) -> None:
        """Move the focuser to base `focus` (in mm, excluding `focus_offset`).

        Args:
            focus: New base focus value in millimeters.
            **kwargs: Additional RPC keyword arguments.

        Raises:
            InvalidArgumentError: If `focus` is non-finite or maps outside `[0, max_step]`.
            DeviceBusyError: If the focuser is already moving.
            AbortedError: If movement was stopped via `stop_motion()`.
            MoveError: If the focuser move fails, stalls, or times out.
        """
        if not math.isfinite(focus):
            raise exc.InvalidArgumentError(f"focus must be finite, got {focus}")
        await self._move_to_total_mm(focus + self._focus_offset)

    @timeout(120)
    async def set_focus_offset(self, offset: Annotated[float, Unit.MM], **kwargs: Any) -> None:
        """Set a new `focus_offset` (in mm) and move the focuser accordingly.

        Args:
            offset: New focus offset in millimeters.
            **kwargs: Additional RPC keyword arguments.

        Raises:
            InvalidArgumentError: If `offset` is non-finite or maps outside `[0, max_step]`.
            DeviceBusyError: If the focuser is already moving.
            AbortedError: If movement was stopped via `stop_motion()`.
            MoveError: If reading status or moving the focuser fails, stalls, or times out.
        """
        if not math.isfinite(offset):
            raise exc.InvalidArgumentError(f"offset must be finite, got {offset}")

        device = self._require_device()
        try:
            status = await device.get_status()
        except ZwoError as err:
            await self._change_motion_status(MotionStatus.ERROR, interface=self.MOTION_INTERFACE)
            raise exc.MoveError(f"Failed to read focuser status: {err}") from err

        if status.moving:
            await self._publish_status(status)
            raise exc.DeviceBusyError("EAF is already moving")

        base_focus = self._step_to_focus(status.step)
        target_total_mm = base_focus + offset
        target_step = round(target_total_mm / self._step_size)
        if not (0 <= target_step <= status.max_step):
            raise exc.InvalidArgumentError(
                f"Target focus {target_total_mm:.4f} mm (step {target_step}) "
                f"outside allowed range [0, {status.max_step}]"
            )

        self._focus_offset = offset
        await self._move_to_total_mm(target_total_mm)

    async def stop_motion(self, device: str | None = None, **kwargs: Any) -> None:
        """Stop focuser motion and publish the settled position.

        Args:
            device: Optional device name (ignored).
            **kwargs: Additional RPC keyword arguments.

        Raises:
            MoveError: If stopping the focuser fails or times out.
        """
        eaf = self._require_device()
        self._aborted = True
        try:
            settled_step = await eaf.stop(timeout=self._stop_timeout)
        except (ZwoError, TimeoutError) as err:
            await self._change_motion_status(MotionStatus.ERROR, interface=self.MOTION_INTERFACE)
            raise exc.MoveError(f"Failed to stop focuser: {err}") from err
        await self._publish_settled_step(settled_step)


__all__ = ["ZwoEaf"]
