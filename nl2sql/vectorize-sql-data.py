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
from datetime import datetime, timezone
import json
import hashlib
from typing import List, Dict, Any
import pyodbc
from azure.core.credentials import AzureKeyCredential
from openai import AzureOpenAI
from dotenv import load_dotenv
import os

load_dotenv()

# Configuration
ai_search_endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
ai_search_key = os.environ["AZURE_SEARCH_KEY"]
ai_search_index = "amc-sql-data-v"

# Azure OpenAI
aoai_deployment = os.getenv("AOAI_DEPLOYMENT")
aoai_key = os.getenv("AOAI_KEY")
aoai_endpoint = os.getenv("AOAI_ENDPOINT")

search_index_client = SearchIndexClient(ai_search_endpoint, AzureKeyCredential(ai_search_key))
search_client = SearchClient(ai_search_endpoint, ai_search_index, AzureKeyCredential(ai_search_key))

aoai_client = AzureOpenAI(
    azure_endpoint=aoai_endpoint,
    api_key=aoai_key,
    api_version="2023-05-15"
)

def get_columns_for_table(cursor, table_name: str, schema: str = "dbo", include_columns: List[str] = None) -> List[str]:
    """
    Get specified columns from a table, with options to filter by name and data type.
    
    Args:
        cursor: Database cursor
        table_name: Name of the table
        schema: Database schema (default: "dbo")
        include_columns: List of specific columns to include. If None, uses default filtering rules.
    
    Returns:
        List[str]: List of column names that match the criteria
    """
    if include_columns:
        placeholders = ','.join(['?' for _ in include_columns])
        query = f"""
            SELECT COLUMN_NAME, DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = ?
            AND TABLE_SCHEMA = ?
            AND COLUMN_NAME IN ({placeholders})
        """
        cursor.execute(query, (table_name, schema, *include_columns))
    else:
        query = f"""
            SELECT COLUMN_NAME, DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = ?
            AND TABLE_SCHEMA = ?
        """
        cursor.execute(query, (table_name, schema))
    
    return {row.COLUMN_NAME: row.DATA_TYPE for row in cursor.fetchall()}

