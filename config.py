"""V4 主链路共享配置（凭据从运行环境读取，不在此文件硬编码）。"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 代码索引与检索
VECTOR_STORE_PATH = str(BASE_DIR / "data" / "chroma")
VECTOR_COLLECTION_NAME = "AI_Coding_Doctor"
EMBEDDING_MODEL = "text-embedding-v3"
EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
RERANK_MODEL = "gte-rerank-v2"
HYBRID_DENSE_K = 12
HYBRID_BM25_K = 12
HYBRID_RRF_K = 12
HYBRID_TOP_K = 3
HYBRID_RRF_C = 60

# Web LLM 默认值
LLM_MODEL = "qwen3.8-max"
LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MAX_TOKENS = 4096

# 检索日志
DEBUG_MODE = True
LOG_TO_FILE = True
