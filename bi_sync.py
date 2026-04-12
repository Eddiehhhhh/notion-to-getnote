#!/usr/bin/env python3
"""
Get笔记 ↔ Flomo 双向同步
- Get笔记 → Flomo: AI 智能匹配标签
- Flomo → Get笔记: 保持原标签同步
"""

import json
import os
import ssl
import time
import re
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# ============ 配置 ============
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "11233b33-7f23-8024-9555-cb8de8c58e02")

GETNOTE_API_KEY = os.environ.get("GETNOTE_API_KEY", "")
GETNOTE_CLIENT_ID = os.environ.get("GETNOTE_CLIENT_ID", "cli_3802f9db08b811f197679c63c078bacc")

FLOMO_WEBHOOK_URL = os.environ.get("FLOMO_WEBHOOK_URL", "https://flomoapp.com/iwh/MTIzMjAxNA/0f073dd2d8154952aca0d83cbf10e2fe/")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

# 状态文件
STATE_FILE = os.path.join(os.path.dirname(__file__), "sync_state.json")

# Flomo 标签列表（从 Notion 获取）
FLOMO_TAGS = []


# ============ 辅助函数 ============

def load_state():
    """加载同步状态"""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {
        "last_flomo_note_id": None,
        "last_getnote_cursor": "0",
        "processed_getnote_ids": [],
        "processed_notion_ids": []
    }


