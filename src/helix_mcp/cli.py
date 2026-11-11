from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import ConfigError, Settings
from .storage import Store, StoreError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Helix Editor private MCP server")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    sub.add_parser("check-config")
    backup = sub.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            store = Store(Path(os.environ.get("HELIX_DATA_DIR", "./data")))
            print(json.dumps({"backup": str(store.backup(args.output))}))
            return 0
        settings = Settings.from_env()
        if args.command == "check-config":
            print(json.dumps({"valid": True, **settings.public_summary()}, ensure_ascii=False, indent=2))
            return 0
        if not 1 <= args.port <= 65535:
            raise ConfigError("Port must be 1-65535.")
        import uvicorn
        from .server import create_app
        # TLS is provided by Caddy. Do not trust arbitrary forwarded headers.
        uvicorn.run(create_app(settings), host=args.host, port=args.port,
                    access_log=False, proxy_headers=False)
        return 0
    except (ConfigError, StoreError) as exc:
        print("Configuration error: " + str(exc), file=sys.stderr)
        return 2
    except OSError:
        print("Storage or bind operation failed; check service permissions.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
