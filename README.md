# Notion 碎片中心 → Get笔记 同步

自动将 Notion 碎片中心的笔记同步到 Get 笔记。

## 功能

- 每 30 分钟自动检查 Notion 碎片中心新增笔记
- 同步笔记标题、标签、来源链接到 Get 笔记
- 避免重复同步（基于笔记 ID 记录）
- 支持手动触发同步
- 支持 repository_dispatch 外部触发

## 配置

### 1. GitHub Secrets

在仓库 Settings → Secrets and variables → Actions 中添加：

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `NOTION_TOKEN` | Notion Integration Token | `ntn_xxx...` |
| `GETNOTE_API_KEY` | Get 笔记 API Key | `gk_live_xxx...` |
| `GETNOTE_CLIENT_ID` | Get 笔记 Client ID | `cli_xxx...` |

### 2. GitHub Variables

在仓库 Settings → Variables → Actions 中添加：

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `NOTION_DB_ID` | 碎片中心数据库 ID | `11233b33-...` |

## 同步触发方式

### 方式一：GitHub Schedule（内置，每30分钟）

```yaml
schedule:
  - cron: '*/30 * * * *'
```

**注意**：GitHub Actions 的 schedule 可能会因使用频率过低而被暂停。

### 方式二：repository_dispatch（推荐，稳定）

使用外部 cron 服务（如 cron-job.org）定期触发：

```
POST https://api.github.com/repos/{owner}/{repo}/dispatches
Authorization: Bearer {GITHUB_PAT}
Content-Type: application/json

{
  "event_type": "sync"
}
```

推荐 cron-job.org 配置：每 5-10 分钟触发一次。

### 方式三：手动触发

在 GitHub Actions 页面手动点击 "Run workflow"。

## 隐私说明

- 仓库设为公开，Secrets 不会在日志中泄露
- 日志中只输出笔记标题和标签，不输出完整内容
- 已同步记录存储在 `processed_ids.json`

## 本地测试

```bash
export NOTION_TOKEN="your_notion_token"
export GETNOTE_API_KEY="your_api_key"
export GETNOTE_CLIENT_ID="cli_xxx"
export NOTION_DB_ID="11233b33-..."

python sync.py
```

## 许可证

MIT
