import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker,DeclarativeBase
_url=os.getenv('DATABASE_URL','mysql+pymysql://solar:solar@db:3306/solar')
# READ COMMITTED is what MySQL row locks need; SQLite (used by tests only)
# rejects the option, so only pass it for MySQL.
engine=create_engine(_url,pool_pre_ping=True,**({'isolation_level':'READ COMMITTED'} if _url.startswith('mysql') else {}))
Session=sessionmaker(engine,expire_on_commit=False)
class Base(DeclarativeBase):pass
def session():
    with Session() as db:yield db
