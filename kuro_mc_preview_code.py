import json
import logging
import os
import re
import time
from datetime import datetime
from functools import wraps
from typing import List, Set, Dict, Any
from zoneinfo import ZoneInfo

import requests

"""
    鸣潮库街区官方帖子自动监控推送脚本
    功能说明：
    1. 拉取库街区鸣潮官方账号最新帖子，仅处理发布2天内内容
    2. 两类帖子识别：
       - 前瞻通讯预告帖：匹配标题含「前瞻通讯」，自动推送企业微信
       - 兑换码帖子：标题同时包含「通讯」+「回顾影像」，爬取评论提取兑换码
    3. 推送渠道：企业微信机器人markdown_v2格式消息
    4. 内置能力：请求失败自动重试、中国时区日志、接口异常捕获、限流重试
    5. 运行依赖：环境变量 WECOM_WEBHOOK_URL 配置企业微信webhook地址
"""

# ====================== 全局配置常量区 ======================
# 企业微信机器人（从环境变量读取）
WECOM_WEBHOOK_URL = os.getenv("WECOM_WEBHOOK_URL")
WECOM_MAX_RETRY = 3
WECOM_RETRY_WAIT_SEC = 60
WECOM_REQUEST_TIMEOUT = 10
WECOM_LIMIT_ERR_CODE = 45009

# 库街区基础配置
MC_OFFICIAL_USER_ID = "10012001"
DEV_CODE = "hPXOcTMY4btfZPvXbvnDYP6LleAcoD9M"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
KURO_API_TIMEOUT = 15
KURO_SOURCE = "h5"
CODE_HTTP_SUCCESS = 200

# 时间配置
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
TWO_DAY_MS = 2 * 24 * 60 * 60 * 10000

# 业务匹配关键词常量
# 兑换码帖子必须同时包含的标题关键词
CODE_POST_TITLE_KEYS = ("通讯", "回顾影像")
# 前瞻帖子标识
PREVIEW_POST_KEY = "前瞻通讯"
# 兑换码提示文案
CODE_MATCH_TIP = "漂泊者们可前往游戏内兑换领取"
# 评论分组字段
COMMENT_GROUP_KEYS = ["hotComments", "postCommentList", "stickComments"]


# ====================== 标准日志初始化 ======================
def init_logger() -> logging.Logger:
    logger = logging.getLogger("kuro_code_scan")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    class TzFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):
            dt = datetime.fromtimestamp(record.created, tz=SHANGHAI_TZ)
            return dt.strftime("%Y-%m-%d %H:%M:%S")

    log_format = "[%(asctime)s] [%(levelname)s] %(message)s"
    formatter = TzFormatter(fmt=log_format)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


log = init_logger()

# 校验企业微信Webhook环境变量
if not WECOM_WEBHOOK_URL or not WECOM_WEBHOOK_URL.startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"):
    log.error("环境变量 WECOM_WEBHOOK_URL 未配置或链接非法，请检查后重试！")
    raise SystemExit(1)


# ====================== 自定义异常 ======================
class KuroApiError(Exception):
    """库街区接口/网络异常"""
    pass


# ====================== 请求重试装饰器 ======================
def request_retry(max_retry: int, wait_sec: int):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            err_msg = ""
            for i in range(max_retry):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    err_msg = str(e)
                    log.warning(f"请求第{i + 1}次失败，等待{wait_sec}s重试：{err_msg}")
                    time.sleep(wait_sec)
            raise KuroApiError(f"请求重试{max_retry}次全部失败，最后错误：{err_msg}")

        return wrapper

    return decorator


# ====================== 时间戳工具 ======================
def get_now_ms() -> int:
    return int(datetime.now(tz=SHANGHAI_TZ).timestamp() * 1000)


