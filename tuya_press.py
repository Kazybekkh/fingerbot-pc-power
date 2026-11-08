"""Connect to the Tuya fingerbot over BLE and optionally press it once.

This tool has no safety checks. Make sure the PC is off before --press.
"""
import argparse
import asyncio
import logging
import os
import sys

from bleak import BleakScanner

from tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice
from tuya_ble.manager import AbstaractTuyaBLEDeviceManager, TuyaBLEDeviceCredentials

SWITCH_DP = 1  # switch_1; in click mode any change makes one press
SUSTAIN_DP = 103  # sustain_time, seconds the finger stays down (0-10)
DOWN_DP = 102  # down_percent, how far the finger pushes (0-100)


def load_env(path):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


class EnvManager(AbstaractTuyaBLEDeviceManager):
    def __init__(self, env):
        self.creds = TuyaBLEDeviceCredentials(
            uuid=env["TUYA_UUID"],
            local_key=env["TUYA_LOCAL_KEY"],
            device_id=env["TUYA_DEVICE_ID"],
            category=env["TUYA_CATEGORY"],
            product_id=env["TUYA_PRODUCT_ID"],
            device_name=None,
            product_model=None,
            product_name=None,
        )

    async def get_device_credentials(self, address, force_update=False, save_data=False):
        return self.creds


async def find(mac, timeout):
    found = asyncio.get_running_loop().create_future()

    def cb(device, adv):
        if device.address.upper() == mac.upper() and not found.done():
            found.set_result((device, adv))

    async with BleakScanner(cb):
        return await asyncio.wait_for(found, timeout)


async def run(mac, env, press, hold, down):
    ble_device, adv = await find(mac, 20.0)
    dev = TuyaBLEDevice(EnvManager(env), ble_device, adv)
    await dev.initialize()
    try:
        await dev.update()
        await asyncio.sleep(3)
        print("paired", dev._is_paired, "dps", {dp.id: dp.value for dp in dev.datapoints._datapoints.values()})
        if hold is not None:
            dp = dev.datapoints.get_or_create(SUSTAIN_DP, TuyaBLEDataPointType.DT_VALUE, 0)
            await dp.set_value(hold)
            await asyncio.sleep(2)
            print("hold set", dev.datapoints[SUSTAIN_DP].value)
        if down is not None:
            dp = dev.datapoints.get_or_create(DOWN_DP, TuyaBLEDataPointType.DT_VALUE, 0)
            await dp.set_value(down)
            await asyncio.sleep(2)
            print("down set", dev.datapoints[DOWN_DP].value)
        if press:
            dp = dev.datapoints.get_or_create(SWITCH_DP, TuyaBLEDataPointType.DT_BOOL, False)
            await dp.set_value(not bool(dp.value))
            await asyncio.sleep(3)
            print("pressed")
    finally:
        await dev.stop()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--press", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--hold", type=int, choices=range(0, 11), help="seconds to hold the press down")
    p.add_argument("--down", type=int, choices=range(0, 101), metavar="0-100", help="how far the finger pushes, percent")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.WARNING)
    here = os.path.dirname(os.path.abspath(__file__))
    env = load_env(os.path.join(here, "tuya.env"))
    cfg = load_env(os.path.join(here, "pc-power.env"))
    mac = cfg.get("FINGERBOT_MAC") or cfg["SWITCHBOT_MAC"]  # SWITCHBOT_MAC is the old name
    asyncio.run(run(mac, env, a.press, a.hold, a.down))


if __name__ == "__main__":
    sys.exit(main())
