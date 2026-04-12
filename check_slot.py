from app.database import engine
from sqlalchemy import text
import pprint

with engine.connect() as c:
    res = c.execute(text("SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS WHERE COLUMN_NAME = 'slot_id'")).fetchall()
    pprint.pprint(res)
