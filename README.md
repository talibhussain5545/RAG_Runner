# Agentic RAG Implementations

This repository contains multiple implementations of Agentic RAG (Retrieval-Augmented Generation) systems, each designed for different data sources.

1. **Document Search**: Extends traditional RAG against document chunks with a multi-attempt, iterative refinement process.
2. **Natural Language to SQL (NL2SQL)**: Extends traditional NL2SQL by adding reflection across multiple attempts.

## What is Agentic RAG?

Agentic RAG extends traditional RAG systems by adding multiple attempts with reflection and iteration. Instead of performing a single retrieval-generation cycle, these systems can:

- Maintain state across multiple iterations
- Analyze the completeness and quality of retrieved information
- Make decisions about whether to continue searching
- Refine their retrieval approach based on previous attempts
- Self-correct when errors or gaps are detected

## Why Agentic RAG?

Traditional, single-attempt RAG systems often yield less-than-ideal results. I've written several articles exploring this topic in depth:

- [My First RAG Use Case: Key Insights & Lessons Learned](https://www.linkedin.com/pulse/my-first-rag-use-case-key-insights-lessons-learned-dan-giannone-sa7ne/)
- [Evaluating RAG Applications: A Deep Dive](https://www.linkedin.com/pulse/evaluating-rag-applications-deep-dive-dan-giannone-qonce/)
- [Non-Technical Challenges of RAG](https://www.linkedin.com/pulse/non-technical-challenges-rag-dan-giannone-st5ze/)

Agentic RAG greatly improves result quality by bringing agentic behavior to the retrieval process. We give the system the ability to make multiple attempts, reflect, adjust, and iterate. As with a human, doing these things yields a much higher quality result. 

## Common Complaints

### "This will impact cost and latency"

In my opinion, accuracy trumps all other considerations when building Gen AI applications. If users don't feel confident in the results, they'll stop using the application—making your efforts to reduce latency and cost meaningless. We need to establish a high degree of confidence in the results before optimizing other considerations.

Additionally, we can put different LLMs in different roles to optimize for cost and latency:
- Use smaller LLMs for final result synthesis
- Use larger LLMs for review and reflection

### "Agentic RAG is a band-aid solution; We should be able to solve this with a single attempt"

This is partially true. You should optimize your retrieval process to be as accurate as possible on the first attempt. However, in reality:
- Data is often messy and complex
- Information has interconnected relationships
- A single attempt rarely yields complete and accurate results at an acceptable rate

### "This will hamper the user experience"

We should be building UIs for the Agent Era. For example, a UI that shows the agent working through the retrieval & iteration process will be much more engaging than a traditional "Loading..." animation. 

## Key Features

Both implementations share core "agentic" characteristics:

- **Stateful Processing**: Maintains context and progress across iterations
- **Iterative Refinement**: Multiple attempts to get complete and accurate results
- **Self-Review**: Analyzes own outputs for completeness and correctness
- **Thought Process Logging**: Maintains clear reasoning chains for transparency
- **Smart Filtering**: Avoids reprocessing previously seen information

## Implementation Summaries

### Document Search System
- Focuses on comprehensive document retrieval
- Maintains lists of vetted and discarded results
- Uses LLM for query generation and result synthesis
- Iterates until all relevant information is found
- [See implementation-specific documentation here](./agentic_doc_chunk_rag/agentic_doc_chunk_rag.md)

### Natural Language to SQL (NL2SQL)
- Converts natural language to SQL queries
- Uses vector search to map user terms to database values
- Supports domain-specific knowledge integration
- Iteratively refines SQL queries until correct
- [See implementation-specific documentation here](./nl2sql/agentic_nl2sql.md)

## Architecture Overview

Both systems follow a similar high-level pattern while specializing for their specific use cases:
```mermaid
Graph TD
    A[User Input] --> B[Initial Processing]
    B --> C[Retrieval/Generation]
    C --> D[Review & Analysis]
    D -->|Need More Info| B
    D -->|Complete| E[Final Output]
```

## Getting Started

1. Choose the implementation that matches your use case:
   - Use **Document Search** for unstructured document retrieval and synthesis
   - Use **NL2SQL** for structured database querying with natural language

2. Follow the setup instructions in the respective implementation directories:
   - [Document Search Setup](./agentic_doc_chunk_rag/agentic_doc_chunk_rag.md#setup--usage)
   - [NL2SQL Setup](./nl2sql/agentic_nl2sql.md#setup--usage)

## Repository Structure
```plaintext
agentic-rag/
├── README.md                         # This file
├── requirements.txt                  # Main requirements file
├── example.env                       # Example environment variables
├── .env                             # Environment variables (not in repo)
├── agentic_doc_chunk_rag/           # Document search implementation
│   ├── agentic_doc_chunk_rag.md     # Implementation details
│   └── agentic_doc_chunk_rag.py     # Main implementation
└── nl2sql/                          # NL2SQL implementation
    ├── agentic_nl2sql.md            # Implementation details
    ├── agentic_nl2sql.py            # Main implementation
    ├── vectorize-sql-data.py        # Data vectorization utility
    └── domain_knowledge.txt         # Domain-specific knowledge
```

## Contributing

Contributions are welcome! Please read our contributing guidelines and submit pull requests for any enhancements, bug fixes, or documentation improvements.

## License

This project is licensed under the MIT License - see the LICENSE file for details.