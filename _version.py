# -*- coding: utf-8 -*-
"""版本号单一来源。

main.py 的 APP_VERSION、mcp_server 的 SERVER_VERSION 和 pyproject.toml 的
项目版本都从这里取(pyproject 用 setuptools dynamic attr 引用),
发版时只改这一处。
"""

__version__ = "1.16.1"
