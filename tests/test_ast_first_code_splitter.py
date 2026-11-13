"""实际 CodeSplitter 集成测试，需要在已安装 requirements.txt 的环境运行。

python -m unittest discover -s tests -p test_ast_first_code_splitter.py -v
"""
import unittest

try:
    from langchain_core.documents import Document
    from rag.splitter.code_splitter import CodeSplitter
except ImportError:
    Document = None
    CodeSplitter = None


@unittest.skipUnless(CodeSplitter is not None, "需要安装 LangChain 依赖")
class TestAstFirstCodeSplitter(unittest.TestCase):
    @staticmethod
    def _doc(text, name="src/service.py", suffix=".py"):
        return Document(page_content=text, metadata={
            "relative_path": name, "file_type": suffix, "start_line": 1,
        })

    def test_two_short_functions_not_combined(self):
        source = (
            'def login():\n    return "ok"\n\n'
            'def logout():\n    return "bye"\n'
        )
        chunks = CodeSplitter(chunk_size=1000, chunk_overlap=150).split([self._doc(source)])
        matched = [c for c in chunks if c.metadata["symbol_type"] == "function"]
        self.assertEqual([c.metadata["symbol_name"] for c in matched], ["login", "logout"])
        self.assertTrue(all(c.metadata["ast_part_count"] == 1 for c in matched))
        self.assertEqual(matched[0].metadata["start_line"], 1)
        self.assertEqual(matched[1].metadata["start_line"], 4)
        for chunk in chunks:
            a, b = chunk.metadata["start_index"], chunk.metadata["end_index"]
            self.assertEqual(source[a:b], chunk.page_content)

    def test_long_function_split_and_metadata_retained(self):
        source = 'class Service:\n    def run(self):\n' + ''.join(
            f'        value_{i} = {i}\n' for i in range(60)
        )
        chunks = CodeSplitter(chunk_size=180, chunk_overlap=30).split([self._doc(source)])
        method_chunks = [c for c in chunks if c.metadata["qualified_name"] == "Service.run"]
        self.assertGreater(len(method_chunks), 1)
        self.assertEqual(
            [c.metadata["ast_part_index"] for c in method_chunks],
            list(range(len(method_chunks))),
        )
        self.assertTrue(all(c.metadata["ast_part_count"] == len(method_chunks) for c in method_chunks))
        for c in chunks:
            a, b = c.metadata["start_index"], c.metadata["end_index"]
            self.assertEqual(source[a:b], c.page_content)

    def test_invalid_code_uses_legacy_fallback(self):
        source = 'def broken(:\n    return 1\n'
        chunks = CodeSplitter().split([self._doc(source)])
        self.assertTrue(chunks)
        self.assertEqual(chunks[0].metadata["symbol_type"], "module")

    def test_markdown_unchanged(self):
        source = '# Introduction\n\nSome text about a project.\n'
        chunks = CodeSplitter().split([self._doc(source, "README.md", ".md")])
        self.assertTrue(chunks)
        self.assertEqual(chunks[0].metadata["symbol_type"], "document")
        self.assertEqual(chunks[0].page_content, source)

    def test_comments_attached_and_comment_only_chunks_filtered(self):
        source = ('"""module docs"""\n# generate token\n'
                  'async def create_token():\n    return "abc"\n'
                  '# trailing comment\n')
        chunks = CodeSplitter().split([self._doc(source)])
        self.assertEqual(len(chunks), 2)
        function = chunks[1]
        self.assertEqual(function.metadata['symbol_name'], 'create_token')
        self.assertEqual(function.metadata['start_line'], 2)
        self.assertTrue(function.page_content.startswith('# generate token'))
        self.assertEqual(CodeSplitter().split([self._doc('# comment only\n')]), [])

    def test_secondary_split_filters_comment_only_parts(self):
        from rag.splitter.ast_segmenter import has_python_code
        source = '# explanation\n' * 40 + 'def run():\n    return 1\n'
        chunks = CodeSplitter(chunk_size=100, chunk_overlap=10).split([self._doc(source)])
        self.assertTrue(chunks)
        self.assertTrue(all(has_python_code(c.page_content) for c in chunks))
        self.assertEqual([c.metadata['ast_part_index'] for c in chunks], list(range(len(chunks))))


if __name__ == '__main__':
    unittest.main()
