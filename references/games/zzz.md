# 绝区零

先读共用 `../SKILL_V2.md` 和 `../workflow.md`。默认来源为日文社区 Wiki `https://wikiwiki.jp/zenless/`，不是官方中文剧情。使用前安装 requirements 中的 beautifulsoup4。

在 W 执行：

```text
python -B scripts/csp.py run --workspace "W" --game zzz zzz_wiki_api roster --output examples/绝区零/_corpus/roster.json
python -B scripts/csp.py run --workspace "W" --game zzz zzz_wiki_api page "已核实的日文页面标题" --output examples/绝区零/_corpus/page.json
```

名册确认日文名及角色身份后，先从 Wiki 索引选择角色实际登场的剧情页。需要大范围剧情时可 `crawl --output <路径> --workers 1`，但不能因脚本写 `complete` 就忽略 failures 列表。

原始日文、译文和蒸馏解释分开保存。中文措辞若来自自译，明确标自译，不冒充官方中文台词。若用户另供本地客户端文本，复制到 W 的原始语料目录并记录提供者、时间、哈希及可证实版本；缺来源就标“用户提供，原始获取过程未核验”。本包不含客户端解包程序或旧工程 dump。

若页面显示 503 维护公告，记录为暂时维护，停止本轮批量请求，待恢复后续作；不据此删除或更换来源。维护公告中的预计结束时间不能当作已经恢复的证明。2026-10-22 用户截图显示过此类维护页，当前状态需在实际执行时重新检查。
