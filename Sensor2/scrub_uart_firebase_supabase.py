#!/usr/bin/env python3
"""
SCRUB V4 - Serial JSON -> Firebase + Supabase

Reads JSON lines from /dev/serial0 and pushes the COMPLETE JSON object
without dropping fields.

Current serial format:
{"seq":83,"gps":{"lat":0.000000,"lon":0.000000,"alt":0.00,"spd":0.00,
"course":0.00,"sats_view":1,"sats_used":0,"fix":1},
"hdg":0.00,"sensors":{"ph":14.00,"tds":259.54,"turb":0.00},"mode":"AUTO"}

No Flask.
No GPIO configuration.

Install dependencies:
    pip install pyserial firebase-admin supabase python-dotenv
No hard-coded sensor CSV parser.

Firebase:
    sensorData/current

Supabase:
    Configure SUPABASE_URL, SUPABASE_KEY and SUPABASE_TABLE.

For Supabase, this script stores the incoming packet as a relational row:
seq, GPS fields, heading, sensor fields, mode, and received_at.
"""

import json
import logging
import os
import time

from dotenv import load_dotenv

import serial
import firebase_admin
from firebase_admin import credentials, db
from supabase import create_client


# ============================================================
# CONFIGURATION
# ============================================================

# Load credentials/configuration from .env in this directory.
load_dotenv()

SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 115200

# Firebase
# .env:
# FIREBASE_CREDENTIALS=/home/siddhant/scrub/latest/firebase/firebase-key.json
# FIREBASE_DATABASE_URL=https://your-project-default-rtdb.region.firebasedatabase.app/
#
FIREBASE_CREDENTIALS = os.path.expanduser(
    os.getenv(
        "FIREBASE_CREDENTIALS",
        "~/scrub/latest/firebase/firebase-key.json",
    )
)

FIREBASE_DATABASE_URL = os.getenv(
    "FIREBASE_DATABASE_URL",
    "https://scrub-v4-default-rtdb.asia-southeast1.firebasedatabase.app/",
)

FIREBASE_PATH = "sensorData/current"

# Supabase
# .env:
# SUPABASE_URL=https://your-project.supabase.co
# SUPABASE_KEY=your-key
#
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SUPABASE_TABLE = "sensorData"

# Local logging
LOG_FILE = "scrub_backend.log"
JSONL_FILE = "sensor_log.jsonl"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)

log = logging.getLogger("scrub")


# ============================================================
# FIREBASE
# ============================================================

firebase_ref = None


def init_firebase():
    global firebase_ref

    if not os.path.isfile(FIREBASE_CREDENTIALS):
        log.error(
            "[FIREBASE] Credential file not found: %s",
            FIREBASE_CREDENTIALS,
        )
        return

    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS)
            firebase_admin.initialize_app(
                cred,
                {"databaseURL": FIREBASE_DATABASE_URL},
            )

        firebase_ref = db.reference(FIREBASE_PATH)

        log.info("[FIREBASE] Connected -> %s", FIREBASE_PATH)

    except Exception as exc:
        log.exception(
            "[FIREBASE] Initialization failed: %s",
            exc,
        )


def push_to_firebase(data):
    """
    Push the COMPLETE JSON object to Firebase.

    Nothing from the incoming packet is discarded.
    """

    if firebase_ref is None:
        return

    try:
        firebase_ref.set(data)

        log.info(
            "[FIREBASE] Updated -> %s",
            FIREBASE_PATH,
        )

    except Exception as exc:
        log.error(
            "[FIREBASE] Update failed: %s",
            exc,
        )


# ============================================================
# SUPABASE
# ============================================================

supabase = None


def init_supabase():
    global supabase

    if not SUPABASE_URL or not SUPABASE_KEY:
        log.warning(
            "[SUPABASE] SUPABASE_URL / SUPABASE_KEY missing. "
            "Supabase disabled."
        )
        return

    try:
        supabase = create_client(
            SUPABASE_URL,
            SUPABASE_KEY,
        )

        log.info(
            "[SUPABASE] Connected -> table '%s'",
            SUPABASE_TABLE,
        )

    except Exception as exc:
        log.exception(
            "[SUPABASE] Initialization failed: %s",
            exc,
        )


