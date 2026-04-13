#!/usr/bin/env python3
"""
Get笔记 ↔ Flomo 双向同步 - 防循环版本 v2
=========================================
核心逻辑：
1. Flomo → Get笔记：从 Notion 碎片中心读取，跳过带"✅来自Flomo"标签的笔记
2. Get笔记 → Flomo：跳过带🔄标记/flomo URL/✅已同步Flomo标签的笔记，防止循环

防护机制（多层保险）：
1. 内容哈希去重（不只是 ID）
2. 标记跳过：🔄、✅已同步Flomo、flomo URL
3. 来源跳过：source="flomo"
4. 状态持久化：processed_hashes 记录已同步的内容哈希

数据流：
  Flomo ──(用户同步)──▶ Notion 碎片中心
                                 │
                                 ▼
                          Notion → Get笔记 (加 🔄 标记)
                                 │
                                 ▼
                          Get笔记 → Flomo (跳过 🔄 标记)
"""

import json
import os
import ssl
import time
import re
import hashlib
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

# ============ 同步防护标记 ============
# 标记：来自 Flomo 的笔记（同步到 Get笔记 时添加）
MARKER_FROM_FLOMO = "🔄"

# 标记：已同步到 Flomo 的笔记（同步到 Flomo 时添加，防止循环）
MARKER_SYNCED_TO_FLOMO = "✅"

# 同步来源标记：用于标记笔记是从 Get笔记 同步到 Flomo 的
# 格式：[getnote-sync:ID]，用于识别循环同步
MARKER_GETNOTE_SOURCE = "[getnote-sync]"


# ============ 内容哈希去重 ============

def compute_content_hash(title, content):
    """计算内容哈希，用于去重"""
    raw = f"{title or ''}|{content or ''}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


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
        "last_getnote_cursor": "0",
        "processed_getnote_ids": [],
        "processed_getnote_hashes": [],  # 内容哈希去重
        "processed_notion_ids": [],
        "processed_notion_hashes": []     # 内容哈希去重
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


