import csv
import io
import os
import random
from datetime import datetime, timedelta
from itertools import islice

import psycopg2

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://feed_admin:feed_pass@localhost:5432/feed_engine"
)
REGIONS = ["US", "EU", "APAC", "LATAM"]
CATEGORIES = ["tech", "business", "sports", "world", "science", "politics", "education"]


def generate_rows(n, publisher_ids):
    now = datetime.utcnow()
    for i in range(n):
        published_at = now - timedelta(minutes=random.randint(0, 60 * 24 * 30))
        yield (
            f"Synthetic Article {i}",
            f"https://example.com/article/{i}",
            random.choice(publisher_ids),
            random.choice(CATEGORIES),
            random.choice(REGIONS),
            published_at.isoformat(),
        )


def batch_generator(rows, batch_size):
    while batch := list(islice(rows, batch_size)):
        yield (batch)


def main(count: int):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("SELECT id FROM publisher")
    publisher_ids = [r[0] for r in cur.fetchall()]
    if not publisher_ids:
        raise RuntimeError("no publishers in DB — seed publisher table first")

    buf = io.StringIO()
    writer = csv.writer(buf)
    rows = generate_rows(count, publisher_ids)
    batch = batch_generator(rows, 1_000_000)
    for tuples in batch:
        for row in tuples:
            writer.writerow(row)
        # seek to start to read from start of buffer
        buf.seek(0)
        cur.copy_expert(
            """
            COPY article (title, url, publisher_id, category, region, published_at)
            FROM STDIN WITH (FORMAT csv)
            """,
            buf,
        )
        buf.seek(0)
        buf.truncate(0)
    conn.commit()
    cur.close()
    conn.close()
    print(f"seeded {count} articles")


if __name__ == "__main__":
    import sys

    main(int(sys.argv[1]) if len(sys.argv) > 1 else 50_000_000)
