"""SQLite 任务和事件存储；条件更新负责阻止重复审批。"""

import json
import sqlite3
from contextlib import contextmanager
from pydantic_core import to_jsonable_python


class Store:
    """保存脱敏任务视图及可按序号重放的事件，不保存模型密钥。"""

    def __init__(self, path):
        """创建数据库结构；path 必须由本地运行时指定。"""
        self.path = str(path)
        with self.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS tasks
                (id TEXT PRIMARY KEY, status TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS events
                (seq INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, data TEXT);''')

    @contextmanager
    def connection(self):
        """每次操作独立连接并提交；异常自动回滚，不跨线程共享连接。"""
        with sqlite3.connect(self.path, timeout=20) as db:
            yield db

    def create(self, task_id, data):
        """保存新任务及固定参数；重复 ID 由 SQLite 拒绝。"""
        with self.connection() as db:
            db.execute("INSERT INTO tasks VALUES (?, 'queued', ?)", (task_id, json.dumps(data)))

    def get(self, task_id):
        """返回任务视图；不存在时抛出 KeyError。"""
        with self.connection() as db:
            row = db.execute("SELECT status,data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return dict(json.loads(row[1]), id=task_id, status=row[0])

    def listing(self):
        """返回最近创建的任务；本地历史保留于数据库。"""
        with self.connection() as db:
            ids = db.execute("SELECT id FROM tasks ORDER BY rowid DESC LIMIT 100").fetchall()
        return [self.get(row[0]) for row in ids]

    def update(self, task_id, status, **fields):
        """原子合并视图并记录事件；调用者只传入已脱敏业务数据。"""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            # 节点状态含 list[ChangeRecord] 等嵌套模型，必须递归转换，
            # 仅调用顶层 model_dump 无法让 SQLite 的 JSON 序列化安全完成。
            fields = to_jsonable_python(fields)
            data = json.loads(row[0]) | fields
            db.execute("UPDATE tasks SET status=?,data=? WHERE id=?", (status, json.dumps(data), task_id))
            db.execute("INSERT INTO events(task,data) VALUES (?,?)", (task_id, json.dumps(dict(status=status, **fields))))

    def claim(self, task_id, expected):
        """仅允许一个请求领取当前状态；重复审批或并发恢复返回 False。"""
        with self.connection() as db:
            return db.execute("UPDATE tasks SET status='running' WHERE id=? AND status=?", (task_id, expected)).rowcount == 1

    def events(self, task_id, after=0):
        """返回序号之后的持久事件，供 SSE 重连和页面刷新重放。"""
        with self.connection() as db:
            return db.execute("SELECT seq,data FROM events WHERE task=? AND seq>? ORDER BY seq LIMIT 200", (task_id, after)).fetchall()
