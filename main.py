import asyncio
import logging

from dotenv import load_dotenv
from browser_use import Agent, Browser, BrowserConfig
from langchain_google_genai import ChatGoogleGenerativeAI

from config import get_gemini_api_key, verify_gemini_api_key

load_dotenv()
logging.getLogger("browser_use").setLevel(logging.WARNING)

ok, msg = verify_gemini_api_key()
if not ok:
    raise SystemExit(msg + "\nChay Setup Gemini Key.cmd de thiet lap 1 lan.")

API_KEY = get_gemini_api_key()

executor_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash-lite",
    google_api_key=API_KEY,
)

planner_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
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
