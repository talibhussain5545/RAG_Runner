from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SimpleField,
    SearchFieldDataType,
    SearchableField,
    SearchField,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SearchIndex
)
from azure.core.credentials import AzureKeyCredential
from openai import AzureOpenAI
from dotenv import load_dotenv
import os
import hashlib
from typing import List, Dict, Any
import glob
from pathlib import Path

load_dotenv()

# Azure Search configuration
ai_search_endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
ai_search_key = os.environ["AZURE_SEARCH_KEY"]
ai_search_index = "agentic-doc-index"

# Azure OpenAI configuration
aoai_deployment = os.getenv("AOAI_DEPLOYMENT")
aoai_key = os.getenv("AOAI_KEY")
aoai_endpoint = os.getenv("AOAI_ENDPOINT")

search_index_client = SearchIndexClient(ai_search_endpoint, AzureKeyCredential(ai_search_key))
search_client = SearchClient(ai_search_endpoint, ai_search_index, AzureKeyCredential(ai_search_key))

aoai_client = AzureOpenAI(
    azure_endpoint=aoai_endpoint,
    api_key=aoai_key,
    api_version="2024-05-15"
)

def create_index():
    """Create Azure Search index if it doesn't exist"""
    try:
        search_index_client.get_index(ai_search_index)
        print("Index already exists")
        return
    except:
        pass

    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SearchableField(name="content", type=SearchFieldDataType.String, filterable=True, searchable=True),
        SimpleField(name="filepath", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="chunk_number", type=SearchFieldDataType.Int32, filterable=True),
        SearchField(
            name="contentVector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=1536,
            vector_search_profile_name="myHnswProfile"
        )
    ]

    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="myHnsw")],
        profiles=[VectorSearchProfile(name="myHnswProfile", algorithm_configuration_name="myHnsw")]
    )

    index = SearchIndex(name=ai_search_index, fields=fields, vector_search=vector_search)
    search_index_client.create_or_update_index(index)
    print("Index created successfully")

def generate_embeddings(texts: List[str], model="text-embedding-ada-002") -> List[List[float]]:
    """Generate embeddings for a batch of texts."""
    return [embedding.embedding for embedding in aoai_client.embeddings.create(input=texts, model=model).data]

def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 100) -> List[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start = 0
    text_length = len(text)

    while start < text_length:
        # Calculate end position with overlap
        end = start + chunk_size
        
        # If not at the last chunk, try to break at a natural point
        if end < text_length:
            # Look for natural break points (period, newline, etc.)
            for breakpoint in ['. ', '\n', '. ', ', ', ' ']:
                natural_end = text.rfind(breakpoint, start + chunk_size - 100, end)
                if natural_end != -1:
                    end = natural_end + 1
                    break
        else:
            end = text_length

        # Add the chunk
        chunk = text[start:end].strip()
        if chunk:  # Only add non-empty chunks
            chunks.append(chunk)

        # Move start position, accounting for overlap
        start = end - overlap if end < text_length else text_length

    return chunks

def generate_document_id(filepath: str, chunk_number: int) -> str:
    """Generate a unique, deterministic ID for a document chunk."""
    unique_string = f"{filepath}:{chunk_number}"
    return hashlib.md5(unique_string.encode()).hexdigest()

def process_document(filepath: str, chunk_size: int = 1000, overlap: int = 100) -> List[Dict[str, Any]]:
    """Process a single document into chunks with embeddings."""
    print(f"\nProcessing document: {filepath}")
    
    # Read document content
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Split into chunks
    chunks = chunk_text(content, chunk_size, overlap)
    print(f"Created {len(chunks)} chunks")

    # Generate embeddings for all chunks
    embeddings = generate_embeddings(chunks)

    # Create documents
    documents = []
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        doc_id = generate_document_id(filepath, i)
        document = {
            "id": doc_id,
            "content": chunk,
            "filepath": filepath,
            "chunk_number": i,
            "contentVector": embedding
        }
        documents.append(document)

    return documents

def process_directory(directory: str, batch_size: int = 100):
    """Process all documents in a directory."""
    create_index()
    total_documents = 0

    # Get all files recursively
    all_files = glob.glob(os.path.join(directory, "**/*.*"), recursive=True)
    supported_extensions = {'.txt', '.md', '.py', '.js', '.html', '.css', '.json', '.yaml', '.yml'}
    
    for filepath in all_files:
        if Path(filepath).suffix.lower() in supported_extensions:
            try:
                # Process document into chunks with embeddings
                documents = process_document(filepath)
                
                # Upload in batches
                for i in range(0, len(documents), batch_size):
                    batch = documents[i:i + batch_size]
                    result = search_client.upload_documents(batch)
                    total_documents += len(result)
                    print(f"Indexed {len(result)} chunks from {filepath}")

            except Exception as e:
                print(f"Error processing {filepath}: {str(e)}")
                continue

    print(f"\nTotal chunks indexed: {total_documents}")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Process documents into searchable chunks')
    parser.add_argument('--input_dir', type=str, required=True, help='Directory containing documents to process')
    parser.add_argument('--chunk_size', type=int, default=1000, help='Size of text chunks')
    parser.add_argument('--overlap', type=int, default=100, help='Overlap between chunks')
    parser.add_argument('--batch_size', type=int, default=100, help='Batch size for indexing')
    
    args = parser.parse_args()
    
    process_directory(
        directory=args.input_dir,
        batch_size=args.batch_size
    )