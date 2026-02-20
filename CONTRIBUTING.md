# Contributing to Damanat PMS AI

## Development Setup

1. Clone the repo and create a virtual environment:
   ```bash
   git clone <repo-url> && cd damanat-backend
   python -m venv venv
   # Linux/Mac: source venv/bin/activate
   # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Set up environment:
   ```bash
   cp .env.example .env
   # Edit .env with your database URL and camera IPs
   ```

3. Start PostgreSQL:
   ```bash
   docker-compose up -d db
   python scripts/setup/init_db.py
   ```

4. Run the server:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
   ```

## Where to Put New Files

| New thing to add | Where to put it |
|-----------------|-----------------|
| New camera | Add to `config.py` CAMERAS + CAMERA_IP_MAP |
| New event type handler | Add service in `services/`, register in `event_dispatcher.py` |
| New API endpoint | Add router in `routers/`, register in `main.py` |
| New DB table | Add model in `models/`, import in `models/__init__.py` and `database.py` |
| New test script | Add to `scripts/test/` |
| New setup script | Add to `scripts/setup/` |

## Code Standards

1. **Logging**: Always use `get_logger(__name__)` — never `print()` in production code
2. **Configuration**: Never hardcode IPs — use `settings` from `config.py`
3. **Docstrings**: Every service file must have a docstring explaining:
   - Purpose
   - Camera (which camera triggers this)
   - Event type (which ISAPI event)
   - ISAPI endpoint (if applicable)
4. **Webhook Safety**: Always return HTTP 200 from the camera webhook — never let exceptions bubble up
5. **Structured Logging**: Use structured format: `logger.info(f"[UC3] zone={zone_id} count={count}")`

## Running Tests

```bash
python -m pytest tests/ -v
```

## Phase 2 Development

Phase 2 files are already implemented but commented out in `main.py`. When ANPR cameras arrive:

1. Update camera IPs in `.env`
2. Uncomment the 3 Phase 2 router lines in `app/main.py`
3. Run camera configuration scripts
4. Test with the event simulator

## Git Workflow

1. Create a feature branch: `git checkout -b feature/my-feature`
2. Make changes and add tests
3. Run tests: `python -m pytest tests/ -v`
4. Commit with a descriptive message
5. Push and create a pull request
