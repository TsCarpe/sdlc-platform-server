"""IDE 断点调试入口：在 PyCharm/IDEA 中直接 Debug 本文件即可（等价 scripts/dev.sh）。

- 单进程、无 --reload：断点即生效（reload 会 fork 子进程，调试器挂不上）。
- 工作目录必须是仓根：./data（SQLite 库与媒体）按 cwd 解析，Run Configuration 里勿改。
"""

from pathlib import Path

import uvicorn

if __name__ == "__main__":
    Path("data").mkdir(exist_ok=True)
    uvicorn.run("platform_sdlc.main:create_app", factory=True, host="0.0.0.0", port=8888)
