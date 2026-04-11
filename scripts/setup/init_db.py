# scripts/setup/init_db.py
"""
Initialize database — creates all tables.
Run once before first launch, or after adding new models.
Usage: python scripts/setup/init_db.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.database import create_tables, engine
from app.config import settings
from sqlalchemy import text


def main():
    print("🗄️  Damanat DB Initialization")
    print("=" * 40)
    print(f"📡 Database: {settings.DATABASE_URL}")

    # Test connection
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("✅ Database connection OK")
    except Exception as e:
        print(f"❌ Cannot connect to database: {e}")
        print("\nMake sure PostgreSQL is running:")
        print("  docker-compose up -d db")
        print("  # or: sudo systemctl start postgresql")
        sys.exit(1)

    # Create all tables
    print("\n📋 Creating tables...")
    create_tables()
    print("✅ All tables created (Phase 1 + Phase 2)")

    # List created tables
    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ))
        tables = [row[0] for row in result]

    print(f"\n📊 Tables in database ({len(tables)} total):")
    for t in tables:
        print(f"   ✓ {t}")

    print("\n🎉 Database ready! You can now start the backend:")
    print("   uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload")


if __name__ == "__main__":
    main()

