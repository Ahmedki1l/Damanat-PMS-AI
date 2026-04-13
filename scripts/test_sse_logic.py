import asyncio
import httpx
from app.utils.event_bus import event_bus

async def test_sse_connection():
    # URL of the SSE endpoint (assuming the app is running locally on port 8000)
    # Since we are testing code directly, we can also test the EventBus itself first.
    print("Testing EventBus directly...")
    
    received_items = []
    
    async def subscriber_task():
        async for item in event_bus.subscribe():
            print(f"Subscriber received: {item}")
            received_items.append(item)
            if len(received_items) >= 1:
                break

    # Start subscriber in background
    sub = asyncio.create_task(subscriber_task())
    
    # Wait a bit for subscriber to start
    await asyncio.sleep(0.1)
    
    # Publish an event
    test_event = {"message": "Test Alert", "id": 123}
    event_bus.publish(test_event)
    print(f"Published: {test_event}")
    
    # Wait for subscriber to finish
    await asyncio.wait_for(sub, timeout=2.0)
    
    if received_items[0] == test_event:
        print("✅ EventBus direct test passed!")
    else:
        print("❌ EventBus direct test failed!")

if __name__ == "__main__":
    asyncio.run(test_sse_connection())

