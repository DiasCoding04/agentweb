import asyncio
import logging

from dotenv import load_dotenv
from browser_use import Agent, Browser, BrowserConfig

from config import get_gemini_api_key, verify_gemini_api_key, use_vertex, get_vertex_project, get_vertex_location, get_vertex_credentials

load_dotenv()
logging.getLogger("browser_use").setLevel(logging.WARNING)

EXECUTOR_MODEL = "gemini-3.1-flash-lite"
PLANNER_MODEL  = "gemini-3.5-flash"


def make_llm_vertex(
    model: str,
    temperature: float = 0,
):
    try:
        from langchain_google_vertexai import ChatVertexAI
        from google.oauth2 import service_account
    except ImportError as exc:
        raise SystemExit(
            "Thieu langchain-google-vertexai cho Vertex AI. "
            "Chay: pip install langchain-google-vertexai google-cloud-aiplatform"
        ) from exc

    creds_path = get_vertex_credentials()
    if creds_path:
        credentials = service_account.Credentials.from_service_account_file(creds_path)
    else:
        credentials = None

    location = get_vertex_location()
    kwargs = {}
    if location == "global":
        kwargs["api_endpoint"] = "aiplatform.googleapis.com"

    return ChatVertexAI(
        model=model,
        project=get_vertex_project(),
        location=location,
        credentials=credentials,
        temperature=temperature,
        **kwargs
    )


if use_vertex():
    print(f"[Vertex AI] Project={get_vertex_project()}, Location={get_vertex_location()}")
    executor_llm = make_llm_vertex(EXECUTOR_MODEL)
    planner_llm  = make_llm_vertex(PLANNER_MODEL)
else:
    ok, msg = verify_gemini_api_key()
    if not ok:
        raise SystemExit(msg + "\nChay Setup Gemini Key.cmd de thiet lap 1 lan.")
    API_KEY = get_gemini_api_key()

    from langchain_google_genai import ChatGoogleGenerativeAI
    executor_llm = ChatGoogleGenerativeAI(
        model=EXECUTOR_MODEL,
        google_api_key=API_KEY,
    )
    planner_llm = ChatGoogleGenerativeAI(
        model=PLANNER_MODEL,
        google_api_key=API_KEY,
    )


async def chat():
    print("AI Browser Agent san sang. Go 'quit' de thoat.\n")
    print("Lenh ngan  -> che do nhanh")
    print("Lenh dai   -> che do planner (AI tu dong lap ke hoach)\n")

    browser = Browser(
        config=BrowserConfig(headless=False, keep_alive=True)
    )

    try:
        while True:
            task = input("Ban: ").strip()

            if task.lower() in ("quit", "exit", "thoat"):
                print("Tam biet!")
                break

            if not task:
                continue

            print("\nDang thuc hien...\n")

            use_planner = len(task) > 100

            if use_planner:
                print("[Che do Planner: AI se tu dong lap ke hoach]\n")

            try:
                agent = Agent(
                    task=task,
                    llm=executor_llm,
                    planner_llm=planner_llm if use_planner else None,
                    planner_interval=4,
                    max_actions_per_step=10,
                    browser=browser,
                )

                result = await agent.run(max_steps=100)
                final = result.final_result()
                print(f"\nXong: {final or 'Hoan thanh'}\n")

            except KeyboardInterrupt:
                print("\n[Ctrl+C] Da dung task hien tai.\n")
            except Exception as e:
                print(f"\nLoi: {e}\n")

            print("-" * 50)

    finally:
        await browser.close()


asyncio.run(chat())
