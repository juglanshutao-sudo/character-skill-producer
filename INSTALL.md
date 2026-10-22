# 安装和使用

解压压缩包，保持 `character-skill-producer/` 整个目录完整。将它放进宿主支持的技能目录，入口是 `SKILL.md`；不同宿主的导入入口以其自身说明为准。如果宿主不支持安装技能，也可让 coding agent 直接读取该目录的 SKILL.md。无需复制原工程或任何游戏语料。

需要 Python 3.10+、网络、可写工作目录；绝区零网页解析需要额外 Python 包。在所使用的 Python 环境执行：

```text
python -m pip install -r "安装目录/requirements.txt"
python -B "安装目录/scripts/csp.py" doctor
python -B "安装目录/scripts/csp.py" init --workspace "新的工作目录"
```

建议给 agent 的指令：

> 使用 character-skill-producer，为《原神》的“七七”生成角色 skill。工作目录为我指定的新目录，沿用配置里的 Yatta 来源，按共用 SKILL_V2 标准蒸馏；实际没测的部分明确写未测。

每个游戏均用同一份 `references/SKILL_V2.md`，游戏指南只管来源和该游戏的取材区别。`init` 将所需脚本、标准和配置复制到新工作目录，后续工具在此执行；不写安装目录。已有非空目录不会被覆盖。

修改网站：编辑工作目录 `references/game-sources.json` 中相应游戏的 `sources`。改安装目录的默认配置只影响之后初始化的工作区，不改变已开始的任务。URL 前缀区分主机及路径；配置任意网站不会自动生成它的新解析器，可先用通用 fetch 留原文，再适配解析。

带入已有语料时放工作目录，并记录原始来源及时间。安装包不包含任何旧角色、游戏文本、账号、API 密钥或模型运行服务。自动生成和独立测试由宿主 agent 执行；脚本检查通过不代表角色生成或网站端点已经通过全流程验证。

本次验证范围与站点状态见 [VALIDATION.md](VALIDATION.md)。这是指定时间和网络下的测试结果，网站可用性仍可能变化。
