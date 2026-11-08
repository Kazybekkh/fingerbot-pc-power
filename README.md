# fingerbot-pc-power

Turn on your desktop PC from anywhere when Wake-on-LAN doesn't work.

A Tuya Bluetooth fingerbot sits on the PC's power button. A Raspberry Pi next to it drives the fingerbot over Bluetooth LE. You reach the Pi over [Tailscale](https://tailscale.com) through a small web page or HTTP API. No router ports are opened, and the Tuya cloud is only used once, during setup.

```
 Phone / laptop ──(Tailscale)──▶ Raspberry Pi ──(Bluetooth LE)──▶ Fingerbot ──▶ PC power button
   web page / curl + token        FastAPI on its 100.x address      Tuya BLE, encrypted
                                  pings the PC over Tailscale ◀───────────────────── PC
```

> [!WARNING]
> The Pi decides the PC is off when the PC doesn't answer ping over Tailscale. If the PC is **on** but unreachable (a VPN on the PC, a Tailscale outage), a Wake press goes through. It can then shut the PC down or suspend it, depending on the OS power-button setting. Force restart always cuts power and loses unsaved work.

## Features

- **Wake:** presses the button with a 3 s hold, only when the PC doesn't answer ping.
- **Force restart:** holds 10 s to force an ATX PC off, waits 15 s, then presses to boot. Needs explicit confirmation.
- **Safety rules:** a 180 s cooldown after a press, one press at a time, and no retry once the press command was sent. Bluetooth failures before that point are retried 3 times.
- **Boot check:** after every press it watches for up to 180 s and logs whether the PC came up.
- **Mobile web page** you can add to your home screen. Plain-text replies (`?format=text`) for Apple Shortcuts.
- Binds **only** to the Pi's Tailscale address, and every call needs a bearer token.
- Runs as a **user** systemd service, so no sudo is needed.

## Status

This runs on the author's setup: a Raspberry Pi 5 on Debian 13 with Python 3.13, a fingerbot with product id `mknd4lci` (model `BS-FB-V3.1`), and an Ubuntu desktop.

