#!/usr/bin/env python3
"""
Notion 碎片中心 → Get笔记 同步脚本
- 检查 Notion 碎片中心新增笔记
- 同步到 Get 笔记
- 记录已同步的笔记 ID，避免重复
"""

import json
import os
import sys
import ssl
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# ============ 配置 ============
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_VERSION = "2022-06-28"
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "11233b33-7f23-8024-9555-cb8de8c58e02")  # 碎片中心

GETNOTE_API_KEY = os.environ.get("GETNOTE_API_KEY", "")
GETNOTE_CLIENT_ID = os.environ.get("GETNOTE_CLIENT_ID", "cli_3802f9db08b811f197679c63c078bacc")

# processed_ids.json 路径
PROCESSED_IDS_FILE = os.path.join(os.path.dirname(__file__), "processed_ids.json")

# 检查时间范围（默认检查过去1小时的笔记，避免遗漏）
CHECK_HOURS = int(os.environ.get("CHECK_HOURS", "1"))


# ============ 辅助函数 ============

def load_processed_ids():
    """加载已处理的笔记 ID"""
    try:
        if os.path.exists(PROCESSED_IDS_FILE):
            with open(PROCESSED_IDS_FILE, 'r') as f:
                return set(json.load(f))
    except Exception as e:
        print(f"[WARN] 加载已处理记录失败: {e}")
    return set()


def save_processed_ids(ids_set):
    """保存已处理的笔记 ID"""
    try:
        with open(PROCESSED_IDS_FILE, 'w') as f:
            json.dump(list(ids_set), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] 保存已处理记录失败: {e}")


def notion_request(url, body=None):
    """发送 Notion API 请求"""
    data = json.dumps(body).encode() if body else None
    method = "POST" if data else "GET"
    req = Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {NOTION_TOKEN}")
    req.add_header("Notion-Version", NOTION_VERSION)
    if data:
        req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    try:
        with urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        error_body = e.read().decode()[:500]
        print(f"[ERROR] Notion API error {e.code}: {error_body}")
        return None
    except Exception as e:
        print(f"[ERROR] Request failed: {e}")
        return None


