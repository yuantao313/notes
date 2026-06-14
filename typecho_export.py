#!/usr/bin/env python3
"""
Typecho 文章导出工具
- 通过 XML-RPC 拉取所有文章（含私密文章）
- 转为 Markdown 上传到 Cloudflare R2
- 内链转 Obsidian 格式，使用实际文件名
- 文件名格式: yyyymmdd_title.md
"""

import xmlrpc.client
import html2text
import os
import re
import sys
import time
import requests


# ========== 配置（从环境变量读取）==========
TYPECHO_URL = os.environ["TYPECHO_URL"]
USERNAME = os.environ["TYPECHO_USERNAME"]
PASSWORD = os.environ["TYPECHO_PASSWORD"]

CF_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
CF_API_TOKEN = os.environ["R2_ACCESS_KEY_ID"]
R2_BUCKET = os.environ["R2_BUCKET"]
R2_PREFIX = os.environ.get("R2_PREFIX", "posts/")
# ===========================================

CF_API = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/r2/buckets/{R2_BUCKET}"


def get_xmlrpc_url(base_url):
    return f"{base_url.rstrip('/')}/index.php/action/xmlrpc"


def slugify(text):
    text = re.sub(r'[<>:"/\\|?*]', '', text)
    text = text.strip()
    if len(text) > 80:
        text = text[:80]
    return text or "untitled"


def parse_date(dt):
    """解析 xmlrpc.client.DateTime 为 datetime 对象"""
    from datetime import datetime
    if dt and hasattr(dt, "value"):
        # dt.value 格式: "20260406T23:00:00"
        return datetime.strptime(dt.value, "%Y%m%dT%H:%M:%S")
    return None


def get_filename(post):
    """生成文件名: yyyymmdd_title.md"""
    title = post.get("title", "无标题")
    dt = parse_date(post.get("dateCreated"))
    date_str = dt.strftime("%Y%m%d") if dt else "00000000"
    return f"{date_str}_{slugify(title)}.md"


def get_filename_no_ext(post):
    """生成不带扩展名的文件名，用于 Obsidian 内链"""
    title = post.get("title", "无标题")
    dt = parse_date(post.get("dateCreated"))
    date_str = dt.strftime("%Y%m%d") if dt else "00000000"
    return f"{date_str}_{slugify(title)}"


def convert_html_to_md(html_content):
    h = html2text.HTML2Text()
    h.body_width = 0
    h.protect_links = True
    h.wrap_links = False
    h.unicode_snob = True
    return h.handle(html_content or "")


def build_frontmatter(post):
    lines = ["---"]
    lines.append(f'title: "{post["title"]}"')

    dt = parse_date(post.get("dateCreated"))
    if dt:
        lines.append(f'date: {dt.strftime("%Y-%m-%d %H:%M:%S")}')

    dm = parse_date(post.get("date_modified"))
    if dm:
        lines.append(f'updated: {dm.strftime("%Y-%m-%d %H:%M:%S")}')

    if post.get("categories"):
        lines.append(f"categories: {post['categories']}")

    if post.get("mt_keywords"):
        tags = [t.strip() for t in post["mt_keywords"].split(",") if t.strip()]
        if tags:
            lines.append(f"tags: {tags}")

    if post.get("wp_author_display_name"):
        lines.append(f'author: "{post["wp_author_display_name"]}"')

    if post.get("password"):
        lines.append(f'password: "{post["password"]}"')
        lines.append("status: private")
    elif post.get("post_status") == "private":
        lines.append("status: private")
    else:
        lines.append("status: publish")

    if post.get("link"):
        lines.append(f'source: "{post["link"]}"')

    lines.append(f'post_id: {post.get("postid", "")}')
    lines.append("---")
    return "\n".join(lines)


def build_markdown(post, id_to_filename=None):
    content_html = post.get("description", "")
    more_html = post.get("mt_text_more", "")
    if more_html:
        content_html += "\n" + more_html
    content_md = convert_html_to_md(content_html)

    # 内链转 Obsidian 格式: [text](url) -> [[filename]] 或 [[filename|text]]
    if id_to_filename:
        def replace_link(m):
            text = m.group(1)
            url = m.group(2)
            match = re.search(r'/archives/(\d+)/', url)
            if match:
                pid = match.group(1)
                if pid in id_to_filename:
                    target = id_to_filename[pid]
                    if text.strip() == target:
                        return f"[[{target}]]"
                    return f"[[{target}|{text}]]"
            return m.group(0)
        content_md = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', replace_link, content_md)

    frontmatter = build_frontmatter(post)
    return f"{frontmatter}\n\n{content_md}"


