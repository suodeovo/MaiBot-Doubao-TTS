"""volc_tts 插件行为测试：不打真实网络，用桩替换合成与发送，验证命令正则、
情绪标记解析、冷却、字数上限、发送链路与失败提示。

运行：
    & "<MaiBot venv python>" test_volc_tts.py
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parents[1]
FAKE_MP3 = b"ID3\x03\x00\x00\x00" + b"\x00" * 64


def load_module():
    spec = importlib.util.spec_from_file_location("volc_tts_plugin_t", PLUGIN_DIR / "plugin.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeSend:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.customs: list[tuple[str, str, str]] = []

    async def text(self, text: str, stream_id: str, **kwargs):
        del kwargs
        self.texts.append((text, stream_id))
        return True

    async def custom(self, custom_type: str, data, stream_id: str, **kwargs):
        del kwargs
        self.customs.append((custom_type, data, stream_id))
        return True


class FakeCtx:
    def __init__(self) -> None:
        self.send = FakeSend()
        self.logger = SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
            debug=lambda *a, **k: None,
        )


def make_plugin(module, *, cooldown: float = 0.0, max_len: int = 300):
    plugin = module.create_plugin()
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "1.0.0", "cooldown_seconds": cooldown, "max_text_length": max_len},
            "credential": {"app_id": "1", "access_key": "k"},
        }
    )
    plugin._set_context(FakeCtx())
    return plugin


async def case_emotion(module) -> None:
    body, instr = module.split_emotion_prefix("[开心] 今天天气真好")
    assert body == "今天天气真好", body
    assert instr == ["用开心的语气说"], instr
    body2, instr2 = module.split_emotion_prefix("参考 [1] 和 [2] 的说法")
    assert body2.startswith("参考 [1]"), body2
    assert instr2 == [], instr2
    body3, instr3 = module.split_emotion_prefix("[撒娇]")
    assert body3 == "[撒娇]" and instr3 == [], (body3, instr3)
    print("[OK] 情绪标记解析")


async def case_command_patterns(module) -> None:
    plugin = module.create_plugin()
    components = plugin.get_components()
    by_name = {c["name"]: c["metadata"] for c in components}
    voice_pat = by_name["volc_tts_speak_cmd"]["command_pattern"]
    assert re.match(voice_pat, "/豆包 你好呀"), voice_pat
    assert re.match(voice_pat, "@麦麦 /豆包 你好呀"), voice_pat
    tts_pat = by_name["volc_tts_tts_cmd"]["command_pattern"]
    assert re.match(tts_pat, "/doubao hello"), tts_pat
    voices_pat = by_name["volc_tts_voice_list"]["command_pattern"]
    setup_pat = by_name["volc_tts_settings"]["command_pattern"]
    # 与已安装的 edge-tts 插件（/语音、/tts、/音色、/语音设置）不得冲突
    for own_pat, sample in (
        (voice_pat, "/语音 你好"),
        (tts_pat, "/tts 你好"),
        (voices_pat, "/音色"),
        (setup_pat, "/语音设置"),
    ):
        assert not re.match(own_pat, sample), (own_pat, sample)
    assert re.match(voices_pat, "/豆包音色"), voices_pat
    assert re.match(setup_pat, "/豆包设置"), setup_pat
    assert not re.match(voice_pat, "/豆包音色"), voice_pat
    assert not re.match(voice_pat, "/豆包设置"), voice_pat
    print("[OK] 命令正则（与 edge-tts 无冲突）")
    print("     豆包  :", voice_pat)
    print("     doubao:", tts_pat)
    print("     音色  :", voices_pat)
    print("     设置  :", setup_pat)


async def case_speak_success(module) -> None:
    plugin = make_plugin(module)
    calls: list[tuple[str, str]] = []

    async def fake_request(text: str, speaker: str) -> bytes:
        calls.append((text, speaker))
        return FAKE_MP3

    plugin._request_once = fake_request  # type: ignore[method-assign]
    ok, message = await plugin._speak(
        text="@<qq:123> /豆包 你好[开心] 世界",
        stream_id="s1",
        user_id="u1",
        speaker="",
        enforce_cooldown=False,
    )
    assert ok, message
    ctx = plugin.ctx
    assert len(ctx.send.customs) == 1
    ctype, data, stream = ctx.send.customs[0]
    assert ctype == "voice" and stream == "s1"
    assert base64.b64decode(data) == FAKE_MP3
    assert "世界" in calls[0][0]
    print("[OK] 合成+发送链路, 文本:", calls[0][0], "音色:", calls[0][1])


async def case_length_and_cooldown(module) -> None:
    plugin = make_plugin(module, cooldown=60.0, max_len=5)
    plugin._request_once = lambda text, speaker: _async(FAKE_MP3)  # type: ignore[method-assign]
    ok, msg = await plugin._speak(text="一二三四五六", stream_id="s", user_id="u", enforce_cooldown=False)
    assert not ok and "过长" in msg, msg
    ok2, _ = await plugin._speak(text="短句", stream_id="s", user_id="u", enforce_cooldown=True)
    assert ok2
    ok3, msg3 = await plugin._speak(text="短句", stream_id="s", user_id="u", enforce_cooldown=True)
    assert not ok3 and "频繁" in msg3, msg3
    print("[OK] 字数上限 + 冷却")


async def case_service_error(module) -> None:
    plugin = make_plugin(module)
    plugin._request_once = lambda text, speaker: _raise(module.VolcTtsError("45000000", "speaker permission denied", "logid-x"))  # type: ignore[method-assign]
    ok, msg = await plugin._speak(text="测试", stream_id="s", user_id="u", enforce_cooldown=False)
    assert not ok and "45000000" in msg and "logid-x" in msg and "音色鉴权失败" in msg, msg
    print("[OK] 服务端错误提示:", msg)


async def case_cache(module) -> None:
    plugin = make_plugin(module)
    hits = {"n": 0}

    async def fake_request(text: str, speaker: str) -> bytes:
        hits["n"] += 1
        return FAKE_MP3

    plugin._request_once = fake_request  # type: ignore[method-assign]
    await plugin._synthesize("同一句话")
    await plugin._synthesize("同一句话")
    assert hits["n"] == 1, hits
    print("[OK] 缓存命中，只请求 1 次")


async def case_debug_command(module) -> None:
    plugin = make_plugin(module)
    plugin._request_once = lambda text, speaker: _async(FAKE_MP3)  # type: ignore[method-assign]
    result = await plugin._handle_debug_command("s9", "/豆包", {})
    assert result[0] is True
    assert plugin.ctx.send.texts, "应给出用法提示"
    print("[OK] /豆包 无内容时给用法:", plugin.ctx.send.texts[-1][0].splitlines()[0])


async def _async(value):
    return value


async def _raise(exc: Exception):
    raise exc


async def main() -> None:
    module = load_module()
    await case_emotion(module)
    await case_command_patterns(module)
    await case_speak_success(module)
    await case_length_and_cooldown(module)
    await case_service_error(module)
    await case_cache(module)
    await case_debug_command(module)
    print("\n全部用例通过")


if __name__ == "__main__":
    asyncio.run(main())
