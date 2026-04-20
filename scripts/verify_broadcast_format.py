import asyncio
import httpx
import json
import sys

# Ensure UTF-8 output for Windows terminal
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

SSE_URL = "http://localhost:8080/api/v1/alerts/stream"
TRIGGER_URL = "http://localhost:8080/api/v1/test-alert"

async def verify_broadcast():
    print("Connecting to SSE...")
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", SSE_URL) as response:
            print("Connected. Triggering test alert...")
            
            # Trigger in a separate task
            async with httpx.AsyncClient() as trigger_client:
                await trigger_client.post(TRIGGER_URL)
            
            print("Waiting for event...")
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_str = line.replace("data: ", "")
                    data = json.loads(data_str)
                    print("Received Data:", json.dumps(data, indent=2))
                    
                    # VALIDATION
                    required_keys = [
                        "is_alert", "alert_id", "severity", "event_type", 
                        "alert_type", "camera_id", "location_display", 
                        "description", "plate_number", "snapshot_path", 
                        "triggered_at", "internal_meta"
                    ]
                    
                    missing = [k for k in required_keys if k not in data]
                    if missing:
                        print(f"FAILED: Missing keys: {missing}")
                        sys.exit(1)
                    
                    print("PASS: JSON Schema is correct and standardized.")
                    return

if __name__ == "__main__":
    try:
        asyncio.run(verify_broadcast())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
