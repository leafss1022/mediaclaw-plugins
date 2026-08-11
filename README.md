# mediaclaw 插件市场

这是 mediaclaw 的官方插件市场仓库（`leafss1022/mediaclaw-plugins`）。
插件体系设计见主仓库 `docs/design/plugin-system.md`；本目录是提交模板与
接入规范，同步发布到市场仓库根目录。

## 插件怎么提交

1. **Fork** 市场仓库，在 `plugins/<你的插件 id>/` 下放插件包：

   ```text
   plugins/<plugin_id>/
   ├── plugin.json   # 清单（必填，见下方规范）
   ├── main.py       # 入口：class Xxx(PluginBase)，可加 static/、locales/
   └── ...           # 其它随包文件
   ```

2. 本地校验：`python -m mediaclaw_plugin_sdk.scan plugins/<plugin_id>`（P4 提供
   SDK 后可用；当前可对照 `docs/design/plugin-system.md §8` 自查：不得导入
   subprocess/socket/ctypes/pickle 等，不得 import 宿主内部模块，只能从
   `mediaclaw_plugins.sdk` 取基类）。
3. **PR 合入**：CI 会校验清单格式 + 静态扫描；合入后 `index.json` 自动生成，
   用户即可在 mediaclaw「插件市场」看到并安装。

## plugin.json 字段

```json
{
  "id": "example-hello",
  "name": "示例插件",
  "description": "一句话说明插件做什么。",
  "author": "你的名字",
  "license": "MIT",
  "version": "1.0.0",
  "icon": "icon.png",
  "tags": ["工具"],
  "category": "tool",
  "homepage": "https://github.com/你/你的插件仓库",
  "min_app_version": "0.8.0",
  "api_range": {"min": 1, "max": 3},
  "entry": "main.py",
  "extension_points": ["scheduler.task"],
  "permissions": {},
  "settings": {
    "type": "object",
    "properties": {
      "greeting": {"type": "string", "default": "你好"}
    }
  },
  "on_events": []
}
```

字段语义见 `docs/design/plugin-system.md §1.2`。注意：**首版不允许声明
额外 pip 依赖**（会破坏镜像 runtime 契约），插件只能用标准库或宿主已装的依赖。
