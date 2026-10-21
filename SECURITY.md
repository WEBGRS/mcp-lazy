# Security

Report a vulnerability privately: **Security → Report a vulnerability** on this repository
(https://github.com/WEBGRS/mcp-lazy/security/advisories/new). Please don't open a public issue for it.

What mcp-lazy touches, so you can judge the impact:

- `apply_config.py` reads and rewrites `~/.claude.json` and the Codex `config.toml`. It saves the
  values it replaces in `original-mcpServers.json` / `original-codex-mcp.json` and a timestamped
  backup of the whole file next to itself. Those copies can hold API keys from your server entries;
  they are in `.gitignore`.
- The proxy starts the command written in your config and passes messages between the client and
  that server. It caches the handshake and list replies in `cache/`, writes logs to `logs/` and live
  state to `run/`.
- Nothing is sent over the network by mcp-lazy itself.
