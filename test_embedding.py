# test_embedding.py - Test embedding generation
import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

def test_embedding():
    """Test if embedding generation works"""
    try:
        response = client.models.embed_content(
            model="gemini-embedding-001",
            contents="Test embedding"
        )
        embedding = response.embeddings[0].values
        print(f"✅ Embedding generated! Length: {len(embedding)}")
        print(f"   First 5 values: {embedding[:5]}")
        return True
    except Exception as e:
        print(f"❌ Embedding failed: {e}")
        return False

if __name__ == "__main__":
    test_embedding()