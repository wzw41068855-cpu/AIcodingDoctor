#!/usr/bin/env python
# coding: utf-8
# SPDX-License-Identifier: MIT
"""复杂切分测试项目：这里只解析源码，不执行服务。"""
from dataclasses import dataclass

DEFAULT = {"token": "令牌", "limit": 5}


# 生成令牌，包含多行和多个装饰器
@trace(
    name="create_token",
    options={"retry": 2},
)
@cache
async def create_token(user_id: int):
    """生成令牌。"""
    def normalize(value):
        return str(value).strip()

    async def persist(value):
        return value

    return await persist(normalize(user_id))


# 服务类说明
@dataclass
class TokenService:
    """令牌服务。"""
    ttl: int = 3600
    settings = {
        "prefix": "Bearer",
        "enabled": True,
    }

    # 校验令牌
    @staticmethod
    def verify(token):
        return bool(token)

    label = "令牌验证"

    # 获取过期时间
    @property
    def expires(self):
        return self.ttl

    @expires.setter
    def expires(self, value):
        self.ttl = value

    class Policy:
        minimum = 1

        # 嵌套类方法
        @classmethod
        def accepts(cls, value):
            return value >= cls.minimum

    final_flag = True


if DEFAULT["limit"]:
    def conditional():
        return "留在模块控制流中"


def factory():
    class Local:
        def run(self):
            return "保留在外层函数"
    return Local()

# 独立结尾注释
