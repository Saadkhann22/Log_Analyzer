# db_seed.py - Seed the database with 13 logs

from db import db
from main import get_embedding
from datetime import datetime

LOGS = [
    {"level": "ERROR", "message": "Database connection timeout after 30 seconds", "user": "app_service"},
    {"level": "WARNING", "message": "API rate limit exceeded for IP 192.168.1.100", "user": "web_user_123"},
    {"level": "ERROR", "message": "Failed to process payment for order #ORD-7890: Insufficient funds", "user": "customer_456"},
    {"level": "CRITICAL", "message": "Disk space at 95% on /dev/sda1 - immediate action required", "user": "system"},
    {"level": "ERROR", "message": "SSL certificate expired for api.example.com", "user": "devops"},
    {"level": "WARNING", "message": "Slow query detected: SELECT * FROM orders WHERE status='pending' (took 12.5s)", "user": "db_user"},
    {"level": "ERROR", "message": "Unable to send email notification: SMTP server refused connection", "user": "notification_service"},
    {"level": "ERROR", "message": "File not found: /data/imports/customer_import_2026-07-01.csv", "user": "data_processor"},
    {"level": "WARNING", "message": "High memory usage detected: 4.2GB / 5.0GB (84%)", "user": "monitoring"},
    {"level": "CRITICAL", "message": "Authentication service unreachable - users cannot log in", "user": "auth_service"},
    {"level": "ERROR", "message": "Synapse federation error: Failed to send event to matrix.org: Connection timeout", "user": "synapse"},
    {"level": "CRITICAL", "message": "MAS authentication failure: OIDC token validation failed for user@example.com", "user": "auth_service"},
    {"level": "ERROR", "message": "Docker container postgres_1: Connection refused - health check failed", "user": "docker"},
]

def seed():
    print("🌱 Seeding database...")
    
    # Clear existing
    db.execute("DELETE FROM log_embeddings")
    db.execute("DELETE FROM logs")
    
    for log in LOGS:
        # Insert log
        result = db.execute("""
            INSERT INTO logs (level, message, user_name)
            VALUES (%s, %s, %s)
            RETURNING id
        """, [log["level"], log["message"], log["user"]])
        
        log_id = result[0]['id'] if result else None
        if log_id:
            # Generate and store embedding
            text = f"{log['level']}: {log['message']}"
            embedding = get_embedding(text)
            if embedding:
                db.execute("""
                    INSERT INTO log_embeddings (log_id, embedding)
                    VALUES (%s, %s::vector)
                """, [log_id, embedding])
                print(f"   ✅ Inserted log {log_id}")
    
    # Verify
    result = db.execute("SELECT COUNT(*) as count FROM logs")
    print(f"✅ Seeded {result[0]['count']} logs")

if __name__ == "__main__":
    seed()