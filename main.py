from typing import Optional
from fastapi import FastAPI, Query
from pydantic import BaseModel
from contextlib import asynccontextmanager
import asyncpg
from datetime import datetime
import os
from fastapi.responses import JSONResponse

DB_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://feed_admin:feed_pass@localhost:5432/feed_engine"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DB_DSN, min_size=5, max_size=22)
    yield
    await app.state.pool.close()


app = FastAPI(title="feed-engine-api", lifespan=lifespan)


class FeedItem(BaseModel):
    id: int
    title: str
    url: str
    publisherName: str
    category: str
    region: str
    publishedAt: datetime


@app.get("/feed", response_model=list[FeedItem])
async def get_feed(
    region: str,
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[int] = None,
):
    if cursor is None:
        query = """
            SELECT a.id, a.title, a.url, p.name AS "publisherName", a.category, a.region, a.published_at AS "publishedAt"
            FROM article a
            JOIN publisher p
            ON p.id = a.publisher_id
            WHERE a.region = $1
            ORDER BY a.id DESC
            LIMIT $2
        """
        args = (region, limit)
    else:
        query = """
            SELECT a.id, a.title, a.url, p.name AS "publisherName",
                   a.category, a.region, a.published_at AS "publishedAt"
            FROM article a
            JOIN publisher p ON p.id = a.publisher_id
            WHERE a.region = $1 AND a.id < $2
            ORDER BY a.id DESC
            LIMIT $3
        """
        args = (region, cursor, limit)
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(query, *args)

    return [dict(r) for r in rows]