def get_table_data(table_name: str, schema: str = "dbo", include_columns: List[str] = None) -> List[str]:
    """
    Connect to SQL Server and retrieve all distinct values from specified table columns.
    """
    # SQL Server configuration from environment variables
    conn_str = (
        r'DRIVER={' + os.getenv("SQL_SERVER_DRIVER") + r'};'
        r'SERVER=' + os.getenv("SQL_SERVER_NAME") + r';'
        r'Trusted_Connection=yes;'
    )



    formatted_strings = []

    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        
        # Get column data types
        columns_info = get_columns_for_table(cursor, table_name, schema, include_columns)
        
        print(f"\nProcessing columns for {table_name}: {', '.join(columns_info.keys())}")
        
        for column, data_type in columns_info.items():
            print(f"Retrieving values for {column} ({data_type}) from {table_name}...")
            
            # Handle different data types
            if data_type.lower() in ('text', 'ntext'):
                # For text columns, convert to varchar and take first 1000 characters
                query = f"""
                    SELECT DISTINCT 
                        CAST(SUBSTRING(CAST({column} AS VARCHAR(MAX)), 1, 1000) AS VARCHAR(1000)) as value
                    FROM [{schema}].[{table_name}]
                    WHERE {column} IS NOT NULL
                """
            else:
                # For non-text columns, use normal DISTINCT
                query = f"""
                    SELECT DISTINCT {column} as value
                    FROM [{schema}].[{table_name}]
                    WHERE {column} IS NOT NULL
                """
            
            cursor.execute(query)
            
            for row in cursor.fetchall():
                value = str(row.value).strip()
                if value:
                    formatted_string = f"{column}: {value}"
                    formatted_strings.append(formatted_string)
        
        return formatted_strings
        
    except pyodbc.Error as e:
        print(f"Database error: {str(e)}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()

def generate_embeddings(texts: List[str], model="text-embedding-ada-002") -> List[List[float]]:
    """Generate embeddings for a batch of texts."""
    return [embedding.embedding for embedding in aoai_client.embeddings.create(input=texts, model=model).data]

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
        SimpleField(name="tableName", type=SearchFieldDataType.String, filterable=True, searchable=True),
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
    print("Index has been created")

def generate_document_id(content: str, table_name: str) -> str:
    """Generate a unique, deterministic ID for a document."""
    unique_string = f"{table_name}:{content}"
    return hashlib.md5(unique_string.encode()).hexdigest()

def process_batch(texts: List[str], table_name: str) -> List[Dict[str, Any]]:
    """Process a batch of texts into documents with embeddings."""
    # Generate embeddings for the batch
    embeddings = generate_embeddings(texts)
    
    # Create documents
    documents = []
    for text, embedding in zip(texts, embeddings):
        doc_id = generate_document_id(text, table_name)
        document = {
            "id": doc_id,
            "content": text,
            "tableName": table_name,
            "contentVector": embedding
        }
        documents.append(document)
    
    return documents

def process_tables(table_names: List[str], column_map: Dict[str, List[str]] = None, schema: str = "dbo", batch_size: int = 100):
    """
    Process multiple tables and index their data in batches.
    
    Args:
        table_names: List of tables to process
        column_map: Optional dictionary mapping table names to lists of columns to include
        schema: Database schema
        batch_size: Number of documents to process in each batch
    """
    create_index()
    total_documents = 0

    for table_name in table_names:
        try:
            print(f"\nProcessing table: {table_name}")
            
            # Get specific columns for this table if provided in column_map
            include_columns = column_map.get(table_name) if column_map else None
            
            # Get data from specified or filtered columns
            formatted_strings = get_table_data(table_name, schema, include_columns)
            print(f"Retrieved {len(formatted_strings)} distinct values from {table_name}")
            
            # Process in batches
            for i in range(0, len(formatted_strings), batch_size):
                batch = formatted_strings[i:i + batch_size]
                print(f"\nProcessing batch {i//batch_size + 1} of {(len(formatted_strings) + batch_size - 1)//batch_size}")
                
                # Create and index documents for the batch
                documents = process_batch(batch, table_name)
                result = search_client.upload_documents(documents)
                
                total_documents += len(result)
                print(f"Indexed {len(result)} documents in this batch")

        except Exception as e:
            print(f"Error processing table {table_name}: {str(e)}")
            continue
    
    print(f"\nTotal documents indexed across all tables: {total_documents}")

if __name__ == "__main__":
    # Define columns to process for each table
    # table_column_map = {
    #     "V_FCT_RAG_MODEL_CONTENT_VIEWS_MOCK": [
    #         "BRAND",
    #         "DISTRIBUTOR",
    #         "COUNTRY_CODE",
    #         "COUNTRY_NAME",
    #         "SUBSCRIPTION_TERM",
    #         "CONTENT_TYPE",
    #         "SERIES",
    #         "SERIES_GENRE",
    #         "NETWORK",
    #         "IS_ACQUIRED",
    #         "IS_LICENSED",
    #         "SERIES_SYNOPSIS",
    #         "SERIES_CAST_AND_CREW",
    #         "SERIES_MPAA_RATING",
    #         "SEASON_SYNOPSIS",
    #         "SEASON_CAST_AND_CREW",
    #         "EPISODE_TITLE",
    #         "EPISODE_SYNOPSIS",
    #         "EPISODE_CAST_AND_CREW"
    #     ]
    # }

    table_column_map = {
         "V_FCT_RAG_MODEL_SUBS_METRICS_MOCK": [
            "BRAND",
            "DISTRIBUTOR",
            "COUNTRY_CODE",
            "COUNTRY_NAME",
            "SUBSCRIPTION_TERM"
        ]
    }
    
    tables_to_process = list(table_column_map.keys())
    
    try:
        process_tables(
            table_names=tables_to_process,
            column_map=table_column_map,
            schema="dbo",
            batch_size=100
        )
        print("\nCompleted processing all tables")
    except Exception as e:
        print(f"Error in main process: {str(e)}")