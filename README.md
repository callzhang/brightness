# Brightness
Simple Linux tray app for controlling **all external monitors** via DDC/CI.

It shows current brightness in the menubar, supports keyboard brightness keys, and applies changes to every detected DDC display.

## Features
- Menubar label like `72%` (refresh every 5 seconds)
- Keyboard brightness keys support (`KEY_BRIGHTNESSUP/DOWN`)
- Applies brightness to all detected displays, not just one
- Single-instance lock (prevents duplicate tray icons)
- Fast path from key press to command dispatch (event-driven)
- State cache for fast startup label
- Built-in logs and crash dump support

## Platform
- Linux desktop (GNOME/KDE/XFCE and other AppIndicator-compatible environments)
- Python 3.9+

## Dependencies
Ubuntu/Debian example:

```bash
sudo apt update
sudo apt install -y ddcutil python3-gi gir1.2-ayatanaappindicator3-0.1 python3-evdev python3-pillow
```

Fedora example:

```bash
sudo dnf install -y ddcutil python3-gobject libappindicator-gtk3 python3-evdev python3-pillow
```

## Install

```bash
git clone https://github.com/callzhang/brightness.git
cd brightness
./install.sh
```

This installs:
- `~/.local/bin/brightness-indicator`
- `~/.local/share/applications/brightness-control.desktop`
- `~/.config/autostart/brightness-control.desktop`

## Run

```bash
~/.local/bin/brightness-indicator
```

Or log out/in once and let autostart run it.

## Permissions
`ddcutil` and `evdev` may require extra permissions depending on distro.

### DDC/CI access
If `ddcutil detect` works as your user, no extra config is needed.

If not, you can either:
1. Add your user to i2c/video-related groups (recommended), or
2. Allow passwordless `ddcutil` via sudoers.

Sudoers example (`visudo`):

```text
<your-user> ALL=(ALL) NOPASSWD: /usr/bin/ddcutil
```

### Keyboard key events
To read `/dev/input/event*`, your user may need `input` group membership or equivalent udev policy.

## Environment variables
- `BRIGHTNESS_STEP` (default `10`, range `1-30`)
- `BRIGHTNESS_USE_SUDO=1` to prefer `sudo -n ddcutil`

## Logs
Default state/log directory:
- `$XDG_STATE_HOME/brightness-indicator` (or `~/.local/state/brightness-indicator`)

Files:
- `app.log`
- `crash.log`
- `state.json`

## Troubleshooting
1. No monitor found:
   - Check monitor OSD: enable DDC/CI.
   - Run: `ddcutil detect`
2. Key press detected but no brightness change:
   - Run app from terminal and check `app.log`.
   - Validate sudo/i2c permissions.
3. No tray icon:
   - Verify your desktop has AppIndicator support.
4. Duplicate icon:
   - This app is singleton; if you still see duplicates, kill old processes:
     `pkill -f brightness-indicator`

## Uninstall

```bash
./uninstall.sh
```

## License
MIT
