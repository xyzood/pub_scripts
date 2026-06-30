import json
import logging
import os
import re
import time
from datetime import datetime
from enum import StrEnum
from functools import wraps
from typing import List, Set, Dict, Any
from zoneinfo import ZoneInfo

import requests
from requests.exceptions import RequestException


# ====================== 【配置常量层】统一管理所有开关、地址、关键词、阈值 ======================
class Config:
    # 时区
    SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
    MS_PER_DAY = 24 * 60 * 60 * 1000
    TWO_DAY_MS = 2 * MS_PER_DAY

    # 企业微信机器人配置
    WECOM_WEBHOOK_URL = os.getenv("WECOM_WEBHOOK_URL", "")
    WECOM_MAX_RETRY = 3
    WECOM_RETRY_WAIT = 60
    WECOM_TIMEOUT = 10
    WECOM_LIMIT_CODE = 45009

    # 库街区接口基础配置
    KURO_OFFICIAL_UID = os.getenv("KURO_OFFICIAL_UID", "10012001")
    KURO_DEV_CODE = os.getenv("KURO_DEV_CODE", "hPXOcTMY4btfZPvXbvnDYP6LleAcoD9M")
    KURO_SOURCE = "h5"
    KURO_VERSION = "3.1.3"
    KURO_TIMEOUT = 15
    HTTP_SUCCESS = 200
    UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"

    # 帖子链接模板
    POST_LINK_TPL = "https://www.kurobbs.com/mc/post/{post_id}"

    # 接口地址
    class ApiPath(StrEnum):
        GET_USER_POSTS = "https://api.kurobbs.com/forum/getMinePost"
        GET_POST_DETAIL = "https://api.kurobbs.com/forum/getPostDetail"
        GET_POST_COMMENTS = "https://api.kurobbs.com/forum/comment/getPostCommentListV2"

    # 业务关键词配置
    PREVIEW_TITLE_KEY = "前瞻通讯"
    PREVIEW_CONTENT_KEY = "版本前瞻通讯将于"
    CODE_TITLE_KEYS = ("通讯", "回顾影像")
    CODE_COMMENT_TIP = "漂泊者们可前往游戏内兑换领取"
    ACTIVITY_TITLE_KEY = "活动"
    ACTIVITY_CONTENT_KEY = "领取星声"

    # 评论分组字段
    COMMENT_GROUPS = ["hotComments", "postCommentList", "stickComments"]

    # 分页默认参数
    POST_PAGE_SIZE = 20
    COMMENT_PAGE_SIZE = 100

    # 日志截断长度
    LOG_CONTENT_TRUNCATE = 1000
    # 企微消息最大长度限制
    MAX_WECOM_MD_LEN = 4000


# ====================== 【自定义异常层】细分异常类型，便于精准捕获 ======================
class BaseMonitorError(Exception):
    """脚本基础异常父类"""
    pass


class KuroApiError(BaseMonitorError):
    """库街区接口网络/业务异常"""
    pass


class WecomPushError(BaseMonitorError):
    """企业微信推送异常"""
    pass


# ====================== 【日志工具层】标准化上海时区日志 ======================
def init_logger() -> logging.Logger:
    logger = logging.getLogger("mc_kuro_monitor")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    class TzFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None) -> str:
            dt = datetime.fromtimestamp(record.created, tz=Config.SHANGHAI_TZ)
            return dt.strftime("%Y-%m-%d %H:%M:%S")

    fmt = "[%(asctime)s] [%(levelname)-4s] %(message)s"
    formatter = TzFormatter(fmt=fmt)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger


log = init_logger()

