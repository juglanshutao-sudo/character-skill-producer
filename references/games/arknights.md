# 明日方舟

先读共用 `../SKILL_V2.md` 和 `../workflow.md`。主源 `Kengxxiao/ArknightsGameData` 是社区维护的游戏数据仓库。PRTS、ASTR 阅读器、萌娘百科和方舟 BWIKI 只作补充/交叉核查，均保留各自来源身份。

先获取仓库元数据确定默认分支，再获取该分支提交 SHA；从固定提交读取目录和目标文件，避免把多个版本拼成同一快照。可用通用 fetch，示例：

```text
python -B scripts/csp.py fetch --workspace "W" --game arknights --url "https://api.github.com/repos/Kengxxiao/ArknightsGameData"
python -B scripts/csp.py fetch --workspace "W" --game arknights --url "https://api.github.com/repos/Kengxxiao/ArknightsGameData/commits/<已核实分支>"
python -B scripts/csp.py fetch --workspace "W" --game arknights --url "https://api.github.com/repos/Kengxxiao/ArknightsGameData/contents/?ref=<提交SHA>"
python -B scripts/csp.py fetch --workspace "W" --game arknights --url "https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/<提交SHA>/<已核实文件路径>"
```

按实际目录查角色表、档案/语音表、密录与相关剧情，不假设旧工程的中文文件夹在上游仓库存在。每次内容 API 返回的 `download_url` 需通过本游戏 URL 检查；大文件拆成实际目标文件，单次响应上限 64 MiB。

需要百科补充时，`csp.py run --workspace "W" --game arknights bwiki_api "条目名" --game arknights --wikitext` 或 `moegirl_api "条目名" --full`。PRTS/ASTR 可用通用 fetch。ASTR 是阅读器页面时须继续定位正文，不能把页面壳当作剧情。

不带旧的 ak_distill/batch 状态机：它们依赖本地整理后的特殊目录，不是远程仓库直接输入。接手 agent 按共享标准从实际抓取内容建立研究与角色产物。同名干员、异格与不同 char_id 分开核实。
