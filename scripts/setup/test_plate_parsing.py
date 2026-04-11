import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.services.event_parser import _parse_json_event, _normalize_plate

print("=== Plate normalization ===")
for raw in ['9444HUD', '7651XHD', '7800ERD', 'HUD-9444', 'ABC1234', '1234ABC']:
    print(f"  {raw} -> {_normalize_plate(raw)}")

print()
print("=== vehicleMatchResult JSON parsing ===")

p1 = b'{"ipAddress":"10.1.13.100","dateTime":"2026-03-11T09:19:01+03:00","eventType":"vehicleMatchResult","channelName":"ANPR-1 Entry","VehicleMatchResult":{"listType":"temporary","PlateInfo":{"plate":"7800ERD","plateColor":"white"}}}'
p2 = b'{"ipAddress":"10.1.13.100","dateTime":"2026-03-11T08:58:56+03:00","eventType":"vehicleMatchResult","VehicleMatchResult":{"listType":"whitelist","PlateInfo":{"plate":"9444HUD","plateType":"92TypeArm","plateColor":"white"}}}'
p3 = b'{"ipAddress":"10.1.13.100","dateTime":"2026-03-11T09:07:38+03:00","eventType":"vehicleMatchResult","VehicleMatchResult":{"listType":"whitelist","PlateInfo":{"plate":"7651XHD","plateColor":"white"}}}'

for p in [p1, p2, p3]:
    ev = _parse_json_event(p, '10.1.13.100')
    print(f"  event_type={ev.event_type}  plate={ev.plate_number}  gate={ev.gate}")

