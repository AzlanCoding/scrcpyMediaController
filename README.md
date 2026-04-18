# scrcpyMediaController

Exposes Android media playback over MPRIS so desktop notification panels (swaync, dunst, waybar, etc.) can display and control it. Starts `scrcpy --no-window --no-video` alongside the controller and tears it down on exit. Pass `--detach` to manage scrcpy yourself. Run `audiocpy --help` for all options.

Requires GNU/Linux with D-Bus, `adb` connected to a device, and `scrcpy` in PATH.


## System dependencies

**Fedora:**
```bash
sudo dnf install android-tools scrcpy python3-devel gobject-introspection-devel cairo-gobject-devel gcc pkg-config
```

**Debian/Ubuntu:**
```bash
sudo apt install adb scrcpy python3.12-dev libgirepository1.0-dev libcairo2-dev pkg-config gcc
```

The compiler and dev headers are only needed at install time to build `pydbus`/`PyGObject`. They can be removed afterwards.


## Install

With the system dependencies above in place:

```bash
uv tool install git+https://github.com/KraXen72/scrcpyMediaController
audiocpy
```


## Local development

```bash
git clone https://github.com/KraXen72/scrcpyMediaController
cd scrcpyMediaController
uv sync
uv run audiocpy
```


## Credits

Default album art icon (`icon.png`) from the [scrcpy repository](https://github.com/Genymobile/scrcpy/blob/master/app/data/icon.png).