def query_new_fragments():
    """
    查询碎片中心最新笔记（基于创建时间过滤）
    """
    # 计算时间范围
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(hours=CHECK_HOURS)
    start_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"[INFO] 检查 {CHECK_HOURS} 小时内的笔记，起始时间: {start_str}")

    # 构建过滤条件：创建时间 > start_time 且 未删除
    filter_body = {
        "and": [
            {
                "property": "创建时间",
                "created_time": {
                    "after": start_str
                }
            },
            {
                "property": "删除",
                "checkbox": {
                    "equals": False
                }
            }
        ]
    }

    body = {
        "page_size": 100,
        "filter": filter_body
    }

    results = []
    while True:
        data = notion_request(f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query", body)
        if not data:
            break
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        body["start_cursor"] = data["next_cursor"]

    return results


def parse_fragment(page):
    """
    解析碎片页面，提取关键信息
    """
    props = page.get("properties", {})

    # 提取标题
    title_prop = props.get("Name", {})
    title = ""
    if title_prop.get("title"):
        title = "".join([t.get("plain_text", "") for t in title_prop["title"]])

    # 提取标签
    tags = []
    tags_prop = props.get("Tags", {})
    if tags_prop.get("multi_select"):
        tags = [t.get("name", "") for t in tags_prop["multi_select"]]

    # 提取链接
    link = props.get("Link", {}).get("url", "")

    # 提取日期
    date_prop = props.get("Created At", {})
    date = date_prop.get("date", {})
    created_date = date.get("start", "") if date else ""

    # 创建时间
    created_time = page.get("created_time", "")

    # 页面 ID
    page_id = page.get("id", "")

    return {
        "id": page_id,
        "title": title,
        "tags": tags,
        "link": link,
        "date": created_date or created_time[:10],
        "created_time": created_time
    }


def save_to_getnote(fragment):
    """
    将碎片保存到 Get 笔记
    """
    title = fragment["title"]
    link = fragment.get("link", "")
    tags = fragment.get("tags", [])

    # 构建内容
    content_parts = [title]
    if link:
        content_parts.append(f"\n来源: {link}")
    if tags:
        content_parts.append(f"\n标签: {' '.join(['#' + t for t in tags])}")

    content = "\n".join(content_parts)

    # 调用 Get 笔记 API
    payload = {
        "title": title[:100] if len(title) > 100 else title,  # 标题限制
        "content": content,
        "note_type": "text" if not link else "link",
        "link_url": link if link else None
    }

    # 移除 None 值
    payload = {k: v for k, v in payload.items() if v is not None}

    req = Request(
        "https://openapi.biji.com/open/api/v1/resource/note/save",
        data=json.dumps(payload).encode(),
        method="POST"
    )
    req.add_header("Authorization", f"Bearer {GETNOTE_API_KEY}")
    req.add_header("x-client-id", GETNOTE_CLIENT_ID)
    req.add_header("Content-Type", "application/json")

    ctx = ssl.create_default_context()
    try:
        with urlopen(req, context=ctx, timeout=30) as resp:
            result = json.loads(resp.read())
            return result
    except HTTPError as e:
        error_body = e.read().decode()[:500]
        print(f"[ERROR] Get笔记 API error {e.code}: {error_body}")
        return {"success": False, "error": error_body}
    except Exception as e:
        print(f"[ERROR] Get笔记 request failed: {e}")
        return {"success": False, "error": str(e)}


def poll_task_progress(task_id, max_attempts=30):
    """
    轮询 Get 笔记任务进度
    """
    for i in range(max_attempts):
        import time
        time.sleep(3)

        req = Request(
            "https://openapi.biji.com/open/api/v1/resource/note/task/progress",
            data=json.dumps({"task_id": task_id}).encode(),
            method="POST"
        )
        req.add_header("Authorization", f"Bearer {GETNOTE_API_KEY}")
        req.add_header("x-client-id", GETNOTE_CLIENT_ID)
        req.add_header("Content-Type", "application/json")

        ctx = ssl.create_default_context()
        try:
            with urlopen(req, context=ctx, timeout=30) as resp:
                result = json.loads(resp.read())
                if result.get("success") and result.get("data"):
                    data = result["data"]
                    if data.get("status") == "success":
                        return {"status": "success", "note_id": data.get("note_id")}
                    elif data.get("status") == "failed":
                        return {"status": "failed", "error": data.get("error_msg")}
        except Exception as e:
            print(f"[WARN] 轮询进度失败: {e}")

    return {"status": "timeout"}


# ============ 主流程 ============

def main():
    print("=" * 50)
    print("🚀 Notion 碎片中心 → Get笔记 同步开始")
    print("=" * 50)

    # 验证配置
    if not NOTION_TOKEN:
        print("[ERROR] NOTION_TOKEN 未配置")
        sys.exit(1)
    if not GETNOTE_API_KEY:
        print("[ERROR] GETNOTE_API_KEY 未配置")
        sys.exit(1)

    # 加载已处理的笔记
    processed_ids = load_processed_ids()
    print(f"[INFO] 已同步笔记数: {len(processed_ids)}")

    # 查询新笔记
    print("[INFO] 正在查询碎片中心...")
    new_pages = query_new_fragments()
    print(f"[INFO] 查询到 {len(new_pages)} 条新笔记")

    if not new_pages:
        print("✨ 没有新的笔记需要同步")
        return

    # 解析并过滤
    new_fragments = []
    for page in new_pages:
        frag = parse_fragment(page)
        if frag["id"] not in processed_ids:
            new_fragments.append(frag)

    print(f"[INFO] 其中 {len(new_fragments)} 条需要同步")

    if not new_fragments:
        print("✨ 没有新的笔记需要同步")
        return

    # 同步每个碎片
    new_processed = set()
    for frag in new_fragments:
        print(f"\n📝 处理: {frag['title'][:50]}...")
        if frag.get("link"):
            print(f"   🔗 链接: {frag['link']}")
        if frag.get("tags"):
            print(f"   🏷️ 标签: {', '.join(frag['tags'])}")

        # 保存到 Get 笔记
        print("   ⏳ 保存到 Get笔记...")
        result = save_to_getnote(frag)

        if result.get("success") and result.get("data", {}).get("tasks"):
            # 有异步任务，需要轮询
            task = result["data"]["tasks"][0]
            task_id = task.get("task_id")
            print(f"   📤 任务已创建: {task_id}")
            print("   ⏳ 等待内容处理...")

            progress = poll_task_progress(task_id)
            if progress["status"] == "success":
                print(f"   ✅ 同步成功!")
            elif progress["status"] == "failed":
                print(f"   ⚠️ 处理失败: {progress.get('error')}")
            else:
                print(f"   ⚠️ 处理超时")
        elif result.get("success"):
            print(f"   ✅ 同步成功!")
        else:
            print(f"   ❌ 同步失败: {result.get('error', '未知错误')}")

        new_processed.add(frag["id"])

    # 更新已处理记录
    all_processed = processed_ids | new_processed
    save_processed_ids(all_processed)

    print("\n" + "=" * 50)
    print(f"🎉 同步完成! 本次同步 {len(new_processed)} 条")
    print(f"📊 累计已同步: {len(all_processed)} 条")
    print("=" * 50)


if __name__ == "__main__":
    main()
