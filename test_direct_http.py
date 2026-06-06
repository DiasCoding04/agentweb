import json
import urllib.request
from google.oauth2 import service_account
import google.auth.transport.requests

# Load service account credentials
creds = service_account.Credentials.from_service_account_file(
    "service_account.json",
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)

# Refresh credentials to get access token
auth_request = google.auth.transport.requests.Request()
creds.refresh(auth_request)
token = creds.token
print("Successfully generated access token.")

# Define URL and payload
project = "gen-lang-client-0335766885"
url = f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/models/gemini-3.1-flash-lite:generateContent"
payload = {
    "contents": [
        {
            "role": "user",
            "parts": [
                {"text": "Hello, answer with 'OK' if you can read this."}
            ]
        }
    ]
}

data = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(
    url,
    data=data,
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    },
    method="POST"
)

try:
    print(f"Sending POST request to {url}...")
    with urllib.request.urlopen(req) as response:
        res_data = response.read().decode("utf-8")
        print("Success! Response:")
        print(res_data)
except Exception as e:
    print("HTTP Request failed:")
    import traceback
    traceback.print_exc()
    if hasattr(e, "read"):
        print("Error response content:")
        print(e.read().decode("utf-8"))
