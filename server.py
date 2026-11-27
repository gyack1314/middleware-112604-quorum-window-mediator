#!/usr/bin/env python3
"""便捷入口：启动联合演练窗口确认中间件的 HTTP 服务。

用法：
    python3 server.py --host 0.0.0.0 --port 8080 --data-dir .window_confirm

等价于：
    python3 -m window_confirm serve ...
"""

from window_confirm.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["serve", *__import__("sys").argv[1:]]))
