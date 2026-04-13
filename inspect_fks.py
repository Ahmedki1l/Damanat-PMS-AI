import sys
import os
from sqlalchemy import create_engine, inspect

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import settings

def inspect_fks():
    engine = create_engine(settings.db_url)
    inspector = inspect(engine)
    
    for table_name in inspector.get_table_names():
        fks = inspector.get_foreign_keys(table_name)
        if fks:
            print(f"Table: {table_name}")
            for fk in fks:
                print(f"  FK Name: {fk['name']}")
                print(f"    Columns: {fk['constrained_columns']}")
                print(f"    Referred Table: {fk['referred_table']}")
                print(f"    Referred Columns: {fk['referred_columns']}")

if __name__ == "__main__":
    inspect_fks()
