# 插件市场提交资料

申请前核对公开仓库及与根目录 manifest 版本一致的非草稿 Release `v1.0.1`。市场接受预发布；仅创建 Tag 不满足发布要求。

项目仓库：<https://github.com/suodeovo/MaiBot-Doubao-TTS>。发布包见 [v1.0.1](https://github.com/suodeovo/MaiBot-Doubao-TTS/releases/tag/v1.0.1)。市场收录需要另行申请，本文件不代表插件已被市场收录。

## 申请字段

- 标题：`[Plugin] MaiBot 豆包语音合成 2.0`
- 插件 ID：`local.volc.tts2`
- 仓库地址：`https://github.com/suodeovo/MaiBot-Doubao-TTS`

完成公开仓库上传和 Release 发布后，再核对并勾选申请表中的公开仓库、根目录 manifest、manifest v2 必需字段及 LICENSE 四项。

## 补充说明

MaiBot 的豆包语音合成插件，提供文字转语音工具、手动命令与公开合成 API，支持音色、情绪标记、缓存和发送控制。名称中的 2.0 指语音模型系列，插件版本为 1.0.1。声明兼容 MaiBot 1.3.16–1.3.19，实际验证范围详见 README 和兼容说明。需要使用者自行配置火山引擎凭据和服务权限，平台语音发送取决于适配器支持的音频格式。

## 官方入口

- [添加插件](https://github.com/Mai-with-u/plugin-repo/issues/new?template=add-plugin.yml)
- [贡献规范](https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md)

申请后等待自动校验和维护者审核；验证失败时按提示修复，再评论 `/recheck`。
