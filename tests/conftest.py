"""pytest 全局：models.TestCase 名字命中 Test* 收集规则，显式豁免（保持产码零测试框架痕迹）。"""

from platform_sdlc import models

models.TestCase.__test__ = False  # type: ignore[attr-defined]
