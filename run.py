"""启动入口：python run.py"""
from __future__ import annotations

import argparse
import atexit
import signal
import threading
import time
import webbrowser

from app import create_app, worker


def main() -> None:
    ap = argparse.ArgumentParser(description="B站投稿台")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认只本机可访问")
    ap.add_argument("--port", type=int, default=5277, help="端口，默认 5277")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    app = create_app()
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"

    print("\n  B站投稿台已启动")
    print(f"  打开浏览器访问：{url}")
    print("  关闭方式：Ctrl+C\n")

    # 退出收尾：停队列 + 把「上传中被强杀」的任务标为已中断。
    # 少了这一步，被 Ctrl+C / kill 打断的任务会永远停在 uploading：
    # 队列取不到它，界面上也看不出发生了什么，还容易被误当成「没发出去」重投一次。
    atexit.register(worker.shutdown)

    def _on_term(_signum, _frame):
        # SIGTERM 默认是直接杀进程、不跑 atexit，这里转成正常退出
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError, AttributeError):
        pass  # 非主线程或平台不支持，交给 atexit 兜底

    if not args.no_browser:
        def _open():
            time.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    try:
        app.run(host=args.host, port=args.port, debug=False, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\n  收到 Ctrl+C，正在收尾…")
    except SystemExit:
        print("\n  收到停止信号，正在收尾…")


if __name__ == "__main__":
    main()
