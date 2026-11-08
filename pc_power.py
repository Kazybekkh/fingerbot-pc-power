"""Press the Tuya fingerbot on the PC power button unless the PC is reachable.

The fingerbot is in click mode. A press toggles data point 1 (switch_1), and
data point 103 (sustain_time) sets how long the finger stays down. The PC needs
about 3 seconds. Credentials come from tuya.env next to this file.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

LOG = logging.getLogger("pc_power")

PRESS_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2.0
PING_COUNT = 2
PING_WAIT_SECONDS = 2
# Tailscale on the PC comes up after the OS boots. A second press in that
# After a successful press, refuse another one for this long. A press in the boot
# window would hit the power button again and can shut the machine down.
POST_PRESS_COOLDOWN_SECONDS = 180

_press_lock = asyncio.Lock()
_last_success_monotonic: float | None = None


class PcAlreadyOn(Exception):
    def __init__(self, host: str) -> None:
        super().__init__(host)
        self.host = host


class PressInProgress(Exception):
    pass


class PressCoolingDown(Exception):
    def __init__(self, seconds_left: int) -> None:
        super().__init__(str(seconds_left))
        self.seconds_left = seconds_left


class NotConfigured(Exception):
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def pc_host() -> str:
    host = _env("PC_HOST") or _env("PC_TAILSCALE_IP")
    if not host:
        raise NotConfigured("PC_HOST is not set")
    return host


HERE = os.path.dirname(os.path.abspath(__file__))


def fingerbot_mac() -> str:
    # SWITCHBOT_MAC is the old name of this setting and is still accepted.
    mac = (_env("FINGERBOT_MAC") or _env("SWITCHBOT_MAC")).upper()
    if not mac:
        raise NotConfigured("FINGERBOT_MAC is not set")
    return mac


def press_log_path() -> str:
    return _env("PRESS_LOG", os.path.join(HERE, "press.log"))


def log_event(message: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    line = f"{stamp} {message}"
    LOG.info("%s", line)
    path = press_log_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def pc_is_up(host: str | None = None) -> bool:
    """Return True when the host answers ping within a short timeout."""
    host = host or pc_host()
    try:
        completed = subprocess.run(
            ["ping", "-c", str(PING_COUNT), "-W", str(PING_WAIT_SECONDS), "-n", host],
            capture_output=True,
            timeout=PING_COUNT * PING_WAIT_SECONDS + 3,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return completed.returncode == 0


def cooldown_remaining() -> int:
    if _last_success_monotonic is None:
        return 0
    left = POST_PRESS_COOLDOWN_SECONDS - (time.monotonic() - _last_success_monotonic)
    return max(0, int(left))


def _mark_success() -> None:
    global _last_success_monotonic
    _last_success_monotonic = time.monotonic()


class PressSentError(Exception):
    """The press write went out, then something failed. Never retry this."""


def press_hold_seconds() -> int:
    return int(_env("TUYA_HOLD_SECONDS", "3"))


def press_down_percent() -> int:
    # 85% did not reach the button on 22 Oct 2026; 100% booted the PC on 23 Oct.
    return int(_env("TUYA_DOWN_PERCENT", "100"))


# Holding an ATX power button for 4 s or more forces the PC off.
FORCE_OFF_HOLD_SECONDS = 10
# Pause between the force-off hold and the boot press during a restart.
RESTART_GAP_SECONDS = 15


async def _press_once(
    mac: str,
    hold: int | None = None,
    down: int | None = None,
) -> bool:
    from bleak import BleakError

    from tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice
    from tuya_press import DOWN_DP, SUSTAIN_DP, SWITCH_DP, EnvManager, find, load_env

    env = load_env(_env("TUYA_ENV", os.path.join(HERE, "tuya.env")))
    hold = press_hold_seconds() if hold is None else hold
    down = press_down_percent() if down is None else down
    try:
        ble_device, adv = await find(mac, 15.0)
    except TimeoutError as exc:
        raise BleakError(f"Fingerbot {mac} was not found") from exc
    dev = TuyaBLEDevice(EnvManager(env), ble_device, adv)
    sent = False
    try:
        await dev.initialize()
        await dev.update()
        await asyncio.sleep(2)
        if not dev._is_paired:
            raise BleakError("Fingerbot did not pair")
        sustain = dev.datapoints[SUSTAIN_DP]
        if sustain is None or sustain.value != hold:
            dp = dev.datapoints.get_or_create(SUSTAIN_DP, TuyaBLEDataPointType.DT_VALUE, 0)
            await dp.set_value(hold)
            await asyncio.sleep(1)
        depth = dev.datapoints[DOWN_DP]
        if depth is None or depth.value != down:
            dp = dev.datapoints.get_or_create(DOWN_DP, TuyaBLEDataPointType.DT_VALUE, 0)
            await dp.set_value(down)
            await asyncio.sleep(1)
        dp = dev.datapoints.get_or_create(SWITCH_DP, TuyaBLEDataPointType.DT_BOOL, False)
        sent = True
        await dp.set_value(not bool(dp.value))
        # Stay connected until the finger has gone down and come back up.
        await asyncio.sleep(hold + 2)
        return True
    except Exception as exc:
        if sent:
            raise PressSentError(str(exc)) from exc
        raise
    finally:
        try:
            await dev.stop()
        except Exception:
            LOG.warning("fingerbot disconnect failed", exc_info=True)


async def press_with_retries(force: bool) -> dict:
    """Press the fingerbot, unless the PC is reachable or a press just happened."""
    if _press_lock.locked():
        log_event(f"force={str(force).lower()} result=busy")
        raise PressInProgress()

    async with _press_lock:
        host = pc_host()
        up = await asyncio.to_thread(pc_is_up, host)
        if up and not force:
            log_event(
                f"host={host} force=false pc_up=true result=refused reason=pc_reachable"
            )
            raise PcAlreadyOn(host)

        remaining = cooldown_remaining()
        if remaining and not force:
            log_event(
                f"host={host} force=false pc_up={str(up).lower()} "
                f"result=refused reason=cooldown seconds_left={remaining}"
            )
            raise PressCoolingDown(remaining)

        try:
            mac = fingerbot_mac()
        except NotConfigured:
            log_event(
                f"host={host} force={str(force).lower()} pc_up={str(up).lower()} "
                "result=not_configured reason=missing_mac"
            )
            raise
        pressed, attempts, last_error = await _press_attempts(
            mac, host, force, up, hold=None, down=None, label="wake"
        )
        result = {
            "pressed": pressed,
            "pc_up": up,
            "force": force,
            "host": host,
            "mac": mac,
            "attempts": attempts,
        }
        if not pressed:
            result["error"] = last_error
        return result


async def _press_attempts(
    mac: str,
    host: str,
    force: bool,
    up: bool,
    hold: int | None,
    down: int | None,
    label: str,
) -> tuple[bool, list[dict], str]:
    """Press once, retrying only BLE failures that happen before the write."""
    hold_value = press_hold_seconds() if hold is None else hold
    down_value = press_down_percent() if down is None else down
    attempts: list[dict] = []
    last_error = "Fingerbot press failed"
    for attempt in range(1, PRESS_ATTEMPTS + 1):
        try:
            ok = await _press_once(mac, hold=hold_value, down=down_value)
        except Exception as exc:
            from bleak import BleakError

            last_error = str(exc)
            connection_failure = isinstance(exc, (BleakError, TimeoutError, OSError))
            result_name = "connection_error" if connection_failure else "error"
            log_event(
                f"attempt={attempt} mac={mac} host={host} force={str(force).lower()} "
                f"pc_up={str(up).lower()} result={result_name} hold={hold_value} "
                f"down={down_value} via={label} error={exc}"
            )
            attempts.append({"attempt": attempt, "result": result_name, "error": str(exc)})
            # A non-BLE error may mean the write already went out.
            if not connection_failure or attempt == PRESS_ATTEMPTS:
                break
            await asyncio.sleep(RETRY_DELAY_SECONDS * attempt)
            continue

        if ok:
            _mark_success()
            log_event(
                f"attempt={attempt} mac={mac} host={host} force={str(force).lower()} "
                f"pc_up={str(up).lower()} result=pressed hold={hold_value} "
                f"down={down_value} via={label}"
            )
            attempts.append({"attempt": attempt, "result": "pressed"})
            return True, attempts, ""

        last_error = "Fingerbot rejected the press command."
        log_event(
            f"attempt={attempt} mac={mac} host={host} force={str(force).lower()} "
            f"pc_up={str(up).lower()} result=rejected via={label}"
        )
        attempts.append({"attempt": attempt, "result": "rejected"})
        break
    return False, attempts, last_error


async def restart_sequence() -> dict:
    """Hold the button to force the PC off, wait, then press it to boot.

    This cuts power to a running PC and loses unsaved work. If the PC was
    already off, the long hold starts it and then cuts power again, so it
    ends up off either way before the boot press.
    """
    if _press_lock.locked():
        log_event("force=true result=busy via=restart")
        raise PressInProgress()

    async with _press_lock:
        host = pc_host()
        up = await asyncio.to_thread(pc_is_up, host)
        mac = fingerbot_mac()
        off_ok, off_attempts, off_error = await _press_attempts(
            mac, host, True, up,
            hold=FORCE_OFF_HOLD_SECONDS, down=None, label="restart_force_off",
        )
        result: dict = {
            "pressed": False,
            "pc_up": up,
            "host": host,
            "force_off": {"pressed": off_ok, "attempts": off_attempts},
        }
        if not off_ok:
            result["error"] = f"force-off press failed: {off_error}"
            return result

        await asyncio.sleep(RESTART_GAP_SECONDS)
        boot_ok, boot_attempts, boot_error = await _press_attempts(
            mac, host, False, False,
            hold=None, down=None, label="restart_boot",
        )
        result["boot"] = {"pressed": boot_ok, "attempts": boot_attempts}
        result["pressed"] = boot_ok
        if not boot_ok:
            result["error"] = f"boot press failed: {boot_error}"
        return result


def _print_status() -> int:
    host = pc_host()
    up = pc_is_up(host)
    print(f"host={host} pc_up={str(up).lower()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Press the fingerbot on the PC power button.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Press even if the PC answers ping. This can shut a running PC down.",
    )
    parser.add_argument("--status", action="store_true", help="Only report whether the PC answers ping.")
    args = parser.parse_args(argv)

    if args.status:
        return _print_status()

    try:
        result = asyncio.run(press_with_retries(force=args.force))
    except PcAlreadyOn as exc:
        print(f"refused: {exc.host} is reachable. Re-run with --force to press anyway.", file=sys.stderr)
        return 2
    except PressCoolingDown as exc:
        print(f"refused: a press succeeded {exc.seconds_left}s ago.", file=sys.stderr)
        return 2
    except PressInProgress:
        print("refused: a press is already running.", file=sys.stderr)
        return 2
    except NotConfigured as exc:
        print(f"not configured: {exc}", file=sys.stderr)
        return 1

    if not result.get("pressed"):
        print(result.get("error") or "press failed", file=sys.stderr)
        return 1
    print("pressed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
