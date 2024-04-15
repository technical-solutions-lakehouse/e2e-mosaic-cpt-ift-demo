import functools
import re
import io
from typing import Iterator, List
import pandas as pd
from langchain_community.document_loaders import GutenbergLoader
from langchain_core.documents import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.vectorstores import FAISS
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseLanguageModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import (
    RunnableParallel,
    RunnablePassthrough,
    RunnableLambda,
)
from langchain_core.vectorstores import VectorStore
from langchain_text_splitters import TextSplitter
from pyspark.sql import DataFrame
from pyspark.sql.functions import col, explode
from pyspark.sql.pandas.functions import pandas_udf
from transformers import AutoTokenizer
from langchain.prompts import PromptTemplate
from langchain.chat_models import ChatDatabricks
from langchain.embeddings import DatabricksEmbeddings
from unstructured.partition.pdf import partition_pdf

from finreganalytics.utils import get_spark


def load_and_clean_data(source_folder: str) -> DataFrame:
    """
    Loads PDFs from the specified folder

    :param source_folder: Folder with PDFs.
    :return: List of `Documents`
    """
    df = (
        get_spark()
        .read.format("binaryFile")
        .option("pathGlobFilter", "*.pdf")
        .load(source_folder)
        .repartition(20)
    )

    def clean(txt):
        txt = re.sub(r"\n", "", txt)
        return re.sub(r" ?\.", ".", txt)

    def parse_and_clean_one_pdf(b: bytes) -> str:
        chunks = partition_pdf(file=io.BytesIO(b))
        return "\n".join([clean(s.text) for s in chunks])

    @pandas_udf("string")
    def parse_and_clean_pdfs_udf(
        batch_iter: Iterator[pd.Series],
    ) -> Iterator[pd.Series]:
        for series in batch_iter:
            yield series.apply(parse_and_clean_one_pdf)

    return df.select(col("path"), parse_and_clean_pdfs_udf("content").alias("text"))


def split(df: DataFrame, hf_tokenizer_name: str, chunk_size: int) -> DataFrame:
    """
    Splits documents into chunks of specified size
    :param docs: list of Documents to split
    :param hf_tokenizer_name: name of the tokenizer to use to count actual tokens
    :param chunk_size: size of chunk
    :return: list of chunks
    """

    def split(text: str, splitter: TextSplitter) -> List[str]:
        return [
            doc.page_content
            for doc in splitter.split_documents([Document(page_content=text)])
        ]

    @pandas_udf("array<string>")
    def split_udf(batch_iter: Iterator[pd.Series]) -> Iterator[pd.Series]:
        text_splitter = RecursiveCharacterTextSplitter.from_huggingface_tokenizer(
            AutoTokenizer.from_pretrained(hf_tokenizer_name),
            chunk_size=chunk_size,
            chunk_overlap=int(chunk_size / 10),
            add_start_index=True,
            strip_whitespace=True,
        )
        for series in batch_iter:
            yield series.apply(functools.partial(split, splitter=text_splitter))

    return df.select(col("path"), explode(split_udf("text")).alias("text"))
