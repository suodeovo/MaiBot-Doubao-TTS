# MaiBot 豆包语音合成 2.0

[项目主页](https://github.com/suodeovo/MaiBot-Doubao-TTS) · [下载发布包](https://github.com/suodeovo/MaiBot-Doubao-TTS/releases) · [问题反馈](https://github.com/suodeovo/MaiBot-Doubao-TTS/issues)

将火山引擎豆包语音合成接入 MaiBot，提供文字转语音工具、手动命令与供其他插件调用的合成 API。

名称中的 **2.0** 指语音模型系列；当前插件版本为 **1.0.1**，插件 ID 为 `local.volc.tts2`。

## 功能

| 功能 | 说明 |
| --- | --- |
| 模型工具 | `volc_tts_speak` 将文字合成并发送到当前会话 |
| 手动命令 | `/豆包`、`/doubao`、`/豆包音色`、`/豆包设置` |
| 公开 API | `local.volc.tts2.synthesize` 返回 base64 音频，不负责发送 |
| 音色 | 支持内置中文别名、预置音色 ID 及已授权复刻音色 ID |
| 情绪标记 | 将开头的 `[开心]` 等标记转为合成指令 |
| 合成控制 | 字数上限、命令冷却、并发限制、失败重试与内存缓存 |

## 运行要求

| 组件 | 版本或要求 |
| --- | --- |
| MaiBot | 声明兼容 1.3.16–1.3.19 |
| 插件 SDK | 2.16.0–2.21.0；MaiBot 1.3.19 配套使用 2.20.0 或 2.21.0 |
| Python | 3.12.x，使用宿主的 Python 环境 |
| Python 依赖 | `httpx>=0.27.0` |
| 火山引擎 | 可用的语音合成凭据、资源权限及音色权限 |
| 平台适配器 | 需要支持语音发送，并接受配置的音频格式 |

本插件独立于微信桥接插件，不依赖微信客户端、WeChatAgent 或 `pilk`。音频能否发送及播放取决于目标平台适配器。

## 安装

1. 将仓库内容放入 `MaiBot/plugins/volc_tts/`，确保该目录直接包含 `plugin.py` 和 `_manifest.json`。插件专用 ZIP 已包含 `volc_tts/` 外层目录。
2. 在 MaiBot 的 Python 环境中安装依赖：

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. 启动或重新加载插件，使用 MaiBot 插件配置页填写鉴权与音色。也可将 [config.toml.example](config.toml.example) 复制为插件目录的 `config.toml` 后填写。
4. 从目标聊天发送 `/豆包 你好`，检查合成与平台发送结果。

更新前备份原插件和配置，更新时保留自己的 `config.toml`。发布包不包含任何可直接使用的账号凭据。

## 配置

完整示例见 [config.toml.example](config.toml.example)。鉴权支持两种填写方式，两种都填时优先使用 API Key：

| 方式 | 字段 |
| --- | --- |
| API Key | `credential.api_key` |
| AppID + Access Key | `credential.app_id` 和 `credential.access_key` |

`credential.resource_id` 需与音色及账户授权匹配：默认预置音色使用 `seed-tts-2.0`；`S_` 开头的复刻音色使用对应复刻资源。当前代码会按音色类型纠正不匹配的资源类别；这不能代替服务端授权。

| 设置 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `true` | 工具、合成命令与公开合成 API 的开关 |
| `plugin.command_enabled` | `true` | 手动调试命令开关 |
| `plugin.max_text_length` | `300` | 清理后的单次文本字数上限 |
| `plugin.cooldown_seconds` | `3` | 手动合成命令的用户/会话冷却；工具和 API 不受此冷却控制 |
| `tts.speaker` | 小何预置音色 | 工具使用配置音色；公开 API 可传入 `speaker` |
| `tts.audio_format` | `mp3` | 可选 `mp3`、`ogg_opus`、`pcm` |
| `tts.sample_rate` | `24000` | 需与服务及目标适配器支持的采样率匹配 |
| `tts.speech_rate` / `loudness_rate` | `0` | 语速、音量参数 |
| `tts.emotion_enabled` | `true` | 处理开头的情绪标记 |
| `tts.timeout_seconds` | `60` | 流响应读取超时，不是整个任务的总时限 |
| `tts.retry_times` | `1` | 可重试错误的额外尝试次数 |
| `tts.cache_size` | `16` | 进程内缓存条数；配置更新或卸载时清空 |

请求文本会发送至火山引擎。实际合成可能产生服务费用，资源及计费以账户控制台为准。

## 使用

```text
/豆包 你好，今天过得怎么样？
/豆包 [开心] 很高兴见到你。
/doubao Hello!
/豆包音色
/豆包设置
```

模型工具 `volc_tts_speak` 只声明 `text` 参数，音色由插件配置固定。手动命令不会修改配置。

其他插件可调用：

```python
result = await self.ctx.api.call(
    "local.volc.tts2.synthesize",
    text="你好，很高兴见到你。",
)
```

调用方需按宿主要求声明 `api.call` 能力。成功返回 `success`、`audio_base64`、`mime_type`、`bytes`；失败返回 `success=false` 和 `error`。当前 MP3 的 MIME 为 `audio/mpeg`，PCM 和 OGG 均返回 `audio/octet-stream`，调用方需结合配置识别格式。

### 与微信桥接配合

微信桥接可通过上述 API 获取音频。当前 MaiBot-WeChat-Bridge 的转换路径应使用 `tts.audio_format="pcm"`、`tts.sample_rate=24000`，再由桥接侧的 `pilk` 编码为 SILK；默认 MP3 不能直接交给该桥接的现有解码路径。

`pilk` 属于桥接侧的可选依赖。该配置不代表本插件的直接发送工具能在所有平台发送裸 PCM，仍需以适配器能力为准。

## 命令行验证

可选工具 [tools/volc_tts_cli.py](tools/volc_tts_cli.py) 只依赖 `httpx`，从环境变量读取凭据，不读取生产插件配置：

```powershell
$env:VOLC_API_KEY = "填写自己的 API Key"
python tools/volc_tts_cli.py --text "你好" --out demo.mp3
```

使用 AppID 鉴权时设置 `VOLC_APP_ID` 与 `VOLC_ACCESS_KEY`；复刻音色需同时指定匹配的 `--speaker` 和 `--resource-id`。该工具仅验证服务合成，不验证 MaiBot 或平台语音发送。

## 验证与限制

```powershell
python -m unittest discover -s tests -v
```

测试使用假凭据、替身发送端和 HTTP 模拟响应，不会访问真实合成接口。SDK 2.16.0、2.20.0、2.21.0 的验证范围见 [兼容与验证说明](docs/COMPATIBILITY.md)。

- 发布检查不等于真实账号、音色及平台播放验收；其他操作系统未单独验证。
- 缓存不是持久化的，相同请求并发到达时不保证合并；工具/API 的调用频率需由使用方管理。
- 本插件只合成语音，不提供语音识别、音色训练或微信音频解码。
- 预置音色、复刻音色及情绪指令的可用性取决于服务端授权和支持情况。

## 许可与参考

项目沿用原插件清单声明的 **MIT** 许可，见 [LICENSE](LICENSE)。依赖说明见 [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md)。

- [MaiBot](https://github.com/Mai-with-u/MaiBot)
- [MaiBot Plugin SDK](https://github.com/Mai-with-u/maibot-plugin-sdk)
- [火山引擎语音合成文档](https://www.volcengine.com/docs/6561/1598757)
