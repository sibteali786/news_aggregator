-- Live Feed Engine — Phase 0 schema
-- Deliberately plain: no indexes beyond PK yet, so Phase 1 load-testing
-- shows real degradation before we optimize anything.

CREATE TABLE publisher (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    feed_url    TEXT NOT NULL,
    region      TEXT NOT NULL,      -- e.g. 'US', 'UK', 'IN'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE article (
    id            BIGSERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    publisher_id  INTEGER NOT NULL REFERENCES publisher(id),
    category      TEXT NOT NULL,    -- e.g. 'tech', 'sports', 'politics'
    region        TEXT NOT NULL,
    published_at  TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed a handful of real publishers to start organic RSS ingestion.
INSERT INTO publisher (name, feed_url, region) VALUES
    ('BBC World',      'http://feeds.bbci.co.uk/news/world/rss.xml', 'UK'),
    ('Reuters World',  'https://www.reutersagency.com/feed/?best-topics=world', 'US'),
    ('TechCrunch',     'https://techcrunch.com/feed/', 'US'),
    ('Hacker News',    'https://hnrss.org/frontpage', 'US');

-- NOTE: intentionally no index on (region, published_at) yet.
-- Phase 1 will prove why you need one (or need Redis instead).
