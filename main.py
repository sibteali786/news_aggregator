from typing import Optional
from fastapi import FastAPI, Query, Response
from pydantic import BaseModel
from contextlib import asynccontextmanager
import asyncpg
from datetime import datetime
import os
from fastapi.responses import JSONResponse
import redis.asyncio as redis
import json
import uuid
import asyncio

from scripts.deleteKey import deleteKeyLuaScript

REDIS_DSN = os.environ.get("REDIS_URL", "redis://redis:6379")
DB_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://feed_admin:feed_pass@localhost:5432/feed_engine"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DB_DSN, min_size=5, max_size=20)
    pool = redis.ConnectionPool.from_url(REDIS_DSN)
    app.state.pool_redis = redis.Redis.from_pool(pool)
    app.state.lockDelSript = app.state.pool_redis.register_script(deleteKeyLuaScript)
    yield
    await app.state.pool.close()
    await app.state.pool_redis.aclose()


app = FastAPI(title="feed-engine-api", lifespan=lifespan)


async def fetchFromDb(query: str, args: tuple[str] | str | int):
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(query, args)
    rows_modified = [
        dict(r) | {"publishedAt": r["publishedAt"].isoformat()} for r in rows
    ]
    json_serialized = json.dumps(rows_modified)
    return json_serialized


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


@app.get("/feed/full-scan")
async def getFullScanFeed(limit: int = Query(1_000_000, le=10_000_000)):
    redis_client = app.state.pool_redis
    query = """
        SELECT a.id, a.title, a.url, p.name AS "publisherName", a.category, a.region, a.published_at AS "publishedAt" 
        FROM article a JOIN
        publisher p on p.id = a.publisher_id ORDER BY a.id DESC LIMIT $1
    """
    args = limit
    key = f"fullscan:limit:{limit}"
    rows = await redis_client.get(key)
    if rows != None:
        # cache hit
        return Response(content=rows, media_type="application/json")
    else:
        # cache miss
        lock_key = f"fullscan:lock:limit:{limit}"
        ## create token
        token = str(uuid.uuid4())
        ## create a lock and acquire it
        lockAcquired = await redis_client.set(lock_key, token, nx=True, ex=45)
        if lockAcquired:
            ## DB fetch
            json_serialized = await fetchFromDb(query, args)
            await redis_client.set(key, json_serialized, ex=20)
            ## release the lock
            await app.state.lockDelSript(keys=[lock_key], args=[token])
            return Response(content=json_serialized, media_type="application/json")
        else:
            for i in range(80):
                await asyncio.sleep(0.5)  # 500ms wait
                rows = await redis_client.get(key)
                if rows != None:
                    return Response(content=rows, media_type="application/json")

            # all 20 polls ran but data was not there
            json_serialized = await fetchFromDb(query, args)
            await redis_client.set(key, json_serialized, ex=20)
            return Response(content=json_serialized, media_type="application/json")
