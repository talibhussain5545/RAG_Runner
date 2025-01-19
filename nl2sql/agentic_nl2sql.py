#test.py
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from azure.core.credentials import AzureKeyCredential
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
import langsmith
from pydantic import BaseModel
import pyodbc
from typing import List, Dict, Any, Union
from dotenv import load_dotenv
import os
from langgraph.graph import StateGraph, START, END
from typing import Dict, Any, TypedDict
from IPython.display import Image, display
from typing import Annotated
from operator import add

# Load environment variables
load_dotenv()

# Configuration and clients setup
ai_search_endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
ai_search_key = os.environ["AZURE_SEARCH_KEY"]
ai_search_index = "amc-sql-data-v"
aoai_deployment = os.getenv("AOAI_DEPLOYMENT")
aoai_key = os.getenv("AOAI_KEY")
aoai_endpoint = os.getenv("AOAI_ENDPOINT")
API_VERSION = "2024-08-01-preview"

LANGCHAIN_TRACING_V2=os.getenv("LANGCHAIN_TRACING_V2")
LANGCHAIN_ENDPOINT=os.getenv("LANGCHAIN_ENDPOINT")
LANGCHAIN_API_KEY=os.getenv("LANGCHAIN_API_KEY")
LANGCHAIN_PROJECT=os.getenv("LANGCHAIN_PROJECT")

search_client = SearchClient(ai_search_endpoint, ai_search_index, AzureKeyCredential(ai_search_key))




# Models
class ReasoningResponse(BaseModel):
    """Schema for parsing project title and description"""
    thought_process: str
    answer: str

# LLM setup
primary_llm = AzureChatOpenAI(
    azure_deployment=aoai_deployment,
    api_version="2024-05-01-preview",
    temperature=0,
    max_tokens=None,
    timeout=None,
    max_retries=2,
    api_key=aoai_key,
    azure_endpoint=aoai_endpoint
)

reasoning_llm = primary_llm.with_structured_output(ReasoningResponse)

embeddings_model = AzureOpenAIEmbeddings(
    azure_deployment="text-embedding-ada-002",
    api_key=aoai_key,
    azure_endpoint=aoai_endpoint
)


# SQL Server configuration from environment variables
conn_str = (
    r'DRIVER={' + os.getenv("SQL_SERVER_DRIVER") + r'};'
    r'SERVER=' + os.getenv("SQL_SERVER_NAME") + r';'
    r'Trusted_Connection=yes;'
)

database_name = os.getenv("SQL_DATABASE_NAME", "AMC-DB")
schema_name = "dbo"

print("conn_str: ", conn_str)

class AttemptState(TypedDict):
    attempt_number: int
    sql_agent_thought_process: str
    generated_sql: str
    query_results: str
    review_agent_thought_process: str

class ChatInteractionState(TypedDict):
    user_input: str
    database: str
    schema: str
    entity_list: list[str]
    dimension_info: str
    current_attempt: AttemptState | None
    attempt_history: List[AttemptState]

def read_metadata_file(filepath: str) -> str:
    """Read metadata from file and return as string."""
    try:
        with open(filepath, 'r') as file:
            return file.read().strip()
    except FileNotFoundError:
        print(f"Warning: Metadata file {filepath} not found")
        return ""
    except Exception as e:
        print(f"Error reading metadata file: {str(e)}")
        return ""

def entity_extraction(state: ChatInteractionState) -> ChatInteractionState:
    """
    Extract and split entities from user input.
    """
    entity_extraction_prompt = """Extract all entities from user input. Please provide them separated by the pipe character '|' with no other output. No spaces needed. Ignore temporal data such as timeframes. The goal is to extract the dimensions on which we will query and aggregate.
    
    ###Examples###
    
    User Input: "What was the total revenue in California last quarter?"
    Entities: "California"

    User Input: "Which product category had the highest sales in Q3?"
    Entities: "product|category|sales"

    User Input: "How many customers used coupons in New York?"
    Entities: "customers|coupons|New York"

    User Input: "What is the average order value for online sales?"
    Entities: "order value|online sales"

    User Input: "Which store had the most returns last month?"
    Entities: "store|returns"

    User Input: "Which countries had the highest number of orders for kitchen products?"
    Entities: "countries|orders|kitchen|product"


    """
    
    messages = [
        {"role": "system", "content": entity_extraction_prompt},
        {"role": "user", "content": state["user_input"]}
    ]
    
    entity_list = primary_llm.invoke(messages).content.split("|")
    print("entity_list: ", entity_list)
    return {"entity_list": entity_list}