def push_to_supabase(data):
    """Insert the packet into Supabase as one relational table row."""
    if supabase is None:
        return

    gps = data.get("gps") or {}
    sensors = data.get("sensors") or {}

    payload = {
        "seq": data.get("seq"),

        "lat": gps.get("lat"),
        "lon": gps.get("lon"),
        "alt": gps.get("alt"),
        "spd": gps.get("spd"),
        "course": gps.get("course"),

        "sats_view": gps.get("sats_view"),
        "sats_used": gps.get("sats_used"),
        "fix": gps.get("fix"),

        "hdg": data.get("hdg"),

        "ph": sensors.get("ph"),
        "tds": sensors.get("tds"),
        "turb": sensors.get("turb"),

        "mode": data.get("mode"),
        "received_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        ),
    }

    try:
        supabase.table(SUPABASE_TABLE).insert(payload).execute()
        log.info("[SUPABASE] Inserted seq=%s", data.get("seq"))
    except Exception as exc:
        log.error("[SUPABASE] Insert failed: %s", exc)


# ============================================================
# LOCAL LOGGING
# ============================================================

def save_local(data):
    try:
        with open(JSONL_FILE, "a") as f:
            f.write(json.dumps(data, separators=(",", ":")) + "\n")

    except OSError as exc:
        log.error(
            "[LOCAL] Failed to save packet: %s",
            exc,
        )


# ============================================================
# TERMINAL
# ============================================================

def print_update(data):
    print()
    print("=" * 70)
    print("SCRUB SERIAL UPDATE")
    print("=" * 70)

    print(json.dumps(data, indent=2))

    print("=" * 70)
    print(
        "seq=%s | mode=%s | lat=%s | lon=%s | hdg=%s | "
        "pH=%s | TDS=%s | turb=%s"
        % (
            data.get("seq", "-"),
            data.get("mode", "-"),
            data.get("gps", {}).get("lat", "-"),
            data.get("gps", {}).get("lon", "-"),
            data.get("hdg", "-"),
            data.get("sensors", {}).get("ph", "-"),
            data.get("sensors", {}).get("tds", "-"),
            data.get("sensors", {}).get("turb", "-"),
        )
    )
    print("=" * 70)
    print()


# ============================================================
# SERIAL
# ============================================================

def open_serial():
    while True:
        try:
            ser = serial.Serial(
                port=SERIAL_PORT,
                baudrate=BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=3,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )

            log.info(
                "[SERIAL] Connected -> %s @ %d",
                SERIAL_PORT,
                BAUD_RATE,
            )

            return ser

        except Exception as exc:
            log.error(
                "[SERIAL] Could not open %s: %s",
                SERIAL_PORT,
                exc,
            )

            time.sleep(3)


# ============================================================
# MAIN
# ============================================================

def main():
    print()
    print("=" * 70)
    print("SCRUB V4 - SERIAL JSON -> FIREBASE + SUPABASE")
    print("=" * 70)
    print(f"Serial    : {SERIAL_PORT}")
    print(f"Baud      : {BAUD_RATE}")
    print(f"Firebase  : {FIREBASE_PATH}")
    print(f"Supabase  : {SUPABASE_TABLE}")
    print("=" * 70)
    print()

    init_firebase()
    init_supabase()

    while True:
        ser = open_serial()

        try:
            while ser.is_open:
                raw_bytes = ser.readline()

                if not raw_bytes:
                    continue

                raw = raw_bytes.decode(
                    "utf-8",
                    errors="ignore",
                ).strip()

                if not raw:
                    continue

                # The Arduino sends one JSON object per line.
                try:
                    data = json.loads(raw)

                except json.JSONDecodeError:
                    log.warning(
                        "[SERIAL] Ignoring non-JSON line: %r",
                        raw,
                    )
                    continue

                if not isinstance(data, dict):
                    log.warning(
                        "[SERIAL] JSON is not an object: %r",
                        raw,
                    )
                    continue

                # 1. Print EVERYTHING.
                print_update(data)

                # 2. Save EVERYTHING locally.
                save_local(data)

                # 3. Push EVERYTHING to Firebase.
                push_to_firebase(data)

                # 4. Push EVERYTHING to Supabase.
                push_to_supabase(data)

        except KeyboardInterrupt:
            print("\nStopping SCRUB backend...")
            try:
                ser.close()
            except Exception:
                pass
            break

        except serial.SerialException as exc:
            log.error(
                "[SERIAL] Connection lost: %s",
                exc,
            )

        except Exception as exc:
            log.exception(
                "[MAIN] Unexpected error: %s",
                exc,
            )

        finally:
            try:
                ser.close()
            except Exception:
                pass

            log.warning(
                "[SERIAL] Disconnected. Reconnecting in 3 seconds..."
            )

            time.sleep(3)


if __name__ == "__main__":
    main()
