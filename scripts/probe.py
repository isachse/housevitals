"""Quick connectivity check: read the overview values once and print them.

    python scripts/probe.py 192.168.1.50 --profile iwr [--port 502] [--unit-id 1]
"""

import argparse
import asyncio

from housevitals.modbus import ModbusClient, ModbusReadError
from housevitals.registry import PROFILE_NAMES, load_profile


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("host")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--unit-id", type=int, default=1)
    p.add_argument("--profile", choices=PROFILE_NAMES, default="iwr")
    p.add_argument("--zones", default="1")
    args = p.parse_args()

    zones = [int(z) for z in args.zones.split(",")]
    profile = load_profile(args.profile, zones)
    client = ModbusClient(args.host, args.port, args.unit_id)
    regs = profile.find(summary_only=True)
    try:
        values = await client.read(regs)
    except ModbusReadError as err:
        raise SystemExit(f"ERROR: {err}")
    finally:
        await client.close()

    ok = 0
    for reg in regs:
        v = values[reg.key]
        if "error" in v:
            shown = f"<error: {v['error']}>"
        else:
            ok += 1
            shown = f"{v['value']} {v.get('unit', '')}".strip()
        print(f"{reg.address:>6} {reg.key:<40} {shown}")
    print(f"\n{ok}/{len(regs)} registers read successfully ({profile.description}).")


if __name__ == "__main__":
    asyncio.run(main())
