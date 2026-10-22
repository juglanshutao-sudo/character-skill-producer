# 鸣潮

先读共用 `../SKILL_V2.md` 和 `../workflow.md`。主源库街区 Wiki 及其 API；POST 用于文档化的查询接口。不要把 Wiki 攻略和角色正文混在一起。

在 W 执行：

```text
python -B scripts/csp.py run --workspace "W" --game wuwa mc_wiki_api tree --out examples/鸣潮/_corpus/catalogues.json
python -B scripts/csp.py run --workspace "W" --game wuwa mc_wiki_api list --out examples/鸣潮/_corpus/characters.json
python -B scripts/csp.py run --workspace "W" --game wuwa mc_wiki_api entry --id <实际entry_id> --raw
```

大整数 entry_id 作为字符串保存，避免精度损失。目录和条目正文的 API 响应自动存进 `_corpus/http/`。`corpus --out <目录> --workers 1` 是全量选项，不用于默认单角色任务。原文缺说话人时结合场景确认，不把任务目标或玩法说明写成人格规则。
