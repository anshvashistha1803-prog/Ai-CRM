-- Automatically executed on first Postgres container startup
SELECT 'CREATE DATABASE crm_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'crm_test')\gexec