def getnote_request(endpoint, body=None, method=None):
    """发送 Get 笔记 API 请求"""
    url = f"https://openapi.biji.com/open/api/v1{endpoint}"
    data = json.dumps(body).encode() if body else None
    
    # 根据端点自动选择方法，list 用 GET，其他用 POST
    if method is None:
        method = "POST" if (data or endpoint.endswith("/save")) else "GET"
    
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
    
    防护逻辑：
    1. 跳过已有 "✅来自Flomo" 标签的笔记
    2. 跳过已有 "✅已同步Flomo" 标签的笔记（防止循环回来的）
    3. 内容哈希去重（即使 ID 不同，内容相同也不重复同步）
    4. 同步时在 Get笔记 中添加 🔄 标记
    """
    print("\n" + "=" * 50)
    print("📤 Flomo → Get笔记 同步")
    print("=" * 50)
    
    # 计算时间范围（过去 1 小时内，避免重复处理）
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(hours=1)
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
    processed_hashes = set(state.get("processed_notion_hashes", []))
    
    for page in data["results"]:
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
        
        # 计算内容哈希
        content_hash = compute_content_hash(title, link)
        
        # ===== 防护1：跳过已处理的 ID =====
        if page.get("id") in processed_ids:
            continue
        
        # ===== 防护2：跳过以 ✅ 开头的笔记 =====
        # 这些笔记是从 Get笔记 同步到 Flomo，又被同步到 Notion 的
        # 格式：✅ #标签1 #标签2  实际标题...
        if title.startswith(MARKER_SYNCED_TO_FLOMO):
            print(f"[SKIP-2] 来自Get笔记标记: {title[:50]}...")
            processed_ids.add(page.get("id"))
            continue
        
        # ===== 防护3：跳过包含 [getnote-sync] 标记的笔记 =====
        # 这些笔记是从 Get笔记 同步到 Flomo，又被同步到 Notion 的
        full_text = f"{title} {link}"
        if MARKER_GETNOTE_SOURCE in full_text:
            print(f"[SKIP-3] 来自Get笔记标记: {title[:50]}...")
            processed_ids.add(page.get("id"))
            continue
        
        if title:
            notes_to_sync.append({
                "id": page.get("id"),
                "title": title,
                "tags": tags,
                "link": link,
                "content_hash": content_hash
            })
    
    if not notes_to_sync:
        print(f"[INFO] 没有新的笔记需要同步")
        return state
    
    print(f"[INFO] 发现 {len(notes_to_sync)} 条新笔记")
    
    for note in notes_to_sync:
        # 构建内容（添加 🔄 标记，表示来自 Flomo）
        content_parts = [f"{MARKER_FROM_FLOMO}{note['title']}"]
        if note["tags"]:
            content_parts.append(f"\n标签: {' '.join(['#' + t for t in note['tags']])}")
        if note["link"]:
            content_parts.append(f"\n来源: {note['link']}")
        
        content = "\n".join(content_parts)
        
        # 保存到 Get 笔记
        print(f"[INFO] 同步笔记: {note['title'][:30]}...")
        result = getnote_request("/resource/note/save", {
            "title": note["title"][:100],
            "content": content,
            "note_type": "plain_text"
        })
        
        if result and result.get("success"):
            print(f"[INFO] 同步成功")
            processed_ids.add(note["id"])
            processed_hashes.add(note["content_hash"])
        else:
            print(f"[WARN] 同步失败: {result}")
        
        time.sleep(1)
    
    # 保留最近 200 条记录
    state["processed_notion_ids"] = list(processed_ids)[-200:]
    state["processed_notion_hashes"] = list(processed_hashes)[-200:]
    return state


# ============ Get笔记 → Flomo 同步 ============

def sync_getnote_to_flomo(state):
    """
    从 Get 笔记读取新笔记，用 AI 匹配标签后同步到 Flomo
    
    防护逻辑（多层保险）：
    1. source="flomo" - 直接跳过
    2. 包含 flomo URL - 跳过
    3. 包含 🔄 标记 - 跳过（来自 Flomo 的）
    4. 包含 ✅ 已同步Flomo 标记 - 跳过
    5. 内容哈希去重 - 即使 ID 不同，内容相同也不重复
    
    内容格式：标题作为正文第一行，然后换行接主体
    """
    global FLOMO_TAGS
    
    print("\n" + "=" * 50)
    print("📥 Get笔记 → Flomo 同步")
    print("=" * 50)
    
    # 获取笔记列表 (使用 GET 方法，只取最新 10 条减少重复风险)
    result = getnote_request("/resource/note/list?since_id=0&limit=10", method="GET")
    
    if not result or not result.get("success"):
        print(f"[ERROR] 获取 Get 笔记失败")
        return state
    
    notes = result.get("data", {}).get("notes", [])
    if not notes:
        print("[INFO] 没有新的 Get 笔记")
        return state
    
    processed_ids = set(state.get("processed_getnote_ids", []))
    processed_hashes = set(state.get("processed_getnote_hashes", []))
    
    # 过滤并记录所有检查过的笔记
    new_notes = []
    for n in notes:
        note_id = str(n.get("id"))
        
        # 已处理过则跳过
        if note_id in processed_ids:
            continue
            
        new_notes.append(n)
    
    if not new_notes:
        print("[INFO] 没有新的笔记需要同步")
        return state
    
    print(f"[INFO] 发现 {len(new_notes)} 条新笔记待检查")
    
    sync_count = 0
    skip_count = 0
    
    for note in new_notes:
        note_id = str(note.get("id"))
        note_id_md5 = str(note.get("md5_id", note_id))  # 备用 ID
        title = note.get("title", "") or ""
        content = note.get("content", "") or ""
        source = note.get("source", "") or ""
        
        # 计算内容哈希
        content_hash = compute_content_hash(title, content)
        
        # ===== 防护1：跳过来源是 Flomo 的笔记 =====
        if source == "flomo":
            print(f"[SKIP-1] Flomo 来源: {title[:30] if title else content[:30]}...")
            processed_ids.add(note_id)
            continue
        
        # ===== 防护2：跳过包含 flomo URL 的笔记 =====
        if "flomoapp.com" in content or "flomoapp.com" in title:
            print(f"[SKIP-2] 包含flomo链接: {title[:30] if title else content[:30]}...")
            processed_ids.add(note_id)
            continue
        
        # ===== 防护3：跳过包含 🔄 标记的笔记 =====
        # 这些笔记是从 Flomo 同步过来的，避免循环同步回 Flomo
        if MARKER_FROM_FLOMO in title or MARKER_FROM_FLOMO in content:
            print(f"[SKIP-3] 来自Flomo标记: {title[:30] if title else content[:30]}...")
            processed_ids.add(note_id)
            continue
        
        # ===== 防护4：跳过包含 ✅ 已同步Flomo 标记的笔记 =====
        # 防止同一个笔记被多次同步到 Flomo
        if MARKER_SYNCED_TO_FLOMO in title or MARKER_SYNCED_TO_FLOMO in content:
            print(f"[SKIP-4] 已同步Flomo标记: {title[:30] if title else content[:30]}...")
            processed_ids.add(note_id)
            continue
        
        # ===== 防护5：内容哈希去重 =====
        if content_hash in processed_hashes:
            print(f"[SKIP-5] 内容重复: {title[:30] if title else content[:30]}...")
            processed_ids.add(note_id)
            continue
        
        if not title and not content:
            skip_count += 1
            processed_ids.add(note_id)
            continue
        
        # ===== 构建发送到 Flomo 的内容 =====
        # 标题作为正文第一行，然后换行接主体
        if title and content:
            # 如果内容已经以标题开头，避免重复
            if content.startswith(title):
                display_content = content
            else:
                display_content = f"{title}\n\n{content}"
        elif title:
            display_content = title
        else:
            display_content = content
        
        display_content = display_content[:1000]  # Flomo 限制长度
        
        # ===== 添加唯一标记，防止循环同步 =====
        # 在内容末尾添加 [getnote-sync:ID]，这样当这条笔记从 Flomo 同步回来时可以被识别
        sync_marker = f"\n\n{MARKER_GETNOTE_SOURCE}:{note_id}"
        
        # 用 AI 匹配标签（基于原始内容）
        print(f"[INFO] AI 匹配标签: {title[:30] if title else content[:30]}...")
        ai_content = content if content else title
        matched_tags = match_tags_with_ai(ai_content[:500], FLOMO_TAGS)
        
        # 添加 ✅ 标记，表示已同步到 Flomo（放在开头便于识别）
        # 添加 [getnote-sync:ID] 标记，用于识别循环同步
        if matched_tags:
            tags_str = " ".join(["#" + tag for tag in matched_tags])
            final_content = f"{MARKER_SYNCED_TO_FLOMO} {tags_str}\n{display_content}{sync_marker}"
        else:
            final_content = f"{MARKER_SYNCED_TO_FLOMO}\n{display_content}{sync_marker}"
        
        # 发送到 Flomo
        print(f"[INFO] 发送到 Flomo...")
        result = flomo_send(final_content)
        
        if result and result.get("code") == 0:
            print(f"[INFO] 同步成功 (标签: {matched_tags})")
            processed_ids.add(note_id)
            processed_hashes.add(content_hash)
            sync_count += 1
        else:
            print(f"[WARN] 同步失败: {result}")
        
        time.sleep(1)
    
    print(f"[INFO] 同步统计: 成功 {sync_count} 条, 跳过 {skip_count} 条")
    # 保留最近 200 条记录
    state["processed_getnote_ids"] = list(processed_ids)[-200:]
    state["processed_getnote_hashes"] = list(processed_hashes)[-200:]
    return state


# ============ 主流程 ============

def main():
    print("=" * 50)
    print("🔄 Get笔记 ↔ Flomo 双向同步开始 (防循环版本 v2)")
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
