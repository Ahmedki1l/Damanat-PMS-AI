from sqlalchemy import Column, Integer, String
from app.database import Base

class SystemConfig(Base):
    __tablename__ = "system_config"
    
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(255), unique=True, index=True)
    value = Column(String(500))

