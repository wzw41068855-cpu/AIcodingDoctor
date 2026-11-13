
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings

import config
from utils.logger import get_logger
from utils.operation_timing import timed, timed_operation

logger = get_logger("vector_store")
class VectorStoreManager:
    """
    AicodingDoctor Chunk 级增量向量索引管理器。

    主要功能：
    1. 初始化 Embedding 和 Chroma。
    2. 保存每个文件的内容 Hash、索引配置与 Chunk ID。
    3. 新文件：生成 Chunk 并写入向量。
    4. 未修改文件：直接跳过，不重复 Embedding。
    5. 修改文件：复用原文不变的 Chunk，仅为新增/变化内容生成向量。
    6. 删除文件：按需删除对应的旧向量。
    7. 根据自然语言执行相似度检索。
    清单 hash 用于文件跳过；Chunk ID 与行号和整文件 hash 无关。

    当前约束：
    - 一个 Collection 对应一个本地代码项目。
    - 只允许一个进程执行索引更新。
    - 清单文件和 Chroma 都保存在本地。
    """

    def __init__(
        self,
        persist_directory: str = config.VECTOR_STORE_PATH,
        collection_name: str = config.VECTOR_COLLECTION_NAME,
    ):
        # 离线服务和内存存储测试不需要加载真实 Chroma 客户端。
        from langchain_chroma import Chroma

        self.collection_name = collection_name
        load_dotenv(config.BASE_DIR / ".env")

        api_key = os.getenv("DASHSCOPE_API_KEY")

        if not api_key:
            raise ValueError("未配置 DASHSCOPE_API_KEY")

        # 1. 初始化 Embedding 模型
        self.embedding_model = OpenAIEmbeddings(
            model=config.EMBEDDING_MODEL,
            api_key=api_key,
            base_url=config.EMBEDDING_BASE_URL,
            check_embedding_ctx_length=False,
            chunk_size=10,# 每次向 Embedding API 发送最多 10 条文本
        )

        # 2. 初始化数据库目录 , 将数据库目录转成绝对路径
        self.persist_directory = (
            Path(persist_directory).expanduser().resolve()
        )
        # 如果目录不存在，则自动创建
        self.persist_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        # 每个 Collection 使用独立的清单
        self.manifest_path = (
            self.persist_directory
            / f"{collection_name}_manifest.json"
        )

        # 3. 创建或加载 Chroma
        self.vector_store = Chroma(
            collection_name=collection_name,
            embedding_function=self.embedding_model,
            persist_directory=str(self.persist_directory),
        )
        logger.info(f"[Chroma] 数据库路径：{self.persist_directory}")
        logger.info(f"[Chroma] Collection_name：{self.collection_name}")

        # 4. 加载历史文件 Hash 清单
        self.manifest = self._load_manifest()

    @staticmethod
    def _sha256(text: str) -> str:
        """计算字符串的 SHA256 哈希值。"""
        return hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()

    def _load_manifest(self) -> dict:
        """
        从 JSON 文件加载历史索引信息。

        如果第一次运行：
            返回空的文件清单。

        如果清单损坏：
            抛出异常，不盲目重新创建。
        """

        if not self.manifest_path.exists():

            # Chroma 有数据但清单丢失时，
            # 不允许直接当作全新项目继续索引。
            # 否则可能出现无法管理的旧向量。
            if self.vector_store.get(
                limit=1,
                include=[],
            )["ids"]:
                raise RuntimeError(
                    "Chroma 已有数据，但索引清单不存在。"
                    "请恢复清单，或确认备份后重建测试集合。"
                )

            return {"files": {}}

        with self.manifest_path.open(
            "r", encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data.get("files"), dict):
            raise ValueError("索引清单格式错误")

        return data

    @timed("索引：原子保存 manifest")
    def _save_manifest(self, manifest: dict):
        """
        原子替换清单文件。

        先写入临时文件，再使用 os.replace() 替换。

        这样可以减少程序中途退出导致
        JSON 文件只写入一半的风险。
        """

        temp_path = self.manifest_path.with_suffix(
            ".json.tmp"
        )

        try:
            with temp_path.open(
                "w", encoding="utf-8"
            ) as f:
                json.dump(
                    manifest,
                    f,
                    ensure_ascii=False,
                    indent=2,
                )

                f.flush()#把 Python 进程层面的缓冲区数据推给操作系统
                os.fsync(f.fileno())#要求操作系统把该文件描述符对应的数据强制同步到磁盘。
                #先保证新内容尽量写实，再原子替换旧清单。
            os.replace(
                temp_path,
                self.manifest_path,
            )

        finally:
            temp_path.unlink(missing_ok=True)

    def _index_signature(self, splitter) -> str:
        """
        为影响向量索引结果的配置生成指纹。

        修改切分参数或 Embedding 模型后(修改options)，
        即使源码没变，也应该重新建立索引。

        如果以后改变代码切分算法，
        需要同步更新下面的版本号。
        """

        options = {
            "embedding_model": config.EMBEDDING_MODEL,
            "embedding_base_url": config.EMBEDDING_BASE_URL,
            "splitter": type(splitter).__name__,#这行代码的作用是把 **切分器对象的“类名”**写进配置字典中。
            "chunk_size": splitter.chunk_size,
            "chunk_overlap": splitter.chunk_overlap,
            "splitter_version": 5,  # 修复特殊字符串字符导致的 AST 行号错位
        }

        return self._sha256(
            json.dumps(options, sort_keys=True)
        )

    def _make_chunk_id(
        self,
        relative_path: str,
        content_hash: str,
        signature: str,
        occurrence: int,
    ) -> str:
        """
        为每个 Chunk 生成稳定 ID。

        文件路径、Chunk 原文哈希、配置与相同内容的出现序号决定 ID。
        不包含文件哈希、行号或全局 Chunk 编号；重复文本仍保留独立记录。

        """

        raw = (
            f"chunk-v2\0{relative_path}\0"
            f"{content_hash}\0"
            f"{signature}\0"
            f"{occurrence}"#occurrence是同一文件中相同内容的第几次出现，保证了即使内容相同但位置不同的 chunk 也能有不同的 ID。
        )

        return self._sha256(raw)

    def _update_metadata(self, ids: list[str], metadatas: list[dict]) -> None:
        """Chroma metadata-only update: never calls the embedding function.

        Chroma merges metadata keys, so explicitly remove obsolete keys with None.
        """
        rows = self.vector_store.get(ids=ids, include=["metadatas"])
        previous = dict(zip(rows["ids"], rows["metadatas"]))
        patches = [{**dict.fromkeys(previous[key] or {}, None), **metadata}
                   for key, metadata in zip(ids, metadatas)]
        self.vector_store._collection.update(ids=ids, metadatas=patches)

    @timed("数据库：检查与清理孤儿 Chunk")
    def _cleanup_orphans(self, manifest: dict):
        """
        清理 Chroma 中不在清单里的记录。

        作用：
        - 移除已被新版本替代的旧 Chunk。
        - 清理上次程序中断时遗留的新 Chunk。

        这里只适用于本 Collection 独占当前项目。
        """

        valid_ids = {
            chunk_id
            for file_info in manifest["files"].values()
            for chunk_id in file_info["ids"]
        }

        # 控制台版数据规模较小，可以一次读取 ID。
        # 这里不获取代码内容或向量。
        stored_ids = set(
            self.vector_store.get(include=[])["ids"]
        )

        orphan_ids = list(stored_ids - valid_ids)
        #存在于vector存储中,但不在json有效ID中的，是孤儿记录，可能是旧版本的chunk，也可能是上次中断时遗留的chunk。

        if orphan_ids:
            # 分批删除，避免单次请求过大
            batch_size = 100

            for i in range(0, len(orphan_ids), batch_size):
                self.vector_store.delete(
                    ids=orphan_ids[i:i + batch_size]
                )

            logger.debug(
                f"[清理] 删除 {len(orphan_ids)} "
                f"条过期或遗留的 Chunk"
            )

    def sync_documents(
        self,
        documents: list[Document],
        splitter,
        remove_missing: bool = False,
        *,
        deleted_paths: list[str] | None = None,
        repository_root: str | None = None,
    ) -> dict[str, int]:
        """同步完整文件文档，按 Chunk 原文复用向量。

        文件 hash 和配置未变时跳过切分；变化文件重新切分，仅嵌入新 ID。
        保留 ID 仅更新元数据，提交清单后清理被移除的向量。
        提交失败时恢复已更新元数据并删除新增向量；回滚失败会记录日志。
        旧版 ID 清单首次同步时迁移；配置变化会生成全新的 ID。
        remove_missing 仅适用于成功的完整扫描；deleted_paths 支持显式删除。
        返回 added/updated/skipped/deleted 文件统计，保持调用接口兼容。
        """

        deleted_paths = set(deleted_paths or [])
        if not documents and (not deleted_paths or remove_missing):
            # 防止因路径错误或加载失败，
            # 误将所有文件识别为删除。
            raise ValueError(
                "没有输入 Document，取消同步"
            )

        if repository_root is not None:
            self.validate_repository(repository_root)
        elif self.manifest.get("repository_root"):
            repository_root = self.manifest["repository_root"]
        elif documents:
            # 旧调用仍可传 Loader 文档，由真实 source 和相对路径推导仓库。
            first = documents[0].metadata
            source, relative = first.get("source"), first.get("relative_path")
            if not source or not relative or Path(relative).is_absolute():
                raise ValueError("无法确认仓库归属，需要 source 和 relative_path")
            inferred = Path(source).resolve()
            for _ in Path(relative).parts:
                inferred = inferred.parent
            repository_root = str(inferred)
            self.validate_repository(repository_root)
        elif deleted_paths:
            raise ValueError("显式删除需要已绑定的仓库或 repository_root")
        if repository_root is not None:
            root = Path(repository_root).resolve()
            for doc in documents:
                relative = doc.metadata.get("relative_path", "")
                if (not relative or Path(relative).is_absolute()
                        or not (root / relative).resolve().is_relative_to(root)
                        or Path(doc.metadata.get("source", "")).resolve() != (root / relative).resolve()):
                    raise ValueError("Document 来源不属于绑定仓库")
        if deleted_paths & {d.metadata.get("relative_path") for d in documents}:
            raise ValueError("同一文件不能同时更新和删除")
        signature = self._index_signature(splitter)

        # 先清理之前中断留下的多余 Chunk
        self._cleanup_orphans(self.manifest)

        # 新清单：复制旧数据，不直接修改 self.manifest
        new_manifest = {
            **self.manifest,
            "files": {
                path: {
                    **info,
                    "ids": list(info["ids"]),
                }
                for path, info in self.manifest["files"].items()
            }
        }
        if repository_root is not None:
            new_manifest["repository_root"] = str(Path(repository_root).resolve())

        stats = {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "deleted": 0,
        }

        # 当前扫描到的文件路径
        current_paths = set()

        # 这次新增的 ID。
        # 如果中途失败，用于尝试回滚。
        staged_ids = []
        metadata_updates = []
        applied_metadata = []

        try:
            for doc in documents:

                relative_path = doc.metadata.get(
                    "relative_path"
                )

                if not relative_path:
                    raise ValueError(
                        "Document 缺少 relative_path 元数据"
                    )

                if relative_path in current_paths:
                    raise ValueError(
                        f"重复文件路径：{relative_path}"
                    )

                current_paths.add(relative_path)

                # 1. 计算当前文件的内容 Hash
                file_hash = self._sha256(
                    doc.page_content
                )

                # 2. 获取旧索引信息
                old_info = self.manifest["files"].get(
                    relative_path
                )

                # 3. 文件和配置都没有变化，直接跳过
                if (
                    old_info is not None
                    and old_info["hash"] == file_hash
                    and old_info.get("signature") == signature
                    and old_info.get("chunk_id_version") == 2
                ):
                    stats["skipped"] += 1
                    logger.debug(f"[跳过] {relative_path}")
                    continue

                # 4. 只切分这个发生变化的文件
                with timed_operation("索引：切分单个变更文件"):
                    chunks = splitter.split([doc]) if doc.page_content.strip() else []

                from rag.splitter.ast_segmenter import has_python_code
                if (not chunks and doc.page_content.strip()
                        and not (doc.metadata.get("file_type") == ".py"
                                 and not has_python_code(doc.page_content))):
                    raise ValueError(
                        f"文件未生成有效 Chunk：{relative_path}"
                    )

                # 5. Match unchanged content independently of physical position.
                chunk_ids = []
                occurrences = {}
                old_ids = set(old_info["ids"]) if old_info else set()
                rows = self.vector_store.get(ids=list(old_ids), include=["metadatas"]) if old_ids else {"ids": [], "metadatas": []}
                old_metadata = {key: dict(metadata or {})
                                for key, metadata in zip(rows["ids"], rows["metadatas"])}
                added_chunks, added_ids = [], []

                for index, chunk in enumerate(chunks):

                    content_hash = self._sha256(chunk.page_content)
                    occurrence = occurrences.get(content_hash, 0)
                    occurrences[content_hash] = occurrence + 1
                    chunk_id = self._make_chunk_id(
                        relative_path,
                        content_hash,
                        signature,
                        occurrence,
                    )

                    chunk.metadata["file_hash"] = file_hash
                    chunk.metadata["relative_path"] = relative_path

                    chunk_ids.append(chunk_id)
                    if chunk_id in old_metadata:
                        if old_metadata[chunk_id] != chunk.metadata:
                            metadata_updates.append((chunk_id, old_metadata[chunk_id], dict(chunk.metadata)))
                    else:
                        added_chunks.append(chunk)
                        added_ids.append(chunk_id)

                # 6. 先写新向量，不提前删除旧向量
                #
                # 如果 Embedding 失败，旧向量还在。
                #
                # 即使 Chroma 中已有部分新 ID，
                # 清单仍然没有提交。
                staged_ids.extend(added_ids)

                if added_chunks:
                    # Chroma 在此同步调用 Embedding，故本范围明确包含网络和数据库写入。
                    with timed_operation("索引：文件 Embedding 与 Chroma 写入"):
                        self.vector_store.add_documents(
                            documents=added_chunks,
                            ids=added_ids,
                        )

                # 7. 更新内存中的新清单
                new_manifest["files"][relative_path] = {
                    "hash": file_hash,
                    "signature": signature,
                    "ids": chunk_ids,
                    "chunk_id_version": 2,
                }

                if old_info is None:
                    stats["added"] += 1
                    logger.debug(f"[新增] {relative_path}")
                else:
                    stats["updated"] += 1
                    logger.debug(f"[更新] {relative_path}")

            # 8. 处理已经从项目中删除的文件
            for path in deleted_paths:
                if path in new_manifest["files"]:
                    del new_manifest["files"][path]
                    stats["deleted"] += 1
            if remove_missing:

                old_paths = set(
                    self.manifest["files"]
                )

                missing_paths = old_paths - current_paths - deleted_paths

                for path in missing_paths:
                    del new_manifest["files"][path]
                    stats["deleted"] += 1
                    logger.debug(f"[移除索引] {path}")

            # 9. 新向量全部写入成功后，
            # 才正式提交 JSON 清单
            for key, before, after in metadata_updates:
                # Record before calling: a backend may fail after applying the update.
                applied_metadata.append((key, before))
                self._update_metadata([key], [after])
            self._save_manifest(new_manifest)

        except Exception:
            for key, before in reversed(applied_metadata):
                try:
                    self._update_metadata([key], [before])
                except Exception:
                    logger.exception("[警告] Chunk 元数据回滚失败：%s", key)
            # 清单没有成功提交时，
            # 尝试删除此次新写入的记录。
            #
            # 回滚失败也不能隐藏原始异常：
            # 下次同步会再次清理孤儿记录。
            try:
                if staged_ids:
                    self.vector_store.delete(
                        ids=staged_ids
                    )
            except Exception as cleanup_error:
                logger.exception(
                    "[警告] 回滚清理失败：%s",
                    cleanup_error,
                )

            raise

        # 10. 清单提交成功后，以新清单为准
        self.manifest = new_manifest

        # 删除不再属于新版本的旧 Chunk
        # 如果清理失败，下次同步还会再次清理。
        self._cleanup_orphans(self.manifest)

        logger.debug(
            "\n[索引同步完成] "
            f"新增 {stats['added']}，"
            f"更新 {stats['updated']}，"
            f"跳过 {stats['skipped']}，"
            f"删除 {stats['deleted']}"
        )

        return stats

    def validate_repository(self, repository_root: str) -> None:
        """只读验证 Collection 归属；旧清单必须能由全部 Chunk 来源证明。"""
        root = Path(repository_root).resolve(strict=True)
        owner = self.manifest.get("repository_root")
        if owner is not None:
            if Path(owner).resolve() != root:
                raise ValueError("Collection 已绑定其他仓库，请显式选择独立 Collection")
            return
        for relative, info in self.manifest["files"].items():
            expected = (root / relative).resolve()
            if not expected.is_relative_to(root) or not info["ids"]:
                raise ValueError("旧索引无法验证仓库归属，请使用独立 Collection")
            rows = self.vector_store.get(ids=info["ids"], include=["metadatas"])
            if set(rows["ids"]) != set(info["ids"]):
                raise ValueError("旧索引缺少 Chunk，无法验证仓库归属")
            for metadata in rows["metadatas"]:
                if not metadata or not metadata.get("source") or Path(metadata["source"]).resolve() != expected:
                    raise ValueError("旧索引来源与目标仓库不一致")

    @timed("检索：查询 Embedding 与 Chroma 向量查询")
    def similarity_search(
        self,
        query: str,
        k: int = 5,
        filter: dict[str, Any] | None = None,
    ) -> list[Document]:
        """根据自然语言检索最相关的代码 Chunk。"""

        if not query.strip():
            raise ValueError("查询文本不能为空")

        if k <= 0:
            raise ValueError("k 必须大于 0")

        return self.vector_store.similarity_search(
            query=query,
            k=k,
            filter=filter,#filter 是元数据过滤条件（metadata filter）。
            #eg：只搜 Python 文件
            #filter = {"file_type": ".py"}
        )

    def clear_all(self) -> bool:
        """清空当前 Collection 及对应的索引清单。"""
        from langchain_chroma import Chroma

        # 1. 删除 Chroma Collection
        self.vector_store.delete_collection()

        # 2. 删除 JSON 索引清单
        self.manifest_path.unlink(missing_ok=True)

        # 3. 清空内存中的清单
        self.manifest = {"files": {}}

        # 4. 重新创建 Collection
        self.vector_store = Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embedding_model,
            persist_directory=str(self.persist_directory),)

        logger.info("[Chroma] 索引已清空")
