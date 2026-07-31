---
name: skill-manage
description: 管理 Skill 的下载、创建、测试和分配流程。
---

# Skill 管理

在沙箱内为子 Agent 创建或下载 Skill，测试通过后用 assign_skill 分配并持久化。

## 阶段一 — 获取 Skill

① 下载模式：用户提供 URL 时，用 execute 下载 ZIP 并解压到 /skills/main/{name}/；
   校验 SKILL.md 存在，且 frontmatter 含 name 和 description。
② 创建模式：用户描述需求时，用 write_file 生成 /skills/main/{name}/SKILL.md，
   附属脚本一并写入同目录。

## 阶段二 — 功能测试

③ 在 /skills/main/{name}/ 中用 ls 查看目录结构、read_file 阅读说明、execute 运行脚本；
   缺少第三方依赖时执行 pip install <包名>。测试失败则按错误信息修改后重新测试。

## 阶段三 — 分配

④ 测试通过后调用：

   assign_skill(skill_name="{name}", targets=["supervisor"] 或 ["crawl-worker"])

   工具自动把 Skill 持久化到 MongoDB 并清理临时目录。未指定目标时先向用户确认；
   目标无效时按返回的“可用目标”纠正。

## 阶段四 — 完成

目标 Agent 在下一轮对话中自动加载该 Skill（恢复至 /persisted-skills/）。

## 注意事项

- 候选 Skill 必须位于 /skills/main/ 下；SKILL.md frontmatter 必须恰好为 {name, description}，
  且 name 等于目录名。
- 不要用异步任务工具或异步子 Agent 处理 Skill。
- 下载与依赖安装依赖沙箱联网；失败时说明原因并继续对话。

## 可用工具

- write_file / read_file / ls / execute / assign_skill
