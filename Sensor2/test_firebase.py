import os
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv

load_dotenv()

firebase_credentials = os.getenv("FIREBASE_CREDENTIALS")
firebase_url = os.getenv("FIREBASE_DATABASE_URL")

print("Firebase URL:", firebase_url)
print("Credentials:", firebase_credentials)

if not firebase_credentials:
    raise RuntimeError("FIREBASE_CREDENTIALS is missing from .env")

if not os.path.isfile(os.path.expanduser(firebase_credentials)):
    raise RuntimeError(
        f"Firebase credential file not found: {firebase_credentials}"
    )

try:
    cred = credentials.Certificate(os.path.expanduser(firebase_credentials))

    firebase_admin.initialize_app(
        cred,
        {
            "databaseURL": firebase_url
        }
    )

    test_data = {
        "test": True,
        "message": "SCRUB Firebase connection test",
        "number": 123
    }

    db.reference("sensorData/test").set(test_data)

    print()
    print("===================================")
    print("FIREBASE TEST SUCCESS")
    print("===================================")
    print("Written to: sensorData/test")
    print(test_data)

except Exception as e:
    print()
    print("===================================")
    print("FIREBASE TEST FAILED")
    print("===================================")
    print(e)