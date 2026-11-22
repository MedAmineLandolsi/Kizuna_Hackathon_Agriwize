"""Simulated ESP32 controller, to demo the full loop before the hardware is ready.

It behaves like the real firmware will: posts sensor readings, polls for
commands, "opens" a valve, counts delivered litres and reports back.

Usage:
    python tools/esp32_simulator.py --api http://localhost:8000 --key <DEVICE_API_KEY>
    python tools/esp32_simulator.py --blocked-zone 1     # simulate a blocked pipe on valve channel 1
"""
from __future__ import annotations

import argparse
import random
import time

import httpx

# Same two-point calibration as the demo zones (raw 3000 -> 0.05, raw 1300 -> 0.40)
RAW_DRY, THETA_DRY, RAW_WET, THETA_WET = 3000, 0.05, 1300, 0.40


def theta_to_raw(theta: float) -> int:
    slope = (RAW_WET - RAW_DRY) / (THETA_WET - THETA_DRY)
    return int(RAW_DRY + slope * (theta - THETA_DRY))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--key", default="")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between cycles")
    ap.add_argument("--blocked-zone", type=int, default=None, help="valve channel that only gets 40%% of the water")
    ap.add_argument("--area-m2", type=float, default=10.0)
    ap.add_argument("--root-depth-m", type=float, default=0.5)
    args = ap.parse_args()

    headers = {"X-Device-Key": args.key} if args.key else {}
    theta = {0: 0.16, 1: 0.15}          # starting soil moisture per valve channel
    tank = 90.0
    client = httpx.Client(base_url=args.api, headers=headers, timeout=10)
    print("Simulated ESP32 started. Ctrl+C to stop.")

    while True:
        # soil slowly dries (accelerated ET)
        for ch in theta:
            theta[ch] = max(theta[ch] - 0.002, 0.06)
        air = 24 + 8 * random.random()
        body = {"controller": {"air_temp_c": round(air, 1), "air_rh_pct": 45, "tank_level_pct": round(tank, 1),
                               "flow_lpm": 0.0, "valves_open": []},
                "zones": [{"valve_channel": ch, "soil_raw": theta_to_raw(t)} for ch, t in theta.items()]}
        try:
            client.post("/api/controller/readings", json=body).raise_for_status()
            cmd = client.get("/api/controller/next-command").json()
        except httpx.HTTPError as exc:
            print("backend unreachable:", exc, "- local safe mode: valves closed")
            time.sleep(args.interval)
            continue

        action = cmd.get("action")
        if action == "irrigate":
            ch, target = cmd["valve_channel"], cmd["target_volume_l"]
            print(f"[valve {ch}] OPEN  target {target:.1f} L")
            client.post(f"/api/controller/commands/{cmd['command_id']}/started")
            share = 0.4 if args.blocked_zone == ch else 1.0
            delivered = target * share * random.uniform(0.98, 1.02)
            stop_reason = "volume_reached" if share == 1.0 else "timeout"
            net_mm = delivered / args.area_m2 * 0.9
            theta[ch] = min(theta[ch] + net_mm / (1000 * args.root_depth_m), 0.40)
            tank = max(tank - delivered / 50, 0)
            client.post(f"/api/controller/commands/{cmd['command_id']}/report",
                        json={"delivered_l": round(delivered, 2), "stop_reason": stop_reason})
            print(f"[valve {ch}] CLOSED delivered {delivered:.1f} L ({stop_reason})")
        elif action in ("stop", "stop_all"):
            print("STOP received -> all valves closed")
            if action == "stop":
                client.post(f"/api/controller/commands/{cmd['command_id']}/report",
                            json={"delivered_l": 0, "stop_reason": "manual_stop"})
        print("theta:", {k: round(v, 3) for k, v in theta.items()}, "| tank", round(tank, 1), "% |", action)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
