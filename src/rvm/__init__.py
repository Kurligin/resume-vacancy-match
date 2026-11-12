"""Сопоставление резюме и вакансий."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="rvm")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="запустить HTTP API и интерфейс")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("rvm.api:create_app", factory=True, host=args.host, port=args.port)
