---
name: skill-manage
description: 管理 Skill 的下载、创建、测试和分配流程。
---

# Skill 管理

在 supervisor 沙箱内创建或下载 Skill，测试通过后用 assign_skill 分配并持久化。
路径映射规则见系统提示词。

## 阶段一 — 获取 Skill

① 下载模式（用户提供 URL）：
   - 用 execute 调用 Python 标准库 urllib.request 下载到 /tmp/，再用 zipfile 或 tarfile 解压；
   - 把文件放到物理 /{name}/，注意把压缩包内多余的顶层目录平铺掉，确保 SKILL.md 位于根级；
   - 用 read_file(/skills/main/{name}/SKILL.md) 校验 frontmatter 含 name 和 description。
② 创建模式（用户描述需求）：用 write_file 写 /skills/main/{name}/SKILL.md，
   附属脚本一并写入同目录。

## 阶段二 — 功能测试

③ 用 read_file 阅读 /skills/main/{name}/SKILL.md，用 ls 查看目录结构、execute 运行脚本；
   候选测试使用临时物理路径 /{name}/。缺少第三方依赖时执行 pip install <包名>。
   测试失败则按错误信息修改后重新测试。

## 阶段三 — 分配

④ 测试通过后调用：

   assign_skill(skill_name="{name}", targets=["supervisor"] 或 ["crawl-worker"])

   工具自动把 Skill 持久化到 MongoDB 并清理临时目录。未指定目标时先向用户确认；
   目标无效时按返回的“可用目标”纠正。

## 阶段四 — 完成

目标 Agent 在下一轮对话中自动加载该 Skill（恢复至
/persisted-skills/active/{name}/，沙箱物理路径相同）。

## 注意事项

- 候选 Skill 必须位于 /skills/main/ 下；SKILL.md frontmatter 必须恰好为 {name, description}，
  且 name 等于目录名。
- /skills/main/{name}/ 和物理 /{name}/ 仅用于候选创建、下载和测试，分配完成后不得使用。
- 最终 SKILL.md 不得引用 /skills/main/{name}/ 或物理 /{name}/；最终脚本路径必须写成
  /persisted-skills/active/{name}/{script_path}。
- 不要在物理层用 mkdir / find / cp 做路径实验；一切以虚拟路径 /skills/main/{name}/ 为准。
- 不要用 glob 匹配 /skills/main/**（会扫描整个文件系统导致超时）；用 ls 逐层或直接 read_file。
- 不安装 curl、wget、unzip 等系统工具，不使用 apt/apk 或 `curl | sh`。下载和解压统一
  通过 execute 调用 Python 标准库 urllib.request、zipfile 或 tarfile。仅允许使用 pip
  安装候选 Skill 测试所需的 Python 依赖。
- assign_skill 失败时，按错误信息中给出的虚拟与物理路径检查，不要盲目试错。
- 不要用异步任务工具或异步子 Agent 处理 Skill。
- 下载与依赖安装依赖沙箱联网；失败时说明原因并继续对话。

## 可用工具

- write_file / read_file / edit_file / ls / execute / assign_skill
