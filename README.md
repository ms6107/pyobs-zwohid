# `pyobs-zwohid`

`pyobs` modules for ZWO USB HID astronomy hardware using [`pyzwohid`](https://github.com/ms6107/pyzwohid):

- **`ZwoHidModule` (`pyobs_zwohid.hid`):** Shared base module for persistent `pyzwohid` device lifecycle, single-device auto-discovery or explicit path binding, and `reset_error()` hardware fault recovery.
- **`ZwoEfw` (`pyobs_zwohid.efw`):** Filter wheel module for ZWO Electronic Filter Wheels (`Efw`).
- **`ZwoEaf` (`pyobs_zwohid.eaf`):** Focuser module for ZWO Electronic Automatic Focusers (`Eaf`).

## License

Mozilla Public License 2.0 (`MPL-2.0`). See [`LICENSE`](LICENSE).
