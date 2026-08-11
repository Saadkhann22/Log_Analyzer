# test_all.py - Complete system test
from db import db
from main import get_embedding, classify_log_with_gemini

print("=" * 50)
print("🔍 SYSTEM CHECK")
print("=" * 50)

# 1. Check database
print("\n📊 Database:")
logs = db.execute("SELECT COUNT(*) as count FROM logs")
emb = db.execute("SELECT COUNT(*) as count FROM log_embeddings")
print(f"   Logs: {logs[0]['count'] if logs else 0}")
print(f"   Embeddings: {emb[0]['count'] if emb else 0}")

# 2. Check one log with embedding
print("\n📋 Sample Log:")
log = db.execute("SELECT id, level, message FROM logs LIMIT 1")
if log:
    print(f"   ID: {log[0]['id']}")
    print(f"   Level: {log[0]['level']}")
    print(f"   Message: {log[0]['message'][:50]}...")

# 3. Test embedding generation
print("\n🔢 Embedding Test:")
text = "Database connection timeout"
emb = get_embedding(text)
if emb:
    print(f"   ✅ Generated {len(emb)}-dim embedding")
else:
    print("   ❌ Embedding generation failed")

# 4. Check if similarity search works
print("\n🔍 Similarity Test:")
emb_result = db.execute("SELECT embedding FROM log_embeddings LIMIT 1")
if emb_result:
    target = emb_result[0]['embedding']
    results = db.execute("""
        SELECT l.id, 1 - (le.embedding <=> %s) AS sim
        FROM logs l
        JOIN log_embeddings le ON l.id = le.log_id
        LIMIT 3
    """, [target])
    print(f"   ✅ Found {len(results)} similar logs")
    for r in results:
        print(f"      Log {r['id']}: similarity {r['sim']:.4f}")
else:
    print("   ⚠️  No embeddings to test similarity")

print("\n✅ System check complete!")