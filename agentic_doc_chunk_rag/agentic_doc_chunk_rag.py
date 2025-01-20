from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from azure.core.credentials import AzureKeyCredential
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
from pydantic import BaseModel
from typing import List, Dict, Any, Union, Literal
from dotenv import load_dotenv
import os
from langgraph.graph import StateGraph, START, END
from typing import Dict, Any, TypedDict, Set

load_dotenv()

# Azure Search configuration
ai_search_endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
ai_search_key = os.environ["AZURE_SEARCH_KEY"]
ai_search_index = "agentic-doc-index"

# Azure OpenAI configuration
aoai_deployment = os.getenv("AOAI_DEPLOYMENT")
aoai_key = os.getenv("AOAI_KEY")
aoai_endpoint = os.getenv("AOAI_ENDPOINT")

search_client = SearchClient(ai_search_endpoint, ai_search_index, AzureKeyCredential(ai_search_key))

# Type Definitions
class SearchResult(TypedDict):
    id: str
    content: str
    filepath: str
    chunk_number: int
    score: float

class ReviewDecision(BaseModel):
    """Schema for review agent decisions"""
    thought_process: str
    valid_results: List[int]  # Indices of valid results
    invalid_results: List[int]  # Indices of invalid results
    decision: Literal["retry", "finalize"]
    missing_aspects: str  # What information we still need if retrying

class ChatState(TypedDict):
    """Complete state of the conversation"""
    user_input: str
    current_results: List[SearchResult]
    vetted_results: List[SearchResult]
    discarded_results: List[SearchResult]
    processed_ids: Set[str]  # Track all processed document IDs
    reviews: List[str]  # Thought processes from reviews
    final_answer: str | None

# LLM Setup
llm = AzureChatOpenAI(
    azure_deployment=aoai_deployment,
    api_version="2024-05-01-preview",
    temperature=0,
    max_tokens=None,
    timeout=None,
    max_retries=2,
    api_key=aoai_key,
    azure_endpoint=aoai_endpoint
)

review_llm = llm.with_structured_output(ReviewDecision)


embeddings_model = AzureOpenAIEmbeddings(
    azure_deployment="text-embedding-ada-002",
    api_key=aoai_key,
    azure_endpoint=aoai_endpoint
)

def generate_search_query(state: ChatState) -> ChatState:
    """
    Generate an optimized search query based on the current state.
    """
    query_prompt = """Generate a focused search query based on the user's question and what we've learned from previous searches.

    User Question: {user_input}

    Previous Review Analysis:
    {reviews}
    
    Your task:
    1. Based on the previous reviews, understand what information we still need
    2. Generate a targeted search query to find the missing information
    3. Return just the search query, nothing else
    """
    
    messages = [
        {"role": "system", "content": query_prompt},
        {"role": "user", "content": query_prompt.format(
            user_input=state["user_input"],
            reviews="\n".join(state["reviews"])
        )}
    ]
    
    search_query = llm.invoke(messages).content
    print(f"\nGenerated search query: {search_query}")
    
    # Perform the search with the generated query
    query_vector = embeddings_model.embed_query(search_query)
    
    vector_query = VectorizedQuery(
        vector=query_vector,
        k_nearest_neighbors=5,
        fields="contentVector"
    )
    
    # Filter out already processed documents
    filter_str = None
    if state["processed_ids"]:
        id_list = "','".join(state["processed_ids"])
        filter_str = f"id not in ('{id_list}')"
    
    results = search_client.search(
        search_text=search_query,
        vector_queries=[vector_query],
        filter=filter_str,
        select=["id", "content", "filepath", "chunk_number"],
        top=5
    )
    
    current_results = []
    for result in results:
        search_result = SearchResult(
            id=result["id"],
            content=result["content"],
            filepath=result["filepath"],
            chunk_number=result["chunk_number"],
            score=result["@search.score"]
        )
        current_results.append(search_result)
    
    state["current_results"] = current_results
    return state