def format_ms_timestamp(ms_ts: int, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if not isinstance(ms_ts, int) or ms_ts <= 0:
        return "无效时间"
    sec = ms_ts / 1000
    dt = datetime.fromtimestamp(sec, SHANGHAI_TZ)
    return dt.strftime(fmt)


def is_ms_ts_within_2days(ms_ts: int) -> bool:
    now_ms = get_now_ms()
    diff = abs(now_ms - ms_ts)
    return diff <= TWO_DAY_MS


# ====================== 文本工具 ======================
def extract_codes(text: str) -> List[str]:
    if not text:
        return []
    return re.findall(r"【([^】]+)】", text)


# ====================== 帖子日志打印公共方法 ======================
def log_hit_post(post: Dict[str, Any], hit_type: str):
    """统一打印命中帖子完整信息，无文本截断"""
    post_id = post.get("postId", "")
    title = post.get("postTitle", "")
    content = post.get("postContent", "")
    ts_ms = int(post.get("createTimestamp", 0))
    create_time = format_ms_timestamp(ts_ms)
    link = f"https://www.kurobbs.com/mc/post/{post_id}"

    log.info(f"===== 命中{hit_type}帖子详情 =====")
    log.info(f"帖子ID: {post_id}")
    log.info(f"创建时间: {create_time}")
    log.info(f"帖子标题: {title}")
    log.info(f"帖子完整内容: {content}")
    log.info(f"帖子链接: {link}")


# ====================== 接口通用校验 ======================
def check_response(resp: requests.Response) -> Dict[str, Any]:
    if resp.status_code != 200:
        raise KuroApiError(f"HTTP状态异常 {resp.status_code} | 响应片段：{resp.text[:500]}")
    try:
        res = resp.json()
    except Exception as e:
        raise KuroApiError(f"接口返回非JSON，解析失败：{str(e)}，原文：{resp.text[:500]}")
    if res.get("code") != CODE_HTTP_SUCCESS:
        raise KuroApiError(f"业务码异常 code={res.get('code')} msg={res.get('msg')}")
    return res.get("data", {})


# ====================== 企业微信推送 ======================
@request_retry(max_retry=WECOM_MAX_RETRY, wait_sec=WECOM_RETRY_WAIT_SEC)
def send_wechat_markdown(md_content: str) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    payload = {
        "msgtype": "markdown_v2",
        "markdown_v2": {"content": md_content}
    }
    with requests.Session() as sess:
        resp = sess.post(
            WECOM_WEBHOOK_URL,
            data=json.dumps(payload, ensure_ascii=False),
            headers=headers,
            timeout=WECOM_REQUEST_TIMEOUT
        )
        ret = resp.json()
        if ret.get("errcode") == WECOM_LIMIT_ERR_CODE:
            log.warning(f"企业微信触发限流，等待{WECOM_RETRY_WAIT_SEC}秒重试")
            raise KuroApiError("企业微信45009限流错误")
        if ret.get("errcode") != 0:
            log.error(f"企业微信消息推送失败，返回：{ret}")
        return ret


# ====================== 库街区接口封装 ======================
def build_kuro_headers(token: str = "") -> Dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "User-Agent": USER_AGENT,
        "devCode": DEV_CODE,
        "source": KURO_SOURCE,
        "token": token
    }


def get_kuro_user_posts(
        other_user_id: str,
        search_type: int = 1,
        type_val: int = 2,
        page_index: int = 1,
        page_size: int = 100,
        token: str = ""
) -> Dict[str, Any]:
    url = "https://api.kurobbs.com/forum/getMinePost"
    headers = build_kuro_headers(token)
    data = {
        "searchType": str(search_type),
        "type": str(type_val),
        "otherUserId": str(other_user_id),
        "pageIndex": str(page_index),
        "pageSize": str(page_size)
    }
    with requests.Session() as sess:
        resp = sess.post(url, headers=headers, data=data, timeout=KURO_API_TIMEOUT)
    return check_response(resp)


def get_kuro_post_comments(post_id: str, token: str = "") -> Dict[str, Any]:
    url = "https://api.kurobbs.com/forum/comment/getPostCommentListV2"
    headers = build_kuro_headers(token)
    data = {
        "postId": post_id,
        "showOrderType": "2",
        "isOnlyPublisher": "0",
        "pageIndex": "1",
        "pageSize": "50"
    }
    with requests.Session() as sess:
        resp = sess.post(url, headers=headers, data=data, timeout=KURO_API_TIMEOUT)
    return check_response(resp)


# ====================== 业务过滤工具（核心拆分） ======================
def is_preview_post(post: Dict[str, Any]) -> bool:
    """判断是否为前瞻预告帖子"""
    title = post.get("postTitle", "")
    content = post.get("postContent", "")
    ts_ms = int(post.get("createTimestamp", 0))
    if not is_ms_ts_within_2days(ts_ms):
        return False
    return PREVIEW_POST_KEY in title and "版本前瞻通讯将于" in content


