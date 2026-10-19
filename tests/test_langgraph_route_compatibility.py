"""Exercise LangGraph's installed middleware mounting against our custom app."""

import ast
import copy
from importlib.util import find_spec
from pathlib import Path

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import BaseRoute

from deep_data_research_agent.api.app import app


def test_custom_routes_support_langgraph_middleware_mounting():
    # 导入 server 模块会启动整套服务；只加载已安装版本的真实挂载函数，
    # 既覆盖实际失败路径，也避免单元测试依赖数据库、Redis 和监听端口。
    specification = find_spec("langgraph_api")
    source = Path(specification.origin).with_name("server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "apply_middleware")
    namespace = {"BaseRoute": BaseRoute, "Middleware": Middleware}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "langgraph_api.server", "exec"), namespace)  # noqa: S102 - 仅执行已安装依赖的真实挂载函数
    routes = [copy.copy(route) for route in app.routes]
    mounted = namespace["apply_middleware"](routes, [Middleware(CORSMiddleware, allow_origins=[])])
    assert all(isinstance(route.app, CORSMiddleware) for route in mounted)
    assert {route.path for route in mounted if route.path.startswith("/data/")} == {
        "/data/catalog", "/data/table", "/data/query", "/data/export",
    }
