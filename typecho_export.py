#!/usr/bin/env python3
"""
Typecho 文章导出工具
- 通过 XML-RPC 拉取所有文章（含私密文章）
- 转为 Markdown 上传到 Cloudflare R2（通过 Cloudflare API）
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
CF_API_TOKEN = os.environ["R2_ACCESS_KEY_ID"]  # Cloudflare API Token (cfat_...)
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

    if post.get("dateCreated"):
        dt = post["dateCreated"]
        if hasattr(dt, "strftime"):
            lines.append(f'date: {dt.strftime("%Y-%m-%d %H:%M:%S")}')

    if post.get("categories"):
        lines.append(f"categories: {post['categories']}")

    if post.get("mt_keywords"):
        tags = [t.strip() for t in post["mt_keywords"].split(",") if t.strip()]
        if tags:
            lines.append(f"tags: {tags}")

    if post.get("password"):
        lines.append(f'password: "{post["password"]}"')
        lines.append("status: private")
    elif post.get("post_status") == "private":
        lines.append("status: private")
    else:
        lines.append("status: publish")

    lines.append(f'post_id: {post.get("postid", "")}')
    lines.append("---")
    return "\n".join(lines)


def build_markdown(post):
    content_html = post.get("description", "")
    more_html = post.get("mt_text_more", "")
    if more_html:
        content_html += "\n" + more_html
    content_md = convert_html_to_md(content_html)
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
    """列出 R2 桶中指定前缀的所有 key"""
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
        # 分页
        result_info = data.get("result_info", {})
        if not result or result_info.get("page", 0) >= result_info.get("total_pages", 1):
            break
        params["cursor"] = result[-1].get("key", "")
    return keys


def r2_upload(key, content):
    """上传文件到 R2"""
    url = f"{CF_API}/objects/{key}"
    resp = requests.put(
        url,
        headers={**cf_headers(), "Content-Type": "text/markdown; charset=utf-8"},
        data=content.encode("utf-8"),
    )
    if not resp.json().get("success"):
        print(f"  上传失败 {key}: {resp.json().get('errors')}")


def r2_delete(key):
    """删除 R2 中的文件"""
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

    uploaded_keys = set()

    for i, post in enumerate(posts, 1):
        title = post.get("title", "无标题")
        post_id = post.get("postid", "0")
        is_private = post.get("password") or post.get("post_status") == "private"
        status = "私密" if is_private else "公开"

        filename = f"{post_id}_{slugify(title)}.md"
        r2_key = f"{R2_PREFIX}{filename}"
        md = build_markdown(post)

        r2_upload(r2_key, md)
        uploaded_keys.add(r2_key)
        print(f"  [{i}/{len(posts)}] [{status}] {title} -> {r2_key}")

    # 清理已删除的文章
    to_delete = existing_keys - uploaded_keys
    for key in to_delete:
        r2_delete(key)
        print(f"  [删除] {key}")

    print(f"\n完成: 上传 {len(posts)} 篇, 清理 {len(to_delete)} 篇")


if __name__ == "__main__":
    main()
