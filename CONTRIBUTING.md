# Contributing

Thanks for taking a look.

- Run the tests before a pull request: `python -m unittest discover -s tests -v`. CI runs them on
  Windows and Linux with Python 3.11 and 3.13.
- Keep the proxy on the standard library. It starts once per server per session, so a new import
  costs every user memory and start time.
- A behaviour change needs a test. `tests/fake_server.py` is a minimal MCP server that leaves a marker
  file each time it starts, which is usually enough to check when a server was started.
- For a bug, the end of `logs/NAME.log` and the output of `python lazy_mcp.py --status` help most.
  Remove API keys and tokens from anything you paste.
