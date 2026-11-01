"""V4 Web 的轻量兼容入口；实际修复图仅在 graph.workflow 中定义。"""

from graph.workflow import build_repair_graph


def build_graph(nodes, saver):
    """构建含审批节点、SQLite Checkpointer 的正式 V4 修复图。"""
    return build_repair_graph(nodes, saver, require_approval=True)
