import os
from pathlib import Path
from dotenv import load_dotenv
from langchain_google_vertexai import ChatVertexAI
from google.oauth2 import service_account

load_dotenv()

project = "gen-lang-client-0335766885"
location = "global"
creds_path = Path("service_account.json")

print(f"Project: {project}")
print(f"Location: {location}")
print(f"Credentials path exists: {creds_path.exists()}")

if creds_path.exists():
    credentials = service_account.Credentials.from_service_account_file(str(creds_path))
    print("Loaded service account credentials successfully.")
else:
    credentials = None
    print("Warning: service_account.json not found.")

try:
    print("Initializing ChatVertexAI with location='global' and api_endpoint='aiplatform.googleapis.com'...")
    llm = ChatVertexAI(
        model="gemini-3.1-flash-lite",
        project=project,
        location=location,
        credentials=credentials,
        api_endpoint="aiplatform.googleapis.com",
        temperature=0,
    )
    print("Invoking ChatVertexAI...")
    res = llm.invoke("Hello, answer with 'OK' if you can read this.")
    print("Success! Response:")
    print(res.content)
except Exception as e:
    print("Failed to call Vertex AI:")
    import traceback
    traceback.print_exc()
