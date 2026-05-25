-- Create application database (Langfuse uses its own)
SELECT 'CREATE DATABASE data_assistant'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'data_assistant')\gexec

SELECT 'CREATE DATABASE langfuse'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec
