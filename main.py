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
from scripts.renewLock import renewLockLuaScript

REDIS_DSN = os.environ.get("REDIS_URL", "redis://redis:6379")
DB_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://feed_admin:feed_pass@localhost:5432/feed_engine"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DB_DSN, min_size=5, max_size=20)
    pool = redis.ConnectionPool.from_url(REDIS_DSN)
    app.state.pool_redis = redis.Redis.from_pool(pool)
    # registering lua scripts
    app.state.lockDelSript = app.state.pool_redis.register_script(deleteKeyLuaScript)
    app.state.lockRenewScript = app.state.pool_redis.register_script(renewLockLuaScript)
    yield
    await app.state.pool.close()
    await app.state.pool_redis.aclose()


app = FastAPI(title="feed-engine-api", lifespan=lifespan)


def processArticleResponse(rows) -> str:
    rows_modified = [
        dict(r) | {"publishedAt": r["publishedAt"].isoformat()} for r in rows
    ]
    json_serialized = json.dumps(rows_modified)
    return json_serialized


async def fetchFromDb(query: str, args: tuple[str] | str | int):
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(query, args)
    json_serialized = await asyncio.to_thread(processArticleResponse, rows)
    return json_serialized


async def release_lock(lock_key: str, token: str):
    try:
        await app.state.lockDelSript(keys=[lock_key], args=[token])
    except Exception as e:
        print(f"Error releasing lock {lock_key}: ", e)


async def renew_loop(
    event: asyncio.Event, key: str, token: str, ttl: int = 10, interval: int = 5
):
    while True:
        try:
            await asyncio.wait_for(event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            try:
                await app.state.lockRenewScript(keys=[key], args=[token, ttl])
            except Exception as e:
                print("Error in redis script for renewal: ", e)


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
        lockAcquired = await redis_client.set(lock_key, token, nx=True, ex=10)
        if lockAcquired:
            event = asyncio.Event()

            async def fetch_and_signal():
                try:
                    rows = await fetchFromDb(query, args)
                    return rows
                finally:
                    event.set()

            try:

                async with asyncio.TaskGroup() as tg:
                    fetch = tg.create_task(fetch_and_signal())
                    renewal_loop = tg.create_task(renew_loop(event, lock_key, token))
            except* Exception as eg:
                await release_lock(lock_key, token)
                print(f"Request failed for database: {eg.exceptions}")
                raise
            ## DB fetch
            json_serialized = fetch.result()
            await redis_client.set(key, json_serialized, ex=20)
            ## release the lock
            await release_lock(lock_key, token)
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
