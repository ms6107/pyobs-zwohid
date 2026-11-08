# `pyobs-zwohid`

[`pyobs`](https://www.pyobs.org) ([`pyobs-core`](https://github.com/pyobs/pyobs-core)) modules for ZWO USB HID astronomy hardware (Electronic Filter Wheels and Electronic Automatic Focusers), built on [`pyzwohid`](https://github.com/ms6107/pyzwohid):

- **[`ZwoHidModule`](pyobs_zwohid/hid.py) (`pyobs_zwohid.hid`):** Shared base module managing a persistent `pyzwohid` device handle across `open()` / `close()`, single-device auto-discovery when `path` is omitted, and `reset_error()` hardware fault recovery (`clear_error()` + state refresh).
- **[`ZwoEfw`](pyobs_zwohid/efw.py) (`pyobs_zwohid.efw`):** Filter wheel module implementing `IFilters`, `ICalibrate`, and `IFitsHeaderBefore` (`FilterHeaderMixin`).
- **[`ZwoEaf`](pyobs_zwohid/eaf.py) (`pyobs_zwohid.eaf`):** Focuser module implementing `IFocuser`, `ITemperatures`, and `IFitsHeaderBefore` (`FocuserHeaderMixin`).

## Prerequisites

- **Python `>= 3.11`**
- **System `libhidapi` shared library** (e.g. `libhidapi-hidraw0` on openSUSE Tumbleweed and Debian/Ubuntu) and non-root `hidraw` permissions via `pyzwohid`'s `udev` rules (`70-pyzwohid.rules`).

## Configuration Examples

### 1. Filter Wheel (`ZwoEfw`)

When only a single ZWO EFW is connected to the host, `path` can be omitted and `ZwoEfw` will auto-discover it on `open()`. When multiple EFWs are attached (or to bind to a specific USB port), pass a persistent `udev` symlink such as `/dev/hidraw/by-path/...`:

```yaml
class: pyobs_zwohid.ZwoEfw
# path: /dev/hidraw/by-path/pci-0000:00:14.0-usb-0:4:1.0
filters:
  - L
  - R
  - G
  - B
  - SII
  - Ha
  - OIII
unidirectional: false
move_timeout: 30.0
calibrate_timeout: 60.0
```

Filter names in `filters` map 1-to-1 to 1-based physical carousel slots `1..slot_count`. If `filters` is omitted, slot numbers `"1"`, `"2"`, ..., `str(slot_count)` are used automatically after reading `slot_count` from the wheel during `open()`.

### 2. Focuser (`ZwoEaf`)

`ZwoEaf` converts between `pyobs` millimeter coordinates and integer stepper coordinates using `step_size` (in `mm/step`, defaulting to `0.001` = $1\,\mu\text{m}/\text{step}$). Optional hardware parameters (`max_step`, `backlash`, `reverse`, `beep`, `max_force`) default to `None` (keeping the settings stored in the focuser's NVRAM) and are written on `open()` only if a specified value differs from the focuser's live status:

```yaml
class: pyobs_zwohid.ZwoEaf
# path: /dev/hidraw/by-path/pci-0000:00:14.0-usb-0:5:1.0
step_size: 0.001
move_timeout: 60.0
stop_timeout: 5.0
poll_interval: 10.0
# Optional NVRAM hardware settings (omit or set to null to preserve device state):
# max_step: 60000
# backlash: 15
# reverse: false
# beep: false
# max_force: 3
```

### 3. Temperature Compensation & Filter Offsets (`FocusModel`)

`ZwoEaf` implements `ITemperatures` and publishes the focuser's temperature probe reading under sensor name `"Focus"`, which can be referenced alongside `ZwoEfw` in `pyobs.modules.focus.FocusModel`:

```yaml
class: pyobs.modules.focus.FocusModel
focuser: focuser
filter_wheel: filter_wheel
default_filter: L
filter_offsets:
  L: 0.0
  R: -0.012
  G: -0.008
  B: 0.015
  SII: 0.038
  Ha: 0.042
  OIII: 0.019
temperatures:
  T_focus:
    module: focuser
    sensor: Focus
model: -0.015 * T_focus + 25.400
```

## Running Tests

The default test suite uses in-memory `pyzwohid` fakes and runs without physical hardware attached:

```bash
uv run pytest
uv run mypy --strict pyobs_zwohid tests
uv run ruff check .
uv run ruff format --check .
```

### Opt-In Hardware Tests

When a physical ZWO EFW or EAF is connected:

- **Read-only status and state-publishing tests:**
  ```bash
  PYZWOHID_RUN_HARDWARE_TESTS=1 uv run pytest tests/test_efw_hardware.py tests/test_eaf_hardware.py -v
  ```
- **Physical motion tests (moves to the next slot / step and returns to the starting position):**
  ```bash
  PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1 uv run pytest tests/test_efw_hardware.py tests/test_eaf_hardware.py -v
  ```

> **Warning:** Never run motion tests on equipment mounted on an active telescope or during live observations.

## License

Mozilla Public License 2.0 (`MPL-2.0`). See [`LICENSE`](LICENSE).
