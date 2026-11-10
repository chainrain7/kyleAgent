"""Web Console 构建产物完整性守护(CI 不装 Node 的补偿闸门)。

前端源码在 webui/frontend/(需 Node 构建),产物 webui/dist/ 提交入库。
本测试防止"改了前端忘 build / build 了忘提交":
- dist/index.html 必须存在,且其引用的每个 /assets/* 文件真实在 dist 里
- bridge 模块必须可导入,_DIST_DIR 必须存在(静态服务段依赖它)
"""
from __future__ import annotations

import re
from pathlib import Path

_DIST_DIR = Path(__file__).parent.parent.parent / "src" / "kyle_agent" / "webui" / "dist"


# 功能:dist/index.html 存在且引用的 assets 全部真实存在
# 设计:用正则直接解析构建产物 HTML 的 script/link 标签,逐一核对 dist 内
#       文件——漏 build 或漏提交时此测试最先红,无需装 Node 即可定位
def test_dist_index_and_assets_consistent() -> None:
    index = _DIST_DIR / "index.html"
    assert index.exists(), (
        "webui/dist/index.html 不存在——前端构建产物未提交;先在 webui/frontend 跑 npm run build"
    )
    html = index.read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="/(assets/[^"]+)"', html)
    assert refs, "index.html 未引用任何 /assets/* 产物,疑似空壳构建"
    for ref in refs:
        assert (_DIST_DIR / ref).exists(), f"index.html 引用了缺失的产物: {ref}"


# 功能:bridge 模块可导入且 _DIST_DIR 指向真实存在的目录
# 设计:桥接器在 import 期就 mount dist/assets(目录缺失会炸),这里提前
#       以纯文件断言暴露问题,报错信息直接指向构建/提交流程
def test_bridge_dist_dir_exists() -> None:
    from kyle_agent.webui import bridge  # noqa: F401

    assert bridge._DIST_DIR.is_dir(), (
        "bridge._DIST_DIR 不存在——webui/dist 未提交或被误删;先 npm run build 再提交产物"
    )
