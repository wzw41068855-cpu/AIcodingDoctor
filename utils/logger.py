
import logging
import os
from pathlib import Path
import sys
import config

# 所有模块统一使用这个名称
LOGGER_NAME = "CodingDoctor2"

def setup_logging() -> logging.Logger:
    """
    配置 CodingDoctor2 日志系统。

    DEBUG_MODE=true:
        输出 DEBUG / INFO / WARNING / ERROR。

    DEBUG_MODE=false:
        只输出 INFO / WARNING / ERROR。

    LOG_TO_FILE=true:
        除了终端，还把日志保存到文件。
    """


    debug_mode = config.DEBUG_MODE


    log_to_file = config.LOG_TO_FILE

    # 创建项目专用 Logger
    #Python 会复用相同名称的 Logger，而不是每调用一次就创建独立的日志系统。
    logger = logging.getLogger(LOGGER_NAME)

    # 设置最低记录等级
    logger.setLevel(
        logging.DEBUG if debug_mode else logging.INFO
    )

    # 不把日志继续传递给根 Logger，
    # 避免和第三方日志配置重复输出
    logger.propagate = False

    # 避免重复初始化导致日志打印多次
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()

    # 配置日志格式
    formatter = logging.Formatter(
        fmt=(
            "%(asctime)s | "
            "%(levelname)-7s | "
            "%(name)s | "
            "%(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(
        logging.DEBUG if debug_mode else logging.INFO
    )
    console_handler.setFormatter(formatter)

    logger.addHandler(console_handler)

    # 2. 可选文件输出
    if log_to_file:

        log_path = (
            config.BASE_DIR / "logs" / "CodingDoctor2.log"
        )

        log_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        file_handler = logging.FileHandler(
            filename=log_path,
            mode="a",
            encoding="utf-8",
        )

        file_handler.setLevel(
            logging.DEBUG if debug_mode else logging.INFO
        )

        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.info(
        "日志初始化完成 | DEBUG=%s | FILE=%s",
        debug_mode,
        log_to_file,
    )

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    获取子模块 Logger。

    例如：
        get_logger("rag_chain")
        get_logger("retriever")
    """

    return logging.getLogger(
        f"{LOGGER_NAME}.{name}"
    )