def review_results(state: ChatState) -> ChatState:
    """
    Review current results and categorize them as valid or invalid.
    """
    review_prompt = """Review these search results and determine which are relevant to answering the user's question.

    User Question: {question}

    Current Search Results:
    {current_results}

    Previously Vetted Results:
    {vetted_results}

    Previous Reviews:
    {reviews}

    Respond with:
    1. thought_process: Your analysis of the results
    2. valid_results: List of indices (0-4) for useful results
    3. invalid_results: List of indices (0-4) for irrelevant results
    4. decision: Either "retry" if we need more info or "finalize" if we can answer the question
    5. missing_aspects: What specific information we still need (if retrying)
    """
    
    messages = [
        {"role": "system", "content": review_prompt},
        {"role": "user", "content": review_prompt.format(
            question=state["user_input"],
            current_results="\n".join([
                f"{i}. {r['content'][:200]}..." 
                for i, r in enumerate(state["current_results"])
            ]),
            vetted_results="\n".join([
                f"- {r['content'][:200]}..." 
                for r in state["vetted_results"]
            ]) if state["vetted_results"] else "None yet",
            reviews="\n".join(state["reviews"])
        )}
    ]
    
    review = review_llm.invoke(messages)
    print(f"\nReview thought process: {review.thought_process}")
    print(f"Decision: {review.decision}")
    
    # Update state based on review
    state["reviews"].append(review.thought_process)
    
    # Add valid results to vetted_results
    for idx in review.valid_results:
        result = state["current_results"][idx]
        state["vetted_results"].append(result)
        state["processed_ids"].add(result["id"])
    
    # Add invalid results to discarded_results
    for idx in review.invalid_results:
        result = state["current_results"][idx]
        state["discarded_results"].append(result)
        state["processed_ids"].add(result["id"])
    
    # Clear current results
    state["current_results"] = []
    
    if review.decision == "finalize":
        # Generate final answer
        final_prompt = """Create a comprehensive answer to the user's question using the vetted search results.

        User Question: {question}

        Vetted Results:
        {vetted_results}

        Provide:
        1. thought_process: How you're synthesizing the information
        2. answer: Clear, complete answer to the user's question
        """
        
        messages = [
            {"role": "system", "content": final_prompt},
            {"role": "user", "content": final_prompt.format(
                question=state["user_input"],
                vetted_results="\n".join([
                    f"- {r['content']}" for r in state["vetted_results"]
                ])
            )}
        ]
        
        final = llm.invoke(messages)
        state["final_answer"] = final.answer
    
    return state

def finalize(state: ChatState) -> ChatState:
    """Generate final answer from vetted results."""
    final_prompt = """Create a comprehensive answer to the user's question using these vetted results.

    User Question: {question}

    Vetted Results:
    {vetted_results}

    Synthesize these results into a clear, complete answer."""
    
    messages = [
        {"role": "system", "content": final_prompt},
        {"role": "user", "content": final_prompt.format(
            question=state["user_input"],
            vetted_results="\n".join([
                f"- {r['content']}" for r in state["vetted_results"]
            ])
        )}
    ]
    
    state["final_answer"] = llm.invoke(messages).content
    return state

def review_router(state: ChatState) -> str:
    """Route to either retry search or go to finalize node."""
    # Check the decision from the last review
    last_review = state["reviews"][-1]
    if "decision: finalize" in last_review.lower():
        return "finalize"
    return "retry"

def build_graph() -> StateGraph:
    """Build the workflow graph."""
    builder = StateGraph(ChatState)
    
    # Add nodes
    builder.add_node("generate_search_query", generate_search_query)
    builder.add_node("review_results", review_results)
    builder.add_node("finalize", finalize)
    
    # Define the flow
    builder.add_edge(START, "generate_search_query")
    builder.add_edge("generate_search_query", "review_results")
    
    # Add conditional edges from review
    builder.add_conditional_edges(
        "review_results",
        review_router,
        {
            "retry": "generate_search_query",
            "finalize": "finalize"
        }
    )
    
    # Final node goes to END
    builder.add_edge("finalize", END)
    
    return builder.compile()

if __name__ == "__main__":
    # Initialize graph
    graph = build_graph()
    
    while True:
        print("\n" + "="*50)
        user_input = input("Enter your question (or 'quit' to exit): ").strip()
        
        if user_input.lower() == 'quit':
            print("Exiting system...")
            break
        
        if not user_input:
            print("Please enter a valid question.")
            continue
        
        # Initialize state
        initial_state = ChatState(
            user_input=user_input,
            current_results=[],
            vetted_results=[],
            discarded_results=[],
            processed_ids=set(),
            reviews=[],
            final_answer=None
        )
        
        # Process query
        final_state = graph.invoke(initial_state)
        
        if final_state["final_answer"]:
            print("\n=== Final Answer ===")
            print(final_state["final_answer"])
        else:
            print("\nUnable to find a satisfactory answer after maximum attempts.")