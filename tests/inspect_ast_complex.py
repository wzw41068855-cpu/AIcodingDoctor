"""在项目根目录运行：
python -c "import runpy; runpy.run_path('tests/inspect_ast_complex.py', run_name='__main__')"
输出逐 Chunk 的切分报告，不调用 Embedding 或 LLM。
"""
import json
from pathlib import Path

from langchain_core.documents import Document
from rag.splitter.code_splitter import CodeSplitter


def inspect():
    root = Path(__file__).parent / 'fixtures' / 'ast_complex_project'
    report = []
    for size, overlap in ((1000, 150), (180, 30)):
        for path in sorted(root.glob('*.py')):
            source = path.read_text(encoding='utf-8')
            chunks = CodeSplitter(size, overlap).split([Document(
                page_content=source, metadata={
                    'relative_path': path.name, 'file_type': '.py',
                })])
            report.append({
                'file': path.name, 'size': size, 'overlap': overlap,
                'chunk_count': len(chunks),
                'chunks': [{
                    'symbol': c.metadata['qualified_name'],
                    'type': c.metadata['symbol_type'],
                    'lines': [c.metadata['start_line'], c.metadata['end_line']],
                    'offsets': [c.metadata['start_index'], c.metadata['end_index']],
                    'length': len(c.page_content),
                    'preview': c.page_content.strip()[:80],
                } for c in chunks],
            })
    return report


if __name__ == '__main__':
    print(json.dumps(inspect(), ensure_ascii=False, indent=2))
