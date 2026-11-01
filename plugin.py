"""豆包语音合成 2.0（火山引擎 Seed TTS 2.0）MaiBot 插件。

链路：

    插件 -> POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
         -> HTTP Chunked JSON 行，data 字段为 base64 音频
         -> 拼接解码得配置格式的音频
         -> ctx.send.custom("voice", base64, stream_id)
         -> 核心语音组件 -> 支持对应音频格式的平台适配器

触发方式：
1. ``volc_tts_speak`` 工具：由大模型自行决定用声音说话。
2. ``/豆包``、``/doubao`` 调试命令：手动验证链路。

鉴权两种控制台都支持：
- 新版控制台：``X-Api-Key``（填 api_key 即可）
- 旧版控制台：``X-Api-App-Id`` + ``X-Api-Access-Key``（填 app_id + access_key）
都填时优先用 api_key。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from collections import OrderedDict
from typing import Any, Literal, Optional

import httpx

from maibot_sdk import API, Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

TTS_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"

# 音色别名 -> 官方 voice_type。全部为「豆包语音合成模型2.0」音色，
# 必须配 X-Api-Resource-Id: seed-tts-2.0 使用，否则报 45000000。
VOICE_ALIASES: dict[str, str] = {
    "小何": "zh_female_xiaohe_uranus_bigtts",
    "爽快思思": "zh_female_shuangkuaisisi_uranus_bigtts",
    "Vivi": "zh_female_vv_uranus_bigtts",
    "云舟": "zh_male_m191_uranus_bigtts",
    "小天": "zh_male_taocheng_uranus_bigtts",
    "儒雅逸辰": "zh_male_ruyayichen_uranus_bigtts",
    "甜美小源": "zh_female_tianmeixiaoyuan_uranus_bigtts",
    "柔美女友": "zh_female_roumeinvyou_uranus_bigtts",
    "温柔小哥": "zh_male_wenrouxiaoge_uranus_bigtts",
    "邻家女孩": "zh_female_linjianvhai_uranus_bigtts",
    "少年梓辛": "zh_male_shaonianzixin_uranus_bigtts",
    "魅力苏菲": "zh_female_sophie_uranus_bigtts",
    "刘飞": "zh_male_liufei_uranus_bigtts",
    "知性灿灿": "zh_female_cancan_uranus_bigtts",
    "撒娇学妹": "zh_female_sajiaoxuemei_uranus_bigtts",
    "武则天": "zh_female_wuzetian_uranus_bigtts",
    "佩奇猪": "zh_female_peiqi_uranus_bigtts",
    "猴哥": "zh_male_sunwukong_uranus_bigtts",
    "天才童声": "zh_male_tiancaitongsheng_uranus_bigtts",
    "英语-Dacey": "en_female_dacey_uranus_bigtts",
}
VOICE_ID_TO_ALIAS: dict[str, str] = {v: k for k, v in VOICE_ALIASES.items()}

# 合法 voice_type 的形状：zh_female_xxx_bigtts / en_male_xxx_mars_bigtts / S_xxx（声音复刻）
# 其他任何字符串（角色昵称、中文名、空串）都视为无效，直接回退到配置音色，
# 否则会把 resource_id 校正逻辑带偏，最终由服务端报 55000000。
_VALID_VOICE_PATTERN = re.compile(r"^(?:[a-zA-Z]{2,3}_[a-zA-Z0-9_]+|[Ss]_[A-Za-z0-9]+)$")

# 情绪标记 -> 2.0 的 context_texts 语音指令；仅 seed-tts-2.0 / seed-icl-2.0 支持
_EMOTION_PREFIX = re.compile(r"^(?:\s*\[([^\[\]]+)\])+\s*")
_TAG_PATTERN = re.compile(r"\[([^\[\]]+)\]")

_AT_TAG_PATTERN = re.compile(r"@<[^>]*:\d+>")
_CQ_CODE_PATTERN = re.compile(r"\[CQ:[^\]]*\]")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_LEADING_NUMBER_PATTERN = re.compile(r"^[+-]?\d+(?:\.\d+)?")

COMMAND_AT_PREFIX = r"(?:@\S+\s*)?"

# 官方错误码 -> 人话
ERROR_HINTS: dict[str, str] = {
    "45000000": "音色鉴权失败：speaker 未授权或与 resource_id 版本不匹配（2.0 音色必须配 seed-tts-2.0）",
    "45000010": "鉴权失败：AppID / Access Key（或 API Key）无效、与资源 ID 不匹配，或该资源未在控制台开通",
    "45000030": "该资源未在控制台开通：资源 ID 对应商品没购买/没授权",
    "55000000": "服务端拒绝，见返回原文",
}

# 服务端原文里能直接定位病因的片段 -> 处理建议
MESSAGE_HINTS: tuple[tuple[str, str], ...] = (
    (
        "resource ID is mismatched with speaker related resource",
        "speaker 与 resource_id 版本不匹配：S_ 开头的声音复刻音色要用 seed-icl-2.0，"
        "预置 2.0 音色要用 seed-tts-2.0；若 speaker 是角色名/昵称，那它根本不是音色 ID",
    ),
    ("requested resource not granted", "该 resource_id 对应商品未开通，去控制台购买/开通对应版本"),
)

# 声音复刻音色（S_ 前缀）只能走复刻资源
CLONE_RESOURCE_ID = "seed-icl-2.0"
DEFAULT_SPEAKER = "zh_female_xiaohe_uranus_bigtts"


def _resource_id_for_speaker(speaker: str, configured: str) -> tuple[str, str]:
    """按音色前缀校正 resource_id，返回 (实际使用的 id, 校正说明)。

    S_ 开头是声音复刻音色，只认 seed-icl-*；预置音色只认 seed-tts-*。
    配错时服务端会以 55000000 / 45000030 拒绝，这里直接纠正并提示。

    非法 speaker（既不是 S_ 也不是合法预置 voice_type）不做任何翻转：
    翻转只会把错误伪装成另一条更难查的报错。
    """

    speaker = speaker.strip()
    if not _VALID_VOICE_PATTERN.match(speaker):
        return configured, ""
    is_clone_voice = speaker.upper().startswith("S_")
    is_clone_resource = configured.startswith("seed-icl")
    if is_clone_voice and not is_clone_resource:
        return CLONE_RESOURCE_ID, f"speaker {speaker} 是声音复刻音色，resource_id 已从 {configured} 自动切换为 {CLONE_RESOURCE_ID}"
    if not is_clone_voice and is_clone_resource:
        return "seed-tts-2.0", f"speaker {speaker} 是预置音色，resource_id 已从 {configured} 自动切换为 seed-tts-2.0"
    return configured, ""


def _parse_error_body(raw: str, status_code: int) -> tuple[str, str]:
    """从错误响应体里取出火山业务错误码。

    401/403 这类 HTTP 层错误，服务端会在 body 里带
    ``{"header":{"code":45000010,"message":"..."}}``；取不到时退回 HTTP 状态码。
    """

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return f"HTTP{status_code}", raw.strip() or f"HTTP {status_code}"

    header = payload.get("header") if isinstance(payload, dict) else None
    if isinstance(header, dict):
        code = header.get("code")
        message = header.get("message") or header.get("Message") or ""
        if code is not None:
            return str(code), str(message) or f"HTTP {status_code}"
    code = payload.get("code") if isinstance(payload, dict) else None
    if code is not None:
        return str(code), str(payload.get("message") or f"HTTP {status_code}")
    return f"HTTP{status_code}", raw.strip()[:300]


class VolcTtsError(RuntimeError):
    """服务端返回的业务错误。"""

    def __init__(self, code: str, message: str, logid: str = "") -> None:
        hint = ERROR_HINTS.get(code, "")
        # 服务端原文往往比错误码更精确，优先用它挑建议
        for needle, message_hint in MESSAGE_HINTS:
            if needle in message:
                hint = message_hint
                break
        detail = f"[{code}] {message}" + (f"（{hint}）" if hint else "")
        if logid:
            detail += f" logid={logid}"
        super().__init__(detail)
        self.code = code
        self.message = message
        self.logid = logid


def _to_instruction(tag: str) -> str:
    """把裸标记包装成自然语言指令，2.0 的 context_texts 接受口语描述。"""

    tag = tag.strip()
    if not tag:
        return ""
    if tag.startswith("用") or tag.endswith("说"):
        return tag
    if tag.endswith("语气") or tag.endswith("情绪"):
        return f"用{tag}说"
    return f"用{tag}的语气说"


def split_emotion_prefix(raw_text: str) -> tuple[str, list[str]]:
    """剥离开头的 ``[开心]`` 情绪标记，返回 (正文, 指令列表)。

    只处理开头连续的方括号标记，正文里的 ``[1]``、``[链接]`` 原样保留。
    """

    matched = _EMOTION_PREFIX.match(raw_text)
    if not matched:
        return raw_text, []
    instructions = [x for x in (_to_instruction(t) for t in _TAG_PATTERN.findall(matched.group(0))) if x]
    body = raw_text[matched.end():].strip()
    if not body or not instructions:
        return raw_text, []
    return body, instructions


class PluginSectionConfig(PluginConfigBase):
    """插件基础开关。"""

    __ui_label__ = "插件开关"
    __ui_icon__ = "toggle-left"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="关闭后不再合成语音，已注册的工具也会直接拒绝执行",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置版本，由运行器维护",
        json_schema_extra={"label": "配置版本"},
    )
    command_enabled: bool = Field(
        default=True,
        description="关闭后 /豆包、/doubao、/豆包音色、/豆包设置 等调试命令不响应",
        json_schema_extra={"label": "启用调试命令"},
    )
    cooldown_seconds: float = Field(
        default=3.0,
        description="同一用户在同一会话的最短触发间隔，0 表示不限制",
        json_schema_extra={"label": "冷却时间（秒）"},
    )
    max_text_length: int = Field(
        default=300,
        description="单次合成最大字符数，超过直接拒绝（2.0 按字符计费）",
        json_schema_extra={"label": "单次字数上限"},
    )


class CredentialSectionConfig(PluginConfigBase):
    """火山引擎鉴权。"""

    __ui_label__ = "鉴权信息"
    __ui_icon__ = "key"
    __ui_order__ = 1

    app_id: str = Field(
        default="",
        description="旧版控制台鉴权用：X-Api-App-Id，火山引擎控制台的 APP ID（数字串）",
        json_schema_extra={"label": "AppID", "placeholder": "例如 1234567890"},
    )
    access_key: str = Field(
        default="",
        description="旧版控制台鉴权用：X-Api-Access-Key，控制台的 Access Token",
        json_schema_extra={"label": "Access Key", "placeholder": "控制台 Access Token"},
    )
    api_key: str = Field(
        default="",
        description="新版控制台鉴权用：X-Api-Key；填了它就不再用 AppID / Access Key",
        json_schema_extra={"label": "API Key（可选）", "placeholder": "新版控制台只需这一项"},
    )
    resource_id: Literal[
        "seed-tts-2.0",
        "seed-tts-1.0",
        "seed-tts-1.0-concurr",
        "seed-icl-2.0",
        "seed-icl-1.0",
    ] = Field(
        default="seed-tts-2.0",
        description=(
            "X-Api-Resource-Id，决定模型版本与计费商品。"
            "预置音色用 seed-tts-2.0；S_ 开头的声音复刻音色用 seed-icl-2.0。"
            "配错时插件会按音色前缀自动纠正并写日志"
        ),
        json_schema_extra={"label": "资源 ID（模型版本）"},
    )


class TtsSectionConfig(PluginConfigBase):
    """合成参数。"""

    __ui_label__ = "语音合成"
    __ui_icon__ = "mic"
    __ui_order__ = 2

    speaker: str = Field(
        default="zh_female_xiaohe_uranus_bigtts",
        description=(
            "音色 ID。预置音色填中文别名（小何 / 爽快思思 / Vivi / 云舟 …）"
            "或 voice_type 原值；声音复刻音色填 S_ 开头的音色 ID（配套 resource_id 用 seed-icl-2.0）"
        ),
        json_schema_extra={
            "label": "音色 ID",
            "placeholder": "zh_female_xiaohe_uranus_bigtts 或 S_xxxxxxxx",
            "hint": "别名可用 /豆包音色 查看",
        },
    )
    audio_format: Literal["mp3", "ogg_opus", "pcm"] = Field(
        default="mp3",
        description="返回音频格式；默认 mp3，需与目标适配器支持的格式匹配",
        json_schema_extra={"label": "音频格式"},
    )
    sample_rate: int = Field(
        default=24000,
        description="采样率，可选 8000/16000/22050/24000/32000/44100/48000",
        json_schema_extra={"label": "采样率", "placeholder": "24000"},
    )
    speech_rate: int = Field(
        default=0,
        ge=-50,
        le=100,
        description="语速，-50 ~ 100，负数变慢",
        json_schema_extra={"label": "语速"},
    )
    loudness_rate: int = Field(
        default=0,
        ge=-50,
        le=100,
        description="音量，-50 ~ 100，负数变小",
        json_schema_extra={"label": "音量"},
    )
    emotion_enabled: bool = Field(
        default=True,
        description="允许文本开头的 [开心] 这类标记转成 2.0 的语音指令（context_texts）",
        json_schema_extra={"label": "启用情绪标记"},
    )
    timeout_seconds: int = Field(
        default=60,
        description="流响应读取超时（秒），不是整个合成过程的总时限",
        json_schema_extra={"label": "合成超时（秒）"},
    )
    retry_times: int = Field(
        default=1,
        description="网络错误或 5xx 时的重试次数",
        json_schema_extra={"label": "失败重试次数"},
    )
    cache_size: int = Field(
        default=16,
        description="相同文本+音色的缓存条数，0 关闭；2.0 按字符计费，缓存能省钱",
        json_schema_extra={"label": "缓存条数"},
    )


class DebugSectionConfig(PluginConfigBase):
    """调试。"""

    __ui_label__ = "调试"
    __ui_icon__ = "bug"
    __ui_order__ = 3

    log_synthesis: bool = Field(
        default=True,
        description="是否在 MaiBot 主日志记录每次合成结果（含 logid、字数、字节数）",
        json_schema_extra={"label": "记录合成日志"},
    )


class VolcTtsPluginConfig(PluginConfigBase):
    """豆包语音合成 2.0 插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    credential: CredentialSectionConfig = Field(default_factory=CredentialSectionConfig)
    tts: TtsSectionConfig = Field(default_factory=TtsSectionConfig)
    debug: DebugSectionConfig = Field(default_factory=DebugSectionConfig)


