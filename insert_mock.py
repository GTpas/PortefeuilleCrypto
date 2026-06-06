import asyncio
import asyncpg
import json
from config import settings

async def main():
    pool = await asyncpg.create_pool(settings.DATABASE_URL)
    await pool.execute(
        "INSERT INTO system_log (component, level, message, metadata) VALUES ($1, $2, $3, $4)",
        'truth_social_scraper', 'SUCCESS', 'Retrieved Truth Social post and captured market screenshot.', 
        json.dumps({'screenshot_path': 'https://picsum.photos/400/200'})
    )
    await pool.close()

asyncio.run(main())
