# 兼容与验证说明

本发布候选为插件 **1.0.1**；名称中的“豆包语音合成 2.0”指语音模型系列。

## 声明范围

| 组件 | 范围 |
| --- | --- |
| MaiBot | 1.3.16–1.3.19 |
| maibot-plugin-sdk | 2.16.0–2.21.0 |
| MaiBot 1.3.19 配套 SDK | 2.20.0 或 2.21.0 |
| Python | 3.12.x |

验证机器为 Windows；其他系统和上述范围内的每个中间版本没有逐一测试。

## 离线验证

- SDK 2.16.0、2.20.0、2.21.0 各运行 20 项 unittest，全部通过。
- 使用假凭据、HTTP 模拟响应与替身发送端，覆盖鉴权选择、情绪、音色保护、命令匹配、缓存、冷却、字数限制、API 开关、响应解析和生命周期。
- 使用官方 MaiBot 1.3.19 的清单校验器和插件加载器，分别配合 SDK 2.20.0 / 2.21.0，验证新目录发现、入口创建、空凭据配置和加载/配置更新/卸载。
- 组件正常收集：1 个模型工具、1 个公开 API、4 条命令。
- 市场当前官方 manifest 字段校验通过。公开仓库可访问性和 Release 要求需在实际上传并发布后再次确认。

宿主的日志、路径与版本发现服务在隔离加载检查中使用替身；没有启动完整 MaiBot Host、调用真实合成接口或向聊天发送消息。

## 未覆盖范围

离线结果不能证明具体账号鉴权、特定音色授权、音频听感或目标平台播放已经通过。完整 Host 中的命令路由、长时间并发、网络中断恢复和配置热更竞争仍需实际场景验收。

公开 API 不发送消息；微信桥接等调用方需要自行匹配音频格式与采样率，不能将“合成成功”当作“平台发送成功”。

## 官方参考

- [MaiBot v1.3.19](https://github.com/Mai-with-u/MaiBot/releases/tag/v1.3.19)
- [MaiBot 1.3.19 依赖](https://github.com/Mai-with-u/MaiBot/blob/v1.3.19/pyproject.toml)
- [SDK v2.20.0](https://github.com/Mai-with-u/maibot-plugin-sdk/tree/v2.20.0)
- [SDK v2.21.0](https://github.com/Mai-with-u/maibot-plugin-sdk/tree/v2.21.0)
- [市场提交规范](https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md)
