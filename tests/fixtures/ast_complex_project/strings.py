"""模块文档中的 # 不是注释。
# def fake():
"""

TEMPLATE = """示例文本
# 不归属于后续函数
@fake
def imaginary():
    pass
"""

# 真正函数
def render():
    return TEMPLATE
