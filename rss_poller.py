import time

import feedparser
import psycopg2
from datetime import datetime, timezone
import os

DB_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://feed_admin:feed_pass@localhost:5432/feed_engine"
)
POLL_INTERVAL_SECONDS = (
    300  # 5 min, matches the, "high priority" tier from your design doc
)


def get_publishers(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, feed_url, region FROM publisher")
        return cur.fetchall()


def insert_article(conn, title, url, publisher_id, category, region, published_at):
    with conn.cursor() as cur:
        cur.execute(
            """
        INSERT INTO article (title, url, publisher_id, category, region, published_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (url) DO NOTHING
        """,
            (title, url, publisher_id, category, region, published_at),
        )
        conn.commit()


def poll_once(conn):
    for publisher_id, name, feed_url, region in get_publishers(conn):
        print(f"Polling {name}....")
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            title = entry.get("title", "")
            url = entry.get("link", "")
            # TODO: real category detection — for now hardcode or infer from feed
            category = "general"
            published_at = datetime.now(
                timezone.utc
            )  # TODO: parse entry.published if present
            insert_article(
                conn, title, url, publisher_id, category, region, published_at
            )
        print(f"  -> {len(feed.entries)} entries processed")


def main():
    conn = psycopg2.connect(DB_DSN)
    try:
        while True:
            poll_once(conn)
            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
