# V0.2.2：AST-first 代码切分（用于与 V0.2.1 对比）

注释边界修复：当前 `splitter_version=4`。同缩进的前置说明注释与函数、
方法或类声明合并（包括装饰器），范围不越过前一个兄弟语句。
许可证、编码声明、shebang 和常见工具指令注释不会被移入声明。
纯注释/空白段不进入 Python AST-first 索引，模块文档字符串保留；
超长段二次切分后也执行相同过滤。Markdown 和语法错误回退保持原逻辑。

修复后必须执行 `manager.sync_documents(documents=documents, splitter=splitter)`。
签名升级会触发未修改文件的重新切分和 Embedding。若保留 V0.2.2 原始 AST-first
索引用于 A/B 比较，请先为修复版选择独立 Collection；同步当前集合会替换旧数据。

## 1. 为什么修改

旧版流程：`整文件 -> PythonCodeTextSplitter -> 按行号 AST 打标签`。
新版流程：`整文件 -> ast.parse -> 函数/类方法/模块代码分段 -> 超长分段使用 PythonCodeTextSplitter -> 行号和 metadata`。

新版的普通短函数不会和相邻函数混合；`chunk_size=1000` **单位仍然是字符，不是 Token**。对于超长函数，继续复用 LangChain 自带的 Python 切分器，而不是自己实现文本递归算法。

## 2. 修改范围

- **新增** `rag/splitter/ast_segmenter.py`：使用标准库 `ast` 确定模块和类中的函数/方法边界，保留原始字符区间。
- **修改** `rag/splitter/code_splitter.py`：先 AST 分段；每个分段在超过 `chunk_size` 时使用 `PythonCodeTextSplitter` 二次切分；保持 `CodeSplitter.split(documents)` 接口不变。
- **修改** `rag/vector_store.py`：原版 `splitter_version=3`，注释修复后为 4，防止旧文件 Hash 不变导致跳过新切分。
- **修改** `config.py`：使用独立的 `AI_JueJin_ASTFirst` Collection，避免与旧 `AI_JueJin` 向量数据混在一起。
- **修改** `main.py`：`my_test_vector_store()` 使用配置的 Collection 名称，不再硬编码旧集合。
- **新增** `tests/test_ast_first_segmenter.py`、`tests/test_ast_first_code_splitter.py`：源码覆盖、嵌套类、装饰器、CRLF、回退及长函数切分测试。
- **新增** `scripts/export_chunk_report.py` 与 `scripts/compare_chunk_reports.py`：可对同一个文件导出切分报告并汇总对比，无需调用大模型。

Agent、RAGChain、`create_retriever_tool`、日志、Middleware、Embedding 模型没有修改。`ast_tagger.py` 保留，用于 Markdown 默认字段以及 Python 解析失败时的普通切分回退。

## 3. AST-first 的具体行为

1. 顶层函数、异步函数各形成一个 AST 分段。
2. 类不再整体与各方法重复入库：类声明/类属性/文档字符串等划为类级分段，各方法单独分段。
3. 顶层 `import`、常量、表达式归入 module 分段；前置说明注释归入相应声明，独立纯注释段不索引。
4. `@decorator` 所在行划给函数或类，不单独拆散。
5. 嵌套函数默认留在外部函数段中，避免重复索引；嵌套类依次递归识别。
6. AST 分段小于阈值则保持完整；超过阈值才使用原始的 LangChain PythonCodeTextSplitter。
7. Python 语法错误时退化为旧版 PythonCodeTextSplitter，然后复用旧版 AstTagger 默认标签。
8. Markdown 按旧方式切分，保持原有字段。

新增 metadata：`ast_segment_index`、`ast_segment_type`、`ast_part_index`、`ast_part_count`；原有 `qualified_name`、`symbol_name`、`start_index`、`end_index`、`start_line`、`end_line` 等仍然保留。`start_line`/`end_line` 是闭区间。

**注意：** 对于 AST-first 的 Chunk，`symbol_names` 代表其主分段的限定名称（通常只有一个），而不是旧版那种“所有与 Chunk 行范围重叠的符号名称”列表。这有助于比较标签纯度，但不是精确的全项目符号索引。某个方法内部的嵌套函数仍属于外层方法的 Chunk。

## 4. 如何执行（先测试再更新向量索引）

在 PyCharm 项目根目录运行：

```bash
python -m unittest discover -s tests -p test_ast_first_segmenter.py -v
python -m unittest discover -s tests -p test_ast_first_code_splitter.py -v
```

第三方依赖通过 `pip install -r requirements.txt` 安装。AST 纯测试不依赖 LangChain，集成测试依赖 LangChain。

随后使用原来的 Loader/VectorStoreManager 增量同步流程。因版本号升级，**第一次索引需要重新生成 Embedding，可能产生费用**。

### 不调用 API 的公平 A/B 对比

1. 保留备份的 V0.2.1 目录，两个项目使用**相同源文件**、`chunk_size=1000`、`chunk_overlap=150`。
2. 把新版 `scripts/export_chunk_report.py` **复制一份**到旧版项目的 `scripts/` 下（脚本仅依赖共有的 `CodeSplitter` 接口，不会修改旧项目的切分逻辑）。
3. 在 **旧版项目根目录**执行：

```bash
python scripts/export_chunk_report.py --source "路径/你的代码.py" --output old_chunks.json --size 1000 --overlap 150
```

4. 在 **新版项目根目录**执行：

```bash
python scripts/export_chunk_report.py --source "路径/你的代码.py" --output new_chunks.json --size 1000 --overlap 150
python scripts/compare_chunk_reports.py old_chunks.json new_chunks.json
```

对比报告包括 Chunk 数量、平均/中位字符数、各 Chunk 的行号、符号、正文预览。**不要仅凭 Chunk 数量判断好坏**；更重要的是相同问题能否命中正确函数、Top-K 是否包含完整相关代码、LLM 是否能正确回答。

### 检索问答对比

旧版 Collection 为 `AI_JueJin`；新版为 `AI_JueJin_ASTFirst`。两个版本如使用同一个 Chroma 目录，也不会覆盖彼此数据。两个集合的检索问题、Embedding 模型和 `k` 应保持一致。完整运行前要先同步新版索引，不能直接对空集合问答。

## 5. 当前取舍

- 有些仅 1～2 行的短函数会独立成 Chunk，向量数量可能增加；第一版不合并短函数，保证函数级完整性。
- 类级别的声明、类变量和方法可能位于不同 Chunk；通过 `parent_class`、`qualified_name` 帮助 LLM 了解所属关系，必要时下一阶段新增 `read_code_file` 读取完整上下文。
- 顶层 `if`、`try` 等内部可能包含条件定义，第一版视为模块代码，不递归拆分，以避免复杂的条件结构重复切分。
- 二次切分后的相邻部分存在 `chunk_overlap` 重叠，这是预期行为；它们会共享同一 `qualified_name`。
- 如果调整 AST 规则或分段算法，应再次升级 `splitter_version`，而不是只依赖文件 Hash。
- 当前实现是 **Python 专用** AST；Markdown 继续用 `MarkdownTextSplitter`。
- 新版不是已经被证明比旧版检索准确：还需要相同问题集的 Recall@K / MRR / 回答质量测试。
