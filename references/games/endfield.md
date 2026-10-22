# 终末地

> 发布时已知限制：旧 roster 解析器未能从当前响应中提取干员。下方批量命令仅供适配维护参考，不应将零条输出当作成功名册。优先通用 fetch/本地导入并人工核对，解析器修复后再恢复自动批量流程。

先读共用 `../SKILL_V2.md` 和 `../workflow.md`。默认来源 `fz.wiki/wiki/` 为社区 Wiki，页面使用 Next.js 数据，不是 MediaWiki API。配置同时登记站点实际跳转的 `www.fz.wiki/wiki/` 地址。

单角色优先通用 fetch：

```text
python -B scripts/csp.py fetch --workspace "W" --game endfield --url "https://fz.wiki/wiki/干员/<经过URL编码的姓名>"
```

实际 URL 应由站点导航确认，用 Python `urllib.parse.quote` 编码中文路径。原始 HTML 中的 Next.js 片段需解码后核对页面显示内容；只有标题、空壳或脚本错误不能当作成功取材。

浏览器能打开而程序报 SSL/连接错误时，区分浏览器可用性与自动抓取能力，检查网络、代理、证书和实际跳转路径，不直接判网站故障，不关闭 TLS 证书校验。仍不能抓取时，可正常浏览同站取得正文，再按 workflow 的 import-file 方式留档；导入不标成脚本联网成功。

需要名册或完整任务索引时，可运行：

```text
python -B scripts/csp.py run --workspace "W" --game endfield endfield_wiki_api roster
python -B scripts/csp.py run --workspace "W" --game endfield endfield_wiki_api quests-index
```

`operators` 依赖前一步 roster，`quests` 依赖 quests-index；两者都是批量动作，只在请求范围内使用。输出在 `examples/终末地/_corpus/` 与 `runs/endfield/`。便携版已把旧抓取器写死的检索日期改为运行日期；沿用缓存时仍保留其原日期，不冒充本次重新抓取。
