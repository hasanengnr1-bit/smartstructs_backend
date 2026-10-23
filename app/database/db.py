import os
from sqlalchemy.orm import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

USERNAME = os.environ['DB_USER']
PASSWORD = os.environ['DB_PASSWORD']
DB_NAME = os.environ['DB_NAME']
HOST = os.environ['DB_HOST']
PORT = os.environ['DB_PORT']

SQLALCHEMY_DATABASE_URL = f"postgresql+asyncpg://{USERNAME}:{PASSWORD}@{HOST}:{PORT}/{DB_NAME}"

engine = create_async_engine(
    SQLALCHEMY_DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_recycle=1800,   # recycle connections every 30 min — avoids stale-connection errors
    pool_pre_ping=True,
)

local_async_session = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False, # Recommended for async to avoid unexpected lazy loads
)

Base = declarative_base()

async def get_db():
    async with local_async_session() as db:
        try:
            yield db
        finally:
            await db.close()