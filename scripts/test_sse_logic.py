import asyncio
import httpx
import json

SSE_URL = "http://localhost:8080/api/v1/alerts/stream"
TRIGGER_URL = "http://localhost:8080/api/v1/test-alert"


async def sse_listener(received_events):
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", SSE_URL) as response:
            print("📡 Connected to SSE")

            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data = line.replace("data: ", "")
                    print(f"📥 SSE Received: {data}")

                    try:
                        parsed = json.loads(data)
                        received_events.append(parsed)
                        break
                    except Exception as e:
                        print("⚠️ Failed to parse JSON:", e)


async def publisher():
    # ندي وقت للـ SSE إنه يفتح
    await asyncio.sleep(1)

    async with httpx.AsyncClient() as client:
        response = await client.post(TRIGGER_URL)
        print(f"📤 Triggered backend: {response.json()}")


async def run_test():
    received_events = []

    listener_task = asyncio.create_task(sse_listener(received_events))
    publisher_task = asyncio.create_task(publisher())

    await asyncio.wait(
        [listener_task, publisher_task],
        timeout=5
    )

    # ✅ Validation
    if not received_events:
        print("❌ TEST FAILED: No event received")
        return

    received = received_events[0]

    if received.get("alert_id") == 999:
        print("✅ TEST PASSED: SSE working correctly")
    else:
        print("❌ TEST FAILED: Data mismatch")


if __name__ == "__main__":
    asyncio.run(run_test())