# 便携版执行流程

所有游戏都读取 `SKILL_V2.md`。它是原工程标准的原样快照，细化了模式、例子、知识边界和测试要求。这里仅适配可移植执行：S 为安装目录，W 为新工作区；工作区自带脚本和 references，不需要原工程或个人磁盘。

## 标准与路径

- `W/SKILL_V2.md` 与安装包 `references/SKILL_V2.md` 内容一致；`W/references/` 存放模板、checks、蒸馏框架。
- 原标准中 `examples/<游戏>/CSP/SKILL.md`、旧批次 SOP、历史台账及渠道实测，由本包 `references/games/` 的指南替代。游戏来源以 `game-sources.json` 为唯一配置；旧 `channel-registry.json` 仅为传统 source_search 兼容，取材不使用该旧路由。
- 原标准中的 Git 打 tag、批量调度、平台 CLI 和历史日期不自动执行。本包不依赖 ZCode、WorkBuddy 或特定供应商。使用当前宿主已授权的独立执行能力，不自动启动额外付费 CLI。先串行；只有宿主支持时再提高并发，不把旧机器经验当作普遍上限。
- 原标准中的“五维并行”在不能并行的宿主可顺序调研；质量维度不减少。答题和判卷的独立性要求不能省掉。
- 脚本须在 W 作为当前目录执行；来源适配器通过 csp run 调用，它会自动切到 W。内部路径不含个人盘符。

## 取材和蒸馏

1. 在 `examples/<游戏>/TASK.md` 留下用户要求、角色身份、语种、版本和范围。已有角色先备份，再更新。
2. 读取对应游戏指南，用明确的 ID/页面定位角色，保留实际响应。`examples/<游戏>/_corpus/http/` 中 `.raw` 是按 SHA-256 命名的响应字节，旁边 JSON 记录 URL、请求时间、来源类别、响应码和哈希；失败也有记录。`downloaded_unreviewed` 只是下载成功。
3. 回源检查内容，再清洗切片；保留说话人、上下文、剧情位置及原文件/来源 ID。研究结论区分原文事实与解释。社区站不是厂商认证接口，用户本地材料的来源不明时明确标注未知。
4. 按共用标准形成研究文件、`sources.json`（键 `records`）、`distillation.md`、`manifest.json` 和角色 `SKILL.md`。来源不足如实列出，不靠模型常识补引文。原句引用和新编对话例子明确区分。
5. 运行 `python -B scripts/merge_research.py "examples/<游戏>/<目录>"`，解决实际研究缺口，再运行 `python -B scripts/quality_check.py "examples/<游戏>/<目录>"`。相似度只比较本工作区角色；空工作区没有历史基线，不能称“与全部旧角色均不相似”。

网站暂时不可用时，可以使用同站正常浏览取得的页面或用户已有材料：`python -B scripts/csp.py import-file --workspace "W" --game <游戏ID> --file "原文件路径" --source-url "已配置的实际页面URL"`。未知网络来源的本地文本则用 `--source-note "用户提供，原始获取过程未核验"` 替代 URL。只在原检索日期已知时填 `--retrieved-at`；导入日期和检索日期分别记录，不伪装成重新联网获取。

## 行为验证

在 W 内、输入稳定后执行以下步骤。C 代表角色目录实际路径，不是字面目录名。

1. `python -B scripts/behavior_test.py exam "C"` 生成考卷及输入哈希，检查题目和证据确实相关，K1 不泄漏边界答案。
2. 新上下文 A 组只读角色 SKILL 和题目，保存真实回答到 `C/references/behavior-answers.json`。
3. `python -B scripts/behavior_test.py judge "C" --arm a --answers "C/references/behavior-answers.json"` 生成判卷提示词。另一个上下文只看题目、证据与答卷，保存带匹配 exam_id 的 `behavior-judge.json`。
4. `python -B scripts/behavior_test.py mutation "C"` 生成突变版本。保留相同原题，以各突变正文分别真实作答、独立判卷；各臂原文、答卷、判卷分别留档。核对哪些突变实际生成，不能默认总有四臂。不重新出另一套题，不覆盖主答卷。
5. 可用 `python -B scripts/v2_collect.py mutation-answer-all "C" "突变名称"` 准备提示词（名称取 `runs/mutation/<角色目录名>/manifest.json`，不是文件路径）。每臂匹配原 exam_id 的判卷存入 `runs/mutation/<角色目录名>/judges/`，再用 `python -B scripts/v2_collect.py mutation-report "C"` 收集；逐臂检查无缺漏，不能只放一个通过文件就称全部测试完。
6. 突变未检出按 checks 中 `ab_control_rule` 补 A/B：`behavior_test.py control "C"`；B 组只读角色名、作品名及同题；保存 `behavior-control-answers.json`，再 `judge "C" --arm control --answers "C/references/behavior-control-answers.json"`，由独立上下文完成基线判卷。其他情况是否做全量 A/B 以此次用户约定为准。
7. `python -B scripts/behavior_test.py finalize "C"` 校验并汇总。当前实现中，A/B 检出贡献也不自动把突变 INCONCLUSIVE 改成 PASS。主卷 FAIL、缺突变、未检出、完整通过分别如实报告。

从提示词生成到实际模型作答之间需要宿主执行，脚本不自带模型服务。独立上下文不可用时交付草稿并标“行为未测”，不自行模拟独立结果。

## 更新与交付

修改旧角色前执行 `python -B scripts/file_manager.py backup "C"`。哈希只能绑定出卷当时输入，不给旧卷追填今天的哈希；改输入后重出卷。备份、运行记录、语料留在 W。模型试跑失败或站点被限制时保留可恢复记录，不无限重试。

向用户交付角色目录、来源覆盖时间、结构结果、行为结果及缺口。可另外压缩产出的角色供个人使用；本生产器安装包不包含原始游戏文本或旧角色资料。
