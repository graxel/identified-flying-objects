import asyncio
import asyncpg
from core.config import settings
from database.db import init_db

async def create_database():
    sys_conn = await asyncpg.connect(
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        host=settings.POSTGRES_SERVER,
        port=settings.POSTGRES_PORT,
        database='postgres'
    )
    
    try:
        exists = await sys_conn.fetchval(f"SELECT 1 FROM pg_database WHERE datname = '{settings.POSTGRES_DB}'")
        if not exists:
            print(f"Creating database {settings.POSTGRES_DB}...")
            await sys_conn.execute(f'CREATE DATABASE "{settings.POSTGRES_DB}"')
        else:
            print(f"Database {settings.POSTGRES_DB} already exists.")
    finally:
        await sys_conn.close()

async def main():
    await create_database()
    print("Initializing tables...")
    await init_db()
    print("Tables initialized.")

if __name__ == "__main__":
    asyncio.run(main())
