# 原神

先读共用 `../SKILL_V2.md` 和 `../workflow.md`。主源 Yatta 是第三方游戏数据站，不能登记为厂商官网。先在 Yatta 角色索引确认角色 ID、形态和语种。

以下在 W 执行（将 ID、姓名和 W 替换成真实值；命令会联网）：

```text
python -B scripts/csp.py fetch --workspace "W" --game genshin --url "https://gi.yatta.moe/api/v2/chs/avatar"
python -B scripts/csp.py run --workspace "W" --game genshin genshin_yatta_api avatar <ID> --out examples/原神/_corpus
python -B scripts/csp.py run --workspace "W" --game genshin genshin_yatta_api derive --avatar <ID> --names <姓名> --corpus examples/原神/_corpus
```

档案和语音不足以代表全部剧情。需要剧情行为时，抓任务索引，再按已确认的任务 ID 取 `https://gi.yatta.moe/api/v2/chs/quest/<ID>`，保留上下文。只有用户要求整游戏时才运行 `avatar-all` 或完整 `quest-scan`。玩家选项、旅行者台词和同场其他角色对白不得归给目标角色。

派生 Markdown 为检索辅助，原始响应与精确来源另存。遇到 403/失效就留缺口，不擅自切换未配置镜像。
