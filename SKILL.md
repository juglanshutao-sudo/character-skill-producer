---
name: character-skill-producer
description: 为原神、崩坏星穹铁道、绝区零、鸣潮、明日方舟、终末地生成或更新角色 Skill。按游戏指定网站获取语料，遵循共用 SKILL_V2 蒸馏标准，整理证据并执行结构检查和独立行为测试。用户要求蒸馏角色、制作角色技能或从语料生成角色时使用。
---

# 角色 Skill 蒸馏器

## 开始

将本文件所在目录记为安装目录 S；另选可写工作目录 W。命令中的 S/W 是说明用变量，执行时替换成实际绝对路径并加引号。依赖 Python 3.10+，绝区零的 HTML 解析另需 `requirements.txt` 中的依赖。宿主需有文件、命令执行和网络能力；行为验证还需独立、无记忆的模型执行上下文。

1. 读 [共用标准](references/SKILL_V2.md) 和 [便携版执行流程](references/workflow.md)。六游戏共用这一份标准，不能自行降低蒸馏质量要求。
2. 新工作目录执行 `python -B "S/scripts/csp.py" init --workspace "W"`。已有便携工作区直接续作；不覆盖它的配置或产物。
3. 执行 `python -B "W/scripts/csp.py" doctor`，按实际缺项修环境。首次成功后不必每个角色重复检查。
4. 执行 `python -B "W/scripts/csp.py" sources --game "游戏名" --workspace "W"`，读返回的游戏指南，确认角色身份、形态、语种和资料截止范围。用户只给角色名且作品不明确时询问作品。

## 按游戏取材

| 游戏 | 专用指南 | 默认来源 |
|---|---|---|
| 原神 | [genshin](references/games/genshin.md) | Yatta |
| 崩坏星穹铁道 | [hsr](references/games/hsr.md) | 米哈游 Wiki、星铁 BWIKI |
| 绝区零 | [zzz](references/games/zzz.md) | 日文 wikiwiki.jp/zenless；可导入用户文本 |
| 鸣潮 | [wuwa](references/games/wuwa.md) | 库街区 |
| 明日方舟 | [arknights](references/games/arknights.md) | Kengxxiao 数据仓库；百科辅助 |
| 终末地 | [endfield](references/games/endfield.md) | fz.wiki |

默认网站由用户指定。新工作区从 [game-sources.json](references/game-sources.json) 复制配置，后续修改 W 内同名文件。禁用某来源可设 `enabled: false`。网站变更后，通用 fetch 能获取已配置 URL；专用解析器是否适配需另行验证，不能只改域名就声称支持新 API。

网络取材使用 `csp.py fetch` 或 `csp.py run`，以保存请求证据并校验本游戏 URL。浏览器补查也遵守同一清单；其他游戏的站点、未配置镜像不自动作为替代来源。失败时记录实际原因，先用已有合法语料；确需新增来源再询问用户。访问成功仍需检查正文是否为错误页、空页或与角色无关。

## 产出与验证

按 workflow 执行：原始响应 → 清洗切片 → 五维调研 → 行为规则和例子 → 角色 SKILL → 结构质检 → 独立行为验证。只生成用户请求的角色或批次，不默认下载六游戏全量数据。

产出位于 `W/examples/<游戏>/<游戏ID>-<角色slug>skill/`。不同游戏角色目录名保持唯一，避免旧工具的运行日志相撞。原始响应保留，翻译和清洗另存派生文件；游戏文本与生成角色均不写进安装目录。

最终说明实际完成的步骤、可用角色路径、缺失证据、测试结果与未测部分。源码快照里的历史批次已完成声明不代表此次验证；没有真实答卷及独立判卷，就不能写行为测试通过。