class VolcTtsPlugin(MaiBotPlugin):
    """火山引擎豆包语音合成 2.0。"""

    config_model = VolcTtsPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._cache: OrderedDict[tuple[str, str, int, int], bytes] = OrderedDict()
        self._last_trigger: dict[tuple[str, str], float] = {}
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._client: Optional[httpx.AsyncClient] = None

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        cfg = self.config
        if not self._has_auth():
            self.ctx.logger.warning(
                "[VolcTTS] 鉴权未配置：填 api_key（新版控制台），或 app_id + access_key（旧版控制台）"
            )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=float(cfg.tts.timeout_seconds), write=30.0, pool=10.0),
            follow_redirects=True,
        )
        effective_resource, corrected = _resource_id_for_speaker(
            self._resolve_speaker(cfg.tts.speaker), cfg.credential.resource_id
        )
        if corrected:
            self.ctx.logger.warning("[VolcTTS] %s（建议把 resource_id 直接改成它，省掉每次校正）", corrected)
        self.ctx.logger.info(
            "[VolcTTS] 已加载 speaker=%s resource=%s format=%s",
            self._resolve_speaker(cfg.tts.speaker),
            effective_resource,
            cfg.tts.audio_format,
        )

    async def on_unload(self) -> None:
        self._cache.clear()
        self._last_trigger.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data
        # 音色、鉴权、参数都可能变，全部失效
        self._cache.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=float(self.config.tts.timeout_seconds),
                    write=30.0,
                    pool=10.0,
                ),
                follow_redirects=True,
            )
        self.ctx.logger.info("[VolcTTS] 配置已更新: version=%s", version)

    # ===== 大模型工具 =====

    @Tool(
        "volc_tts_speak",
        description=(
            "把一段话合成语音发到当前聊天（豆包语音合成 2.0）。"
            "只有你真的想用「说话」表达时才调用；text 只放要朗读的正文，不要带 @、CQ 码或 Markdown。"
            "可在开头加一个情绪标记，如 [开心] [悲伤] [撒娇] [严肃]，2.0 音色会按情绪说。"
            "音色由插件配置固定，你不需要也不能指定，不要传 speaker 参数。"
            "调用成功后不要再发送内容相同的文字，避免重复表达。"
        ),
        parameters=[
            ToolParameterInfo(
                name="text",
                param_type=ToolParamType.STRING,
                description="要朗读的文本，支持中英混排，建议 200 字以内；不要包含自己的名字或角色名",
                required=True,
            ),
        ],
    )
    async def handle_speak_tool(self, text: str = "", **kwargs: Any) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        user_id = str(kwargs.get("user_id") or "").strip() or "tool"
        if not self.config.plugin.enabled:
            return {"name": "volc_tts_speak", "success": False, "content": "语音插件已禁用。"}
        if not stream_id:
            return {"name": "volc_tts_speak", "success": False, "content": "缺少 stream_id，无法发送语音。"}

        # 模型偶尔仍会塞 speaker 参数（角色昵称等），这里只记日志，不采用
        stray_speaker = str(kwargs.get("speaker") or "").strip()
        if stray_speaker:
            self.ctx.logger.info("[VolcTTS] 忽略模型传入的 speaker=%r，使用配置音色", stray_speaker)

        ok, message = await self._speak(
            text=text,
            stream_id=stream_id,
            user_id=user_id,
            speaker="",
            enforce_cooldown=False,
        )
        return {"name": "volc_tts_speak", "success": ok, "content": message}

    # ===== 插件间 API =====

    @API("synthesize", description="把文本合成为配置格式的 base64 音频，供其他插件调用", version="1", public=True)
    async def api_synthesize(self, text: str, speaker: str = "") -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "error": "语音插件已禁用"}
        cleaned = self._clean_text(text)
        if not cleaned:
            return {"success": False, "error": "没有可朗读的文本"}
        max_length = max(1, int(self.config.plugin.max_text_length))
        if len(cleaned) > max_length:
            return {"success": False, "error": f"文本过长（{len(cleaned)} 字），上限 {max_length} 字，请分段发送"}
        try:
            audio = await self._synthesize(cleaned, speaker)
        except Exception as exc:  # noqa: BLE001 - 对调用方只暴露文本错误
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "mime_type": "audio/mpeg" if self.config.tts.audio_format == "mp3" else "audio/octet-stream",
            "audio_base64": base64.b64encode(audio).decode("ascii"),
            "bytes": len(audio),
        }

    # ===== 调试命令 =====

    @Command(
        "volc_tts_speak_cmd",
        description="把 /豆包 后面的文字合成为豆包语音并发送",
        pattern=rf"^{COMMAND_AT_PREFIX}/豆包(?:\s+(?P<content>[\s\S]+))?$",
    )
    async def handle_voice_command(self, stream_id: str = "", text: str = "", **kwargs: Any):
        return await self._handle_debug_command(stream_id, text, kwargs)

    @Command(
        "volc_tts_tts_cmd",
        description="把 /豆包tts 后面的文字合成为豆包语音并发送（英文别名）",
        pattern=rf"(?i)^{COMMAND_AT_PREFIX}/doubao(?:\s+(?P<content>[\s\S]+))?$",
    )
    async def handle_tts_command(self, stream_id: str = "", text: str = "", **kwargs: Any):
        return await self._handle_debug_command(stream_id, text, kwargs)

    @Command(
        "volc_tts_voice_list",
        description="列出内置音色别名",
        pattern=rf"^{COMMAND_AT_PREFIX}/豆包音色$",
    )
    async def handle_voice_list(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        if not self.config.plugin.command_enabled:
            return True, "语音命令已关闭。", True
        current = self._resolve_speaker(self.config.tts.speaker)
        lines = [f"🎤 豆包语音合成 2.0　当前音色：{current}"]
        for alias, voice_id in VOICE_ALIASES.items():
            mark = " ← 当前" if voice_id == current else ""
            lines.append(f"· {alias}　{voice_id}{mark}")
        lines.append("\n不在列表里的音色，直接把控制台 voice_type 填到「音色 ID」；完整列表见官方音色列表。")
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "已发送音色列表", True

    @Command(
        "volc_tts_settings",
        description="查看当前豆包语音配置",
        pattern=rf"^{COMMAND_AT_PREFIX}/豆包设置$",
    )
    async def handle_settings(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        if not self.config.plugin.command_enabled:
            return True, "语音命令已关闭。", True
        cfg = self.config
        auth = "API Key" if cfg.credential.api_key.strip() else (
            "AppID + AccessKey"
            if self._has_auth()
            else "未配置（合成会失败）"
        )
        speaker = self._resolve_speaker(cfg.tts.speaker)
        effective, corrected = _resource_id_for_speaker(speaker, cfg.credential.resource_id)
        resource_line = (
            f"{cfg.credential.resource_id} → 实际使用 {effective}（已自动校正）"
            if corrected
            else cfg.credential.resource_id
        )
        kind = "声音复刻音色（S_）" if speaker.upper().startswith("S_") else "预置音色"
        text = (
            "🔧 豆包语音合成 2.0 当前配置\n"
            f"鉴权：{auth}\n"
            f"资源 ID：{resource_line}\n"
            f"音色：{speaker}（{kind}）\n"
            f"格式：{cfg.tts.audio_format}／{cfg.tts.sample_rate}Hz\n"
            f"语速：{cfg.tts.speech_rate:+d}　音量：{cfg.tts.loudness_rate:+d}\n"
            f"单次上限：{cfg.plugin.max_text_length} 字\n"
            f"情绪标记：{'开' if cfg.tts.emotion_enabled else '关'}\n"
            f"缓存：{cfg.tts.cache_size} 条（当前 {len(self._cache)}）"
        )
        await self.ctx.send.text(text, stream_id)
        return True, "已发送语音配置", True

    # ===== 内部实现 =====

    async def _handle_debug_command(self, stream_id: str, text: str, kwargs: dict[str, Any]):
        if not self.config.plugin.enabled:
            return False, None, True
        if not self.config.plugin.command_enabled:
            return True, "语音命令已在插件配置中关闭。", True
        if not stream_id:
            return False, "缺少 stream_id，无法发送语音。", True

        content = self._extract_command_content(text, kwargs)
        user_id = str(kwargs.get("user_id") or "").strip() or "command"
        if not content:
            await self.ctx.send.text(
                "用法：/豆包 要朗读的内容\n例如：/豆包 [开心] 你好，我是豆包语音合成二点零。\n"
                "其他：/豆包音色 看音色列表，/豆包设置 看当前配置",
                stream_id,
            )
            return True, "已提示用法", True

        ok, message = await self._speak(
            text=content,
            stream_id=stream_id,
            user_id=user_id,
            speaker="",
            enforce_cooldown=True,
        )
        if not ok:
            await self.ctx.send.text(f"语音发送失败：{message}", stream_id)
        return True, message, True

    def _extract_command_content(self, text: str, kwargs: dict[str, Any]) -> str:
        groups = kwargs.get("matched_groups")
        if isinstance(groups, dict):
            content = str(groups.get("content") or "").strip()
            if content:
                return content
        raw = str(kwargs.get("text") or text or "").strip()
        return re.sub(rf"(?i)^{COMMAND_AT_PREFIX}/(?:豆包|doubao)(?:音色|设置)?\s*", "", raw).strip()

    def _configured_speaker(self) -> str:
        """配置里的音色，若本身非法则退回内置默认，避免回退到同一个非法值。"""

        raw = str(self.config.tts.speaker or "").strip()
        resolved = VOICE_ALIASES.get(raw, raw)
        return resolved if _VALID_VOICE_PATTERN.match(resolved) else DEFAULT_SPEAKER

    def _resolve_speaker(self, value: str) -> str:
        """把别名/原值解析成官方 voice_type。

        非法值（角色昵称、中文名、空串）一律回退到配置音色：
        大模型会把角色名当 speaker 传进来，若放任它透传，
        后续按前缀校正 resource_id 会算错，服务端只会回一句
        ``55000000 resource ID is mismatched``，掩盖真实原因。
        """

        raw = str(value or "").strip()
        if not raw:
            return self._configured_speaker()
        resolved = VOICE_ALIASES.get(raw, raw)
        if _VALID_VOICE_PATTERN.match(resolved):
            return resolved
        fallback = self._configured_speaker()
        self.ctx.logger.warning(
            "[VolcTTS] speaker=%r 不是合法音色 ID（也不是内置别名），已回退到 %s",
            raw,
            fallback,
        )
        return fallback

    async def _speak(
        self,
        *,
        text: str,
        stream_id: str,
        user_id: str,
        speaker: str = "",
        enforce_cooldown: bool,
    ) -> tuple[bool, str]:
        cleaned = self._clean_text(text)
        if not cleaned:
            return False, "没有可朗读的文本。"

        max_length = max(1, int(self.config.plugin.max_text_length))
        if len(cleaned) > max_length:
            return False, f"文本过长（{len(cleaned)} 字），上限 {max_length} 字，请分段发送。"

        if enforce_cooldown and not self._check_cooldown(stream_id, user_id):
            return False, f"触发太频繁，请 {self.config.plugin.cooldown_seconds:g} 秒后再试。"

        try:
            audio = await self._synthesize(cleaned, speaker)
        except VolcTtsError as exc:
            self.ctx.logger.error("[VolcTTS] 服务端拒绝: %s", exc)
            return False, str(exc)
        except asyncio.TimeoutError:
            return False, f"合成超时（{self.config.tts.timeout_seconds} 秒）。"
        except Exception as exc:  # noqa: BLE001 - 需要转成可读提示
            self.ctx.logger.error("[VolcTTS] 合成失败: %s", exc, exc_info=True)
            return False, f"合成失败：{exc}"

        if not audio:
            return False, "合成结果为空，检查音色 ID 与资源 ID 是否匹配。"

        encoded = base64.b64encode(audio).decode("ascii")
        try:
            sent = await self.ctx.send.custom("voice", encoded, stream_id, storage_message=False)
        except TypeError:
            sent = await self.ctx.send.custom("voice", encoded, stream_id)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("[VolcTTS] 发送失败: %s", exc, exc_info=True)
            return False, f"发送失败：{exc}"

        if not sent:
            return False, "发送失败：适配器未确认。"

        if self.config.debug.log_synthesis:
            self.ctx.logger.info(
                "[VolcTTS] 已发送语音 stream=%s speaker=%s chars=%d bytes=%d",
                stream_id,
                self._resolve_speaker(speaker or self.config.tts.speaker),
                len(cleaned),
                len(audio),
            )
        return True, f"已发送语音（{len(cleaned)} 字）"

    # ----- 合成 -----

    async def _synthesize(self, text: str, speaker: str = "") -> bytes:
        cfg = self.config
        final_speaker = self._resolve_speaker(speaker or cfg.tts.speaker)
        cache_key = (text, final_speaker, cfg.tts.speech_rate, cfg.tts.loudness_rate)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached

        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(2)

        async with self._semaphore:
            audio = await self._synthesize_with_retry(text, final_speaker)

        if audio and int(cfg.tts.cache_size) > 0:
            self._cache[cache_key] = audio
            while len(self._cache) > int(cfg.tts.cache_size):
                self._cache.popitem(last=False)
        return audio

    async def _synthesize_with_retry(self, text: str, speaker: str) -> bytes:
        retry_times = max(0, int(self.config.tts.retry_times))
        last_error: Optional[Exception] = None
        for attempt in range(retry_times + 1):
            try:
                return await self._request_once(text, speaker)
            except VolcTtsError as exc:
                # 业务错误重试没意义（音色没授权、并发超限除外）
                if exc.code not in {"quota exceeded for types: concurrency"}:
                    raise
                last_error = exc
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                last_error = exc
            if attempt < retry_times:
                if self.config.debug.log_synthesis:
                    self.ctx.logger.warning(
                        "[VolcTTS] 第 %d 次合成失败，%.1fs 后重试: %s", attempt + 1, attempt + 1.5, last_error
                    )
                await asyncio.sleep(attempt + 1.5)
        if last_error is None:
            return b""
        raise RuntimeError(f"合成失败：{last_error}") from last_error

    async def _request_once(self, text: str, speaker: str) -> bytes:
        cfg = self.config
        resource_id, corrected = _resource_id_for_speaker(speaker, cfg.credential.resource_id)
        if corrected and self.config.debug.log_synthesis:
            self.ctx.logger.warning("[VolcTTS] %s", corrected)
        headers = self._build_headers(resource_id)
        body_text, context_texts = (text, [])
        if cfg.tts.emotion_enabled and resource_id in {"seed-tts-2.0", "seed-icl-2.0"}:
            body_text, context_texts = split_emotion_prefix(text)

        req_params: dict[str, Any] = {
            "text": body_text,
            "speaker": speaker,
            "audio_params": {
                "format": cfg.tts.audio_format,
                "sample_rate": int(cfg.tts.sample_rate),
            },
        }
        additions: dict[str, Any] = {}
        if int(cfg.tts.speech_rate) != 0:
            additions["speech_rate"] = int(cfg.tts.speech_rate)
        if int(cfg.tts.loudness_rate) != 0:
            additions["loudness_rate"] = int(cfg.tts.loudness_rate)
        if context_texts:
            additions["context_texts"] = context_texts
        if additions:
            # additions 必须是 JSON 字符串，不是对象
            req_params["additions"] = json.dumps(additions, ensure_ascii=False)

        payload = {"user": {"uid": "maibot-volc-tts"}, "req_params": req_params}

        if self.config.debug.log_synthesis:
            # 失败时也要能看到实际发出的组合，否则只能靠猜
            self.ctx.logger.info(
                "[VolcTTS] 请求 speaker=%s resource_id=%s chars=%d emotions=%s",
                speaker,
                resource_id,
                len(body_text),
                context_texts or "无",
            )

        client = self._client
        if client is None:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=10.0, read=float(cfg.tts.timeout_seconds), write=30.0, pool=10.0
                ),
                follow_redirects=True,
            )
            self._client = client

        buffer = bytearray()
        async with client.stream("POST", TTS_ENDPOINT, headers=headers, json=payload) as resp:
            logid = resp.headers.get("X-Tt-Logid", "")
            if resp.status_code != 200:
                raw = (await resp.aread()).decode("utf-8", "replace")[:600]
                code, message = _parse_error_body(raw, resp.status_code)
                raise VolcTtsError(code, message, logid)

            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                data = chunk.get("data")
                if isinstance(data, str) and data:
                    try:
                        buffer.extend(base64.b64decode(data))
                    except (ValueError, TypeError):
                        continue
                code = chunk.get("code")
                if code in (None, 0, "0"):
                    continue
                code_text = str(code)
                if code_text == "20000000":
                    break
                raise VolcTtsError(code_text, str(chunk.get("message") or ""), logid)

        if not buffer:
            raise VolcTtsError("empty", "服务端未返回音频数据（检查 speaker 与 resource_id 是否匹配）")
        if self.config.debug.log_synthesis:
            self.ctx.logger.debug(
                "[VolcTTS] 合成完成 speaker=%s chars=%d bytes=%d context=%s",
                speaker,
                len(body_text),
                len(buffer),
                context_texts,
            )
        return bytes(buffer)

    def _has_auth(self) -> bool:
        cfg = self.config.credential
        if cfg.api_key.strip():
            return True
        return bool(cfg.app_id.strip()) and bool(cfg.access_key.strip())

    def _build_headers(self, resource_id: str | None = None) -> dict[str, str]:
        cfg = self.config.credential
        headers = {
            "Content-Type": "application/json",
            "X-Api-Resource-Id": resource_id or cfg.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "Accept": "*/*",
        }
        api_key = cfg.api_key.strip()
        if api_key:
            headers["X-Api-Key"] = api_key
        else:
            if not cfg.app_id.strip() or not cfg.access_key.strip():
                raise VolcTtsError("auth", "缺少鉴权：填 api_key，或同时填 app_id + access_key")
            headers["X-Api-App-Id"] = cfg.app_id.strip()
            headers["X-Api-Access-Key"] = cfg.access_key.strip()
        return headers

    # ----- 杂项 -----

    def _check_cooldown(self, stream_id: str, user_id: str) -> bool:
        cooldown = max(0.0, float(self.config.plugin.cooldown_seconds))
        if cooldown <= 0:
            return True
        now = time.monotonic()
        key = (stream_id, user_id)
        if now - self._last_trigger.get(key, 0.0) < cooldown:
            return False
        self._last_trigger[key] = now
        if len(self._last_trigger) > 512:
            expire_before = now - max(cooldown * 10, 600)
            self._last_trigger = {k: t for k, t in self._last_trigger.items() if t >= expire_before}
        return True

    @staticmethod
    def _clean_text(text: str) -> str:
        cleaned = _AT_TAG_PATTERN.sub("", str(text or ""))
        cleaned = _CQ_CODE_PATTERN.sub("", cleaned)
        # 命令前缀可能随 matched_groups 缺失一起传进来，统一在这里剥掉
        cleaned = re.sub(rf"(?i)^{COMMAND_AT_PREFIX}/(?:豆包|doubao)(?:音色|设置)?\s*", "", cleaned.strip())
        cleaned = cleaned.replace("`", "").replace("**", "")
        cleaned = _WHITESPACE_PATTERN.sub(" ", cleaned)
        return cleaned.strip()


def create_plugin() -> VolcTtsPlugin:
    """插件入口。"""

    return VolcTtsPlugin()
