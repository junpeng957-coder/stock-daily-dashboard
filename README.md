# 每日国内外股市资讯看板

每天北京时间 07:30 搜索、筛选并提炼 A 股、港股、美股、基金/ETF及相关全球宏观资讯，更新 GitHub Pages，并向飞书群推送摘要卡片。

## 本地预览

无需密钥即可生成固定示例：

```bash
python3 generate.py --self-test
python3 generate.py --demo
python3 -m http.server 8000 -d docs
```

浏览器打开 `http://localhost:8000`。

## GitHub 配置

1. 新建公开仓库并推送本项目。
2. 在仓库 `Settings → Pages` 中将 Source 设为 **GitHub Actions**。
3. 在 `Settings → Secrets and variables → Actions` 添加：

| 名称 | 必填 | 内容 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 是 | OpenAI API 密钥 |
| `FEISHU_WEBHOOK_URL` | 推荐 | 飞书测试群自定义机器人 Webhook |
| `FEISHU_WEBHOOK_SECRET` | 否 | 机器人启用签名校验时填写 |
| `WATCHLIST_JSON` | 否 | 私密自选清单，只用于排序和飞书提示 |

`WATCHLIST_JSON` 示例：

```json
["510300", "0700.HK", {"symbol": "AAPL", "name": "Apple"}]
```

可在 Actions variables 中设置 `OPENAI_MODEL` 覆盖默认的 `gpt-5.6-luna`。

配置完成后，在 `Actions → 更新每日股市资讯看板 → Run workflow` 手动试运行一次。之后工作流每天北京时间 07:30 自动执行。

## 隐私与失败策略

- 自选清单和所有密钥只从 GitHub Secrets 读取，不写入公开文件。
- 公开数据会在落盘前移除自选匹配字段。
- 生成失败时保留上一期看板，并尝试向飞书发送失败提示。
- 飞书推送失败不会撤销已经发布的看板。
- 页面及摘要不构成投资建议。
