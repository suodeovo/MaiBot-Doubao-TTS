"""回归：非法 speaker（角色昵称）、别名、S_ 复刻音色、预置音色 的分辨与资源匹配。

重点复现角色昵称误作音色的故障：模型把 bot 昵称「小柚」当 speaker 传进来。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location("volc_tts_speaker_t", PLUGIN_DIR / "plugin.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.send = SimpleNamespace(custom=self._custom, text=self._text)
        self.logger = SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda msg, *a, **k: self.warnings.append(msg % a if a else msg),
            error=lambda *a, **k: None,
            debug=lambda *a, **k: None,
        )

    async def _custom(self, *a, **k):
        return True

    async def _text(self, *a, **k):
        return True


def make(module, speaker: str, resource_id: str = "seed-icl-2.0"):
    plugin = module.create_plugin()
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "1.0.0"},
            "credential": {"app_id": "1", "access_key": "k", "resource_id": resource_id},
            "tts": {"speaker": speaker},
        }
    )
    plugin._set_context(FakeCtx())
    return plugin


def resolve(module, value: str, *, configured: str = "S_TESTVOICE01", resource_id: str = "seed-icl-2.0"):
    plugin = make(module, configured, resource_id)
    resolved = plugin._resolve_speaker(value)
    effective, note = module._resource_id_for_speaker(resolved, resource_id)
    return resolved, effective, note, plugin.ctx.warnings


async def main() -> None:
    module = load_module()

    # 1. 昵称误作音色场景：模型传昵称「小柚」
    resolved, effective, note, warnings = resolve(module, "小柚")
    assert resolved == "S_TESTVOICE01", resolved
    assert effective == "seed-icl-2.0", effective
    assert not note, note
    assert warnings and "小柚" in warnings[0], warnings
    print("[OK] 昵称「小柚」→ 回退配置音色 S_TESTVOICE01，resource 保持 seed-icl-2.0")
    print("     warn:", warnings[0])

    # 2. 空串走配置
    resolved, effective, note, _ = resolve(module, "")
    assert resolved == "S_TESTVOICE01" and effective == "seed-icl-2.0" and not note
    print("[OK] 空 speaker → 配置音色，不翻转 resource")

    # 3. 别名解析
    resolved, effective, note, _ = resolve(module, "小何", configured="小何", resource_id="seed-tts-2.0")
    assert resolved == "zh_female_xiaohe_uranus_bigtts", resolved
    assert effective == "seed-tts-2.0" and not note
    print("[OK] 别名「小何」→", resolved)

    # 4. S_ 音色配错 resource，自动纠正
    resolved, effective, note, _ = resolve(module, "S_TESTVOICE01", configured="S_TESTVOICE01", resource_id="seed-tts-2.0")
    assert effective == "seed-icl-2.0" and note, (effective, note)
    print("[OK] S_ 音色 + seed-tts-2.0 → 自动纠正:", note)

    # 5. 预置音色配了 icl，反向纠正
    resolved, effective, note, _ = resolve(
        module, "zh_female_xiaohe_uranus_bigtts", configured="zh_female_xiaohe_uranus_bigtts", resource_id="seed-icl-2.0"
    )
    assert effective == "seed-tts-2.0" and note, (effective, note)
    print("[OK] 预置音色 + seed-icl-2.0 → 自动纠正:", note)

    # 6. 配置本身非法时也不能返回非法值
    resolved, effective, note, _ = resolve(module, "", configured="小柚", resource_id="seed-tts-2.0")
    assert resolved == module.DEFAULT_SPEAKER, resolved
    print("[OK] 配置写错成昵称时回退内置默认:", resolved)

    # 7. 工具已不再声明 speaker 参数
    plugin = module.create_plugin()
    tool = next(c for c in plugin.get_components() if c["name"] == "volc_tts_speak")
    params = [p["name"] for p in tool["metadata"]["parameters"]]
    assert params == ["text"], params
    print("[OK] volc_tts_speak 参数只剩:", params)

    # 8. 模型仍硬塞 speaker 参数时被忽略
    class Ctx(FakeCtx):
        pass

    plugin = make(module, "S_TESTVOICE01", "seed-icl-2.0")
    captured: dict[str, str] = {}

    async def fake_speak(**kwargs):
        captured.update(kwargs)
        return True, "ok"

    plugin._speak = fake_speak  # type: ignore[method-assign]
    result = await plugin.handle_speak_tool(text="你好", stream_id="s1", speaker="小柚")
    assert result["success"] is True, result
    assert captured.get("speaker") == "", captured
    print("[OK] 模型硬塞 speaker=小柚 时被丢弃，实际传 speaker=%r" % captured.get("speaker"))

    print("\n全部用例通过")


if __name__ == "__main__":
    asyncio.run(main())