from langsmith import traceable

@traceable(run_type="retriever", name="search_dimensions")
def search_dimensions(state: ChatInteractionState) -> ChatInteractionState:
    """
    Perform hybrid search for each entity and format results.
    """
    def generate_embeddings(text, model="text-embedding-ada-002"):
        return embeddings_model.embed_query(text)

    search_results_dict = {}
    
    # Perform search for each entity
    for entity_name in state["entity_list"]:
        entity_vector = generate_embeddings(entity_name)
        vector_query = VectorizedQuery(
            vector=entity_vector,
            k_nearest_neighbors=3,
            fields="contentVector"
        )
        
        results = search_client.search(
            search_text=entity_name,
            vector_queries=[vector_query],
            top=3
        )
        search_results_dict[entity_name] = list(results)
    
    # Format results
    context = []
    
    for entity in state["entity_list"]:
        context.append(f"\n{entity}:")
        results = search_results_dict[entity]
        #Print the raw results: score, content, tableName
        for result in results:
            print(f"{result['content']} in {result['tableName']}")
        
        relevant_results = [r for r in results if r['@search.score'] > 0.02]
        
        if not relevant_results:
            context.append("  No strong matches found in the metadata")
            continue
            
        for result in relevant_results:
            context.append(f"- Found in {result['tableName']}")
            # Add the full content value regardless of format
            context.append(f"  Content: {result['content']}")
            context.append(f"  Table: {result['tableName']}")
            context.append(f"  Confidence Score: {result['@search.score']}")
            context.append("  ---")
    
    dimension_info = "\n".join(context)
    print("dimension_info: ", dimension_info)
    return {"dimension_info": dimension_info}

def get_table_list(cursor, database: str, schema: str) -> List[tuple]:
        query = f"""
            SELECT distinct TABLE_SCHEMA, TABLE_NAME 
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_CATALOG='{database}' AND TABLE_SCHEMA='{schema}'
        """
        cursor.execute(query)
        return cursor.fetchall()

def get_database_info(conn_str: str, database: str, schema: str, table_list: List[str] = None) -> str:
    """
    Get and format database schema information.
    
    Args:
        conn_str (str): Database connection string
        database (str): Database name
        schema (str): Schema name
        table_list (List[str], optional): List of specific tables to query. If None, queries all tables.
    
    Returns:
        str: Formatted database information
    """
    # Connect to database
    full_conn_str = conn_str + f'DATABASE={database};'
    conn = pyodbc.connect(full_conn_str)
    cursor = conn.cursor()
    
    # Get system information
    cursor.execute("select @@VERSION")
    sys_info = cursor.fetchall()
    
    # Get tables and columns
    if table_list is None:
        # Get all tables if no specific tables are provided
        tables = get_table_list(cursor, database, schema)
        table_names = [table[1] for table in tables]
    else:
        # Use the provided table list
        table_names = table_list
        # Verify the tables exist
        tables = []
        for table_name in table_names:
            cursor.execute(f"""
                SELECT TABLE_SCHEMA, TABLE_NAME 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_CATALOG='{database}' 
                AND TABLE_SCHEMA='{schema}' 
                AND TABLE_NAME='{table_name}'
                GROUP BY TABLE_SCHEMA, TABLE_NAME
            """)
            result = cursor.fetchone()
            if result:
                tables.append(result)
            else:
                print(f"Warning: Table '{table_name}' not found in {database}.{schema}")
    
    columns = get_table_columns(cursor, table_names, schema)
    
    cursor.close()
    conn.close()
    
    # Format information with minimal whitespace
    formatted_info = f"""System Info:{sys_info}
Database:{database}
Schema:{schema}
###Tables###
{tables}
###Column-level Info###
{columns}
    """

    # Print the formatted information
    print(formatted_info)

    # Return the formatted information
    return formatted_info


