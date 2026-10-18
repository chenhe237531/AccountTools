"""AccountTools 应用包。

新框架分层：
  app/core     共享内核（路径/配置/日志/后台任务/签到编排/FastAPI 工厂）
  app/agents   软件 agent 适配器插件（一个文件 = 一个 agent，自动注册）
  app/static   前端单页（web 端与桌面端共用同一后端与同一页面）

运行方式：
  开发/Web 形态:  python3 -m app [--port 8800] [--no-open]
  桌面形态:       desktop/ Electron 壳拉起打包后的同一后端（见 scripts/ 与 .github/workflows/）
"""
__version__ = "2.0.0"
