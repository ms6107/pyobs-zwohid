# Agent Instructions: `pyobs-zwohid`

`pyobs-zwohid` provides `pyobs` modules for ZWO USB HID astronomy hardware (Electronic Filter Wheels `Efw` and Electronic Automatic Focusers `Eaf`) built on top of `pyzwohid` and `pyobs-core`.

## Architectural Boundaries
- **Base module (`pyobs_zwohid/hid.py`):** `ZwoHidModule[DeviceT, StatusT]` manages device path discovery/opening (`open()`), rollback on startup failure, clean handle teardown (`close()`), `IMotion` common methods (`init()`, `park()`), and MCU error clearing (`reset_error()`).
- **Filter wheel (`pyobs_zwohid/efw.py`):** `ZwoEfw` wraps `pyzwohid.Efw` and implements `IFilters`, `ICalibrate`, and `IFitsHeaderBefore` (via `FilterHeaderMixin`).
- **Focuser (`pyobs_zwohid/eaf.py`):** `ZwoEaf` wraps `pyzwohid.Eaf` and implements `IFocuser`, `ITemperatures`, and `IFitsHeaderBefore` (via `FocuserHeaderMixin`).

## Testing & Hygiene
- Mirror the production class hierarchy in tests: `tests/test_hid.py` defines `FakeZwoHidDevice` and tests `ZwoHidModule` lifecycle invariants once; `tests/test_efw.py` and `tests/test_eaf.py` inherit from `FakeZwoHidDevice` and test device-specific module behavior without physical hardware.
- Run `ruff check`, `ruff format --check`, `mypy --strict`, and `pytest`, then clean temporary artifacts (`git clean -fdX -e !.venv`).
