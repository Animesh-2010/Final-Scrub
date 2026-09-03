import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")

print("Supabase URL:", supabase_url)
print("Supabase key loaded:", bool(supabase_key))

if not supabase_url:
    raise RuntimeError("SUPABASE_URL is missing from .env")

if not supabase_key:
    raise RuntimeError("SUPABASE_KEY is missing from .env")

try:
    supabase = create_client(
        supabase_url,
        supabase_key
    )

    test_data = {
        "seq": 999999,

        "lat": 12.971600,
        "lon": 77.594600,
        "alt": 900.0,
        "spd": 0.0,
        "course": 0.0,

        "sats_view": 5,
        "sats_used": 4,
        "fix": 1,

        "hdg": 180.0,

        "ph": 7.0,
        "tds": 250.0,
        "turb": 100.0,

        "mode": "TEST"
    }

    result = (
        supabase
        .table("sensorData")
        .insert(test_data)
        .execute()
    )

    print()
    print("===================================")
    print("SUPABASE TEST SUCCESS")
    print("===================================")
    print("Inserted test row:")
    print(result.data)

except Exception as e:
    print()
    print("===================================")
    print("SUPABASE TEST FAILED")
    print("===================================")
    print(e)