- Real presses with `tuya_press.py` booted the PC (21 and 23 Oct 2026). The service's Wake path has pressed the real fingerbot several times (`press.log`, Oct–Nov 2026).
- The Force restart sequence and the boot check have only been tested offline (`tests/`). They have **not** yet run against the real fingerbot.
- Other fingerbot models may use different data points. See [Data points](#data-points).

## What you need

| Part | Notes |
| --- | --- |
| Tuya BLE fingerbot | Set up in the **Smart Life** app. It advertises as `TY`. It is *not* a SwitchBot Bot, so PySwitchbot can't drive it. |
| Linux box with BLE, in range of the fingerbot | A Raspberry Pi with built-in Bluetooth works. Your user doesn't need to be in the `bluetooth` group. |
| Tailscale | On the Pi, the PC, and every device you control it from. |
| A free Tuya developer account | Needed once, to fetch the fingerbot's local encryption key. |

## Setup

### 1. Mount and test the fingerbot

Add it in Smart Life and stick it so the arm hits the centre of the power button. In the app, check that a press with a **3 s hold** boots the PC. On the author's PC a 0 s tap did nothing, and an 85% push depth missed the button. 100% worked.

### 2. Get the local key (once)

1. Create a Cloud project at <https://platform.tuya.com>. Choose the **data centre that matches your Smart Life account**. With the wrong one, your device list is empty. Central Europe is `eu`, Western Europe is `eu-w`.
2. On the project's **Service API** tab, subscribe to **IoT Core**. Without it you get `Code 28841001: 'IoT Core service is not subscribed.'`.
3. Under **Devices → Link App Account**, scan the QR code with Smart Life.
4. Copy the project's Access ID and Access Secret, and the fingerbot's device id.
5. Run:

   ```bash
   pip install tinytuya
   python tools/get_local_key.py --region eu --access-id ... --access-secret ... --device-id ... > tuya.env
   chmod 600 tuya.env
   ```

Re-pairing the fingerbot in the app changes the key. Run the script again after that.

### 3. Find the fingerbot's Bluetooth address

Turn your phone's Bluetooth off first. The fingerbot only accepts one connection at a time.

```bash
bluetoothctl --timeout 20 scan on   # look for "TY"
bluetoothctl info <MAC>             # UUIDs 0x1910 / 0xa201, manufacturer 0x07d0
```

### 4. Install on the Pi

```bash
git clone https://github.com/Kazybekkh/fingerbot-pc-power ~/fingerbot-pc-power
cd ~/fingerbot-pc-power
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp pc-power.env.example pc-power.env && chmod 600 pc-power.env   # then edit it
```

Test the connection. Without flags, `tuya_press.py` only connects and prints the data points:

```bash
./.venv/bin/python tuya_press.py                              # expect: paired True dps {...}
./.venv/bin/python tuya_press.py --hold 3 --down 100 --press  # real press, no safety checks
```

### 5. Run it as a service

```bash
mkdir -p ~/.config/systemd/user
cp pc-power.service ~/.config/systemd/user/
loginctl enable-linger "$USER"    # start at boot without logging in
systemctl --user daemon-reload
systemctl --user enable --now pc-power.service
ss -ltn | grep 8741               # must show only your 100.x address
```

`serve.sh` waits up to 60 s for a `100.x` address on `tailscale0`, and refuses to start without one. It never listens on `0.0.0.0` or your LAN address.

## Use

Open `http://<pi-tailscale-ip>:8741/` on any device on your tailnet. Enter `API_TOKEN` once. It is kept in that browser's localStorage. You can also open `/#token=...` to fill it in.

```bash
curl -H "Authorization: Bearer $TOKEN" http://<pi>:8741/status
curl -X POST -H "Authorization: Bearer $TOKEN" http://<pi>:8741/press
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"confirm": true}' http://<pi>:8741/restart
```

| Endpoint | Responses |
| --- | --- |
| `GET /status` | `{"pc_up", "host", "cooldown_s", "job"}` |
| `POST /press` | 200 pressed · 409 PC reachable, cooldown or busy · 502 Bluetooth failure · 401 bad token |
| `POST /press?force=true` | Skips the ping check. **Can switch off a running PC.** |
| `POST /restart` | 400 without `{"confirm": true}` or `?confirm=true` |
| any + `?format=text` | Short plain text, for Apple Shortcuts |

Every decision is appended to `press.log` with a UTC timestamp.

## Data points

Read from the author's fingerbot (`mknd4lci`). Run `tuya_press.py` without flags to see yours.

| DP | Name | Used as |
| --- | --- | --- |
| 1 | `switch_1` | Toggled. In click mode any change makes one press. |
| 101 | `mode` | `0` = click. Left as is. |
| 102 | `down_percent` | Push depth, set to `TUYA_DOWN_PERCENT` (100) on every press |
| 103 | `sustain_time` | Hold seconds, set to `TUYA_HOLD_SECONDS` (3), or 10 for force-off |
| 105 | battery | % |
| 111 | `total_movement` | +2 per press. Shows the arm moved. |

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Fingerbot not found | Close Smart Life or turn off your phone's Bluetooth |
| `CharacteristicMissingError cba20003-...` | You're using SwitchBot code on a Tuya device. Use this project. |
| Tuya `28841001` | Subscribe the cloud project to IoT Core |
| Empty device list in the cloud | Wrong data centre |
| Fingerbot reports pressed, PC stays off | Raise push depth to 100 and check the mounting. Hold 3 s. |
| PC is on but shows "off" | Something on the PC blocks Tailscale, for example a VPN. Exclude `tailscaled` in its split tunnel. Don't use Wake in this state. |
| Tailscale shows the PC `Online` but ping fails | Trust ping |

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
PYTHONPATH=. pytest -q tests     # offline: fake fingerbot, fake ping
```

## Credits and licence

MIT, see [LICENSE](LICENSE). The Tuya BLE protocol code in [`tuya_ble/`](tuya_ble/) is copied unchanged from [PlusPlus-ua/ha_tuya_ble](https://github.com/PlusPlus-ua/ha_tuya_ble) (MIT).
