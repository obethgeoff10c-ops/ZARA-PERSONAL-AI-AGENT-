-- Runs once on a fresh PostgreSQL volume. Migrations also create these
-- idempotently, but having pgvector available before the first migration makes
-- the database itself ready for M1 work.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS citext;
