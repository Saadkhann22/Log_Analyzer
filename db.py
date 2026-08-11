# db.py - Database connection with pgvector

import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv

load_dotenv()

class Database:
    def __init__(self):
        self.conn = None
        self.connect()
    
    def connect(self):
        try:
            self.conn = psycopg2.connect(
                host=os.getenv('DB_HOST', 'localhost'),
                port=os.getenv('DB_PORT', '5432'),
                database=os.getenv('DB_NAME', 'log_analyzer'),
                user=os.getenv('DB_USER', 'postgres'),
                password=os.getenv('DB_PASSWORD', 'mysecretpassword')
            )
            print("✅ Connected to PostgreSQL")
        except Exception as e:
            print(f"❌ Database connection failed: {e}")
            raise
    
    def execute(self, query, params=None):
        """Execute query. SELECT returns list, INSERT/UPDATE returns rowcount."""
        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, params)
                if query.strip().upper().startswith('SELECT') or 'RETURNING' in query.upper():
                    rows = cur.fetchall()
                    self.conn.commit()
                    return rows
                self.conn.commit()
                return cur.rowcount
        except Exception:
            # Roll back so a single failed statement doesn't poison the
            # shared connection for every subsequent query.
            self.conn.rollback()
            raise
    
    def close(self):
        if self.conn:
            self.conn.close()

db = Database()

def init_database():
    """Create tables and enable pgvector"""
    try:
        # Enable pgvector
        db.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        
        # Create logs table
        db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP DEFAULT NOW(),
                level VARCHAR(20),
                message TEXT,
                user_name VARCHAR(100)
            );
        """)
        
        # Create embeddings table (3072 dimensions)
        db.execute("""
            CREATE TABLE IF NOT EXISTS log_embeddings (
                id SERIAL PRIMARY KEY,
                log_id INTEGER REFERENCES logs(id) ON DELETE CASCADE,
                embedding vector(3072),
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        
        # Add unique constraint for log_id (Postgres has no
        # ADD CONSTRAINT IF NOT EXISTS, so guard it with a DO block)
        db.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'unique_log_id'
                ) THEN
                    ALTER TABLE log_embeddings
                    ADD CONSTRAINT unique_log_id UNIQUE (log_id);
                END IF;
            END $$;
        """)
        
        # NO similarity index — deliberate. pgvector's ANN indexes (ivfflat/hnsw)
        # cap out at 2000 dims and our embeddings are 3072. Without an index,
        # ORDER BY embedding <=> x is an EXACT sequential scan: slower at scale,
        # but perfectly fine (and more accurate) for a small learning dataset.
        # At production scale the options are:
        #   1. halfvec: hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)
        #      — 16-bit floats, indexable up to 4000 dims (pgvector >= 0.7)
        #   2. ask Gemini for smaller vectors (output_dimensionality=768)
        #      and re-embed everything into a vector(768) column

        print("✅ Database initialized successfully!")
        return True
    except Exception as e:
        print(f"❌ Database init failed: {e}")
        return False