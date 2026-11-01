"""AICodingDoctor V4 本地 Web 启动入口。

执行 `python main.py`，或直接使用 uvicorn 的单 worker 模式。
"""

import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(description="启动 AICodingDoctor 本地 Web")
    parser.add_argument("--host", default="127.0.0.1", help="仅供本机使用，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("本地 Web 仅允许绑定回环地址")
    import uvicorn
    uvicorn.run("web_runtime.app:app", host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
