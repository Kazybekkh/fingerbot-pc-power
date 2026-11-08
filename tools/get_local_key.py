"""Fetch the fingerbot's BLE credentials from the Tuya cloud, once.

Usage:
    python tools/get_local_key.py --region eu --access-id ID --access-secret SECRET --device-id DEVICE_ID

Prints the lines to paste into tuya.env. The output contains the local key, so
do not paste it anywhere public. Re-run it if you re-pair the fingerbot in the
Smart Life app, because that changes the key.
"""
import argparse
import sys

import tinytuya


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--region", required=True, help="Tuya data centre: eu, us, cn, in, ... (must match your app account)")
    p.add_argument("--access-id", required=True)
    p.add_argument("--access-secret", required=True)
    p.add_argument("--device-id", required=True)
    a = p.parse_args()

    cloud = tinytuya.Cloud(apiRegion=a.region, apiKey=a.access_id, apiSecret=a.access_secret)
    reply = cloud.cloudrequest(f"/v1.0/devices/{a.device_id}")
    if not isinstance(reply, dict) or not reply.get("success"):
        print(f"Tuya cloud error: {reply}", file=sys.stderr)
        print("Check the region, and that IoT Core is subscribed on the project's Service API tab.", file=sys.stderr)
        return 1

    dev = reply["result"]
    missing = [k for k in ("local_key", "uuid", "category", "product_id") if not dev.get(k)]
    if missing:
        print(f"Device record has no {', '.join(missing)}.", file=sys.stderr)
        return 1

    print(f"TUYA_DEVICE_ID={a.device_id}")
    print(f"TUYA_UUID={dev['uuid']}")
    print(f"TUYA_LOCAL_KEY={dev['local_key']}")
    print(f"TUYA_CATEGORY={dev['category']}")
    print(f"TUYA_PRODUCT_ID={dev['product_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
