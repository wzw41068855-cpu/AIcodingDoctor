"""运行：python -m unittest tests/test_ast_tagger.py（建议从项目根目录执行）。"""
import json
import unittest
from langchain_core.documents import Document
from rag.splitter.ast_tagger import AstTagger


class TestAstTagger(unittest.TestCase):
    def setUp(self):
        self.tagger = AstTagger()

    def test_class_method_and_nested_function(self):
        source = '''class UserService:\n    def login(self):\n        def verify():\n            return True\n        return verify()\n\nasync def fetch_user():\n    return None\n'''
        doc = Document(page_content=source, metadata={"relative_path": "app/services/user.py", "file_type": ".py"})
        chunks = [
            Document(page_content="login", metadata={"start_line": 2, "end_line": 2}),
            Document(page_content="verify", metadata={"start_line": 3, "end_line": 4}),
            Document(page_content="fetch", metadata={"start_line": 7, "end_line": 8}),
        ]
        self.tagger.enrich(doc, chunks)
        self.assertEqual(chunks[0].metadata["qualified_name"], "UserService.login")
        self.assertEqual(chunks[0].metadata["symbol_type"], "method")
        self.assertEqual(chunks[1].metadata["qualified_name"], "UserService.login.verify")
        self.assertEqual(chunks[1].metadata["symbol_type"], "nested_function")
        self.assertEqual(chunks[2].metadata["qualified_name"], "fetch_user")
        self.assertIn("UserService", json.loads(chunks[0].metadata["symbol_names"]))
        self.assertEqual(chunks[0].metadata["module_path"], "app.services.user")

    def test_markdown_defaults(self):
        doc = Document(page_content="# Guide", metadata={"relative_path": "README.md", "file_type": ".md"})
        chunk = Document(page_content="# Guide", metadata={"start_line": 1, "end_line": 1})
        self.tagger.enrich(doc, [chunk])
        self.assertEqual(chunk.metadata["symbol_names"], "[]")
        self.assertEqual(chunk.metadata["symbol_type"], "document")

    def test_syntax_error_falls_back(self):
        doc = Document(page_content="def broken(:", metadata={"relative_path": "bad.py", "file_type": ".py"})
        chunk = Document(page_content="def broken(:", metadata={"start_line": 1, "end_line": 1})
        self.tagger.enrich(doc, [chunk])
        self.assertEqual(chunk.metadata["symbol_name"], "")


if __name__ == "__main__":
    unittest.main()
