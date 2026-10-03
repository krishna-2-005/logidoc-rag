from pathlib import Path
import sys
import chromadb
import ollama

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import CHROMA_DIR, COLLECTION_NAME, EMBEDDING_MODEL
from chunker import create_all_chunks


def get_collection():
    """
    Create or load the ChromaDB collection.
    """

    client = chromadb.PersistentClient(
        path=str(CHROMA_DIR)
    )

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME
    )

    return collection


def create_embedding(text):
    """
    Generate an embedding using Ollama.
    """

    response = ollama.embeddings(
        model=EMBEDDING_MODEL,
        prompt=text
    )

    return response["embedding"]


def build_vector_database():
    """
    Convert all document chunks into embeddings
    and store them in ChromaDB.
    """

    print("=" * 70)
    print("LOGIDOC-RAG VECTOR DATABASE")
    print("=" * 70)

    chunks = create_all_chunks()

    if not chunks:
        print("\nNo chunks found.")
        return

    collection = get_collection()

    # Clear previous data so indexing is always fresh
    existing = collection.get()

    if existing["ids"]:
        collection.delete(ids=existing["ids"])
        print(f"\nRemoved {len(existing['ids'])} old vectors.")

    print(f"\nCreating embeddings for {len(chunks)} chunks...")

    ids = []
    documents = []
    embeddings = []
    metadatas = []

    for index, chunk in enumerate(chunks):

        print(
            f"Embedding {index + 1}/{len(chunks)}...",
            end="\r"
        )

        embedding = create_embedding(
            chunk["text"]
        )

        ids.append(
            f"{chunk['metadata']['shipment_id']}_{index}"
        )

        documents.append(
            chunk["text"]
        )

        embeddings.append(
            embedding
        )

        metadatas.append(
            chunk["metadata"]
        )

    collection.add(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas
    )

    print("\n")
    print("=" * 70)
    print("VECTOR DATABASE CREATED SUCCESSFULLY")
    print("=" * 70)

    print(f"\nDocuments indexed : {len(chunks)}")
    print(f"Collection        : {COLLECTION_NAME}")
    print(f"Database path     : {CHROMA_DIR}")
    print(f"Embedding model   : {EMBEDDING_MODEL}")

    print("\nVector database is ready for RAG.")


if __name__ == "__main__":
    build_vector_database()