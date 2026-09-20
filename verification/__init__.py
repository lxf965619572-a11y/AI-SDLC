"""代码验证执行层：把编译、运行、覆盖率采集放到可信的 Linux 工具链上执行。

分工：
  runner    执行环境抽象（SSH 远端 / 本机），只认 run(cmd, cwd) 与 sync(files, dir)
  buildkit  构建脚本与 C 测试桩模板生成（gcc -std=c99 + gcov）
  parsers   把工具输出解析成结构化结论（测试用例 PASS/FAIL、gcov 覆盖率）

这一层刻意不含任何 LLM 与数据库依赖：全部是可离线单测的纯逻辑 + 子进程调用，
出问题时能明确区分「AI 生成的东西不对」和「执行环境不对」。
"""
