from pathlib import Path
import sys

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ollama

from config import EMBEDDING_MODEL
from chunker import create_all_chunks


def generate_embedding(text):
    """
    Generate an embedding for a piece of text
    using the local nomic-embed-text model.
    """

    response = ollama.embeddings(
        model=EMBEDDING_MODEL,
        prompt=text
    )

    return response["embedding"]


def main():

    print("=" * 70)
    print("LogiDoc-RAG Embedding Test")
    print("=" * 70)

    print(f"\nEmbedding model: {EMBEDDING_MODEL}")

    print("\nCreating chunks...")

    chunks = create_all_chunks()

    print(f"Total chunks: {len(chunks)}")

    print("\nGenerating embeddings...")

    for index, chunk in enumerate(chunks, start=1):

        embedding = generate_embedding(chunk["text"])

        print("\n" + "-" * 70)

        print(f"Chunk         : {index}")
        print(f"Source        : {chunk['metadata']['source']}")
        print(f"Document Type : {chunk['metadata']['document_type']}")
        print(f"Shipment ID   : {chunk['metadata']['shipment_id']}")

        print(f"Embedding Size: {len(embedding)}")

        print("\nFirst 10 values:")

        print(embedding[:10])

    print("\n" + "=" * 70)
    print("Embedding generation completed successfully.")
    print("=" * 70)


if __name__ == "__main__":
    main()