def save_state(state):
    """保存同步状态"""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def notion_request(url, body=None):
    """发送 Notion API 请求"""
    data = json.dumps(body).encode() if body else None
    method = "POST" if data else "GET"
    req = Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {NOTION_TOKEN}")
    req.add_header("Notion-Version", "2022-06-28")
    if data:
        req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    try:
        with urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        print(f"[ERROR] Notion API error {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"[ERROR] Request failed: {e}")
        return None


def getnote_request(endpoint, body=None):
    """发送 Get 笔记 API 请求"""
    url = f"https://openapi.biji.com/open/api/v1{endpoint}"
    data = json.dumps(body).encode() if body else None
    method = "POST" if data else "GET"
    req = Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {GETNOTE_API_KEY}")
    req.add_header("X-Client-ID", GETNOTE_CLIENT_ID)
    if data:
        req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    try:
        with urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        print(f"[ERROR] Get笔记 API error {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"[ERROR] Get笔记 request failed: {e}")
        return None


def flomo_send(content):
    """发送笔记到 Flomo"""
    req = Request(
        FLOMO_WEBHOOK_URL,
        data=json.dumps({"content": content}).encode(),
        method="POST"
    )
    req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    try:
        with urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[ERROR] Flomo request failed: {e}")
        return {"code": -1, "message": str(e)}


def deepseek_chat(messages):
    """调用 DeepSeek API"""
    url = f"{DEEPSEEK_BASE_URL}/chat/completions"
    data = json.dumps({
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.3
    }).encode()
    req = Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {DEEPSEEK_API_KEY}")
    req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    try:
        with urlopen(req, context=ctx, timeout=60) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[ERROR] DeepSeek API failed: {e}")
        return None


# ============ 获取 Flomo 标签列表 ============

def fetch_flomo_tags():
    """从 Notion 碎片中心获取 Flomo 的标签列表"""
    global FLOMO_TAGS
    print("[INFO] 从 Notion 获取 Flomo 标签列表...")
    
    results = []
    body = {"page_size": 100}
    while True:
        data = notion_request(
            f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
            body
        )
        if not data:
            break
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        body["start_cursor"] = data["next_cursor"]
    
    # 收集唯一标签
    tags_set = set()
    for page in results:
        props = page.get("properties", {})
        tags = props.get("Tags", {}).get("multi_select", [])
        for t in tags:
            tags_set.add(t["name"])
    
    FLOMO_TAGS = sorted(tags_set)
    print(f"[INFO] 获取到 {len(FLOMO_TAGS)} 个 Flomo 标签")
    return FLOMO_TAGS


# ============ AI 标签匹配 ============

def match_tags_with_ai(content, available_tags):
    """用 AI 根据内容匹配最适合的 Flomo 标签"""
    if not available_tags:
        return []
    
    tags_str = "\n".join([f"- {tag}" for tag in available_tags])
    
    prompt = f"""根据笔记内容，从以下标签列表中选择最合适的 1-3 个标签。

笔记内容：
{content}

可用标签：
{tags_str}

要求：
1. 只选择直接相关的标签，不要过度标签化
2. 选择最具体的标签（如果有层级，如"领域/AI"和"领域"，选更具体的）
3. 直接返回标签名称，每行一个，不要其他解释

直接返回标签："""

    response = deepseek_chat([
        {"role": "user", "content": prompt}
    ])
    
    if not response:
        return []
    
    # 解析 AI 返回的标签
    matched = []
    for line in response.strip().split("\n"):
        tag = line.strip().lstrip("- ").strip()
        if tag and tag in available_tags:
            matched.append(tag)
    
    return matched[:3]


# ============ Flomo → Get笔记 同步 ============

def sync_flomo_to_getnote(state):
    """
    从 Notion 碎片中心读取 Flomo 笔记，同步到 Get 笔记
    """
    print("\n" + "=" * 50)
    print("📤 Flomo → Get笔记 同步")
    print("=" * 50)
    
    # 计算时间范围（过去 2 小时内的笔记）
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(hours=2)
    start_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # 查询最近的笔记
    filter_body = {
        "and": [
            {
                "property": "创建时间",
                "created_time": {"after": start_str}
            },
            {
                "property": "删除",
                "checkbox": {"equals": False}
            }
        ]
    }
    
    data = notion_request(
        f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
        {"filter": filter_body, "page_size": 50, "sorts": [{"timestamp": "created_time", "direction": "descending"}]}
    )
    
    if not data or not data.get("results"):
        print("[INFO] 没有新的 Flomo 笔记需要同步")
        return state
    
    # 收集需要同步的笔记
    notes_to_sync = []
    processed_ids = set(state.get("processed_notion_ids", []))
    
    for page in data["results"]:
        if page.get("id") in processed_ids:
            continue
            
        props = page.get("properties", {})
        
        # 提取标题
        title_prop = props.get("Name", {})
        title = ""
        if title_prop.get("title"):
            title = "".join([t.get("plain_text", "") for t in title_prop["title"]])
        
        # 提取标签
        tags = [t["name"] for t in props.get("Tags", {}).get("multi_select", [])]
        
        # 提取链接
        link = props.get("Link", {}).get("url", "")
        
        if title:
            notes_to_sync.append({
                "id": page.get("id"),
                "title": title,
                "tags": tags,
                "link": link
            })
    
    if not notes_to_sync:
        print(f"[INFO] 没有新的笔记需要同步")
        return state
    
    print(f"[INFO] 发现 {len(notes_to_sync)} 条新笔记")
    
    for note in notes_to_sync:
        # 构建内容
        content_parts = [note["title"]]
        if note["tags"]:
            content_parts.append(f"\n标签: {' '.join(['#' + t for t in note['tags']])}")
        if note["link"]:
            content_parts.append(f"\n来源: {note['link']}")
        
        content = "\n".join(content_parts)
        
        # 保存到 Get 笔记
        print(f"[INFO] 同步笔记...")
        result = getnote_request("/resource/note/save", {
            "title": note["title"][:100],
            "content": content,
            "note_type": "text"
        })
        
        if result and result.get("success"):
            print(f"[INFO] 同步成功")
            processed_ids.add(note["id"])
        else:
            print(f"[WARN] 同步失败")
        
        time.sleep(1)
    
    state["processed_notion_ids"] = list(processed_ids)[-100:]
    return state


# ============ Get笔记 → Flomo 同步 ============

def sync_getnote_to_flomo(state):
    """
    从 Get 笔记读取新笔记，用 AI 匹配标签后同步到 Flomo
    """
    global FLOMO_TAGS
    
    print("\n" + "=" * 50)
    print("📥 Get笔记 → Flomo 同步")
    print("=" * 50)
    
    # 获取笔记列表
    cursor = state.get("last_getnote_cursor", "0")
    result = getnote_request("/resource/note/list", {
        "since_id": cursor,
        "limit": 20
    })
    
    if not result or not result.get("success"):
        print(f"[ERROR] 获取 Get 笔记失败")
        return state
    
    notes = result.get("data", {}).get("notes", [])
    if not notes:
        print("[INFO] 没有新的 Get 笔记")
        return state
    
    processed_ids = set(state.get("processed_getnote_ids", []))
    new_notes = [n for n in notes if str(n.get("id")) not in processed_ids]
    
    if not new_notes:
        print("[INFO] 没有新的笔记需要同步")
        return state
    
    print(f"[INFO] 发现 {len(new_notes)} 条新笔记")
    
    for note in new_notes:
        note_id = str(note.get("id"))
        title = note.get("title", "")
        content = note.get("content", "")
        
        if not title and not content:
            continue
        
        # 使用标题作为内容（如果内容太长）
        display_content = content if content else title
        display_content = display_content[:500]
        
        # 用 AI 匹配标签
        print(f"[INFO] AI 匹配标签...")
        matched_tags = match_tags_with_ai(display_content, FLOMO_TAGS)
        
        if matched_tags:
            tags_str = " ".join(["#" + tag for tag in matched_tags])
            final_content = f"{display_content}\n\n{tags_str}"
        else:
            final_content = display_content
        
        # 发送到 Flomo
        print(f"[INFO] 发送到 Flomo...")
        result = flomo_send(final_content)
        
        if result and result.get("code") == 0:
            print(f"[INFO] 同步成功 (标签: {matched_tags})")
            processed_ids.add(note_id)
        else:
            print(f"[WARN] 同步失败")
        
        # 更新游标
        new_cursor = result.get("data", {}).get("cursor", cursor)
        if new_cursor and new_cursor != cursor:
            state["last_getnote_cursor"] = new_cursor
        
        time.sleep(1)
    
    state["processed_getnote_ids"] = list(processed_ids)[-100:]
    return state


# ============ 主流程 ============

def main():
    print("=" * 50)
    print("🔄 Get笔记 ↔ Flomo 双向同步开始")
    print("=" * 50)
    
    # 加载状态
    state = load_state()
    
    # 获取 Flomo 标签
    fetch_flomo_tags()
    
    # Flomo → Get笔记 同步
    state = sync_flomo_to_getnote(state)
    
    # Get笔记 → Flomo 同步
    state = sync_getnote_to_flomo(state)
    
    # 保存状态
    save_state(state)
    
    print("\n" + "=" * 50)
    print("✅ 同步完成")
    print("=" * 50)


if __name__ == "__main__":
    main()
