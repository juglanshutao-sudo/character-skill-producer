# Character Skill Producer

把游戏角色资料整理成可溯源、可更新的角色 Agent Skill。

本项目是可独立安装的 **Skill 生产器**：六游戏共用 `references/SKILL_V2.md`，按游戏指定网站获取资料，再执行研究、行为蒸馏、结构检查和独立行为验证。需要宿主 coding agent 执行流程；不包含模型服务。

**当前版本为预览版。** 工具自检和可移植性已测试，未对六游戏完成全新的端到端模型验收。已知限制见下表及 [VALIDATION.md](VALIDATION.md)。

## 默认来源

| 游戏 | 指定来源 | 本次联网验证范围 |
|---|---|---|
| 原神 | Yatta（第三方游戏数据站） | 角色索引响应成功 |
| 崩坏星穹铁道 | 米哈游 Wiki、星铁 BWIKI | 米哈游剧情索引解析成功；未全量验证 BWIKI |
| 绝区零 | wikiwiki.jp/zenless（日文社区 Wiki） | 2026-10-22 观察到维护公告，恢复后需复测 |
| 鸣潮 | 库街区 Wiki API | 目录树解析成功 |
| 明日方舟 | Kengxxiao 游戏数据仓库；PRTS/ASTR/萌百/BWIKI 辅助 | GitHub 仓库元数据可读取；未全量验证正文和辅助站 |
| 终末地 | fz.wiki（社区 Wiki） | 首页及条目可下载；旧名册解析器不适配当前响应，尚待修复 |

终末地旧 `roster` 命令目前不能作为可靠名册生成器。可先用通用 fetch 留存实际页面，或导入用户已有原文，人工核对后再蒸馏。网页 HTTP 200 不等于正文提取成功。

## 安装

下载本仓库 ZIP 或克隆，将完整目录放入宿主支持的技能目录，入口是 [SKILL.md](SKILL.md)。具体宿主如何发现技能，以它的安装说明为准。也可直接让 agent 读取本项目的 SKILL.md。

```text
git clone https://github.com/juglanshutao-sudo/character-skill-producer.git
python -m pip install -r character-skill-producer/requirements.txt
python -B character-skill-producer/scripts/csp.py doctor
python -B character-skill-producer/scripts/csp.py init --workspace "新的工作目录"
```

需要 Python 3.10+。beautifulsoup4 用于绝区零网页解析。初始化目录必须是新的或空的；安装目录保持只读，所有语料、产物和运行记录写入独立工作区。

对 agent 说：

> 使用 character-skill-producer，在指定工作目录为《原神》的“七七”生成角色 Skill。沿用配置的来源，遵守共用 SKILL_V2 标准。缺证据或未执行的测试如实标注。

详见 [INSTALL.md](INSTALL.md) 和 [执行流程](references/workflow.md)。

## 自定义网站

编辑工作区的 `references/game-sources.json`。各游戏独立设置来源与 URL 前缀，`enabled: false` 可停用某来源。通用抓取支持新的已配置 URL；新站点的页面结构或 API 如有不同，还需编写适配器。修改配置并不意味着解析器自动适配。

## 产出和测试

角色产出位于工作区 `examples/<游戏>/<角色目录>/`，包括 SKILL.md、manifest、研究材料、来源索引与实际测试结果。抓取保留原始响应和时间/哈希；本地导入与联网抓取分别标注。

```text
python -B scripts/preflight_check.py
python -B tests/test_validation_gates.py
python -B tests/test_portable.py
```

当前 27 项门禁回归和 8 项便携测试通过。模型答题和判卷需要宿主提供独立上下文；只有生成提示词或执行 finalize，不算实际完成行为测试。新版本输入须重新绑定，不能为旧报告补造历史哈希。

## 数据范围与许可证

本仓库只发布生产器代码、标准、配置、文档和测试。不包含原始游戏文本、已生成角色库、个人工作记录或账号凭据。使用者获取的资料保留在自己的工作区。

代码沿用上游 [MIT 许可证](LICENSE)，保留原版权声明。第三方内容不属于本项目许可证范围。贡献演示和测试请使用自拟内容，不提交游戏原文或抓取缓存。

共用标准的上游文件指纹及便携修改记录见 [upstream-snapshot.json](references/upstream-snapshot.json)。这里保留的历史标准不是对当前所有渠道和角色质量的认证。