# 启动前置校验webhook
if not Config.WECOM_WEBHOOK_URL or not Config.WECOM_WEBHOOK_URL.startswith(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"):
    log.critical("WECOM_WEBHOOK_URL 环境变量缺失或非法，程序退出")
    raise SystemExit(1)

if not Config.KURO_DEV_CODE:
    log.critical("KURO_DEV_CODE 环境变量未配置，程序退出")
    raise SystemExit(1)


# ====================== 【通用工具层】时间、字典安全取值、文本处理、重试装饰器 ======================
def request_retry(max_retry: int, wait_sec: int):
    """简化版重试装饰器：仅捕获网络、接口、推送异常，其余直接抛出不重试"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_err = ""
            for i in range(max_retry):
                try:
                    return func(*args, **kwargs)
                except (RequestException, KuroApiError, WecomPushError) as e:
                    last_err = str(e)
                    log.warning(f"第{i + 1}次请求失败，{wait_sec}s后重试：{last_err[:Config.LOG_CONTENT_TRUNCATE]}")
                    time.sleep(wait_sec)
            raise BaseMonitorError(f"重试{max_retry}次均失败：{last_err}")

        return wrapper

    return decorator


def safe_get(data: Dict[str, Any], key: str, default: Any = None) -> Any:
    """字典安全取值，统一封装，消除大量dict.get样板代码"""
    if not isinstance(data, dict):
        return default
    return data.get(key, default)


# 时间工具
def now_ms() -> int:
    """获取当前上海时区毫秒时间戳"""
    return int(datetime.now(tz=Config.SHANGHAI_TZ).timestamp() * 1000)


def format_ms(ms_ts: int, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """毫秒时间戳格式化"""
    if not isinstance(ms_ts, int) or ms_ts <= 0:
        return "无效时间戳"
    dt = datetime.fromtimestamp(ms_ts / 1000, tz=Config.SHANGHAI_TZ)
    return dt.strftime(fmt)


def is_within_days(ms_ts: int, day_count: int = 1) -> bool:
    """判断时间戳前后N天范围内，包含未来时间"""
    diff = abs(now_ms() - ms_ts)
    return diff <= day_count * Config.MS_PER_DAY


# 文本工具（修复兑换码正则，兼容全角/半角括号）
def extract_codes_from_text(text: str) -> List[str]:
    """兼容【】和[]两种括号提取兑换码，自动清洗空格、过滤无效长度"""
    if not text:
        return []
    pattern = r"[【\[](.*?)[】\]]"
    raw_list = re.findall(pattern, text.strip())
    result = []
    for code in raw_list:
        clean_code = code.strip()
        if 3 <= len(clean_code) <= 30:
            result.append(clean_code)
    return result


def truncate_log_text(text: str) -> str:
    """超长日志文本截断，防止刷屏"""
    if len(text) <= Config.LOG_CONTENT_TRUNCATE:
        return text
    return f"{text[:Config.LOG_CONTENT_TRUNCATE]}...(已截断，总长{len(text)})"


def escape_md_var(text: str) -> str:
    """仅转义动态变量，固定markdown语法不处理"""
    special_chars = r"_*[]()#+-!"
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


# 日志打印帖子详情统一方法
def log_post_detail(post: Dict[str, Any], hit_tag: str):
    post_id = safe_get(post, "postId", "")
    title = safe_get(post, "postTitle", "")
    content = safe_get(post, "postContent", "")
    ts_ms = int(safe_get(post, "createTimestamp", 0))
    create_time = format_ms(ts_ms)
    link = Config.POST_LINK_TPL.format(post_id=post_id)

    log.info(f"===== 命中[{hit_tag}]帖子 =====")
    log.info(f"帖子ID: {post_id}")
    log.info(f"创建时间: {create_time}")
    log.info(f"标题: {truncate_log_text(title)}")
    log.info(f"正文: {truncate_log_text(content)}")
    log.info(f"帖子链接: {link}")


# ====================== 【库街区接口层】单例Session复用，统一响应校验 ======================
class KuroApiClient:
    def __init__(self, token: str = ""):
        self.token = token
        self.session = requests.Session()
        self.headers = self._build_headers()

    def _build_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "User-Agent": Config.UA,
            "devCode": Config.KURO_DEV_CODE,
            "source": Config.KURO_SOURCE,
            "token": self.token,
            "version": Config.KURO_VERSION
        }

    def _check_resp(self, resp: requests.Response) -> Dict[str, Any]:
        if resp.status_code != Config.HTTP_SUCCESS:
            raise KuroApiError(f"HTTP{resp.status_code} 异常，响应片段: {truncate_log_text(resp.text)}")
        try:
            json_data = resp.json()
        except Exception as e:
            raise KuroApiError(f"JSON解析失败: {str(e)}，原文: {truncate_log_text(resp.text)}")
        if safe_get(json_data, "code") != Config.HTTP_SUCCESS:
            msg = safe_get(json_data, "msg", "无错误信息")
            raise KuroApiError(f"业务码异常 code={json_data.get('code')} msg={msg}")
        return safe_get(json_data, "data", {})

    def get_user_posts(self, user_id: str) -> Dict[str, Any]:
        """获取用户帖子列表"""
        data = {
            "searchType": "1",
            "type": "2",
            "otherUserId": user_id,
            "pageIndex": "1",
            "pageSize": str(Config.POST_PAGE_SIZE)
        }
        resp = self.session.post(
            url=Config.ApiPath.GET_USER_POSTS,
            headers=self.headers,
            data=data,
            timeout=Config.KURO_TIMEOUT
        )
        return self._check_resp(resp)

    def get_post_detail(self, post_id: str) -> Dict[str, Any]:
        """获取帖子完整详情"""
        data = {
            "postId": post_id,
            "showOrderType": "2",
            "isOnlyPublisher": "0",
        }
        resp = self.session.post(
            url=Config.ApiPath.GET_POST_DETAIL,
            headers=self.headers,
            data=data,
            timeout=Config.KURO_TIMEOUT
        )
        return self._check_resp(resp)

    def get_post_comments(self, post_id: str) -> Dict[str, Any]:
        """获取帖子评论"""
        data = {
            "postId": post_id,
            "showOrderType": "2",
            "isOnlyPublisher": "0",
            "pageIndex": "1",
            "pageSize": str(Config.COMMENT_PAGE_SIZE)
        }
        resp = self.session.post(
            url=Config.ApiPath.GET_POST_COMMENTS,
            headers=self.headers,
            data=data,
            timeout=Config.KURO_TIMEOUT
        )
        return self._check_resp(resp)


# ====================== 【企业微信推送层】独立封装，重试+限流处理 ======================
@request_retry(max_retry=Config.WECOM_MAX_RETRY, wait_sec=Config.WECOM_RETRY_WAIT)
def push_wecom_markdown(raw_md: str):
    """发送markdown_v2消息，仅转义动态变量，超长截断"""
    # 超长截断保护
    if len(raw_md) > Config.MAX_WECOM_MD_LEN:
        raw_md = raw_md[:Config.MAX_WECOM_MD_LEN] + "\n...内容过长已截断"

    payload = {
        "msgtype": "markdown_v2",
        "markdown_v2": {"content": raw_md}
    }
    headers = {"Content-Type": "application/json;charset=utf-8"}
    with requests.Session() as sess:
        resp = sess.post(
            url=Config.WECOM_WEBHOOK_URL,
            data=json.dumps(payload, ensure_ascii=False),
            headers=headers,
            timeout=Config.WECOM_TIMEOUT
        )
        ret = resp.json()
        errcode = safe_get(ret, "errcode", -1)
        if errcode == Config.WECOM_LIMIT_CODE:
            log.warning("企业微信45009限流，触发重试机制")
            raise WecomPushError("接口触发限流")
        if errcode != 0:
            log.error(f"企业微信推送失败，返回完整数据: {ret}")
            raise WecomPushError(f"推送返回错误码{errcode}")
    log.info("企业微信消息推送成功")
    return ret


# ====================== 【业务判断层】帖子分类匹配逻辑，纯判断无副作用 ======================
def match_preview_post(post: Dict[str, Any]) -> bool:
    """匹配前瞻通讯预告帖"""
    title = safe_get(post, "postTitle", "")
    content = safe_get(post, "postContent", "")
    ts_ms = int(safe_get(post, "createTimestamp", 0))
    if not is_within_days(ts_ms, day_count=1):
        return False
    return Config.PREVIEW_TITLE_KEY in title and Config.PREVIEW_CONTENT_KEY in content


def match_code_post(post: Dict[str, Any]) -> bool:
    """匹配兑换码回顾影像帖"""
    title = safe_get(post, "postTitle", "")
    ts_ms = int(safe_get(post, "createTimestamp", 0))
    if not all(key in title for key in Config.CODE_TITLE_KEYS):
        return False
    return is_within_days(ts_ms, day_count=2)


def match_star_activity_post(post: Dict[str, Any], full_detail: Dict[str, Any]) -> bool:
    """匹配可领取星声活动帖（需完整帖子详情）"""
    title = safe_get(post, "postTitle", "")
    ts_ms = int(safe_get(post, "createTimestamp", 0))
    if not is_within_days(ts_ms, day_count=1) or Config.ACTIVITY_TITLE_KEY not in title:
        return False
    post_detail = safe_get(full_detail, "postDetail", {})
    h5_content = safe_get(post_detail, "postH5Content", "")
    return Config.ACTIVITY_CONTENT_KEY in h5_content


# ====================== 【业务数据提取层】从评论解析兑换码文本 ======================
def fetch_code_comment_text(api_client: KuroApiClient, post_id: str) -> str:
    """遍历所有评论分组，提取包含兑换码提示的文案，增加类型容错"""
    comment_root = api_client.get_post_comments(post_id)
    match_text_set: Set[str] = set()

    for group_key in Config.COMMENT_GROUPS:
        comment_list = safe_get(comment_root, group_key, [])
        if not isinstance(comment_list, list):
            continue
        for comment in comment_list:
            content_items = safe_get(comment, "commentContent", [])
            if not isinstance(content_items, list):
                continue
            for item in content_items:
                text = safe_get(item, "content", "")
                if Config.CODE_COMMENT_TIP in text:
                    match_text_set.add(text.strip())

    if not match_text_set:
        return ""
    # 返回最短完整文案
    return sorted(match_text_set, key=lambda s: len(s))[0]


# ====================== 【消息模板层】分离消息渲染，方便自定义样式 ======================
class MessageBuilder:
    @staticmethod
    def build_preview(post: Dict[str, Any]) -> str:
        pid = safe_get(post, "postId", "")
        title = escape_md_var(safe_get(post, "postTitle", ""))
        content = escape_md_var(safe_get(post, "postContent", ""))
        ts_ms = int(safe_get(post, "createTimestamp", 0))
        create_time = format_ms(ts_ms)
        link = Config.POST_LINK_TPL.format(post_id=pid)

        lines = [
            "## 📢 鸣潮版本前瞻预告",
            f"**时间**：{create_time}",
            f"**标题**：{title}",
            f"**摘要**：{content}",
            f"**链接**：{link}"
        ]
        return "\n\r".join(lines)

    @staticmethod
    def build_code(post: Dict[str, Any], code_raw_text: str) -> str:
        pid = safe_get(post, "postId", "")
        title = escape_md_var(safe_get(post, "postTitle", ""))
        content = escape_md_var(safe_get(post, "postContent", ""))
        ts_ms = int(safe_get(post, "createTimestamp", 0))
        create_time = format_ms(ts_ms)
        link = Config.POST_LINK_TPL.format(post_id=pid)
        code_list = extract_codes_from_text(code_raw_text)
        code_text_esc = escape_md_var(code_raw_text)

        lines = [
            f"**时间**：{create_time}",
            f"**标题**：{title}",
            f"**摘要**：{content}",
            f"**链接**：{link}"
        ]

        if code_raw_text:
            lines.insert(0, "## 🎁 鸣潮前瞻兑换码更新")
            lines.extend(["", f"**兑换码原文**：{code_text_esc}"])
        else:
            lines.insert(0, "## 🎁 鸣潮前瞻兑换码")

        if code_list:
            lines.append("\n### 兑换码列表")
            lines.extend([f"- `{escape_md_var(code)}`" for code in code_list])
        return "\n\r".join(lines)

    @staticmethod
    def build_star_activity(post: Dict[str, Any]) -> str:
        pid = safe_get(post, "postId", "")
        title = escape_md_var(safe_get(post, "postTitle", ""))
        content = escape_md_var(safe_get(post, "postContent", ""))
        ts_ms = int(safe_get(post, "createTimestamp", 0))
        create_time = format_ms(ts_ms)
        link = Config.POST_LINK_TPL.format(post_id=pid)

        lines = [
            "## ⭐ 鸣潮星声福利活动",
            f"**时间**：{create_time}",
            f"**标题**：{title}",
            f"**摘要**：{content}",
            f"**链接**：{link}"
        ]
        return "\n\r".join(lines)


# ====================== 【主业务流程层】单一入口，缓存去重请求 ======================
def scan_official_posts():
    start_ts = time.perf_counter()
    log.info("========== 启动库街区鸣潮官方帖子扫描任务 ==========")

    # 初始化统一API会话
    api_client = KuroApiClient()
    root_data = api_client.get_user_posts(Config.KURO_OFFICIAL_UID)
    post_list = safe_get(root_data, "postList", [])

    if not isinstance(post_list, list) or len(post_list) == 0:
        log.info("未获取任何官方帖子，扫描结束")
        return

    # 缓存：避免同一帖子重复拉取详情/评论
    post_detail_cache: Dict[str, Dict[str, Any]] = {}
    stat = {"preview": 0, "code": 0, "activity": 0}

    for post in post_list:
        post_id = safe_get(post, "postId", "")
        if not post_id:
            continue

        # 1. 前瞻帖子优先处理（无需拉详情）
        if match_preview_post(post):
            stat["preview"] += 1
            log_post_detail(post, "前瞻预告")
            md = MessageBuilder.build_preview(post)
            push_wecom_markdown(md)
            continue

        # 2. 兑换码帖子（无需拉详情，仅需评论）
        if match_code_post(post):
            stat["code"] += 1
            log_post_detail(post, "兑换码福利")
            code_text = fetch_code_comment_text(api_client, post_id)
            log.info(f"解析兑换码原文：{truncate_log_text(code_text)}")
            md = MessageBuilder.build_code(post, code_text)
            push_wecom_markdown(md)
            continue

        # 3. 星声活动帖（需要拉完整详情，缓存复用）
        if Config.ACTIVITY_TITLE_KEY in safe_get(post, "postTitle", ""):
            if post_id not in post_detail_cache:
                post_detail_cache[post_id] = api_client.get_post_detail(post_id)
            full_detail = post_detail_cache[post_id]
            if match_star_activity_post(post, full_detail):
                stat["activity"] += 1
                log_post_detail(post, "星声活动")
                md = MessageBuilder.build_star_activity(post)
                push_wecom_markdown(md)
        continue

    # 清空缓存释放内存
    post_detail_cache.clear()

    # 扫描统计输出
    cost_sec = round(time.perf_counter() - start_ts, 2)
    log.info("========== 扫描任务完成 ==========")
    log.info(f"前瞻推送: {stat['preview']} | 兑换码推送: {stat['code']} | 星声活动推送: {stat['activity']}")
    if sum(stat.values()) == 0:
        log.info("本轮未匹配任何需要推送的帖子")

    log.info(f"任务总耗时: {cost_sec}s")


# ====================== 程序入口 ======================
if __name__ == "__main__":
    try:
        scan_official_posts()
    except KuroApiError as e:
        log.error(f"库街区接口执行失败：{str(e)}")
    except WecomPushError as e:
        log.error(f"企业微信推送流程异常：{str(e)}")
    except BaseMonitorError as e:
        log.error(f"监控任务通用异常：{str(e)}")
    except Exception as e:
        log.error("监控任务未知全局异常", exc_info=True)