def get_table_samples(cursor, table_names: List[str], schema: str = None) -> str:
    """Get sample records from each table"""
    sample_data = []
    
    for table in table_names:
        table_reference = f"{schema}.{table}" if schema else table
        try:
            query = f"""SELECT TOP 10 * 
FROM {table_reference}
ORDER BY NEWID();"""
            cursor.execute(query)
            rows = cursor.fetchall()
            
            sample_data.append(f"SAMPLE DATA FOR:{table_reference}")
            
            if rows:
                columns = [column[0] for column in cursor.description]
                sample_data.append("Columns:" + "|".join(columns))
                #sample_data.append("-" * 80)
                
                for row in rows:
                    formatted_row = "|".join(str(value)[:100] + '...' if isinstance(value, str) and len(str(value)) > 100 
                                           else str(value) if value is not None else 'NULL'
                                           for value in row)
                    sample_data.append(formatted_row)
            else:
                sample_data.append("No data available in table")
            
            sample_data.append("=" * 80)
            
        except Exception as e:
            sample_data.append(f"Could not retrieve samples:{str(e)}")
            sample_data.append("=" * 80)
    
    return "\n".join(sample_data)

def get_table_columns(cursor, table_names: List[str], schema: str = None) -> str:
    table_columns = {}
    
    for table in table_names:
        table_reference = f"{schema}.{table}" if schema else table
        query = f"""SELECT COLUMN_NAME,DATA_TYPE 
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME='{table}'
{f"AND TABLE_SCHEMA='{schema}'" if schema else ""}
ORDER BY ORDINAL_POSITION"""
        cursor.execute(query)
        rows = cursor.fetchall()
        table_columns[table_reference] = [(row.COLUMN_NAME, row.DATA_TYPE) for row in rows]

    column_info = []
    for table, columns in table_columns.items():
        column_info.append(f"TABLE:{table}")
        for column_name, data_type in columns:
            column_info.append(f"{column_name} - {data_type.lower()}")

    return "\n".join(column_info)




def generate_sql_query(state: ChatInteractionState) -> ChatInteractionState:
    """
    Generate SQL query using Azure OpenAI with structured output.
    """
    import datetime
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")
    current_date = "2024-10-01"

    # Include previous attempts in the prompt if they exist
    previous_attempts_str = ""
    if state["attempt_history"]:
        previous_attempts_str = "\nPrevious attempts and their analysis:\n"
        for attempt in state["attempt_history"]:
            previous_attempts_str += f"""
###Attempt {attempt['attempt_number']}###

SQL Agent Thought Process:
{attempt['sql_agent_thought_process']}

Generated SQL:
{attempt['generated_sql']}

Query Results:
{attempt['query_results']}

Review Agent Analysis:
{attempt['review_agent_thought_process']}

-------------------
"""

    sql_generation_prompt = f"""
    Given a user question and context about available tables and columns, generate a SQL query to answer the question. Only use the views. Respond as follows:

    1. thought_process: Explain your thought process. If there were any previous attempts, reflect on those. What entities are being asked about, how do they relate to the entity & dimension info provided? What options do we have in terms of dimension values to query on and which make the most sense? What tables, columns, and values are relevant?  How would you solve this problem step-by-step (and what tables, columns, values would you use at each step)? Take note of the system information as you will need to use proper syntax to avoid errors. 
    2. answer: Provide the generated SQL query. You must only generate syntactically correct SQL, nothing else (take note of the system information and use that syntax). You MUST only use the tables and columns provided in the context; if it is not listed then it doesn't exist. If you have to query for specific values, make sure to use the entity & dimension info if possible. Make sure to alias columns to avoid ambiguity. Write modular SQL with clear separation of filtering, calculation, and result selection. Your SQL will be executed in the next step.

    You MUST state the verbatim dimension values that you see and plan to use in the entity & dimension info. You will need to use these values exactly as they are in your query otherwise you will likely get zero results.
    """
    
    formatted_input = f"""
    User Question: {state["user_input"]}\n\n
    Current Date: {current_date}\n\n
    ###Database Schema Information###
    {database_info}\n\n
    ###Entity & Dimension Information###
    {state["dimension_info"]}\n\n
    ###Domain Metadata###
    {domain_knowledge}\n\n
    ###Previous Attempts###
    {previous_attempts_str}
    """
    
    messages = [
        {"role": "system", "content": sql_generation_prompt},
        {"role": "user", "content": formatted_input}
    ]

    response = reasoning_llm.invoke(messages)
    attempt_number = len(state["attempt_history"]) + 1
    
    print(f"Attempt {attempt_number} of 3:")
    print("###SQL Agent Thought Process###\n ", response.thought_process)
    print("###Generated SQL###\n", response.answer)
        
    current_attempt: AttemptState = {
        "attempt_number": attempt_number,
        "sql_agent_thought_process": response.thought_process,
        "generated_sql": response.answer,
        "query_results": None,
        "review_agent_thought_process": None
    }
    
    return {"current_attempt": current_attempt}