def is_code_post(post: Dict[str, Any]) -> bool:
    """判断是否为兑换码帖子：标题包含指定关键词 + 发布2天内"""
    title = post.get("postTitle", "")
    ts_ms = int(post.get("createTimestamp", 0))
    # 标题全部关键词匹配 + 时间过滤
    if not all(k in title for k in CODE_POST_TITLE_KEYS):
        return False
    if not is_ms_ts_within_2days(ts_ms):
        return False
    return True


def extract_code_text(post_id: str) -> str:
    """从评论提取含兑换码提示的文本"""
    comment_data = get_kuro_post_comments(post_id)
    text_set: Set[str] = set()
    for group in COMMENT_GROUP_KEYS:
        comments = comment_data.get(group, [])
        for comment in comments:
            content_items = comment.get("commentContent", [])
            for item in content_items:
                text = item.get("content", "")
                if CODE_MATCH_TIP in text:
                    text_set.add(text)
    if not text_set:
        return ""
    # 优先最短完整文案
    return sorted(text_set, key=lambda s: len(s))[0]


# ====================== 消息模板构建 ======================
def build_preview_md(post: Dict[str, Any]) -> str:
    """构建前瞻预告推送消息"""
    title = post.get("postTitle", "")
    content = post.get("postContent", "")
    ts_ms = int(post.get("createTimestamp", 0))
    post_id = post.get("postId", "")
    create_time = format_ms_timestamp(ts_ms)
    link = f"https://www.kurobbs.com/mc/post/{post_id}"
    md = [
        f"## 📢 鸣潮版本前瞻预告",
        f"标题：{title}",
        f"时间：{create_time}",
        f"摘要：{content}",
        f"链接：{link}"
    ]
    return "\n\r".join(md)


def build_code_md(post: Dict[str, Any], code_text: str) -> str:
    """构建兑换码帖子推送消息（含帖子正文+兑换码列表）"""
    title = post.get("postTitle", "")
    content = post.get("postContent", "")
    ts_ms = int(post.get("createTimestamp", 0))
    post_id = post.get("postId", "")
    create_time = format_ms_timestamp(ts_ms)
    link = f"https://www.kurobbs.com/mc/post/{post_id}"
    codes = extract_codes(code_text)

    md = [
        f"## 🎁 鸣潮版本前瞻兑换码更新",
        f"标题：{title}",
        f"时间：{create_time}",
        f"链接：{link}",
        f"摘要：{content}"
    ]
    if code_text:
        md.append(f"\n\r兑换码：{code_text}")
    if codes:
        code_block = "\n\r".join([f"- {c}" for c in codes])
        md.append(f"\n\r### 兑换码列表\n{code_block}")
    return "\n\r".join(md)


# ====================== 主扫描逻辑 ======================
def scan_official_post():
    start = time.time()
    log.info("===== 开始扫描库街区官方鸣潮帖子 =====")
    post_data = get_kuro_user_posts(MC_OFFICIAL_USER_ID)
    post_list = post_data.get("postList", [])

    if not post_list:
        log.info("未获取到官方帖子列表，本次扫描结束")
        return

    preview_count = 0
    code_count = 0

    for post in post_list:
        # 分支1：前瞻预告帖
        if is_preview_post(post):
            preview_count += 1
            log_hit_post(post, "前瞻预告")
            md = build_preview_md(post)
            send_wechat_markdown(md)
            continue

        # 分支2：兑换码帖子
        if is_code_post(post):
            code_count += 1
            log_hit_post(post, "兑换码")
            pid = post.get("postId", "")
            code_text = extract_code_text(pid)
            log.info(f"提取兑换码文案：{code_text}")
            md = build_code_md(post, code_text)
            send_wechat_markdown(md)
            continue

        # 无关帖子直接跳过
        continue

    # 扫描结果汇总日志
    log.info(f"本轮扫描完成 | 前瞻帖子推送：{preview_count} 条 | 兑换码帖子推送：{code_count} 条")
    if preview_count == 0 and code_count == 0:
        log.info("本轮未匹配到有效期内的前瞻预告、兑换码相关帖子")

    cost = round(time.time() - start, 2)
    log.info(f"扫描任务总耗时：{cost}s\n")


# ====================== 程序入口 ======================
if __name__ == '__main__':
    try:
        scan_official_post()
    except KuroApiError as e:
        log.error(f"库街区接口异常终止：{str(e)}")
    except Exception as e:
        log.error(f"全局未知异常", exc_info=True)