def fetch_all_posts(proxy, blog_id, username, password):
    all_posts = []
    batch_size = 100

    while True:
        offset = len(all_posts)
        print(f"  拉取第 {offset + 1} ~ {offset + batch_size} 篇...")
        try:
            posts = proxy.metaWeblog.getRecentPosts(
                blog_id, username, password, batch_size
            )
        except Exception as e:
            print(f"  拉取出错: {e}")
            break

        if not posts:
            break

        all_posts.extend(posts)

        if len(posts) < batch_size:
            break

        time.sleep(0.5)

    return all_posts


def cf_headers():
    return {"Authorization": f"Bearer {CF_API_TOKEN}"}


def r2_list_keys(prefix):
    keys = set()
    url = f"{CF_API}/objects"
    params = {"prefix": prefix}
    while True:
        resp = requests.get(url, headers=cf_headers(), params=params)
        data = resp.json()
        if not data.get("success"):
            print(f"  列表失败: {data.get('errors')}")
            break
        result = data.get("result", [])
        for obj in result:
            keys.add(obj["key"])
        result_info = data.get("result_info", {})
        if not result or result_info.get("page", 0) >= result_info.get("total_pages", 1):
            break
        params["cursor"] = result[-1].get("key", "")
    return keys


def r2_upload(key, content):
    url = f"{CF_API}/objects/{key}"
    resp = requests.put(
        url,
        headers={**cf_headers(), "Content-Type": "text/markdown; charset=utf-8"},
        data=content.encode("utf-8"),
    )
    if not resp.json().get("success"):
        print(f"  上传失败 {key}: {resp.json().get('errors')}")


def r2_delete(key):
    url = f"{CF_API}/objects/{key}"
    resp = requests.delete(url, headers=cf_headers())
    if not resp.json().get("success"):
        print(f"  删除失败 {key}: {resp.json().get('errors')}")


def main():
    rpc_url = get_xmlrpc_url(TYPECHO_URL)
    print(f"连接 Typecho XML-RPC: {rpc_url}")

    proxy = xmlrpc.client.ServerProxy(rpc_url, allow_none=True)

    try:
        blogs = proxy.blogger.getUsersBlogs("", USERNAME, PASSWORD)
        blog_id = blogs[0]["blogid"]
        print(f"博客: {blogs[0].get('blogName', 'Unknown')} (id={blog_id})")
    except Exception as e:
        print(f"连接失败: {e}")
        sys.exit(1)

    print("\n开始拉取文章...")
    posts = fetch_all_posts(proxy, blog_id, USERNAME, PASSWORD)
    print(f"共获取 {len(posts)} 篇文章\n")

    if not posts:
        print("没有文章，退出。")
        return

    # 列出 R2 现有文件
    print(f"R2 桶: {R2_BUCKET}，前缀: {R2_PREFIX}")
    existing_keys = r2_list_keys(R2_PREFIX)
    print(f"  桶中现有 {len(existing_keys)} 个文件")

    # 构建 post_id -> 文件名（无扩展名）映射，用于内链转换
    id_to_filename = {p.get("postid", ""): get_filename_no_ext(p) for p in posts}

    uploaded_keys = set()

    for i, post in enumerate(posts, 1):
        title = post.get("title", "无标题")
        is_private = post.get("password") or post.get("post_status") == "private"
        status = "私密" if is_private else "公开"

        filename = get_filename(post)
        r2_key = f"{R2_PREFIX}{filename}"
        md = build_markdown(post, id_to_filename)

        r2_upload(r2_key, md)
        uploaded_keys.add(r2_key)
        print(f"  [{i}/{len(posts)}] [{status}] {title} -> {filename}")

    # 清理已删除的文章
    to_delete = existing_keys - uploaded_keys
    for key in to_delete:
        r2_delete(key)
        print(f"  [删除] {key}")

    print(f"\n完成: 上传 {len(posts)} 篇, 清理 {len(to_delete)} 篇")


if __name__ == "__main__":
    main()
