"""명령줄 진입점.

  python -m influencer_studio mcp          MCP 서버 (stdio) — Claude Desktop/Claude Code 등에서 연결
  python -m influencer_studio web          관리자 웹 대시보드 (http://127.0.0.1:8765)
  python -m influencer_studio backup       백업 zip 만들기
  python -m influencer_studio restore ZIP DIR   백업을 새 data 폴더에 복구
  python -m influencer_studio check        DB·파일 연결 관계 점검
  python -m influencer_studio status       연결된 제작 도구 상태
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "help"
    if cmd == "mcp":
        from .mcp_server import main as run
        run()
    elif cmd == "web":
        from .web.app import main as run
        run()
    elif cmd in ("backup", "check", "status"):
        from .studio import Studio
        st = Studio()
        res = (st.archive.backup(actor="admin") if cmd == "backup" else
               st.archive.verify_integrity() if cmd == "check" else st.models_status())
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    elif cmd == "restore" and len(args) == 3:
        from .services.archive import ArchiveService
        print(json.dumps(ArchiveService.restore(args[1], args[2]), ensure_ascii=False, indent=2))
    else:
        print(__doc__)
        return 0 if cmd == "help" else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