def execute_sql_query(state: ChatInteractionState) -> dict:
    """Execute SQL query and return updated current_attempt."""
    try:
        full_conn_str = conn_str + f'DATABASE={database_name};'
        conn = pyodbc.connect(full_conn_str)
        cursor = conn.cursor()
        
        current_attempt = state["current_attempt"]
        if not current_attempt:
            raise ValueError("No current attempt found")
            
        cursor.execute(current_attempt["generated_sql"])
        columns = [column[0] for column in cursor.description]
        
        query_results = []
        for row in cursor.fetchall():
            query_results.append(dict(zip(columns, row)))
            
        cursor.close()
        conn.close()
        
        results_str = ""
        for result in query_results:
            results_str += str(result) + "\n"
        
        current_attempt["query_results"] = results_str
        print("Query Results: ", results_str)
        
        return {"current_attempt": current_attempt}
    
    except Exception as e:
        error_message = f"Error executing SQL query: {str(e)}"
        if not state["current_attempt"]:
            raise ValueError("No current attempt found")
            
        current_attempt = state["current_attempt"]
        current_attempt["query_results"] = error_message
        print(error_message)
        
        return {"current_attempt": current_attempt}


def review(state: ChatInteractionState) -> ChatInteractionState:
    """Review the latest attempt and determine if we need to retry."""
    current_attempt = state["current_attempt"]
    if not current_attempt:
        raise ValueError("No current attempt found")

    review_prompt = """Analyze the user question, SQL query, and results to determine if we found the correct answer. Answer as follows:
    
    1. thought_process: What do you see?  Are we fully addressing the user's question? If not, what additional information would we need to provide a complete answer? Give a recommendation to the SQL agent on what to try next (such as adding a group by or querying on a different column)
    2. answer: If we found the answer and feel we have holistically and definitively answered the query, provide a clear, concise answer to the user's question using the query results. If we got an error or are missing data from the user's question, output exactly "retry".

    Tips:

    - If we got 0 records, look back at the entity dimensions info and reflect on them. Is it possible we picked the wrong dimension to query on?
    - State what attempt you are on. You only get 3 attempts. If you are on the third attempt and you have some data, you MUST return that and consider it the true answer! 

    """
    
    previous_attempts_str = ""
    if len(state["attempt_history"]) > 0:  # Check if there are previous attempts excluding current
        previous_attempts_str = "\nPrevious attempts and their analysis:\n"
        for attempt in state["attempt_history"]:
            previous_attempts_str += f"""
###Attempt {attempt['attempt_number']}###

SQL Agent Thought Process:
{attempt['sql_agent_thought_process']}

Generated SQL:
{attempt['generated_sql']}

Query Results:
{attempt['query_results']}

Review Analysis:
{attempt['review_agent_thought_process']}

-------------------
"""


    formatted_input = f"""
    User Question: {state["user_input"]}\n\n
    ###Domain Metadata###
    {domain_knowledge}\n\n
    ###Entity & Dimension Info###\n {state["dimension_info"]}\n\n
     {previous_attempts_str}
    ###Current Attempt - Attempt {current_attempt["attempt_number"]}###
    SQL Agent Thought Process: {current_attempt["sql_agent_thought_process"]}\n\n
    Generated SQL: {current_attempt["generated_sql"]}\n\n
    Query Results: {current_attempt["query_results"]}\n\n
   

    Note - if you are on your 3rd attempt, you must return the data you have and consider it the true answer.
    """

    messages = [
        {"role": "system", "content": review_prompt},
        {"role": "user", "content": formatted_input}
    ]
    
    response = reasoning_llm.invoke(messages)
    current_attempt["review_agent_thought_process"] = response.thought_process
    
    print("Review Agent Thought Process: ", response.thought_process)
    print("\n")
    print("Review Answer: ", response.answer)
    
    state["review_answer"] = response.answer.strip().lower()
    
    # Move current attempt to history before potentially starting a new attempt
    state["attempt_history"].append(current_attempt)
    state["current_attempt"] = None
    return state


