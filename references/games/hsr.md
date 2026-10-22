# 崩坏星穹铁道

先读共用 `../SKILL_V2.md` 和 `../workflow.md`。米哈游 Wiki 用于角色档案和可获取的剧情；星铁 BWIKI 用于补充剧情和交叉检查，标社区来源。BWIKI 只允许 `/sr/`，不能串到其他游戏子站。

在 W 执行：

```text
python -B scripts/csp.py run --workspace "W" --game hsr hsr_official_api roster --output examples/崩坏星穹铁道/_corpus/roster.json
python -B scripts/csp.py run --workspace "W" --game hsr hsr_official_api profile <content_id> --output examples/崩坏星穹铁道/_corpus/profile.json
python -B scripts/csp.py run --workspace "W" --game hsr bwiki_api "角色名" --game sr --search
python -B scripts/csp.py run --workspace "W" --game hsr bwiki_api "已确认条目名" --game sr --wikitext
```

先从名册或站内查询确认 content_id，不凭角色编号猜测 Wiki ID。需要剧情可用 `hsr_official_api summary` 查看索引，再选范围；全量 `crawl --output <路径> --workers 1` 仅在确需全量时执行。Wiki 动态页缺正文时，索引标题不能充当场景证据。不同命途/形态和玩家分支分别标识。
