"""
This module provides functions for refining and processing documents
using a language model.
"""

from functools import partial
from operator import itemgetter

from langchain_core.callbacks.manager import trace_as_chain_group
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import format_document, PromptTemplate
from langchain_core.runnables import RunnableParallel, RunnablePassthrough
from langchain.chains.combine_documents import (
    collapse_docs,
    split_list_of_docs,
)


document_prompt = PromptTemplate.from_template("{page_content}")
partial_format_doc = partial(format_document, prompt=document_prompt)


def docs_refine(llm, docs, initial_prompt, refine_prompt):
    """
    Refines documents by applying an initial prompt followed by a
    refine prompt.

    Args:
        llm: The language model used for processing.
        docs: A list of documents to refine.
        initial_prompt: The initial prompt template for processing the first
                        document.
        refine_prompt: The prompt template for refining subsequent documents.

    Returns:
        str: The refined context after processing all documents.
    """
    _initial_prompt = PromptTemplate.from_template(initial_prompt)
    initial_chain = (
            {'context': partial_format_doc}
            | _initial_prompt
            | llm
            | StrOutputParser()
            )
    _refine_prompt = PromptTemplate.from_template(refine_prompt)
    refine_chain = (
            {
                'prev_context': itemgetter('prev_context'),
                'context': lambda param: partial_format_doc(param['doc']),
            }
            | _refine_prompt
            | llm
            | StrOutputParser()
            )

    with trace_as_chain_group('refine loop', inputs={'input': docs}) as \
            manager:
        context = initial_chain.invoke(docs[0], config={'callbacks': manager})
        for doc in docs[1:]:
            context = refine_chain.invoke(
                    {"prev_context": context, "doc": doc},
                    config={"callbacks": manager}
                    )
            manager.on_chain_end({"output": context})
    return context


def docs_map_reduce(llm, docs, map_prompt, reduce_prompt):
    """
    Applies a map-reduce strategy to process and summarize a list of documents.

    Args:
        llm: The language model used for processing.
        docs: A list of documents to process.
        map_prompt: The prompt template for mapping individual documents.
        reduce_prompt: The prompt template for reducing the results.

    Returns:
        str: The final result after mapping and reducing the documents.
    """
    _map_prompt = PromptTemplate.from_template(map_prompt)
    map_chain = (
            {'context': partial_format_doc}
            | _map_prompt
            | llm
            | StrOutputParser()
            )
    map_as_doc_chain = (
            RunnableParallel({'doc': RunnablePassthrough(),
                              'content': map_chain})
            | (lambda param: Document(page_content=param['content']))
            )

    def format_docs(docs):
        return "\n\n".join(partial_format_doc(doc) for doc in docs)

    _collapse_prompt = PromptTemplate.from_template('Collapse this '
                                                    'content:\n\n{context}')
    collapse_chain = (
            {"context": format_docs}
            | _collapse_prompt
            | llm
            | StrOutputParser()
            )

    def get_num_tokens(docs):
        return llm.get_num_tokens(format_docs(docs))

    def collapse(docs, config, token_max=3072):
        while get_num_tokens(docs) > token_max:
            invoke = partial(collapse_chain.invoke, config=config)
            split_docs = split_list_of_docs(docs, get_num_tokens, token_max)
            docs = [collapse_docs(_docs, invoke) for _docs in split_docs]
        return docs

    _reduce_prompt = PromptTemplate.from_template(reduce_prompt)
    reduce_chain = (
            {"context": format_docs}
            | _reduce_prompt
            | llm
            | StrOutputParser()
            )
    map_reduce = map_as_doc_chain.map() | collapse | reduce_chain

    return map_reduce.invoke(docs, config={"max_concurrency": 5})
