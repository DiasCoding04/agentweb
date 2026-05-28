import asyncio
import os
import logging
from dotenv import load_dotenv
from browser_use import Agent, Browser, BrowserConfig
from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv()
logging.getLogger("browser_use").setLevel(logging.WARNING)

# Model thực thi — nhanh, rẻ
executor_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash-lite",
    google_api_key=os.getenv("AIzaSyDd1n7gS2iVz7SajY5wZ3WhNHhbDJgLnhA"),
)

# Model lập kế hoạch — mạnh hơn, chạy ít lần hơn
planner_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    google_api_key=os.getenv("AIzaSyDd1n7gS2iVz7SajY5wZ3WhNHhbDJgLnhA"),
)

async def chat():
    print("AI Browser Agent san sang. Go 'quit' de thoat.\n")
    print("Lenh ngan  → che do nhanh")
    print("Lenh dai   → che do planner (AI tu dong lap ke hoach)\n")

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

            # Task dài → bật planner
            use_planner = len(task) > 100

            if use_planner:
                print("[Che do Planner: AI se tu dong lap ke hoach]\n")

            try:
                agent = Agent(
                    task=task,
                    llm=executor_llm,
                    planner_llm=planner_llm if use_planner else None,
                    planner_interval=4,        # Planner xem xét lại mỗi 4 bước
                    max_actions_per_step=10,
                    browser=browser,
                )

                result = await agent.run(max_steps=100)  # 100 bước ~ vài chục phút
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