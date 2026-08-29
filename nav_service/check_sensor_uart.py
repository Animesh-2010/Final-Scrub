#!/usr/bin/env python3
"""
check_sensor_uart.py — Standalone Pi-side test to verify the sensor Arduino's
JSON stream is reaching the Raspberry Pi.

The Arduino (sensor_gps_compass_mq.ino) outputs newline JSON @ 115200.
This script opens a serial port (USB by default, auto-detected; falls back to
the GPIO UART /dev/ttyAMA0), reads the JSON, and prints/live-validates it.

The user must be in the `dialout` group (or run with sudo) to access serial
ports:

    sudo usermod -a -G dialout $USER   # then reboot / re-login

Usage:
    python3 check_sensor_uart.py                       # auto-detect serial port
    python3 check_sensor_uart.py /dev/ttyUSB0          # pick a specific port
    python3 check_sensor_uart.py /dev/ttyACM0
    python3 check_sensor_uart.py /dev/ttyAMA0          # GPIO UART
"""

import glob
import json
import sys

import serial


def find_serial_port() -> str:
    """Return the first available USB serial port, else ttyAMA0."""
    for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]
    return "/dev/ttyAMA0"


def main() -> None:
    device = sys.argv[1] if len(sys.argv) > 1 else find_serial_port()
    baud = 115200  # matches Arduino Serial.begin(115200)

    print(f"Opening {device} @ {baud} ...")
    try:
        ser = serial.Serial(device, baud, timeout=2)
    except Exception as exc:
        print(f"ERROR: could not open {device}: {exc}")
        print("Hint: run `sudo usermod -a -G dialout $USER` then reboot,")
        print("      or re-run with: sudo python3 check_sensor_uart.py")
        sys.exit(1)

    print(f"OK opened. Listening for sensor JSON (Ctrl+C to stop)...\n")
    try:
        while True:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("ascii", errors="ignore").strip()
            if not line.startswith("{"):
                # Skip Arduino banner / non-JSON debug lines
                print(f"  [info] {line}")
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                print(f"  [PARSE ERR] {line}")
                continue

            gps = obj.get("gps", {})
            sensors = obj.get("sensors", {})
            print(
                f"[seq {obj.get('seq', '?'):>4}] "
                f"fix={gps.get('fix', 0)} sats={gps.get('sats', 0)} "
                f"lat={gps.get('lat', 0):.6f} lon={gps.get('lon', 0):.6f} "
                f"hdg={obj.get('hdg', 0):.1f} | "
                f"pH={sensors.get('ph', 0):.2f} TDS={sensors.get('tds', 0):.0f}ppm "
                f"NTU={sensors.get('turb', 0):.1f} MQ={sensors.get('mq', 0):.0f} "
                f"T={sensors.get('atemp', 0):.1f}C RH={sensors.get('hum', 0):.1f}%"
            )
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
