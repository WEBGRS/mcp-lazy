# mcp-lazy

让 MCP 服务器先睡着，真要调用工具时再叫醒。mcp-lazy 是给 Claude Code 和 Codex 用的一个小代理：握手从缓存里答，第一次调用工具时才启动真正的服务器，闲置一段时间后再停掉。

![空闲时的 MCP 内存：Claude Code 一个对话 500 MB → 45 MB，Codex 一个对话约 820 MB → 约 45 MB](docs/memory.png)

[English](README.md)

## 快速开始

```
git clone https://github.com/WEBGRS/mcp-lazy
cd mcp-lazy
python apply_config.py claude      # Codex 用：python apply_config.py codex
```

重启 Claude Code（或 Codex）就生效。想还原就运行 `python apply_config.py claude rollback`。需要 Python 3.11 以上，只用标准库。装完别挪这个文件夹，配置里指向的就是它。

## 为什么做

Claude Code 每开一个对话、Codex 每起一个 app-server，都会把配置里的 MCP 服务器全部启动，用不用都一直开着。有天晚上我开了 9 个 Claude Code，后台跑出 87 个 node 进程，占了大约 8 GB 内存，VS Code 卡得动不了。mcp-lazy 夹在客户端和服务器中间，没调用工具之前，服务器根本不会启动。

## 省了多少

- Claude Code 一个对话、5 个服务器（2026-10-20）：直接启动占 500.0 MB；换成 mcp-lazy 后，5 个休眠的代理一共 44.6 MB。
- Codex 一个 app-server（2026-10-21）：包住的 5 个服务器约 820 MB，换成后约 45 MB。Codex 是通过 `npx` 启动 npm 服务器的，所以每个都更占内存。

代价是：服务器睡着以后，第一次调用要等它启动。3 次冷启动的中位数：playwright 0.5 秒，firecrawl 0.9 秒，rea 2.4 秒。每个服务器的数字和测量方法见[英文版](README.md#what-it-saves)。

## 原理

- `initialize` 和几种 list 请求直接从缓存回答，其他请求来了才启动服务器。
- 缓存按服务器命令、客户端、协议版本和 npm 包版本区分，包升级后旧缓存自动失效。
- 闲置超过设定时间才停；还有请求在处理或者开着订阅时不会停。
- 服务器崩了，正在等的请求会收到错误，下一次调用重新起一个。
- 服务器的进程树和代理绑在一起，代理被杀，服务器也跟着结束。

## apply_config.py 改了什么

它把配置里每个 stdio 服务器（有 `command`、没有 `url` 的）改成 `python -I lazy_mcp.py 名字 -- 原来的命令和参数`。`env`、`cwd`、超时这些设置都不动。被替换的值存在脚本旁边，配置文件另存一份带时间戳的备份，最后预热缓存，第一个对话也不用启动任何服务器。Codex 的 `config.toml` 是原地修改的，注释和顺序都保留。Claude Code 只改 `~/.claude.json` 里用户级的 `mcpServers`。

```
python apply_config.py codex apply --only firecrawl,playwright*   # 只包这几个
python apply_config.py claude apply --skip my-server              # 跳过某个
```

### local.json（可选）

放在脚本旁边，不进 git，写本机专用的设置：

```json
{
  "skip": ["node_repl"],
  "commands": {
    "firecrawl": ["node", "C:/tools/mcp-lazy/pkgs/node_modules/firecrawl-mcp/dist/index.js"]
  },
  "codex_http": {
    "windows-mcp": { "url": "http://127.0.0.1:8000/mcp", "bearer_file": "C:/tools/key.txt" }
  }
}
```

- `skip`：永远不包的服务器，可以写通配符。
- `commands`：代理启动这个命令，而不是配置里原来的。Windows 上的 npm 服务器值得这么做：`npx -y 包名` 会把一个服务器变成 `cmd` → `npx` → `cmd` → `node` 四个进程，缓存也分不清包有没有升级。先 `npm install --prefix pkgs firecrawl-mcp` 装一次，再直接用 `node` 启动它的脚本。
- `codex_http`：让 Codex 的某个服务器改连你已经在跑的 HTTP 实例，不再包它。

## 常用命令

```
python lazy_mcp.py --status   # 各个对话、代理、醒着的服务器和它们的内存
python lazy_mcp.py --reap     # 停掉所有手上没活的服务器
python lazy_mcp.py --warm     # 给两个客户端配置里所有被包的服务器预热缓存
```

## 设置

`settings.json`，代理启动时读取：

| 键 | 含义 |
|---|---|
| `idle_seconds` | 按服务器名查：先找完全相同的名字，再找第一个匹配的 `*` 通配，最后用 `*`。`0` 表示永不停止。 |
| `max_cache_age_days` | 缓存超过这么多天，下次对话启动时重新获取。 |
| `start_timeout_seconds` | 真正的服务器要在这个时间内回应 `initialize`。 |

日志在 `logs/名字.log`。

## 局限

- 日常只在 Windows 11 上用。Linux 上测试能通过，但还没接真实客户端跑过；macOS 没试过。
- 需要自己一直在线的服务器（比如会主动推送通知的）别包。
- 服务器的工具列表如果变了，它启动之前，对话里看到的还是缓存里的旧列表；启动后客户端会收到 `list_changed`。

## 许可证

MIT。没有遥测，代理本身不联网。问题和建议欢迎提 issue，安全问题请看 [SECURITY.md](SECURITY.md)。
