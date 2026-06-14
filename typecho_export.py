#!/usr/bin/env python3
"""
Typecho 文章导出工具
- 通过 XML-RPC 拉取所有文章（含私密文章）
- 转为 Markdown 上传到 Cloudflare R2
"""

import xmlrpc.client
import html2text
import os
import re
import sys
import time
import boto3


# ========== 配置（从环境变量读取）==========
TYPECHO_URL = os.environ["TYPECHO_URL"]
USERNAME = os.environ["TYPECHO_USERNAME"]
PASSWORD = os.environ["TYPECHO_PASSWORD"]

R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET = os.environ["R2_BUCKET"]
R2_PREFIX = os.environ.get("R2_PREFIX", "posts/")
# ===========================================


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

    # 初始化 R2
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )

    # 拉取 R2 现有文件列表，用于清理已删除的文章
    existing_keys = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=R2_PREFIX):
        for obj in page.get("Contents", []):
            existing_keys.add(obj["Key"])

    uploaded_keys = set()

    for i, post in enumerate(posts, 1):
        title = post.get("title", "无标题")
        post_id = post.get("postid", "0")
        is_private = post.get("password") or post.get("post_status") == "private"
        status = "私密" if is_private else "公开"

        filename = f"{post_id}_{slugify(title)}.md"
        r2_key = f"{R2_PREFIX}{filename}"
        md = build_markdown(post)

        s3.put_object(
            Bucket=R2_BUCKET,
            Key=r2_key,
            Body=md.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        uploaded_keys.add(r2_key)
        print(f"  [{i}/{len(posts)}] [{status}] {title} -> {r2_key}")

    # 清理 R2 上已删除的文章
    to_delete = existing_keys - uploaded_keys
    for key in to_delete:
        s3.delete_object(Bucket=R2_BUCKET, Key=key)
        print(f"  [删除] {key}")

    print(f"\n完成: 上传 {len(posts)} 篇, 清理 {len(to_delete)} 篇")


if __name__ == "__main__":
    main()
