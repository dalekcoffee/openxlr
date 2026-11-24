# wave1-gain

A small command for one job: reading and setting the **hardware** gain of an
Elgato Wave:1 on Linux, and choosing whether the microphone's dial turns the
gain or the headphone volume.

It is derived from OpenXLR's Wave:1 support (`src/OpenXLR.Core/Devices/Wave3Device.cs`
and `Wave1Device.cs` in this repository), reduced to the gain and the dial.
It does not depend on OpenXLR and does not run alongside it: stop OpenXLR's
daemon first if you have it.

## What is in it

| File | What it is |
|---|---|
| `wave1gain.py` | The program: one Python file, standard library only, about 350 lines |
| `test_wave1gain.py` | Its tests; they need no microphone |
| `70-wave1-gain.rules` | The udev rule that lets your user open this one microphone |
| `wave1-gain-apply.service` | Optional: sets your gain once at each login |

## What it can and cannot do

- **Reads** four small files per USB device under `/sys/bus/usb/devices` to
  find the Wave:1 (`0fd9:006e`), then **opens** that one device node under
  `/dev/bus/usb`. It checks the node's own descriptor says Wave:1 before
  using it.
- **Sends two USB requests, and no others**: read the 16-byte settings block
  and write it back (`ALLOWED_REQUESTS` in the code; anything else is refused
  before it reaches the kernel). A write changes only the gain (bytes 0 and 1)
  or the dial target (byte 12); every other byte goes back as the microphone
  sent it. After a write it reads the block again and reports an error if the
  microphone did not keep the value.
- **Refuses to write** if the microphone answers anything other than the
  known 16-byte layout.
- **No network, no other programs, no files written, no root.** No libusb
  either: it talks to the kernel's usbfs directly. `python3 -I` (isolated
  mode, in the first line of the file) ignores `PYTHON*` variables and your
  user site-packages, so nothing outside the file changes what runs. The test
  suite fails if anyone adds an import beyond `argparse ctypes errno fcntl os
  struct sys time`, a file opened for writing, or a call such as `subprocess`,
  `socket`, `exec` or `eval`.

## Install (Fedora)

From this folder. The program goes in a root-owned location so other
programs running as you cannot change it.

```sh
python3 -I -B test_wave1gain.py -v
sudo install -m 0755 -o root -g root wave1gain.py /usr/local/bin/wave1-gain
sudo install -m 0644 -o root -g root 70-wave1-gain.rules /etc/udev/rules.d/70-wave1-gain.rules
sudo udevadm control --reload-rules
```

Then unplug the microphone and plug it back in.

If OpenXLR is installed, stop its daemon, which holds the same interface:
`systemctl --user disable --now openxlr-daemon`.

## Use

```sh
wave1-gain status            # gain: 11 dB / dial: headphones
wave1-gain set 8             # 0 to 40 dB, whole numbers
wave1-gain dial gain         # the dial now turns the gain
wave1-gain watch             # prints the gain each time it changes; Ctrl+C stops
wave1-gain dial headphones   # the dial goes back to headphone volume
```

`watch` reads the microphone four times a second and lets go of it between
reads, so `set` and `dial` work in another terminal while it runs.

## The dial

By default the Wave:1's dial sets the headphone volume. Elgato's Wave Link
can switch it to the gain; `wave1-gain dial gain` does the same by writing
byte 12 of the block. The microphone then changes its own gain when you turn
the dial, with no software involved, and `wave1-gain watch` shows the value.

**This is the one part not confirmed on a Wave:1.** Byte 12's meaning
(1 gain, 2 headphones) comes from three public studies of the Wave:3, and an
owner's Wave:1 reported 2 there with the dial on headphones, but nobody has
written it on a Wave:1 before. If the microphone does not keep it, the
command says so and nothing else changes. `wave1-gain dial headphones` puts
it back.

## Optional: set the gain at login

If the gain jumps back up after a replug or a reboot, this user unit sets it
once each time you log in. Edit the number on its `ExecStart` line first.

```sh
mkdir -p ~/.config/systemd/user
install -m 0644 wave1-gain-apply.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wave1-gain-apply.service
systemctl --user status wave1-gain-apply.service
```

It runs with network sockets denied (`RestrictAddressFamilies=none`) and
`NoNewPrivileges=yes`.

## Uninstall

```sh
systemctl --user disable --now wave1-gain-apply.service 2>/dev/null
rm -f ~/.config/systemd/user/wave1-gain-apply.service
sudo rm /usr/local/bin/wave1-gain /etc/udev/rules.d/70-wave1-gain.rules
sudo udevadm control --reload-rules
```

## Notes

- The desktop's input volume slider may keep showing an old value, since the
  kernel caches the microphone's volume and never re-reads it. Some firmware
  also takes the slider's value as the hardware gain. To see which yours
  does, move the slider and run `wave1-gain status`: if the gain changed,
  the slider writes it too, so set the two consistently.
- Gain is in whole dB, 0 to 40, matching what OpenXLR verified on a Wave:1.
- Tested without hardware only (the test suite and the error paths). The
  gain write is the same request OpenXLR's Wave:1 owner confirmed.