def review_router(state: ChatInteractionState) -> str:
    """Route to either retry the query generation or end the process."""
    if not state["attempt_history"]:
        raise ValueError("No attempts in history")
        
    latest_attempt = state["attempt_history"][-1]
    
    if state["review_answer"] == "retry":
        if latest_attempt["attempt_number"] >= 3:
            print("Maximum attempts reached (3). Ending process.")
            return END
        return "retry"
    return END


builder = StateGraph(ChatInteractionState)

# Add nodes to the graph (removed get_database_info)
builder.add_node("entity_extraction", entity_extraction)
builder.add_node("search_dimensions", search_dimensions)
builder.add_node("generate_sql_query", generate_sql_query)
builder.add_node("execute_sql_query", execute_sql_query)
builder.add_node("review", review)

# Define the flow
builder.add_edge(START, "entity_extraction")
builder.add_edge("entity_extraction", "search_dimensions")
builder.add_edge("search_dimensions", "generate_sql_query")
builder.add_edge("generate_sql_query", "execute_sql_query")
builder.add_edge("execute_sql_query", "review")

# Add conditional edge
builder.add_conditional_edges(
    "review",
    review_router,
    {
        "retry": "generate_sql_query",
        END: END
    }
)

# Compile the graph
graph = builder.compile()

selected_tables = ['V_FCT_RAG_MODEL_CONTENT_VIEWS_MOCK', 'V_FCT_RAG_MODEL_SUBS_METRICS_MOCK']
    
    # Get database info for selected tables
database_info = get_database_info(conn_str, database_name, schema_name, table_list=selected_tables)
domain_knowledge = read_metadata_file("domain_knowledge.txt")
#domain_knowledge = ""

if __name__ == "__main__":
    
    
    # Get database info once at startup
    
    #database_info = get_database_info(conn_str, database_name, schema_name)

    #print("Database Info: ", database_info)

    #exit()
    
    while True:
        print("\n" + "="*50)
        user_input = input("Enter your question: ").strip()
        
        if user_input.lower() == 'quit':
            print("Exiting system...")
            break
        
        if not user_input:
            print("Please enter a valid question.")
            continue
        
        
        initial_state = ChatInteractionState(
            user_input=user_input,
            database=database_name,
            schema=schema_name,
            entity_list=[],
            dimension_info="",
            current_attempt=None,
            attempt_history=[]
        )
                    
        final_state = graph.invoke(initial_state)
        
        print("\n=== Query Processing Complete ===")
        
    
       