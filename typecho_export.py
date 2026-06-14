#!/usr/bin/env python3
"""
Typecho 文章导出工具
- 通过 XML-RPC 拉取所有文章（含私密文章）
- SHA256 hash 增量检测，仅上传变更文件
- 支持导出到本地目录 + 上传到 Cloudflare R2
"""

import xmlrpc.client
import html2text
import hashlib
import json
import os
import re
import sys
import time
import boto3
from datetime import datetime


# ========== 配置（环境变量覆盖）==========
TYPECHO_URL = os.environ["TYPECHO_URL"]
USERNAME = os.environ["TYPECHO_USERNAME"]
PASSWORD = os.environ["TYPECHO_PASSWORD"]
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "typecho_posts")
HASH_FILE = os.environ.get("HASH_FILE", "typecho_posts/.hashes.json")

# R2 配置
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET = os.environ.get("R2_BUCKET")
R2_PREFIX = os.environ.get("R2_PREFIX", "posts/")  # 桶内路径前缀
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


def extract_full_content(post):
    content_html = post.get("description", "")
    more_html = post.get("mt_text_more", "")
    if more_html:
        content_html += "\n" + more_html
    return content_html


def compute_post_hash(post):
    content_html = extract_full_content(post)
    md_content = convert_html_to_md(content_html)
    frontmatter = build_frontmatter(post)
    full = f"{frontmatter}\n\n{md_content}"
    return hashlib.sha256(full.encode("utf-8")).hexdigest()


def load_hashes():
    if os.path.exists(HASH_FILE):
        with open(HASH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_hashes(hashes):
    os.makedirs(os.path.dirname(HASH_FILE), exist_ok=True)
    with open(HASH_FILE, "w", encoding="utf-8") as f:
        json.dump(hashes, f, ensure_ascii=False, indent=2)


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


def build_markdown(post):
    """生成单篇文章的完整 markdown 内容"""
    content_html = extract_full_content(post)
    content_md = convert_html_to_md(content_html)
    frontmatter = build_frontmatter(post)
    return f"{frontmatter}\n\n{content_md}"


def get_r2_client():
    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY]):
        return None
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def r2_upload(s3, bucket, key, content, content_type="text/markdown; charset=utf-8"):
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType=content_type,
    )


def r2_delete(s3, bucket, key):
    try:
        s3.delete_object(Bucket=bucket, Key=key)
    except Exception:
        pass


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

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 初始化 R2 客户端
    s3 = get_r2_client()
    r2_enabled = s3 is not None and R2_BUCKET
    if r2_enabled:
        print(f"R2 桶: {R2_BUCKET}，前缀: {R2_PREFIX}")
    else:
        print("R2 未配置，仅导出本地文件")

    # 加载旧 hash
    old_hashes = load_hashes()
    new_hashes = {}

    added = []
    updated = []
    unchanged = []
    removed_keys = set(old_hashes.keys())

    for post in posts:
        post_id = post.get("postid", "0")
        title = post.get("title", "无标题")
        is_private = post.get("password") or post.get("post_status") == "private"
        status = "私密" if is_private else "公开"

        new_hash = compute_post_hash(post)
        new_hashes[post_id] = {
            "hash": new_hash,
            "title": title,
            "status": status,
        }

        filename = f"{post_id}_{slugify(title)}.md"
        filepath = os.path.join(OUTPUT_DIR, filename)
        r2_key = f"{R2_PREFIX}{filename}"

        old = old_hashes.get(post_id)

        if old is None:
            # 新文章
            md = build_markdown(post)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(md)
            if r2_enabled:
                r2_upload(s3, R2_BUCKET, r2_key, md)
            added.append(f"[新增] [{status}] {title} -> {filename}")

        elif old["hash"] != new_hash:
            # 内容变化 - 删旧文件（文件名可能因标题改变）
            old_filename = f"{post_id}_{slugify(old['title'])}.md"
            old_path = os.path.join(OUTPUT_DIR, old_filename)
            if os.path.exists(old_path):
                os.remove(old_path)
            if r2_enabled and old_filename != filename:
                r2_delete(s3, R2_BUCKET, f"{R2_PREFIX}{old_filename}")

            md = build_markdown(post)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(md)
            if r2_enabled:
                r2_upload(s3, R2_BUCKET, r2_key, md)
            updated.append(f"[更新] [{status}] {title} -> {filename}")

        else:
            unchanged.append(f"[不变] [{status}] {title}")

        removed_keys.discard(post_id)

    # 处理已删除的文章
    deleted = []
    for post_id in removed_keys:
        old_title = old_hashes[post_id]["title"]
        old_filename = f"{post_id}_{slugify(old_title)}.md"
        old_path = os.path.join(OUTPUT_DIR, old_filename)
        if os.path.exists(old_path):
            os.remove(old_path)
        if r2_enabled:
            r2_delete(s3, R2_BUCKET, f"{R2_PREFIX}{old_filename}")
        deleted.append(f"[删除] {old_title}")

    # 保存新 hash
    save_hashes(new_hashes)

    # 输出结果
    for line in added:
        print(f"  {line}")
    for line in updated:
        print(f"  {line}")
    for line in deleted:
        print(f"  {line}")
    for line in unchanged:
        print(f"  {line}")

    print(f"\n汇总: 新增 {len(added)}, 更新 {len(updated)}, 删除 {len(deleted)}, 不变 {len(unchanged)}")

    has_changes = bool(added or updated or deleted)
    print(f"HAS_CHANGES={'true' if has_changes else 'false'}")

    return has_changes


if __name__ == "__main__":
    main()
