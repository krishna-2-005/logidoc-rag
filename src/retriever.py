from pathlib import Path
import sys
import ollama
import chromadb

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import CHROMA_DIR, COLLECTION_NAME, EMBEDDING_MODEL


def get_collection():
    """
    Connect to the existing ChromaDB collection.
    """

    client = chromadb.PersistentClient(
        path=CHROMA_DIR
    )

    collection = client.get_collection(
        name=COLLECTION_NAME
    )

    return collection


def create_query_embedding(query):
    """
    Convert the user's question into an embedding.
    """

    response = ollama.embeddings(
        model=EMBEDDING_MODEL,
        prompt=query
    )

    return response["embedding"]


def retrieve_documents(query, top_k=3):
    """
    Retrieve the most relevant logistics document chunks.
    """

    collection = get_collection()

    query_embedding = create_query_embedding(query)

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k
    )

    return results


def display_results(query, results):
    """
    Display retrieved documents and metadata.
    """

    print("\n" + "=" * 70)
    print("LogiDoc-RAG Retrieval Test")
    print("=" * 70)

    print(f"\nQuery:")
    print(query)

    print("\nRetrieved Documents:")
    print("-" * 70)

    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    for i, (document, metadata, distance) in enumerate(
        zip(documents, metadatas, distances),
        start=1
    ):

        print(f"\nResult {i}")
        print("-" * 50)

        print(f"Source        : {metadata.get('source')}")
        print(f"Document Type : {metadata.get('document_type')}")
        print(f"Shipment ID   : {metadata.get('shipment_id')}")
        print(f"Invoice No.   : {metadata.get('invoice_number')}")
        print(f"Page          : {metadata.get('page')}")
        print(f"Distance      : {distance:.4f}")

        print("\nRetrieved Text:")
        print(document)

    print("\n" + "=" * 70)


def main():

    queries = [
        "What is the delivery status of shipment SHJ-2026-001?",
        "What is the total invoice amount?",
        "Who received the shipment?",
    ]

    for query in queries:

        results = retrieve_documents(
            query=query,
            top_k=3
        )

        display_results(
            query=query,
            results=results
        )


if __name__ == "__main__":
    main()