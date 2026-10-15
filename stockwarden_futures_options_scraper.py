from __future__ import annotations

import argparse
import html as html_module
import json
import os
import re
import ssl
import threading
import webbrowser
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape as htmlescape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_URL = "https://storage.googleapis.com/stockwarden-prod-public/api/boards.json"
TAIFEX_LIST_URL = "https://www.taifex.com.tw/cht/2/stockLists"
TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_T86_SITC_URL = "https://www.twse.com.tw/rwd/zh/fund/T86SITC"
TWSE_MI_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_AFTER_TRADING_OTC_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"
TPEX_INSTI_DAILY_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
TPEX_INSTI_DAILY_JSON_URL = "https://www.tpex.org.tw/www/storage/zh-tw/afterTrading/dailyInstBuySell_main.json"

OUTPUT_DIR = Path("output")
HTML_PATH = OUTPUT_DIR / "stockwarden_futures_or_options_stocks.html"
NOTIFICATION_CONFIG_PATH = Path("notification_settings.json")
TAIFEX_CACHE_PATH = Path("taifex_list_cache.json")

DISPOSITION_FIELDNAMES = ["股票代號", "股票名稱", "分盤時間", "風險天數分析", "處置條件訊息"]
INSTITUTIONAL_FIELDNAMES = [
    "股票代號",
    "股票名稱",
    "外陸資買賣超(股,不含自營)",
    "投信買賣超(股)",
    "成交股數(股)",
    "開盤價",
    "最高價",
    "最低價",
    "收盤價",
    "前日收盤價",
    "開盤(%)",
    "漲跌幅(%)",
    "最高幅(%)",
    "最低幅(%)",
    "外陸資占比(%)",
    "投信占比(%)",
]

GROUPS = [
    ("rod_list", "風險股"),
    ("disposition_list", "處置中"),
    ("exit_list", "即將出關"),
    ("weps_list", "自結發布"),
]
GROUP_COLORS = {
    "風險股": ("#f59e0b", "rgba(245, 158, 11, 0.16)", "#fef3c7"),
    "處置中": ("#ef4444", "rgba(239, 68, 68, 0.16)", "#fee2e2"),
    "即將出關": ("#22c55e", "rgba(34, 197, 94, 0.16)", "#dcfce7"),
    "自結發布": ("#38bdf8", "rgba(56, 189, 248, 0.16)", "#e0f2fe"),
}
VIEW_COLORS = {
    "綜合": ("#a855f7", "rgba(168, 85, 247, 0.16)", "#f3e8ff"),
    **GROUP_COLORS,
}
INSTITUTIONAL_VIEW_COLORS = {
    "綜合": ("#a855f7", "rgba(168, 85, 247, 0.16)", "#f3e8ff"),
    "長線多": ("#ef4444", "rgba(239, 68, 68, 0.16)", "#fee2e2"),
    "長線空": ("#22c55e", "rgba(34, 197, 94, 0.16)", "#dcfce7"),
    "短線翻多": ("#f59e0b", "rgba(245, 158, 11, 0.16)", "#fef3c7"),
    "短線翻空": ("#38bdf8", "rgba(56, 189, 248, 0.16)", "#e0f2fe"),
}
def _pct_is_negative(v: Any) -> bool:
    return isinstance(v, (int, float)) and v < 0


def _pct_is_positive(v: Any) -> bool:
    return isinstance(v, (int, float)) and v > 0


def _stock_code_key(r: dict[str, Any]) -> str:
    return str((r or {}).get("股票代號", "")).strip().zfill(8)


INSTITUTIONAL_VIEWS: tuple[tuple[str, Callable[[dict[str, Any]], bool]], ...] = (
    ("綜合", lambda _r: True),
    ("長線多", lambda r: (
        isinstance(r.get("漲跌幅(%)"), (int, float)) and r.get("漲跌幅(%)") > 0
        and (
            (isinstance(r.get("外陸資占比(%)"), (int, float)) and r.get("外陸資占比(%)") > +8)
            or (isinstance(r.get("投信占比(%)"), (int, float)) and r.get("投信占比(%)") > +8)
        )
        # 使用者補充：外陸資占比 或 投信占比「只要其中一個為 - 就不要」
        and not _pct_is_negative(r.get("外陸資占比(%)"))
        and not _pct_is_negative(r.get("投信占比(%)"))
    )),
    ("長線空", lambda r: (
        isinstance(r.get("漲跌幅(%)"), (int, float)) and r.get("漲跌幅(%)") < 0
        and (
            (isinstance(r.get("外陸資占比(%)"), (int, float)) and r.get("外陸資占比(%)") < -8)
            or (isinstance(r.get("投信占比(%)"), (int, float)) and r.get("投信占比(%)") < -8)
        )
        # 使用者補充：外陸資占比 或 投信占比「只要其中一個為 + 就不要」
        and not _pct_is_positive(r.get("外陸資占比(%)"))
        and not _pct_is_positive(r.get("投信占比(%)"))
    )),
    ("短線翻多", lambda r: (
        isinstance(r.get("漲跌幅(%)"), (int, float)) and r.get("漲跌幅(%)") < -4.9
        and isinstance(r.get("外陸資占比(%)"), (int, float)) and r.get("外陸資占比(%)") >= +8
    )),
    ("短線翻空", lambda r: (
        isinstance(r.get("漲跌幅(%)"), (int, float)) and r.get("漲跌幅(%)") > +8
        and isinstance(r.get("外陸資占比(%)"), (int, float)) and r.get("外陸資占比(%)") <= -8
    )),
)
INSTITUTIONAL_VIEW_DESCRIPTIONS: dict[str, str] = {
    "綜合": "全部期貨 / 選擇權相關股票",
    "長線多": "漲幅 > 0 且 (外陸>8% 或 投信>8%)，且外陸/投信皆不為負",
    "長線空": "跌幅 > 0 且 (外陸<-8% 或 投信<-8%)，且外陸/投信皆不為正",
    "短線翻多": "跌幅 > 4.9% 且 外陸≥ +8%（大跌外資大買）",
    "短線翻空": "漲幅 > 8% 且 外陸≤ -8%（大漲外資大砍）",
}
MAIN_TAB_COLORS = {
    "處置股": ("#f59e0b", "rgba(245, 158, 11, 0.18)", "#fef3c7"),
    "外資操作股": ("#3b82f6", "rgba(59, 130, 246, 0.18)", "#dbeafe"),
}

INSTITUTIONAL_TOP_N = 40

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
}

_UNVERIFIED_SSL_CTX = ssl._create_unverified_context()


@dataclass
class SubFilterSpec:
    key: str
    label: str
    default_active: bool = False
    filter_fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None


@dataclass
class PageSpec:
    key: str
    label: str
    palette: dict[str, tuple[str, str, str]]
    tab_order: int = 0
    default_active: bool = False
    fetch_fn: Callable[[], Any] | None = None
    build_rows_fn: Callable[[Any], list[dict[str, Any]]] | None = None
    sub_filter_specs: list[SubFilterSpec] = field(default_factory=list)
    cli_columns: list[str] = field(default_factory=list)
    html_columns: list[str] = field(default_factory=list)
    cli_format_fn: Callable[[dict[str, Any]], dict[str, str]] | None = None
    html_render_row_fn: Callable[[dict[str, Any]], str] | None = None
    html_render_table_view_fn: Callable[[str, list[dict[str, Any]], bool], str] | None = None
    html_render_filter_bar_fn: Callable[[list[dict[str, Any]]], str] | None = None
    fetch_errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _get_url(url: str, params: dict[str, str] | None = None, timeout: int = 75, retries: int = 3) -> tuple[int, str, dict[str, str]]:
    import time as _time
    full_url = url if not params else f"{url}?{urlencode(params)}"
    last_exc: str = ""
    for attempt in range(retries):
        req = Request(full_url, headers={**UA_HEADERS, "Accept": "*/*", "Connection": "close"})
        try:
            with urlopen(req, timeout=timeout, context=_UNVERIFIED_SSL_CTX) as resp:
                body_bytes = resp.read()
                body = body_bytes.decode("utf-8", errors="ignore")
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, body, headers
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            # 4xx/5xx 通常再試也沒用，但 503/504/429 仍重試
            if exc.code in (503, 504, 429) and attempt + 1 < retries:
                last_exc = f"HTTP {exc.code}"
                _time.sleep(0.4 * (attempt + 1))
                continue
            return exc.code, body, {}
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_exc = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            # http.client.IncompleteRead / BadStatusLine 等非上述例外
            last_exc = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < retries:
            _time.sleep(0.4 * (attempt + 1))
    return 0, last_exc or "", {}


def _post_form(url: str, data: dict[str, str], timeout: int = 75, retries: int = 3) -> tuple[int, str, dict[str, str]]:
    import time as _time
    payload = urlencode(data).encode("utf-8")
    last_exc: str = ""
    for attempt in range(retries):
        req = Request(
            url,
            data=payload,
            headers={
                **UA_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Connection": "close",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout, context=_UNVERIFIED_SSL_CTX) as resp:
                body_bytes = resp.read()
                body = body_bytes.decode("utf-8", errors="ignore")
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, body, headers
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            if exc.code in (503, 504, 429) and attempt + 1 < retries:
                last_exc = f"HTTP {exc.code}"
                _time.sleep(0.4 * (attempt + 1))
                continue
            return exc.code, body, {}
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_exc = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            last_exc = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < retries:
            _time.sleep(0.4 * (attempt + 1))
    return 0, last_exc or "", {}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def load_notification_config() -> dict[str, Any]:
    if NOTIFICATION_CONFIG_PATH.exists():
        return json.loads(NOTIFICATION_CONFIG_PATH.read_text(encoding="utf-8"))

    return {
        "telegram": {
            "enabled": bool(os.getenv("STOCKWARDEN_TELEGRAM_BOT_TOKEN") and os.getenv("STOCKWARDEN_TELEGRAM_CHAT_ID")),
            "bot_token": os.getenv("STOCKWARDEN_TELEGRAM_BOT_TOKEN", ""),
            "chat_id": os.getenv("STOCKWARDEN_TELEGRAM_CHAT_ID", ""),
        },
        "line": {
            "enabled": bool(os.getenv("STOCKWARDEN_LINE_CHANNEL_ACCESS_TOKEN") and os.getenv("STOCKWARDEN_LINE_TO")),
            "channel_access_token": os.getenv("STOCKWARDEN_LINE_CHANNEL_ACCESS_TOKEN", ""),
            "to": os.getenv("STOCKWARDEN_LINE_TO", ""),
        },
    }


def roc_date_to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = str(int(value))
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{7,8}", value):
            # 連續數字：7 碼 = 民國年3+月2+日2；8 碼 = 西元年4+月2+日2
            s = value
            if len(s) == 8:
                return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
            return date(int(s[0:3]) + 1911, int(s[3:5]), int(s[5:7]))
        roc_year, month, day = map(int, value.split("/"))
        if roc_year < 1911:
            roc_year += 1911
        return date(roc_year, month, day)
    except (TypeError, ValueError):
        return None


def _parse_int_from_str(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().replace(",", "").replace("--", "")
    if s in ("", "-", "NA", "N/A"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _parse_float_from_str(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("--", "")
    if s in ("", "-", "NA", "N/A", "---"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def format_signed_int(value: Any) -> str:
    if isinstance(value, int):
        return f"+{value:,}" if value >= 0 else f"{value:,}"
    if isinstance(value, float):
        iv = int(value)
        return f"+{iv:,}" if iv >= 0 else f"{iv:,}"
    return "-"


def format_signed_int_or_dash_white(value: Any) -> str:
    """買賣超為 0 或 None 回傳 '-'（之後透過 class/inline 設白色）；否則一般 signed int 格式"""
    if isinstance(value, int) and value == 0:
        return "-"
    if isinstance(value, float) and int(value) == 0:
        return "-"
    if not isinstance(value, (int, float)):
        return "-"
    return format_signed_int(value)


def format_int(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{int(value):,}"
    return "-"


def format_price(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    return "-"


def format_percent(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:+.2f}"
    return "-"


def format_percent_zero_plain(value: Any) -> tuple[str, bool]:
    """回傳 (文字, 是否要顯示白色無 +/-)。0 或 None → '0' or '-' (None 仍 '-')，占比 % 0 要白色不加號；其餘 +/- 正常色。

    - 用在：外陸資占比(%) / 投信占比(%) → 0 → "0" 白；None/missing → "-" 白
    - 一般 % 欄(漲跌幅/最高開/最低開)：0 仍然正常 "+0.00" 色，不使用本 helper
    """
    if isinstance(value, (int, float)) and value == 0:
        return "0", True
    if isinstance(value, (int, float)):
        return f"{value:+.2f}", False
    return "-", True


def get_filter_button_style(view_name: str, palette: dict[str, tuple[str, str, str]]) -> str:
    border_color, background_color, text_color = palette.get(
        view_name,
        ("#94a3b8", "rgba(148, 163, 184, 0.16)", "#e2e8f0"),
    )
    hover_bg = background_color.replace("0.16", "0.26").replace("0.18", "0.28")
    shadow_bg = background_color.replace("0.16", "0.22").replace("0.18", "0.24")
    subtle_text = text_color if view_name in ("綜合", "全部") else "#cbd5e1"
    return (
        f"--button-border:{border_color};"
        f"--button-border-strong:{border_color};"
        f"--button-bg:{background_color};"
        f"--button-bg-strong:{hover_bg};"
        f"--button-text:{text_color};"
        f"--button-subtle:{subtle_text};"
        f"--button-shadow:{shadow_bg};"
    )


# ---------------------------------------------------------------------------
# Disposition page (StockWarden boards.json) — 保留原有行為
# ---------------------------------------------------------------------------


def get_session_time_text(item: dict[str, Any]) -> str:
    disposition = item.get("aj") or {}
    calc_info = item.get("o") or {}
    session_minutes = None
    av = item.get("av") or {}
    cc_list = av.get("cc") or []
    if isinstance(cc_list, list):
        for cc in cc_list:
            cc_text = str(cc or "")
            m = re.search(r"處置期間\s*(\d{2}/\d{2})\s*[-~到至]\s*(\d{2}/\d{2})", cc_text)
            if m:
                continue
            m2 = re.search(r"約每\s*(\d+)\s*分鐘", cc_text)
            if m2:
                try:
                    session_minutes = int(m2.group(1))
                except ValueError:
                    session_minutes = None
                break
    if not (isinstance(session_minutes, int) and session_minutes > 0):
        v_aj_ah = disposition.get("ah")
        if isinstance(v_aj_ah, int) and v_aj_ah > 0:
            session_minutes = v_aj_ah
    if not (isinstance(session_minutes, int) and session_minutes > 0):
        v1 = calc_info.get("ak")
        if isinstance(v1, int) and v1 > 0:
            session_minutes = v1
    if not (isinstance(session_minutes, int) and session_minutes > 0):
        v2 = disposition.get("bb")
        if isinstance(v2, int) and v2 > 0:
            session_minutes = v2
    if isinstance(session_minutes, int) and session_minutes > 0:
        return f"{session_minutes}分盤"
    return "-"


def get_risk_analysis_text(item: dict[str, Any]) -> str:
    calc_info = item.get("o") or {}
    disposition = item.get("aj") or {}
    av = item.get("av") or {}
    risk_days: str | None = None
    v1 = calc_info.get("ak")
    if isinstance(v1, int) and v1 > 0:
        risk_days = str(v1)
    if not risk_days:
        v2 = disposition.get("ah")
        if isinstance(v2, int) and v2 > 0:
            risk_days = str(v2)
    if not risk_days:
        v3 = calc_info.get("bc")
        if isinstance(v3, int) and v3 > 0:
            risk_days = str(v3)
    if not risk_days:
        v4 = disposition.get("bb")
        if isinstance(v4, int) and v4 > 0:
            risk_days = str(v4)
    if not risk_days:
        v5 = av.get("bb")
        if isinstance(v5, int) and v5 > 0:
            risk_days = str(v5)
    if not risk_days:
        return "-"

    start_date = roc_date_to_date(disposition.get("bz"))
    end_date = roc_date_to_date(disposition.get("am"))
    today = datetime.now().date()

    if start_date and end_date and start_date <= today <= end_date:
        return f"最快 {risk_days} 日後再次處置"
    if end_date and end_date < today:
        return f"最快 {risk_days} 日後再次處置"
    return f"最快 {risk_days} 日後進入處置"


def get_condition_message(item: dict[str, Any]) -> str:
    disposition = item.get("aj") or {}
    av = item.get("av") or {}
    ai_text = str(disposition.get("ai") or "").strip()
    bx = av.get("bx") or {}
    bx_reason = str((bx if isinstance(bx, dict) else {}).get("bu") or "").strip()
    reason = str(disposition.get("bu") or "").strip()
    parts: list[str] = []
    if ai_text and ai_text not in ("None",):
        parts.append(ai_text)
    if reason and reason not in ("None",):
        parts.append(f"原因：{reason}")
    if bx_reason and bx_reason not in ("None",):
        parts.append(f"附註：{bx_reason}")
    extend_flag = (item.get("o") or {}).get("ar")
    if extend_flag is True:
        parts.append("延長處置")
    return "\n\n".join(p for p in parts if p) if parts else "-"


def fetch_board_data() -> dict[str, Any]:
    status, body, _ = _get_url(API_URL)
    if status != 200 or not body:
        raise RuntimeError(f"抓取 StockWarden boards.json 失敗: HTTP {status} / {body[:200]}")
    return json.loads(body)


def build_board_key_maps(payload: dict[str, Any]) -> dict[str, str]:
    """
    boards.json 改版會把 2 字母 key -> value 的 schema 換掉 (aq/bm 變回 ah/bd 之類)。
    這裡用 payload['schema'] 反查表動態建立:
      K_HAS_FUTURES / K_HAS_OPTIONS / K_CODE / K_NAME
      K_DISPOSITION_OBJ (disposition 子物件)
      K_CALC_INFO_OBJ (calculation_info 子物件)
      K_SUMMARY_OBJ (av 摘要/行情 子物件)
    """
    schema = payload.get("schema") or {}
    if not isinstance(schema, dict):
        return {}

    rev: dict[str, str] = {str(v): k for k, v in schema.items()}
    result: dict[str, str] = {}

    def _pick(preferred_labels: list[str]) -> str | None:
        for label in preferred_labels:
            if label in rev:
                return rev[label]
        # fallback: schema 裡 value 包含該關鍵字
        for k, v in schema.items():
            vs = str(v or "").lower()
            if any(lbl.lower() in vs for lbl in preferred_labels):
                return k
        return None

    result["K_HAS_FUTURES"] = _pick(["futures", "has_futures", "期貨"]) or "ah"
    result["K_HAS_OPTIONS"] = _pick(["option", "options", "has_option", "has_options", "選擇權"]) or "bd"
    result["K_CODE"] = _pick(["code", "stock_code", "symbol", "代號", "股票代號"]) or "m"
    result["K_NAME"] = _pick(["name", "company_name", "簡稱", "名稱", "股票名稱"]) or "ba"
    # disposition / calc_info / summary 子物件：先找 schema 裡有沒有 "disposition"/"calculation_info"/"summary"
    result["K_DISPOSITION_OBJ"] = _pick(["disposition", "disposition_info", "處置"]) or "aa"
    result["K_CALC_INFO_OBJ"] = _pick(["calculation_info", "calc_info", "risk_info", "計算資訊"]) or "f"
    result["K_SUMMARY_OBJ"] = _pick(["summary", "quote", "行情", "摘要", "remarks", "av"]) or "am"
    return result


# 這三個函數現在從 item 本身動態偵測 disposition/calc/summary 子物件 key (若沒傳 key_maps 就用舊版常數)
def _resolve_objects(item: dict[str, Any], key_maps: dict[str, str] | None = None) -> tuple[dict, dict, dict]:
    km = key_maps or {}
    disp_key = km.get("K_DISPOSITION_OBJ") or "aa"
    calc_key = km.get("K_CALC_INFO_OBJ") or "f"
    sum_key = km.get("K_SUMMARY_OBJ") or "am"
    disp = item.get(disp_key)
    calc = item.get(calc_key)
    summ = item.get(sum_key)
    if not isinstance(disp, dict):
        disp = {}
    if not isinstance(calc, dict):
        calc = {}
    if not isinstance(summ, dict):
        summ = {}
    return disp, calc, summ


def get_session_time_text(item: dict[str, Any], key_maps: dict[str, str] | None = None) -> str:
    disposition, calc_info, av = _resolve_objects(item, key_maps)
    session_minutes: int | None = None
    cc_list = av.get("cc") or []
    if isinstance(cc_list, list):
        for cc in cc_list:
            cc_text = str(cc or "")
            m = re.search(r"處置期間\s*(\d{2}/\d{2})\s*[-~到至]\s*(\d{2}/\d{2})", cc_text)
            if m:
                continue
            m2 = re.search(r"約每\s*(\d+)\s*分鐘", cc_text)
            if m2:
                try:
                    session_minutes = int(m2.group(1))
                except ValueError:
                    session_minutes = None
                break
    # 掃 disposition 裡的大文本 z / bl 等，找 "每 N 分鐘"
    if not (isinstance(session_minutes, int) and session_minutes > 0):
        for dict_obj in (disposition, calc_info, av):
            for v in (dict_obj or {}).values():
                if not isinstance(v, str):
                    continue
                m3 = re.search(r"每\s*(\d+)\s*分\s*(?:鐘)?\s*撮", v)
                if m3:
                    try:
                        session_minutes = int(m3.group(1))
                    except ValueError:
                        session_minutes = None
                    break
            if isinstance(session_minutes, int) and session_minutes > 0:
                break
    # f.at / aa.as：探測結果 disp/exit 都是 at=2 / as=2，猜測處置分鐘 (可能 2 分鐘)
    if not (isinstance(session_minutes, int) and session_minutes > 0):
        for dict_obj in (calc_info, disposition):
            for k in ("at", "as", "ah", "aj", "bb"):
                v = (dict_obj or {}).get(k)
                if isinstance(v, int) and 1 <= v <= 600:
                    # f.ab = 處置期間 5 天；f.at = 2 猜是分鐘；aa.as=2 也猜分鐘；
                    if k in ("at", "as"):
                        session_minutes = v
                        break
            if isinstance(session_minutes, int) and session_minutes > 0:
                break
    if isinstance(session_minutes, int) and session_minutes > 0:
        return f"{session_minutes}分盤"
    return "-"


def get_risk_analysis_text(item: dict[str, Any], key_maps: dict[str, str] | None = None) -> str:
    disposition, calc_info, av = _resolve_objects(item, key_maps)
    risk_days: str | None = None
    # f.ab = 5 (處置期間營業日) -> 這是處置天數，不是下次風險天數
    # 找合理「風險天數」：av.aj = 47, av.bh = 47, av.k = 47 -> 47 天合理 (風險觀察天數)
    for dict_obj in (av, calc_info, disposition):
        for k in ("aj", "bh", "k", "ah", "bb", "ab", "bc"):
            v = (dict_obj or {}).get(k)
            if isinstance(v, int) and 1 <= v <= 180:
                if k in ("aj", "bh", "k"):
                    risk_days = str(v)
                    break
        if risk_days:
            break
    if not risk_days:
        # f.bz = 5 、f.ab = 5 (處置期間天數) 當 fallback
        for dict_obj in (calc_info, disposition):
            for k in ("ab", "bz", "y"):
                v = (dict_obj or {}).get(k)
                if isinstance(v, int) and 1 <= v <= 60:
                    risk_days = str(v)
                    break
            if risk_days:
                break
    if not risk_days:
        return "-"

    start_date = roc_date_to_date(disposition.get("bq") or disposition.get("p"))
    end_date = roc_date_to_date(disposition.get("ad"))
    today = datetime.now().date()

    if start_date and end_date and start_date <= today <= end_date:
        return f"最快 {risk_days} 日後再次處置"
    if end_date and end_date < today:
        return f"最快 {risk_days} 日後再次處置"
    return f"最快 {risk_days} 日後進入處置"


def get_condition_message(item: dict[str, Any], key_maps: dict[str, str] | None = None) -> str:
    disposition, calc_info, av = _resolve_objects(item, key_maps)
    # aa.z = 最完整大文本 (處置原因/期間/措施 全部包在一起)
    z_text = str(disposition.get("z") or "").strip()
    bl_text = str(disposition.get("bl") or "").strip()
    ad_text = str(disposition.get("ad") or "").strip()
    bq_text = str(disposition.get("bq") or "").strip()
    p_text = str(disposition.get("p") or "").strip()
    ab = calc_info.get("ab")
    y = disposition.get("y")

    parts: list[str] = []
    if bl_text and bl_text not in ("None",):
        parts.append(bl_text)
    if isinstance(y, int) and y > 0:
        parts.append(f"處置類型：{y} 分盤")
    if isinstance(ab, int) and ab > 0:
        period = f"{ab} 個營業日"
        if p_text or bq_text or ad_text:
            start = bq_text or p_text or ""
            if start and ad_text:
                period = f"{start} 至 {ad_text}（{ab} 個營業日）"
            elif start:
                period = f"自 {start} 起 {ab} 個營業日"
            elif ad_text:
                period = f"至 {ad_text} 止，共 {ab} 個營業日"
        parts.append(f"處置期間：{period}")
    if z_text and z_text not in ("None",):
        parts.append(z_text)
    # fallback: 掃 dict 裡的長字串
    if not parts:
        extra_texts: list[str] = []
        for dict_obj in (disposition, av, calc_info):
            for v in (dict_obj or {}).values():
                if isinstance(v, str):
                    s = v.strip()
                    if 3 <= len(s) <= 4000:
                        extra_texts.append(s)
        if extra_texts:
            seen: set[str] = set()
            for s in extra_texts:
                if s not in seen:
                    parts.append(s)
                    seen.add(s)
    return "\n\n".join(p for p in parts if p) if parts else "-"


def extract_disposition_rows(payload: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    data = payload.get("data", {})
    key_maps = build_board_key_maps(payload)
    K_HF = key_maps.get("K_HAS_FUTURES", "ah")
    K_HO = key_maps.get("K_HAS_OPTIONS", "bd")
    K_CODE = key_maps.get("K_CODE", "m")
    K_NAME = key_maps.get("K_NAME", "ba")
    # 根據最新 boards 資料，ah/bd 可能全為 False，但 rod_list / disposition_list / exit_list / dt_list / weps_list
    # 本身就已經是「有被處置/注意/發布自結」的清單，所以 filter 應改為：
    #   ① 有 futures/options 標記 (bool)，或 ② 存在 taifex 期權清單，或 ③ 有處置子物件 aa 且有處置說明
    tf_code_set: set[str] = set()
    try:
        tf = fetch_taifex_list(force_refresh=False)
        for c in tf.get("codes", []) or []:
            code = (c.get("code") or "").strip()
            if code:
                tf_code_set.add(code)
    except Exception:
        tf_code_set = set()

    def _item_has_disposition(item: dict[str, Any]) -> bool:
        # 有處置子物件且裡面有處置期間/原因/處置說明 -> 必屬於處置股
        disp_obj = _resolve_objects(item, key_maps)[0]
        if not disp_obj:
            return False
        for v in disp_obj.values():
            if isinstance(v, str):
                s = v.strip()
                if 10 <= len(s) <= 4000 and any(w in s for w in ("處置", "注意", "分盤", "管制", "撮合", "營業日")):
                    return True
        # 有處置起始日 p / 處置迄日 ad / 處置原因文字 bl
        for date_key in ("p", "bq", "ad"):
            if isinstance(disp_obj.get(date_key), str) and "/" in disp_obj[date_key]:
                return True
        return False

    for list_name, group_name in GROUPS:
        for item in data.get(list_name, []):
            has_futures = bool(item.get(K_HF))
            has_options = bool(item.get(K_HO))
            stock_code = str(item.get(K_CODE, "")).strip()
            in_taifex = bool(stock_code) and stock_code in tf_code_set
            has_disp_obj = _item_has_disposition(item)
            if not (has_futures or has_options or in_taifex or has_disp_obj):
                continue
            rows.append(
                {
                    "股票代號": stock_code,
                    "股票名稱": str(item.get(K_NAME, "")).strip(),
                    "分盤時間": get_session_time_text(item, key_maps),
                    "風險天數分析": get_risk_analysis_text(item, key_maps),
                    "處置條件訊息": get_condition_message(item, key_maps),
                    "群組": group_name,
                }
            )
    return rows


def merge_rows_for_combined_view(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    group_order = {group_name: index for index, (_, group_name) in enumerate(GROUPS)}

    for row in rows:
        stock_code = row["股票代號"]
        current = merged.setdefault(
            stock_code,
            {
                "股票代號": stock_code,
                "股票名稱": row["股票名稱"],
                "分盤時間集合": [],
                "風險天數分析集合": [],
                "處置條件訊息集合": [],
                "群組列表": [],
            },
        )

        if row["群組"] not in current["群組列表"]:
            current["群組列表"].append(row["群組"])
        if row["分盤時間"] not in current["分盤時間集合"]:
            current["分盤時間集合"].append(row["分盤時間"])
        if row["風險天數分析"] not in current["風險天數分析集合"]:
            current["風險天數分析集合"].append(row["風險天數分析"])
        if row["處置條件訊息"] not in current["處置條件訊息集合"]:
            current["處置條件訊息集合"].append(row["處置條件訊息"])

    result: list[dict[str, Any]] = []
    for _, item in merged.items():
        groups = sorted(item["群組列表"], key=lambda name: group_order.get(name, 999))
        session_values = [v for v in item["分盤時間集合"] if v != "-"]
        risk_values = [v for v in item["風險天數分析集合"] if v != "-"]
        message_values = [v for v in item["處置條件訊息集合"] if v != "-"]
        result.append(
            {
                "股票代號": item["股票代號"],
                "股票名稱": item["股票名稱"],
                "分盤時間": " / ".join(session_values) if session_values else "-",
                "風險天數分析": " / ".join(risk_values) if risk_values else "-",
                "處置條件訊息": "\n\n".join(message_values) if message_values else "-",
                "群組列表": groups,
            }
        )
    result.sort(key=lambda r: r["股票代號"])
    return result


def render_disposition_filter_bar(rows: list[dict[str, str]], combined_rows: list[dict[str, Any]]) -> str:
    group_counter = Counter(row["群組"] for row in rows)
    filter_buttons: list[tuple[str, int, bool]] = [("綜合", len(combined_rows), True)]
    filter_buttons.extend(
        (group_name, group_counter.get(group_name, 0), False) for _, group_name in GROUPS
    )
    return "\n".join(
        (
            f'<button class="filter-button{" active" if is_active else ""}" '
            f'data-tab="disposition" data-view="{htmlescape(view_name)}" '
            f'style="{get_filter_button_style(view_name, VIEW_COLORS)}">'
            f'<span class="filter-name">{htmlescape(view_name)}</span>'
            f'<span class="filter-count">{count}</span>'
            f"</button>"
        )
        for view_name, count, is_active in filter_buttons
    )


def render_disposition_html_row(row: dict[str, Any]) -> str:
    stock_code = row["股票代號"]
    groups = row.get("群組列表") or [row.get("群組", "")]
    badges = []
    for g in groups:
        border, bg, text = GROUP_COLORS.get(g, ("#94a3b8", "rgba(148,163,184,0.12)", "#e2e8f0"))
        badges.append(
            f'<span class="group-badge" style="border-color:{border};background:{bg};color:{text};">'
            f"{htmlescape(g)}</span>"
        )
    sess_cls = "session-pill" if row["分盤時間"] != "-" else "session-pill none"
    return (
        "<tr>"
        f'<td class="code-cell"><span class="stock-code">{htmlescape(stock_code)}{"".join(badges)}</span></td>'
        f"<td>{htmlescape(row['股票名稱'])}</td>"
        f'<td><span class="{sess_cls}">{htmlescape(row["分盤時間"])}</span></td>'
        f'<td><span class="risk-text">{htmlescape(row["風險天數分析"])}</span></td>'
        f'<td class="message-cell">{htmlescape(row["處置條件訊息"])}</td>'
        "</tr>"
    )


def render_disposition_table_view(view_name: str, rows: list[dict[str, Any]], is_active: bool) -> str:
    if rows:
        body = "\n".join(render_disposition_html_row(r) for r in rows)
    else:
        body = '<tr><td colspan="5">這個群組目前沒有符合條件的股票。</td></tr>'
    return f"""
<div class="table-view disposition-view{' active' if is_active else ''}" data-tab="disposition" data-view="{htmlescape(view_name)}">
  <table>
    <thead><tr><th>股票代號</th><th>股票名稱</th><th>分盤時間</th><th>風險天數分析</th><th>處置條件訊息</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</div>
"""


def render_disposition_table_views(rows: list[dict[str, str]]) -> str:
    combined = merge_rows_for_combined_view(rows)
    sections: list[tuple[str, list[dict[str, Any]], bool]] = [("綜合", combined, True)]
    sections.extend(
        (gname, [r for r in rows if r["群組"] == gname], False) for _, gname in GROUPS
    )
    return "\n".join(render_disposition_table_view(n, v, a) for n, v, a in sections)


def disposition_page_format_cli(row: dict[str, Any]) -> dict[str, str]:
    return {h: str(row.get(h, "-")) for h in DISPOSITION_FIELDNAMES}


# ---------------------------------------------------------------------------
# Taifex 期/權股票清單 + 快取
# ---------------------------------------------------------------------------


def _strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value).strip()


def _has_mark(cell_text: str) -> bool:
    text = _strip_tags(cell_text)
    return "●" in text or text == "V" or text == "是" or "v" in text


TAIFEX_UPDATED_RE = re.compile(r"最新更新\(生效\)日期[:：]\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")


def fetch_taifex_list(force_refresh: bool = False) -> dict[str, Any]:
    """回傳 {updated_on: 'YYYY-MM-DD', generated_at: '...', codes: [{code,name,has_futures,has_options,contract_code}]}"""
    today = date.today()
    cache: dict[str, Any] | None = None
    if TAIFEX_CACHE_PATH.exists() and not force_refresh:
        try:
            cache = json.loads(TAIFEX_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            cache = None

    need_refetch = force_refresh or (cache is None)
    if not need_refetch and cache is not None:
        if today.day == 1:
            need_refetch = True
        else:
            try:
                updated_on = str(cache.get("updated_on", ""))
                parts = updated_on.split("-")
                if len(parts) == 3:
                    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
                    updated = date(y, m, d)
                    if (today - updated).days >= 60:
                        need_refetch = True
                else:
                    need_refetch = True
            except Exception:
                need_refetch = True

    page_html = ""
    new_updated = cache.get("updated_on") if cache else None
    if need_refetch:
        status, body, _ = _get_url(TAIFEX_LIST_URL, timeout=45)
        if status != 200 or not body:
            if cache is not None:
                return cache
            raise RuntimeError(f"抓取 Taifex 股票清單失敗: HTTP {status}")
        page_html = body
        m = TAIFEX_UPDATED_RE.search(body)
        if m:
            new_updated = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if cache is not None and new_updated and new_updated == cache.get("updated_on"):
            cache["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            TAIFEX_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            return cache

    if page_html:
        codes = _parse_taifex_html(page_html)
        result = {
            "updated_on": new_updated or today.strftime("%Y-%m-%d"),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "codes": codes,
        }
        TAIFEX_CACHE_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    if cache is None:
        raise RuntimeError("Taifex 快取不存在且無法抓取。")
    return cache


def _parse_taifex_html(html_text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    # 找到第二個 <table> (先前觀察結果)
    tables = re.findall(r"<table[\s\S]*?</table>", html_text, flags=re.IGNORECASE)
    if len(tables) < 2:
        # fallback: 抓最大的 table
        table_html = max(tables, key=len) if tables else ""
    else:
        table_html = tables[1]

    # 解析每一列 <tr>，抓 <td> 內容
    rows_re = re.findall(r"<tr[\s\S]*?</tr>", table_html, flags=re.IGNORECASE)
    for tr in rows_re:
        tds = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, flags=re.IGNORECASE)
        if len(tds) < 6:
            continue
        cells = [_strip_tags(td) for td in tds]
        if cells[2] == "證券代號" or not cells[2].isdigit():
            continue
        contract_code = cells[0]  # CA/CB/CC...
        target_name = cells[1]
        code = cells[2]
        short_name = cells[3]
        has_futures = _has_mark(cells[4])
        has_options = _has_mark(cells[5])
        if not (has_futures or has_options):
            continue
        results.append(
            {
                "code": code,
                "name": short_name or target_name,
                "contract_code": contract_code,
                "has_futures": has_futures,
                "has_options": has_options,
            }
        )
    return results


# ---------------------------------------------------------------------------
# TWSE T86 三大法人
# ---------------------------------------------------------------------------


def fetch_twse_t86(target_date: date) -> tuple[dict[str, dict[str, Any]], str]:
    """回傳 (key=證券代號 的 dict, 資料日期文字)。
    改使用 TWSE T86 HTML 版 (selectType=ALLBUT0999, response=html)：
    - ALLBUT0999 = 排除權證牛熊證(0999)類，僅保留普通股/ETF/TDR 等實際有三大法人進出的證券
    - HTML 表格第 0 列 = 日期 big title；第 1 列 = 表頭 (19 欄)；第 2 列起 = 1338 筆有效 data rows
    - 欄位 index 對應與舊 JSON 完全一致：[0]代號 [1]名稱 [4]外陸資不含自營買賣超 [10]投信買賣超
    回傳值 downstream 相容：每條 foreign_net / trust_net = int (含 0: 有爬到真 0) 或 None (parse 失敗，罕見)
    若代號不在 result dict：表示該股當日不在 T86 ALLBUT0999 清單 (ex. 上櫃股票、或屬 0999 類 權證 ETF 非股票類等)
    """
    params = {
        "date": target_date.strftime("%Y%m%d"),
        "selectType": "ALLBUT0999",
        "response": "html",
    }
    status, body, _ = _get_url(TWSE_T86_URL, params=params, timeout=60)
    if status != 200 or not body:
        return {}, ""
    # 從 HTML 抓所有 <tr>
    tr_list = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", body, flags=re.IGNORECASE)
    if len(tr_list) < 3:
        # 至少要：日期列 + 表頭列 + 1 筆 data
        return {}, ""
    # 日期列 (tr[0]) 當作 data_date 來源備用；ex. <tr><th colspan=19>115年09月30日 三大法人買賣超日報</th></tr>
    data_date_raw = ""
    m_date = re.search(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", tr_list[0])
    if m_date:
        roc_year = int(m_date.group(1))
        ad_year = roc_year + 1911 if roc_year < 1911 else roc_year
        data_date_raw = f"{ad_year:04d}{int(m_date.group(2)):02d}{int(m_date.group(3)):02d}"
    result: dict[str, dict[str, Any]] = {}
    for tr_html in tr_list[2:]:  # 跳過 tr[0] 日期列、tr[1] 表頭列
        td_cells = re.findall(r"<td[^>]*>([\s\S]*?)</td>", tr_html, flags=re.IGNORECASE)
        if len(td_cells) < 12:
            continue
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in td_cells]
        code = cells[0]
        if not re.fullmatch(r"\d{4}[A-Z0-9]*", code):
            continue
        foreign_net = _parse_int_from_str(cells[4].replace(",", "").replace(":", "")) if len(cells) >= 5 else None
        trust_net = _parse_int_from_str(cells[10].replace(",", "").replace(":", "")) if len(cells) >= 11 else None
        if foreign_net is None and trust_net is None:
            continue
        name = cells[1] if len(cells) >= 2 else ""
        result[code] = {
            "foreign_net": foreign_net,
            "trust_net": trust_net,
            "name": name,
        }
    data_date = data_date_raw or target_date.strftime("%Y%m%d")
    return result, data_date


# ---------------------------------------------------------------------------
# TPEX 上櫃 三大法人日交易資訊
# ---------------------------------------------------------------------------

def fetch_tpex_daily_insti(target_date: date) -> tuple[dict[str, dict[str, Any]], str]:
    """
    雙層來源：先嘗試 TPEX dailyInstBuySell_main.json（新 JSON 後台，比較完整），
    失敗再 fallback 舊 HTML 版 (type=Daily, sect=AL, response=html)。
    欄位 index 定義（JSON HTML 都一樣，for backward compat）：
    - 外陸資不含自營買賣超、投信買賣超
    """
    params = {
        "type": "Daily",
        "sect": "AL",
        "date": target_date.strftime("%Y/%m/%d"),
        "id": "",
        "response": "html",
    }
    result_raw: dict[str, dict[str, Any]] = {}
    data_date_raw = ""

    # 先 JSON 版 (dailyInstBuySell_main.json?l=zh-tw&se=AL&d=YYYY/MM/DD&t=Daily)
    try:
        json_params = {
            "l": "zh-tw",
            "se": "AL",
            "d": target_date.strftime("%Y/%m/%d"),
            "t": "Daily",
        }
        status_j, body_j, _ = _get_url(TPEX_INSTI_DAILY_JSON_URL, params=json_params, timeout=60)
        if status_j == 200 and body_j:
            try:
                payload_j = json.loads(body_j)
                # 常見 schema: [{date:"20260930", data:[{c:"代號",n:"名稱", f:外陸, t:投信, ...}]}]
                def _walk(node):
                    out: list[Any] = []
                    if isinstance(node, dict):
                        if "d" in node and "t" in node and isinstance(node.get("data"), list):
                            nonlocal data_date_raw
                            if not data_date_raw:
                                dv = str(node.get("d", "")).replace("/", "").replace("-", "")
                                if len(dv) == 8 and dv.isdigit():
                                    data_date_raw = dv
                            out.extend(node["data"])
                        for v in node.values():
                            out.extend(_walk(v))
                    elif isinstance(node, list):
                        for x in node:
                            out.extend(_walk(x))
                    return out
                rows = _walk(payload_j)
                seen = set()
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    # 支援 新舊 key: (c,n,f,t) / (代號,名稱,外陸,投信) / (0,1,4,13) for list
                    if isinstance(r, list):
                        cells = [str(x).strip() if not isinstance(x,(int,float)) else x for x in r]
                        if len(cells) < 14:
                            continue
                        code = re.sub(r"<[^>]+>","", str(cells[0])).strip()
                        name = re.sub(r"<[^>]+>","", str(cells[1])).strip() if len(cells) > 1 else ""
                        foreign_net = _parse_int_from_str(str(cells[4]).replace(",","")) if len(cells) > 4 else None
                        trust_net = _parse_int_from_str(str(cells[13]).replace(",","")) if len(cells) > 13 else None
                    else:
                        code = str(r.get("c") or r.get("代號") or r.get("code") or "").strip()
                        name = str(r.get("n") or r.get("名稱") or r.get("name") or "").strip()
                        f_val = r.get("f") or r.get("外陸資買賣超") or r.get("外陸資不含自營買賣超") or r.get("foreign") or r.get(4)
                        t_val = r.get("t") or r.get("投信買賣超") or r.get("trust") or r.get(13)
                        foreign_net = _parse_int_from_str(str(f_val).replace(",","")) if f_val is not None else None
                        trust_net = _parse_int_from_str(str(t_val).replace(",","")) if t_val is not None else None
                    if not re.fullmatch(r"\d{4}[A-Z0-9]*", code):
                        continue
                    if foreign_net is None and trust_net is None:
                        continue
                    if code in seen:
                        continue
                    seen.add(code)
                    result_raw[code] = {"foreign_net": foreign_net, "trust_net": trust_net, "name": name}
            except Exception:
                result_raw = {}
    except Exception:
        result_raw = {}

    if not result_raw:
        status, body, _ = _get_url(TPEX_INSTI_DAILY_URL, params=params, timeout=60)
        if status == 200 and body:
            tr_list = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", body, flags=re.IGNORECASE)
            if len(tr_list) >= 4:
                m_date = re.search(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", tr_list[0])
                if m_date and not data_date_raw:
                    roc_year = int(m_date.group(1))
                    ad_year = roc_year + 1911 if roc_year < 1911 else roc_year
                    data_date_raw = f"{ad_year:04d}{int(m_date.group(2)):02d}{int(m_date.group(3)):02d}"
                for tr_html in tr_list[3:]:
                    td_cells = re.findall(r"<td[^>]*>([\s\S]*?)</td>", tr_html, flags=re.IGNORECASE)
                    if len(td_cells) < 14:
                        continue
                    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in td_cells]
                    code = cells[0]
                    if not re.fullmatch(r"\d{4}[A-Z0-9]*", code):
                        continue
                    if code in result_raw:
                        continue
                    foreign_net = _parse_int_from_str(cells[4].replace(",", "").replace(":", "")) if len(cells) >= 5 else None
                    trust_net = _parse_int_from_str(cells[13].replace(",", "").replace(":", "")) if len(cells) >= 14 else None
                    if foreign_net is None and trust_net is None:
                        continue
                    name = cells[1] if len(cells) >= 2 else ""
                    result_raw[code] = {
                        "foreign_net": foreign_net,
                        "trust_net": trust_net,
                        "name": name,
                    }

    data_date = data_date_raw or target_date.strftime("%Y%m%d")
    return result_raw, data_date


# ---------------------------------------------------------------------------
# TWSE T86SITC 特定投資人(不含指數股票型基金) — 舊 T86 有時會漏掉 KY / ETF 特殊類，補齊上市覆蓋率
# ---------------------------------------------------------------------------


def fetch_twse_t86sitc(target_date: date) -> tuple[dict[str, dict[str, Any]], str]:
    """
    TWSE T86SITC：排除 ETF 類、但 KY/一般股票類有時候反而有更多資料；
    當 T86 ALLBUT0999 沒抓到的上市標的，拿這份當第二層補齊（只填 missing）。
    """
    params = {
        "date": target_date.strftime("%Y%m%d"),
        "selectType": "ALL",
        "response": "html",
    }
    status, body, _ = _get_url(TWSE_T86_SITC_URL, params=params, timeout=60)
    if status != 200 or not body:
        return {}, ""
    tr_list = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", body, flags=re.IGNORECASE)
    if len(tr_list) < 3:
        return {}, ""
    data_date_raw = ""
    m_date = re.search(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", tr_list[0])
    if m_date:
        roc_year = int(m_date.group(1))
        ad_year = roc_year + 1911 if roc_year < 1911 else roc_year
        data_date_raw = f"{ad_year:04d}{int(m_date.group(2)):02d}{int(m_date.group(3)):02d}"
    result: dict[str, dict[str, Any]] = {}
    for tr_html in tr_list[2:]:
        td_cells = re.findall(r"<td[^>]*>([\s\S]*?)</td>", tr_html, flags=re.IGNORECASE)
        if len(td_cells) < 12:
            continue
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in td_cells]
        code = cells[0]
        if not re.fullmatch(r"\d{4}[A-Z0-9]*", code):
            continue
        foreign_net = _parse_int_from_str(cells[4].replace(",", "").replace(":", "")) if len(cells) >= 5 else None
        trust_net = _parse_int_from_str(cells[10].replace(",", "").replace(":", "")) if len(cells) >= 11 else None
        if foreign_net is None and trust_net is None:
            continue
        name = cells[1] if len(cells) >= 2 else ""
        result[code] = {"foreign_net": foreign_net, "trust_net": trust_net, "name": name}
    return result, data_date_raw or target_date.strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# TWSE MI_INDEX 上市每日收盤行情
# ---------------------------------------------------------------------------


def fetch_twse_mi_index(target_date: date) -> dict[str, dict[str, Any]]:
    """回傳 key=代號；value={volume,transactions,open,high,low,close,change,market}"""
    params = {
        "date": target_date.strftime("%Y%m%d"),
        "type": "ALLBUT0999",
        "response": "json",
    }
    status, body, _ = _get_url(TWSE_MI_INDEX_URL, params=params, timeout=60)
    if status != 200 or not body:
        return {}
    try:
        payload = json.loads(body)
    except Exception:
        return {}
    tables = payload.get("tables") or []
    # tables[8] = 每日收盤行情(全部)
    target_table = None
    for t in tables:
        title = str(t.get("title", ""))
        if "每日收盤行情" in title and ("全部" in title or "不含" in title or target_table is None):
            if target_table is None or "不含" not in title:
                target_table = t
    if target_table is None and len(tables) > 8:
        target_table = tables[8]
    if target_table is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    fields = target_table.get("fields") or []
    rows = target_table.get("data") or []
    for row in rows:
        if len(row) < 9:
            continue
        code = str(row[0]).strip()
        if not code or not re.fullmatch(r"\d{4,6}", code):
            continue
        volume = _parse_int_from_str(row[2])
        transactions = _parse_int_from_str(row[3])
        open_p = _parse_float_from_str(row[5])
        high_p = _parse_float_from_str(row[6])
        low_p = _parse_float_from_str(row[7])
        close_p = _parse_float_from_str(row[8])
        sign = ""
        change = None
        change_pct = None
        if len(row) >= 11:
            sign_raw = re.sub(r"<[^>]+>", "", str(row[9])).strip()
            if sign_raw in ("+", "-"):
                sign = sign_raw
            change_raw = _parse_float_from_str(row[10])
            if change_raw is not None:
                if sign == "-" and change_raw > 0:
                    change = -change_raw
                else:
                    change = change_raw
        # 前一日收盤價 = 當日收盤 - 前收漲跌金額（MI_INDEX change 定義即 close - prev_close）
        prev_close: float | None = None
        if isinstance(close_p, (int, float)) and isinstance(change, (int, float)):
            prev_close = round(float(close_p) - float(change), 2)
        # change_pct（使用者 2026-10-02 改版：跟前日收盤比，分母前收）不在這邊算，
        # 因為要跟最高幅/最低幅/開盤% 共用 prev_close 邏輯，移到 build_institutional_page_context 計算
        change_pct = None
        result[code] = {
            "volume": volume or 0,
            "transactions": transactions or 0,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "prev_close": prev_close,
            "change": change,
            "change_pct": change_pct,
            "market": "上市",
        }
    return result


# ---------------------------------------------------------------------------
# TPEX /www/zh-tw/afterTrading/otc 上櫃每日收盤行情
# ---------------------------------------------------------------------------


def fetch_tpex_mi_pricing(target_date: date) -> dict[str, dict[str, Any]]:
    date_str = f"{target_date.year:04d}/{target_date.month:02d}/{target_date.day:02d}"
    form = {
        "date": date_str,
        "type": "EW",  # 所有證券(不含權證、牛熊證)
        "id": "",
        "response": "json",
    }
    status, body, _ = _post_form(TPEX_AFTER_TRADING_OTC_URL, form, timeout=60)
    if status != 200 or not body:
        return {}
    try:
        payload = json.loads(body)
    except Exception:
        return {}
    tables = payload.get("tables") or []
    if not tables:
        return {}
    target = tables[0]
    fields = [re.sub(r"<[^>]+>", "", str(f)).strip() for f in target.get("fields") or []]
    # 建 index 對應
    idx = {name: i for i, name in enumerate(fields)}
    rows = target.get("data") or []
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if len(row) < max(idx.values()) + 1 if idx else len(row) < 9:
            continue
        # 代號、名稱、收盤、漲跌、開盤、最高、最低、成交股數、成交金額、成交筆數
        code = re.sub(r"<[^>]+>", "", str(row[idx.get("代號", 0)])).strip()
        close_s = row[idx.get("收盤 ", 2)] if "收盤 " in idx else row[2]
        change_s = row[idx.get("漲跌", 3)] if "漲跌" in idx else row[3]
        open_s = row[idx.get("開盤 ", 4)] if "開盤 " in idx else row[4]
        high_s = row[idx.get("最高 ", 5)] if "最高 " in idx else row[5]
        low_s = row[idx.get("最低 ", 6)] if "最低 " in idx else row[6]
        vol_s = row[idx.get("成交股數  ", 7)] if "成交股數  " in idx else row[7]
        txn_s = None
        for cand in (" 成交筆數 ", "成交筆數"):
            if cand in idx:
                txn_s = row[idx[cand]]
                break
        if txn_s is None and len(row) > 9:
            txn_s = row[9]
        close_p = _parse_float_from_str(close_s)
        change = _parse_float_from_str(change_s)
        open_p = _parse_float_from_str(open_s)
        high_p = _parse_float_from_str(high_s)
        low_p = _parse_float_from_str(low_s)
        vol = _parse_int_from_str(vol_s)
        txn = _parse_int_from_str(txn_s)
        if not code or not re.fullmatch(r"\d{4,6}", code):
            continue
        change_pct = None
        # 前一日收盤價 = 當日收盤 - 漲跌金額（TPEX OTC change 定義即 close - prev_close）
        prev_close: float | None = None
        if isinstance(close_p, (int, float)) and isinstance(change, (int, float)):
            prev_close = round(float(close_p) - float(change), 2)
        # change_pct（使用者 2026-10-02 改版：跟前日收盤比，分母前收）不在這邊算，
        # 移到 build_institutional_page_context 與 最高幅/最低幅/開盤% 共用 prev_close 邏輯
        result[code] = {
            "volume": vol or 0,
            "transactions": txn or 0,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "prev_close": prev_close,
            "change": change,
            "change_pct": change_pct,
            "market": "上櫃",
        }
    return result


# ---------------------------------------------------------------------------
# Institutional 頁面 JOIN 與顯示
# ---------------------------------------------------------------------------


def build_institutional_page_context(target_date: date | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    today = date.today()
    explicitly_given = target_date is not None and target_date != today
    trade_date_base = target_date or today
    trade_date, used_fallback = resolve_trade_date_with_fallback(
        trade_date_base, explicitly_given=explicitly_given
    )
    errors: list[str] = []
    print(
        f"[外資操作股] 選取資料日期: {trade_date.strftime('%Y/%m/%d')}"
        + (f" (自動回補，因 {trade_date_base.strftime('%Y/%m/%d')} 盤後資料尚未公告)" if used_fallback else "")
    )

    try:
        taifex = fetch_taifex_list()
    except Exception as exc:
        errors.append(f"Taifex 股票清單抓取失敗: {exc}")
        return [], errors
    taifex_updated = taifex.get("updated_on")
    codes_list = taifex.get("codes") or []
    taifex_by_code = {c["code"]: c for c in codes_list}

    try:
        t86_by_code, t86_date = fetch_twse_t86(trade_date)
        if not t86_by_code:
            errors.append("TWSE T86 今日尚無資料(通常 15:00 後更新)")
    except Exception as exc:
        t86_by_code, t86_date = {}, None
        errors.append(f"TWSE T86 抓取失敗: {exc}")
    try:
        t86sitc_by_code, t86sitc_date = fetch_twse_t86sitc(trade_date)
    except Exception as exc:
        t86sitc_by_code, t86sitc_date = {}, None
        errors.append(f"TWSE T86SITC 抓取失敗(非致命): {exc}")
    try:
        tpex_insti_by_code, tpex_insti_date = fetch_tpex_daily_insti(trade_date)
        if not tpex_insti_by_code:
            errors.append("TPEX 三大法人日交易 HTML 今日尚無資料(通常 15:30 後更新)")
    except Exception as exc:
        tpex_insti_by_code, tpex_insti_date = {}, None
        errors.append(f"TPEX 三大法人抓取失敗: {exc}")
    try:
        mi_twse = fetch_twse_mi_index(trade_date)
        if not mi_twse:
            errors.append("TWSE MI_INDEX 今日尚無行情資料")
    except Exception as exc:
        mi_twse = {}
        errors.append(f"TWSE MI_INDEX 抓取失敗: {exc}")
    try:
        mi_tpex = fetch_tpex_mi_pricing(trade_date)
        if not mi_tpex:
            errors.append("TPEX 今日尚無行情資料")
    except Exception as exc:
        mi_tpex = {}
        errors.append(f"TPEX 行情抓取失敗: {exc}")
    if used_fallback:
        errors.append(
            f"[自動回補日期] 今日 {trade_date_base.strftime('%Y/%m/%d')} 未找到行情/法人資料，已回補至最後交易日 {trade_date.strftime('%Y/%m/%d')}"
        )

    # 合併三大法人來源：TPEX (上櫃) → T86SITC (上市 KY/特殊類補遺漏) → T86 (上市主流) 覆蓋
    # 合併規則：同一代號重複出現時，「只有非 None 的欄位才能覆蓋舊值」，避免 T86 parse 失敗的 None 蓋掉前一層的真實資料
    def _merge_insti_layer(dst: dict[str, dict[str, Any]], src: dict[str, dict[str, Any]], overwrite_all: bool = False) -> None:
        for c, v in src.items():
            if not isinstance(v, dict):
                continue
            if c not in dst or overwrite_all:
                f = v.get("foreign_net")
                t = v.get("trust_net")
                # 即使 overwrite_all，全 None 的垃圾列也不覆蓋（避免丟掉上一層的真實值）
                if overwrite_all and f is None and t is None:
                    continue
                dst[c] = {"name": v.get("name") or "", "foreign_net": f, "trust_net": t}
                continue
            existing = dst[c]
            for field in ("foreign_net", "trust_net"):
                new_v = v.get(field)
                if new_v is None:
                    continue
                existing[field] = new_v
            # name 只在原本是空的才補齊
            if not existing.get("name") and v.get("name"):
                existing["name"] = v.get("name")

    institutional_by_code: dict[str, dict[str, Any]] = {}
    _merge_insti_layer(institutional_by_code, tpex_insti_by_code or {}, overwrite_all=True)
    _merge_insti_layer(institutional_by_code, t86sitc_by_code or {}, overwrite_all=False)
    _merge_insti_layer(institutional_by_code, t86_by_code or {}, overwrite_all=False)

    # 除錯：taifex 清單中「在上市 MI_INDEX 有，但 T86 / T86SITC / TPEX 三份都缺」→ 真 missing
    if (t86_by_code or t86sitc_by_code or tpex_insti_by_code) and mi_twse:
        missing_insti_listed: list[str] = []
        for item in codes_list:
            c = str(item.get("code"))
            if c in mi_twse and c not in institutional_by_code:
                missing_insti_listed.append(f"{c} {item.get('name','')}")
        if missing_insti_listed:
            errors.append(f"[三大法人警告] {len(missing_insti_listed)} 支上市標的 T86+T86SITC+TPEX 三份都找不到: {', '.join(missing_insti_listed[:30])}{' ...' if len(missing_insti_listed) > 30 else ''}")
    # taifex 清單中「在 TPEX MI 有，但三份法人來源也都沒資料」→ 真 missing (極少數)
    if (t86_by_code or t86sitc_by_code or tpex_insti_by_code) and mi_tpex:
        missing_insti_tpex: list[str] = []
        for item in codes_list:
            c = str(item.get("code"))
            if c in mi_tpex and c not in mi_twse and c not in institutional_by_code:
                missing_insti_tpex.append(f"{c} {item.get('name','')}")
        if missing_insti_tpex:
            errors.append(f"[三大法人警告] {len(missing_insti_tpex)} 支上櫃標的在 TPEX+T86+T86SITC 三份法人中找不到: {', '.join(missing_insti_tpex[:30])}{' ...' if len(missing_insti_tpex) > 30 else ''}")

    data_date = (
        t86_date
        or t86sitc_date
        or tpex_insti_date
        or trade_date.strftime("%Y%m%d")
    )
    if len(data_date) == 8 and data_date.isdigit():
        data_date_fmt = f"{data_date[:4]}/{data_date[4:6]}/{data_date[6:8]}"
    else:
        data_date_fmt = data_date

    rows: list[dict[str, Any]] = []
    for code, tf in taifex_by_code.items():
        insti = institutional_by_code.get(code)
        quote = mi_twse.get(code) or mi_tpex.get(code)
        foreign_net = (insti or {}).get("foreign_net")
        trust_net = (insti or {}).get("trust_net")
        tags: list[str] = []
        if tf.get("has_futures"):
            tags.append("期")
        if tf.get("has_options"):
            tags.append("權")
        row = {
            "股票代號": code,
            "股票名稱": (
                (insti or {}).get("name")
                or (quote or {}).get("name")
                or tf.get("name")
                or "-"
            ),
            "期/權": "/".join(tags) if tags else "-",
            "tags_list": tags,
            "外陸資買賣超(股,不含自營)": foreign_net if isinstance(foreign_net, int) else None,
            "投信買賣超(股)": trust_net if isinstance(trust_net, int) else None,
            "成交股數(股)": (quote or {}).get("volume"),
            "成交筆數": (quote or {}).get("transactions"),
            "開盤價": (quote or {}).get("open"),
            "最高價": (quote or {}).get("high"),
            "最低價": (quote or {}).get("low"),
            "收盤價": (quote or {}).get("close"),
            "前日收盤價": (quote or {}).get("prev_close"),
            "漲跌幅(%)": (quote or {}).get("change_pct"),
            "市場": (quote or {}).get("market", "-"),
            "taifex_updated_on": taifex_updated,
            "有期貨": bool(tf.get("has_futures")),
            "有選擇權": bool(tf.get("has_options")),
        }
        # 使用者 2026-10-02 新公式：所有「行情相對 %」分母改用「前日收盤價」
        #   漲跌幅(%) = (今日收盤 - 前日收盤) / 前日收盤 * 100
        #   最高幅(%) = (今日最高 - 前日收盤) / 前日收盤 * 100
        #   最低幅(%) = (今日最低 - 前日收盤) / 前日收盤 * 100
        #   開盤(%)   = (今日開盤 - 前日收盤) / 前日收盤 * 100
        prev_close = row.get("前日收盤價")
        open_p = row.get("開盤價")
        high_p = row.get("最高價")
        low_p = row.get("最低價")
        close_p = row.get("收盤價")
        if isinstance(prev_close, (int, float)) and abs(prev_close) > 1e-9:
            def _pct_today(value: Any) -> float | None:
                if not isinstance(value, (int, float)):
                    return None
                return round((float(value) - float(prev_close)) / float(prev_close) * 100, 2)
            row["漲跌幅(%)"] = _pct_today(close_p)
            row["最高幅(%)"] = _pct_today(high_p)
            row["最低幅(%)"] = _pct_today(low_p)
            row["開盤(%)"] = _pct_today(open_p)
        else:
            row["漲跌幅(%)"] = None
            row["最高幅(%)"] = None
            row["最低幅(%)"] = None
            row["開盤(%)"] = None
        # 計算占比：(買賣超 / 成交股數) * 100；成交股數 0/None 則留空
        volume_int = row.get("成交股數(股)")
        volume = int(volume_int) if isinstance(volume_int, (int, float)) else 0
        if volume > 0:
            if isinstance(foreign_net, int):
                row["外陸資占比(%)"] = round(foreign_net / volume * 100, 2)
            else:
                row["外陸資占比(%)"] = None
            if isinstance(trust_net, int):
                row["投信占比(%)"] = round(trust_net / volume * 100, 2)
            else:
                row["投信占比(%)"] = None
        else:
            row["外陸資占比(%)"] = None
            row["投信占比(%)"] = None
        rows.append(row)

    # 排序：外陸資買賣超由大到小
    def _sort_key(r: dict[str, Any]) -> tuple[int, str]:
        v = r.get("外陸資買賣超(股,不含自營)")
        iv = int(v) if isinstance(v, int) else 0
        return (-iv, r["股票代號"])

    rows.sort(key=_sort_key)
    return rows, errors


def institutional_cli_format(row: dict[str, Any]) -> dict[str, str]:
    def _cli_int(v: Any) -> str:
        if v is None: return "-"
        if isinstance(v, (int, float)):
            iv = int(v)
            if iv == 0: return "0"
            return f"+{iv:,}" if iv > 0 else f"{iv:,}"
        return "-"
    return {
        "股票代號": str(row["股票代號"]),
        "股票名稱": str(row["股票名稱"]),
        "外陸資買賣超(股,不含自營)": _cli_int(row.get("外陸資買賣超(股,不含自營)")),
        "投信買賣超(股)": _cli_int(row.get("投信買賣超(股)")),
        "成交股數(股)": format_int(row.get("成交股數(股)")),
        "開盤價": format_price(row.get("開盤價")),
        "最高價": format_price(row.get("最高價")),
        "最低價": format_price(row.get("最低價")),
        "收盤價": format_price(row.get("收盤價")),
        "前日收盤價": format_price(row.get("前日收盤價")),
        "開盤(%)": format_percent(row.get("開盤(%)")),
        "漲跌幅(%)": format_percent(row.get("漲跌幅(%)")),
        "最高幅(%)": format_percent(row.get("最高幅(%)")),
        "最低幅(%)": format_percent(row.get("最低幅(%)")),
        "外陸資占比(%)": format_percent(row.get("外陸資占比(%)")),
        "投信占比(%)": format_percent(row.get("投信占比(%)")),
    }


def _is_zero_or_missing(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, (int, float)):
        return v == 0
    return True


def _signed_value_pct_style(v: Any):
    """for 三個一般 % 欄（漲跌幅 / 最高幅 / 最低幅）：
    None → '-', '#e2e8f0' 白
    0 → '0', '#e2e8f0' 白 （使用者：0% 直接白色顯示 0，不加 +/- 紅綠）
    >0 → +v.xx 紅；<0 → -v.xx 綠
    """
    if not isinstance(v, (int, float)):
        return "-", "#e2e8f0", "nan"
    # 四捨五入到小數 2 位，避免浮點 0.000000001 被誤判為 != 0
    rounded = round(float(v), 2)
    if rounded == 0:
        return "0", "#e2e8f0", "0"
    if rounded > 0:
        c = "#ef4444"
        return f"+{rounded:.2f}", c, f"{rounded:.6f}"
    c = "#22c55e"
    return f"{rounded:.2f}", c, f"{rounded:.6f}"


def render_institutional_html_row(row: dict[str, Any]) -> str:
    code = row["股票代號"]
    tags_html = []
    for t in row.get("tags_list") or []:
        if t == "期":
            tags_html.append(
                '<span class="group-badge" style="border-color:#3b82f6;background:rgba(59,130,246,0.14);color:#93c5fd;">期</span>'
            )
        elif t == "權":
            tags_html.append(
                '<span class="group-badge" style="border-color:#a855f7;background:rgba(168,85,247,0.14);color:#c4b5fd;">權</span>'
            )
    fn = row.get("外陸資買賣超(股,不含自營)")
    tn = row.get("投信買賣超(股)")
    fp = row.get("外陸資占比(%)")
    tp = row.get("投信占比(%)")
    open_pct = row.get("開盤(%)")
    change_pct = row.get("漲跌幅(%)")
    high_open_pct = row.get("最高幅(%)")
    low_open_pct = row.get("最低幅(%)")

    # 買賣超：None → '-', 白 (沒爬到；上櫃/T86沒清單)；0 → '0', 白；>0 紅；<0 綠
    def int_color_style(v: Any) -> tuple[str, str, str]:
        if v is None:
            return "-", "#e2e8f0", "nan"
        if isinstance(v, (int, float)):
            iv = int(v)
            if iv == 0:
                return "0", "#e2e8f0", "0"
            c = "#ef4444" if iv > 0 else "#22c55e"
            return (f"+{iv:,}" if iv > 0 else f"{iv:,}"), c, f"{iv}"
        return "-", "#e2e8f0", "nan"

    fn_t, fn_c, fn_v = int_color_style(fn)
    tn_t, tn_c, tn_v = int_color_style(tn)

    # 占比：0/None → '0' / '-' 白色不加 +/-；其餘 正常 +/- 紅綠
    def pct_zero_plain_style(v: Any) -> tuple[str, str, str]:
        if not isinstance(v, (int, float)):
            return "-", "#e2e8f0", "nan"
        if v == 0:
            return "0", "#e2e8f0", "0"
        c = "#ef4444" if v > 0 else "#22c55e"
        return f"{v:+.2f}", c, f"{v:.6f}"

    fp_t, fp_c, fp_v = pct_zero_plain_style(fp)
    tp_t, tp_c, tp_v = pct_zero_plain_style(tp)

    open_t, open_c, open_v = _signed_value_pct_style(open_pct)
    change_t, change_c, change_v = _signed_value_pct_style(change_pct)
    ho_t, ho_c, ho_v = _signed_value_pct_style(high_open_pct)
    lo_t, lo_c, lo_v = _signed_value_pct_style(low_open_pct)

    return (
        "<tr>"
        f'<td class="code-cell"><span class="stock-code">{htmlescape(code)}{"".join(tags_html)}</span></td>'
        f"<td>{htmlescape(str(row['股票名稱']))}</td>"
        f'<td data-value="{fn_v}"><span class="risk-text" style="color:{fn_c}">{htmlescape(fn_t)}</span></td>'
        f'<td data-value="{tn_v}"><span class="risk-text" style="color:{tn_c}">{htmlescape(tn_t)}</span></td>'
        f"<td>{htmlescape(format_int(row.get('成交股數(股)')))}</td>"
        f"<td>{htmlescape(format_price(row.get('開盤價')))}</td>"
        f"<td>{htmlescape(format_price(row.get('最高價')))}</td>"
        f"<td>{htmlescape(format_price(row.get('最低價')))}</td>"
        f"<td>{htmlescape(format_price(row.get('收盤價')))}</td>"
        f"<td>{htmlescape(format_price(row.get('前日收盤價')))}</td>"
        f'<td data-sort-field="open_pct" data-value="{open_v}"><span class="risk-text" style="color:{open_c}">{htmlescape(open_t)}</span></td>'
        f'<td data-sort-field="change_pct" data-value="{change_v}"><span class="risk-text" style="color:{change_c}">{htmlescape(change_t)}</span></td>'
        f'<td data-sort-field="high_open_pct" data-value="{ho_v}"><span class="risk-text" style="color:{ho_c}">{htmlescape(ho_t)}</span></td>'
        f'<td data-sort-field="low_open_pct" data-value="{lo_v}"><span class="risk-text" style="color:{lo_c}">{htmlescape(lo_t)}</span></td>'
        f'<td data-sort-field="foreign_pct" data-value="{fp_v}"><span class="risk-text" style="color:{fp_c}">{htmlescape(fp_t)}</span></td>'
        f'<td data-sort-field="trust_pct" data-value="{tp_v}"><span class="risk-text" style="color:{tp_c}">{htmlescape(tp_t)}</span></td>'
        "</tr>"
    )


def render_institutional_table_view(view_name: str, rows: list[dict[str, Any]], is_active: bool) -> str:
    if rows:
        body = "\n".join(render_institutional_html_row(r) for r in rows)
    else:
        body = '<tr><td colspan="17">這個群組目前沒有符合條件的股票。</td></tr>'
    def th_cell(title: str, min_width: str | None = None) -> str:
        style = "" if min_width is None else f' style="min-width:{min_width}"'
        return f"<th{style}>{title}</th>"
    def sort_header(title: str, field: str, min_width: str | None = None) -> str:
        style = "" if min_width is None else f' style="min-width:{min_width}"'
        return (
            f'<th class="sortable-header" data-sort-target="{field}"{style}>'
            f'<span class="th-title">{title}</span>'
            f'<span class="sort-btns">'
            f'<button type="button" class="sort-btn sort-up" data-field="{field}" data-dir="desc" title="+ 最大 → - 最大">▲</button>'
            f'<button type="button" class="sort-btn sort-down" data-field="{field}" data-dir="asc" title="- 最大 → + 最大">▼</button>'
            f'</span>'
            f'</th>'
        )
    return f"""
<div class="table-view institutional-view{' active' if is_active else ''}" data-tab="institutional" data-view="{htmlescape(view_name)}">
  <table>
    <thead>
      <tr>
        {th_cell('股票代號', '76px')}
        {th_cell('股票名稱', '96px')}
        {th_cell('外陸資買賣超<br>(不含自營)', '132px')}
        {th_cell('投信買賣超', '108px')}
        {th_cell('成交股數', '112px')}
        {th_cell('開盤', '62px')}
        {th_cell('最高', '62px')}
        {th_cell('最低', '62px')}
        {th_cell('收盤', '62px')}
        {th_cell('前日收盤', '72px')}
        {sort_header('開盤幅<br>(%)', 'open_pct', '80px')}
        {sort_header('漲跌幅<br>(%)', 'change_pct', '80px')}
        {sort_header('最高幅<br>(%)', 'high_open_pct', '80px')}
        {sort_header('最低幅<br>(%)', 'low_open_pct', '80px')}
        {sort_header('外陸資占比<br>(%)', 'foreign_pct', '106px')}
        {sort_header('投信占比<br>(%)', 'trust_pct', '106px')}
      </tr>
    </thead>
    <tbody>{body}</tbody>
  </table>
</div>
"""


def _top_by_field(rows: list[dict[str, Any]], field: str, n: int, reverse: bool) -> list[dict[str, Any]]:
    def val(r: dict[str, Any]) -> int:
        v = r.get(field)
        return int(v) if isinstance(v, (int, float)) else 0
    return sorted(
        [r for r in rows if isinstance(r.get(field), int)],
        key=val,
        reverse=reverse,
    )[:n]


def render_institutional_filter_bar(rows: list[dict[str, Any]]) -> str:
    buttons: list[tuple[str, str, int, bool]] = []
    for idx, (view_name, filter_fn) in enumerate(INSTITUTIONAL_VIEWS):
        count = len([r for r in rows if filter_fn(r)])
        desc = INSTITUTIONAL_VIEW_DESCRIPTIONS.get(view_name, "")
        buttons.append((view_name, desc, count, idx == 0))
    return "\n".join(
        (
            f'<button class="filter-button{" active" if is_active else ""}" '
            f'data-tab="institutional" data-view="{htmlescape(name)}" '
            f'style="{get_filter_button_style(name, INSTITUTIONAL_VIEW_COLORS)}">'
            f'<span class="filter-name">{htmlescape(name)}</span>'
            f'<span class="filter-desc">{htmlescape(desc)}</span>'
            f'<span class="filter-count">{count}</span>'
            f"</button>"
        )
        for name, desc, count, is_active in buttons
    )


def render_institutional_table_views(rows: list[dict[str, Any]]) -> str:
    sections: list[tuple[str, list[dict[str, Any]], bool]] = []
    for idx, (view_name, filter_fn) in enumerate(INSTITUTIONAL_VIEWS):
        filtered = sorted(
            [r for r in rows if filter_fn(r)],
            key=_stock_code_key,
        )
        sections.append((view_name, filtered, idx == 0))
    return "\n".join(render_institutional_table_view(n, v, a) for n, v, a in sections)


# ---------------------------------------------------------------------------
# Notifications (維持處置股行為)
# ---------------------------------------------------------------------------


def build_notification_message(rows: list[dict[str, str]], generated_at: str) -> str:
    combined_rows = merge_rows_for_combined_view(rows)
    group_counter = Counter(row["群組"] for row in rows)
    multi_group_rows = [row for row in combined_rows if len(row["群組列表"]) > 1]

    lines = [
        f"StockWarden 每日摘要 {generated_at}",
        f"綜合 {len(combined_rows)} 檔",
        " / ".join(
            [
                f"風險股 {group_counter.get('風險股', 0)}",
                f"處置中 {group_counter.get('處置中', 0)}",
                f"即將出關 {group_counter.get('即將出關', 0)}",
                f"自結發布 {group_counter.get('自結發布', 0)}",
            ]
        ),
    ]
    if multi_group_rows:
        lines.append("跨群組重疊:")
        for r in multi_group_rows[:8]:
            lines.append(f"{r['股票代號']} {r['股票名稱']} ({'/'.join(r['群組列表'])})")
    return "\n".join(lines)


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[bool, str]:
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30, context=_UNVERIFIED_SSL_CTX) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return True, body or "OK"
    except HTTPError as exc:
        return False, exc.read().decode("utf-8", errors="ignore") or str(exc)
    except URLError as exc:
        return False, str(exc)


def post_form(url: str, payload: dict[str, str]) -> tuple[bool, str]:
    return _post_form(url, payload)


def send_telegram_notification(config: dict[str, Any], message: str) -> tuple[bool, str]:
    bot_token = str(config.get("bot_token", "")).strip()
    chat_id = str(config.get("chat_id", "")).strip()
    if not bot_token or not chat_id:
        return False, "缺少 Telegram bot_token 或 chat_id"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    return post_form(url, {"chat_id": chat_id, "text": message})


def send_line_notification(config: dict[str, Any], message: str) -> tuple[bool, str]:
    token = str(config.get("channel_access_token", "")).strip()
    to = str(config.get("to", "")).strip()
    if not token or not to:
        return False, "缺少 LINE channel_access_token 或 to"
    return post_json(
        "https://api.line.me/v2/bot/message/push",
        {"to": to, "messages": [{"type": "text", "text": message[:4900]}]},
        {"Authorization": f"Bearer {token}"},
    )


def send_notifications(rows: list[dict[str, str]], generated_at: str) -> list[str]:
    config = load_notification_config()
    message = build_notification_message(rows, generated_at)
    results: list[str] = []
    tg = config.get("telegram", {})
    if tg.get("enabled"):
        ok, detail = send_telegram_notification(tg, message)
        results.append(f"Telegram {'成功' if ok else '失敗'}: {detail}")
    ln = config.get("line", {})
    if ln.get("enabled"):
        ok, detail = send_line_notification(ln, message)
        results.append(f"LINE {'成功' if ok else '失敗'}: {detail}")
    if not results:
        results.append("未啟用通知，請填寫 notification_settings.json 或設定環境變數。")
    return results


# ---------------------------------------------------------------------------
# Generic CLI / HTML rendering over PAGES
# ---------------------------------------------------------------------------


def render_main_tabs(pages: list[PageSpec], page_dates: dict[str, str] | None = None) -> str:
    page_dates = page_dates or {}
    items = []
    for p in sorted(pages, key=lambda x: x.tab_order):
        active = p.default_active
        items.append(
            f'<button class="main-tab{" active" if active else ""}" '
            f'data-main-tab="{htmlescape(p.key)}">'
            f"{htmlescape(p.label)}</button>"
        )
    # 日期顯示在 main-tabs 右上角，和兩個 tab 平行
    active_page = next((p for p in sorted(pages, key=lambda x: x.tab_order) if p.default_active), None)
    sorted_pages = sorted(pages, key=lambda x: x.tab_order)
    active_key = active_page.key if active_page else (sorted_pages[0].key if sorted_pages else "")
    date_str = page_dates.get(active_key) or ""
    if date_str:
        items.append(
            f'<div class="tab-date" id="mainTabDateBadge">'
            f'<span class="tab-date-label">資料日期</span>'
            f'<span class="tab-date-value">{htmlescape(date_str)}</span>'
            f"</div>"
        )
    dates_json = json.dumps(page_dates, ensure_ascii=False)
    return "\n".join(items) + (
        f'\n<script id="mainTabDates" type="application/json">{htmlescape(dates_json)}</script>'
    )


def render_main_panels(pages: list[PageSpec], page_results: dict[str, list[dict[str, Any]]], _page_dates: dict[str, str] | None = None) -> str:
    panels = []
    for p in sorted(pages, key=lambda x: x.tab_order):
        rows = page_results.get(p.key) or []
        if p.key == "disposition":
            filter_bar = render_disposition_filter_bar(rows, merge_rows_for_combined_view(rows))
            views = render_disposition_table_views(rows)
        elif p.key == "institutional":
            filter_bar = render_institutional_filter_bar(rows)
            views = render_institutional_table_views(rows)
        else:
            filter_bar = ""
            views = ""
        panels.append(
            f'<section class="main-content-panel{" active" if p.default_active else ""}" data-main-panel="{htmlescape(p.key)}">'
            f'<section class="filter-bar">{filter_bar}</section>'
            f'<section class="table-shell">'
            f'<div class="table-scroll-top" aria-hidden="true"><div class="table-scroll-top-inner"></div></div>'
            f"{views}"
            f'</section>'
            f"</section>"
        )
    return "\n".join(panels)


def save_html_multi(pages: list[PageSpec], page_results: dict[str, list[dict[str, Any]]], generated_at: str, page_dates: dict[str, str] | None = None) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    main_tabs = render_main_tabs(pages, page_dates)
    main_panels = render_main_panels(pages, page_results, page_dates)
    title = " / ".join(p.label for p in sorted(pages, key=lambda x: x.tab_order))
    html_text = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{htmlescape(title)}</title>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: "Microsoft JhengHei", Arial, sans-serif;
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(56, 189, 248, 0.10), transparent 30%),
        radial-gradient(circle at top right, rgba(239, 68, 68, 0.10), transparent 30%),
        #020617;
      color: #e2e8f0;
    }}
    .page {{ margin: 0 auto; padding: 28px 24px 48px; }}
    .main-tabs {{
      display: grid;
      grid-template-columns: auto auto 1fr;
      gap: 12px 20px;
      margin-bottom: 20px;
      align-items: center;
    }}
    .main-tabs .tab-date {{
      grid-column: 3 / 4;
      justify-self: end;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 16px;
      border-radius: 14px;
      border: 1px solid rgba(251,191,36,0.32);
      background: rgba(251,191,36,0.08);
      color: #fde68a;
      font-weight: 700;
      letter-spacing: 0.03em;
      min-height: 44px;
    }}
    .main-tabs .tab-date-label {{ font-size: 12px; color: #fcd34d; opacity: 0.85; }}
    .main-tabs .tab-date-value {{ font-size: 15px; color: #fff7ed; }}
    .main-tab {{
      cursor: pointer; padding: 14px 26px; border-radius: 18px; border: 1px solid #ffffff; font-size: 16px; font-weight: 700; letter-spacing: 0.02em;
      color: #ffffff;
      background: rgba(15, 23, 42, 0.6);
      transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease, box-shadow 0.15s ease, color 0.15s ease;
      backdrop-filter: blur(10px);
    }}
    .main-tab:hover {{ transform: translateY(-1px); }}
    .main-tab.active {{
      transform: translateY(-1px);
      color: #fbbf24;
      border-color: #fbbf24;
      background: rgba(251, 191, 36, 0.08);
      box-shadow: 0 0 24px rgba(251, 191, 36, 0.22);
    }}
    .main-content-panel {{ display: none; position: relative; }}
    .main-content-panel.active {{ display: block; }}
    .filter-bar {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; margin-bottom: 20px; position: relative; }}
    .filter-button {{
      cursor: pointer; text-align: left; padding: 16px 18px; border-radius: 20px;
      border: 1px solid var(--button-border, rgba(148,163,184,0.18));
      background: var(--button-bg, rgba(15,23,42,0.76));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
      color: var(--button-text, #e2e8f0);
      transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease, box-shadow 0.15s ease;
    }}
    .filter-button:hover {{
      transform: translateY(-1px);
      border-color: var(--button-border-strong, rgba(148,163,184,0.42));
      background: var(--button-bg-strong, rgba(30,41,59,0.92));
    }}
    .filter-button.active {{
      border-color: var(--button-border-strong, rgba(96,165,250,0.55));
      background: linear-gradient(180deg, var(--button-bg-strong, rgba(37,99,235,0.18)), rgba(15,23,42,0.82));
      box-shadow: 0 12px 32px var(--button-shadow, rgba(37,99,235,0.18));
    }}
    .filter-name {{ display: block; color: var(--button-subtle, #94a3b8); font-size: 14px; margin-bottom: 6px; }}
    .filter-desc {{ display: block; color: #cbd5e1; opacity: 0.82; font-size: 11px; line-height: 1.45; margin-bottom: 10px; white-space: normal; word-break: break-word; }}
    .filter-count {{ display: block; font-size: 30px; font-weight: 700; letter-spacing: 0.02em; }}
    .tab-update-date {{
      grid-column: 1 / -1;
      justify-self: end;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 16px;
      border-radius: 14px;
      border: 1px solid rgba(251,191,36,0.32);
      background: rgba(251,191,36,0.08);
      color: #fde68a;
      font-weight: 700;
      letter-spacing: 0.03em;
    }}
    .tab-update-date-label {{ font-size: 12px; color: #fcd34d; opacity: 0.85; }}
    .tab-update-date-value {{ font-size: 15px; color: #fff7ed; }}
    .filter-bar .tab-update-date {{ display: none; }}
    .table-shell {{ position: relative; overflow-x: auto; overflow-y: visible; border-radius: 24px; border: 1px solid rgba(148,163,184,0.16); background: rgba(15,23,42,0.78); box-shadow: 0 20px 60px rgba(0,0,0,0.25); backdrop-filter: blur(10px); -webkit-overflow-scrolling: touch; }}
    .table-scroll-top {{ overflow-x: auto; overflow-y: hidden; height: 16px; width: 100%; position: relative; -webkit-overflow-scrolling: touch; }}
    .table-scroll-top-inner {{ height: 1px; }}
    table {{ width: max-content; min-width: 100%; border-collapse: separate; border-spacing: 0; }}
    th, td {{ border-bottom: 1px solid rgba(148,163,184,0.12); padding: 10px 10px; text-align: left; vertical-align: top; white-space: nowrap; }}
    th {{ position: sticky; top: 0; z-index: 2; background: rgba(2,6,23,0.99); color: #cbd5e1; font-size: 13px; letter-spacing: 0; text-transform: none; box-shadow: 0 1px 0 rgba(148,163,184,0.28), 0 6px 12px rgba(0,0,0,0.25); }}
    tbody tr {{ transition: background 0.2s ease; }}
    tbody tr:hover {{ background: rgba(148,163,184,0.08); }}
    td.code-cell {{ white-space: nowrap; }}
    td.message-cell {{ white-space: pre-wrap; min-width: 680px; max-width: 760px; }}
    .stock-code {{ display: inline-flex; align-items: center; gap: 10px; font-weight: 700; letter-spacing: 0.02em; flex-wrap: wrap; }}
    .group-badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 10px; font-size: 12px; font-weight: 700; border: 1px solid transparent; }}
    .session-pill {{ display: inline-flex; align-items: center; min-width: 56px; justify-content: center; border-radius: 999px; padding: 6px 10px; background: rgba(59,130,246,0.16); color: #bfdbfe; border: 1px solid rgba(59,130,246,0.25); font-weight: 700; }}
    .session-pill.none {{ background: rgba(148,163,184,0.10); color: #94a3b8; border-color: rgba(148,163,184,0.16); }}
    .risk-text {{ color: #f8fafc; font-weight: 600; }}
    .sortable-header {{
      position: relative;
      padding-right: 32px !important;
      min-width: 100px;
    }}
    /* 表格第一欄 sticky：橫向滾動時，永遠看到股票代號，避免左右滑到最右邊的投信占比時看不到是哪一支股票 */
    .table-view td:nth-child(1),
    .table-view th:nth-child(1) {{
      position: sticky;
      left: 0;
      z-index: 3;
      background: rgba(2,6,23,0.99);
      box-shadow: 2px 0 0 rgba(148,163,184,0.10);
    }}
    .table-view td:nth-child(2),
    .table-view th:nth-child(2) {{
      position: sticky;
      left: 76px;
      z-index: 3;
      background: rgba(2,6,23,0.99);
      box-shadow: 2px 0 0 rgba(148,163,184,0.10);
    }}
    .sortable-header .th-title {{ display: inline-block; line-height: 1.2; padding-right: 4px; vertical-align: middle; max-width: 84px; }}
    .sortable-header .sort-btns {{
      position: absolute;
      top: 50%;
      right: 6px;
      transform: translateY(-50%);
      display: inline-flex;
      flex-direction: column;
      gap: 2px;
      align-items: stretch;
      justify-content: center;
      z-index: 3;
    }}
    .sort-btn {{
      all: unset;
      cursor: pointer;
      font-size: 10px;
      line-height: 1;
      width: 20px;
      height: 14px;
      border-radius: 5px;
      color: #e2e8f0;
      background: rgba(148,163,184,0.14);
      border: 1px solid rgba(148,163,184,0.24);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      box-sizing: content-box;
      flex-shrink: 0;
      transition: transform 0.06s ease, background 0.12s ease, color 0.12s ease, border-color 0.12s ease;
    }}
    .sort-btn:hover {{ color: #fbbf24; background: rgba(251,191,36,0.16); border-color: rgba(251,191,36,0.42); }}
    .sort-btn.active {{ color: #020617; background: #fbbf24; border-color: #fbbf24; font-weight: 800; }}
    .sort-btn:active {{ transform: scale(0.96); }}
    .message-cell {{ max-width: 760px; white-space: pre-wrap; line-height: 1.65; color: #cbd5e1; }}
    .table-view {{ display: none; }}
    .table-view.active {{ display: block; }}
    a {{ color: #93c5fd; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .fetch-warnings {{ margin-bottom: 18px; padding: 12px 16px; border-radius: 14px; border: 1px solid rgba(251,191,36,0.35); background: rgba(251,191,36,0.10); color: #fde68a; font-size: 13px; line-height: 1.7; }}
    @media (max-width: 900px) {{
      .page {{ padding: 20px 14px 32px; }}
      h1 {{ font-size: 22px; }}
      th, td {{ padding: 12px 10px; }}
      .main-tabs {{
        grid-template-columns: 1fr 1fr;
      }}
      .main-tabs .tab-date {{
        grid-column: 1 / -1;
        justify-self: stretch;
        justify-content: flex-end;
      }}
      .tab-update-date {{ justify-self: stretch; justify-content: flex-end; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <div class="main-tabs">{main_tabs}</div>
    {main_panels}
  </div>
  <script>
    const mainTabs = Array.from(document.querySelectorAll(".main-tab"));
    const mainPanels = Array.from(document.querySelectorAll(".main-content-panel"));
    mainTabs.forEach((tab) => {{
      tab.addEventListener("click", () => {{
        const target = tab.dataset.mainTab;
        mainTabs.forEach((t) => t.classList.toggle("active", t === tab));
        mainPanels.forEach((panel) => panel.classList.toggle("active", panel.dataset.mainPanel === target));
        const firstFilter = document.querySelector(`.filter-button[data-tab="${{target}}"]`);
        if (firstFilter) firstFilter.click();
        setTimeout(() => refreshDoubleScrollbars(target), 0);
      }});
    }});
    const filterButtons = Array.from(document.querySelectorAll(".filter-button"));
    filterButtons.forEach((button) => {{
      button.addEventListener("click", () => {{
        const tab = button.dataset.tab;
        const view = button.dataset.view;
        filterButtons
          .filter((b) => b.dataset.tab === tab)
          .forEach((b) => b.classList.toggle("active", b === button));
        document
          .querySelectorAll(`.table-view[data-tab="${{tab}}"]`)
          .forEach((v) => v.classList.toggle("active", v.dataset.view === view));
        setTimeout(() => refreshDoubleScrollbars(tab), 0);
      }});
    }});
    // 切換 main tab 時，右上角日期同步更新為該頁的資料日期
    (function syncTabDateOnSwitch() {{
      try {{
        const datesEl = document.getElementById("mainTabDates");
        const badgeEl = document.getElementById("mainTabDateBadge");
        const valueEl = badgeEl ? badgeEl.querySelector(".tab-date-value") : null;
        if (!datesEl || !badgeEl || !valueEl) return;
        const dates = JSON.parse(datesEl.textContent || "{{}}");
        mainTabs.forEach((tab) => {{
          tab.addEventListener("click", () => {{
            const key = tab.dataset.mainTab;
            if (dates && dates[key]) {{
              valueEl.textContent = dates[key];
              badgeEl.style.display = "";
            }} else {{
              badgeEl.style.display = "none";
            }}
          }});
        }});
      }} catch (e) {{ /* ignore */ }}
    }})();
    const sortButtons = Array.from(document.querySelectorAll(".sort-btn"));
    function clearActiveSortBtns(scopeEl) {{
      (scopeEl || document).querySelectorAll(".sort-btn.active").forEach((b) => b.classList.remove("active"));
    }}
    sortButtons.forEach((btn) => {{
      btn.addEventListener("click", (ev) => {{
        ev.stopPropagation();
        const field = btn.dataset.field;
        const dir = btn.dataset.dir; // desc = 由+到- (大到小)；asc = 由-到+ (小到大)
        const view = btn.closest(".table-view");
        if (!view) return;
        const tbody = view.querySelector("tbody");
        if (!tbody) return;
        clearActiveSortBtns(view);
        btn.classList.add("active");
        const rowsArr = Array.from(tbody.querySelectorAll(":scope > tr"));
        rowsArr.sort((a, b) => {{
          const avRaw = a.querySelector(`td[data-sort-field="${{field}}"]`)?.dataset?.value;
          const bvRaw = b.querySelector(`td[data-sort-field="${{field}}"]`)?.dataset?.value;
          const av = (avRaw === undefined || avRaw === "nan" || avRaw === null) ? NaN : Number(avRaw);
          const bv = (bvRaw === undefined || bvRaw === "nan" || bvRaw === null) ? NaN : Number(bvRaw);
          // 把 NaN (缺值) 排序到最後
          const aNum = Number.isFinite(av) ? av : (dir === "desc" ? -Infinity : Infinity);
          const bNum = Number.isFinite(bv) ? bv : (dir === "desc" ? -Infinity : Infinity);
          if (dir === "desc") return bNum - aNum; // + 最大 → - 最大 (descending)
          return aNum - bNum;                          // - 最大 → + 最大 (ascending)
        }});
        rowsArr.forEach((tr) => tbody.appendChild(tr));
      }});
    }});
    // 表格上下雙 scrollbar 同步：上方 table-scroll-top <-> 下方 table-shell 水平捲動一致
    (function initDoubleScrollbars() {{
      window._dsSyncing = false;
    }})();
    function _setupPanelDoubleScroll(panelEl) {{
      if (!panelEl) return;
      const shell = panelEl.querySelector(":scope > .table-shell");
      const topScroller = shell ? shell.querySelector(":scope > .table-scroll-top") : null;
      const topInner = topScroller ? topScroller.querySelector(":scope > .table-scroll-top-inner") : null;
      const activeTable = panelEl.querySelector(":scope > .table-shell > .table-view.active > table")
                       || panelEl.querySelector(":scope > .table-shell > .table-view > table");
      if (!shell || !topScroller || !topInner || !activeTable) return;
      const needW = Math.max(activeTable.scrollWidth || 0, Math.ceil(activeTable.getBoundingClientRect().width) || 0);
      topInner.style.width = needW + "px";
      topInner.style.height = "1px";
      // 移除舊監聽
      if (shell._dsTopHandler) topScroller.removeEventListener("scroll", shell._dsTopHandler);
      if (shell._dsShellHandler) shell.removeEventListener("scroll", shell._dsShellHandler);
      shell._dsTopHandler = () => {{
        if (window._dsSyncing) return;
        window._dsSyncing = true;
        shell.scrollLeft = topScroller.scrollLeft;
        window._dsSyncing = false;
      }};
      shell._dsShellHandler = () => {{
        if (window._dsSyncing) return;
        window._dsSyncing = true;
        topScroller.scrollLeft = shell.scrollLeft;
        window._dsSyncing = false;
      }};
      topScroller.addEventListener("scroll", shell._dsTopHandler, {{ passive: true }});
      shell.addEventListener("scroll", shell._dsShellHandler, {{ passive: true }});
    }}
    function refreshDoubleScrollbars(panelKey) {{
      if (panelKey) {{
        const p = document.querySelector(`.main-content-panel[data-main-panel="${{panelKey}}"]`);
        if (p) _setupPanelDoubleScroll(p);
      }} else {{
        document.querySelectorAll(".main-content-panel.active").forEach(_setupPanelDoubleScroll);
      }}
    }}
    window.addEventListener("resize", () => refreshDoubleScrollbars());
    document.addEventListener("DOMContentLoaded", () => refreshDoubleScrollbars());
    if (document.readyState === "complete" || document.readyState === "interactive") {{
      try {{ refreshDoubleScrollbars(); }} catch (e) {{ /* ignore */ }}
    }}
  </script>
</body>
</html>
"""
    HTML_PATH.write_text(html_text, encoding="utf-8")

    # 每次重新寫入兩層「無快取啟動頁」，全部固定檔名、放在 OUTPUT_DIR 下、每次 run 直接覆蓋最新（不堆積動態檔名）。
    #   跳轉鏈：使用者雙擊 output/LATEST_請按兩下這個開啟_最新版_不會有快取.html
    #         → 跳 output/_launcher_no_cache_internal.html (固定檔名、內容每次重寫帶最新 ts)
    #         → 跳 output/stockwarden_futures_or_options_stocks.html?t=<最新ts>
    # ⚠️ query string 永遠寫在 HTML 內部 (meta refresh / JS)，絕對不外露到 file:// URL，避免 Windows Shell FileNotFound。
    try:
        import time as _tt
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(_tt.time() * 1000)
        target_uri = HTML_PATH.resolve().as_uri() + f"?t={ts}"

        # 1) 第二層（內部跳轉頁）：固定檔名，每次 run 直接覆蓋 → query string 永遠最新
        LAUNCHER_INTERNAL_PATH = OUTPUT_DIR / "_launcher_no_cache_internal.html"
        launcher_html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta http-equiv="refresh" content="0; url={target_uri}">
<title>跳轉到最新 StockWarden 儀表板 (ts={ts})</title>
<style>
body{{margin:0;background:#020617;color:#e5e7eb;font-family:system-ui,Segoe UI,Microsoft JhengHei,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px;}}
.card{{background:rgba(15,23,42,0.7);border:1px solid rgba(250,204,21,0.28);border-radius:16px;
  padding:28px 32px;max-width:640px;box-shadow:0 20px 60px rgba(0,0,0,0.5);}}
h1{{margin:0 0 14px;color:#facc15;font-size:22px;}}
p{{margin:8px 0;color:#cbd5e1;font-size:15px;line-height:1.7;}}
code{{background:rgba(15,23,42,0.9);border:1px solid #334155;border-radius:6px;padding:2px 8px;color:#22d3ee;
  word-break:break-all;}}
a.btn{{display:inline-block;margin-top:14px;padding:10px 16px;background:#facc15;color:#0f172a;border-radius:10px;
  text-decoration:none;font-weight:600;}}
</style>
</head>
<body>
  <div class="card">
    <h1>正在跳轉到最新 StockWarden 儀表板…</h1>
    <p>為了完全避開瀏覽器快取，每次都會加上唯一時間戳 <code>?t={ts}</code>。</p>
    <p>如果 3 秒後沒有自動跳轉，請按這顆按鈕：</p>
    <a class="btn" href="{target_uri}">開啟最新版 (?t={ts})</a>
    <p style="font-size:13px;color:#94a3b8;margin-top:16px;">
      目標路徑：<code>{HTML_PATH.resolve()}</code><br>
      永遠按 <code>output/LATEST_請按兩下這個開啟_最新版_不會有快取.html</code>，
      <strong style="color:#f87171;">不要直接按 stockwarden_futures_or_options_stocks.html</strong>（一定會吃到舊快取！）。
    </p>
  </div>
  <script>
    window.location.replace("{target_uri}");
  </script>
</body>
</html>
"""
        LAUNCHER_INTERNAL_PATH.write_text(launcher_html, encoding="utf-8")

        # 2) 第一層（使用者固定捷徑）：固定檔名，內容跳轉到上面的內部啟動頁；永遠按這一個就好
        LATEST_LAUNCH_PATH = OUTPUT_DIR / "LATEST_請按兩下這個開啟_最新版_不會有快取.html"
        fixed_jump_uri = LAUNCHER_INTERNAL_PATH.resolve().as_uri()
        fixed_launcher_html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta http-equiv="refresh" content="0; url={fixed_jump_uri}">
<title>StockWarden - 最新版入口 (ts={ts})</title>
<style>
body{{margin:0;background:#020617;color:#e5e7eb;font-family:system-ui,Segoe UI,Microsoft JhengHei,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px;}}
.card{{background:rgba(15,23,42,0.7);border:1px solid rgba(250,204,21,0.28);border-radius:16px;
  padding:28px 32px;max-width:640px;box-shadow:0 20px 60px rgba(0,0,0,0.5);}}
h1{{margin:0 0 14px;color:#facc15;font-size:22px;}}
a.btn{{display:inline-block;margin-top:14px;padding:10px 16px;background:#facc15;color:#0f172a;border-radius:10px;
  text-decoration:none;font-weight:600;}}
</style>
</head>
<body>
  <div class="card">
    <h1>正在導向最新啟動頁 (ts={ts})…</h1>
    <p style="color:#cbd5e1;">如果 3 秒後沒跳轉，請按下方按鈕：</p>
    <a class="btn" href="{fixed_jump_uri}">手動前往最新版</a>
  </div>
  <script>
    window.location.replace("{fixed_jump_uri}");
  </script>
</body>
</html>
"""
        LATEST_LAUNCH_PATH.write_text(fixed_launcher_html, encoding="utf-8")

        # 3) 清理舊版位置（可攜版根目錄）的啟動頁 html（現在全部改放 output/，舊版全部清掉）
        try:
            _old_patterns = [
                ("_LATEST_LAUNCH_*.html", Path(".")),
                ("LATEST_*.html", Path(".")),
            ]
            for _pat, _root in _old_patterns:
                for old in _root.glob(_pat):
                    try:
                        if old.is_file():
                            old.unlink()
                    except Exception:
                        pass
        except Exception:
            pass
    except Exception:
        pass


def print_cli_table(title: str, headers: list[str], rows: list[dict[str, str]], empty_text: str) -> None:
    print(f"\n=== {title} ({len(rows)}) ===")
    widths = {h: len(h) for h in headers}
    for row in rows:
        for h in headers:
            widths[h] = max(widths[h], len(str(row.get(h, ""))))
    line = "+-" + "-+-".join("-" * widths[h] for h in headers) + "-+"
    print(line)
    print("| " + " | ".join(h.ljust(widths[h]) for h in headers) + " |")
    print(line)
    if not rows:
        total_inner = len(line) - 4
        print("| " + empty_text.ljust(total_inner) + " |")
        print(line)
        return
    for row in rows:
        print("| " + " | ".join(str(row.get(h, "")).ljust(widths[h]) for h in headers) + " |")
    print(line)


# ---------------------------------------------------------------------------
# PAGES 註冊
# ---------------------------------------------------------------------------


def _fetch_disposition() -> list[dict[str, str]]:
    try:
        payload = fetch_board_data()
        updated = str(payload.get("updated") or "").strip()
        print(f"[處置股] boards.json 最新更新時間: {updated or '未知'}")
    except Exception:
        pass
    rows = extract_disposition_rows(payload)
    rows.sort(key=lambda r: (r["股票代號"], r["群組"]))
    taifex = fetch_taifex_list()
    taifex_code_set = {str(item.get("code")) for item in taifex.get("codes") or []}
    return [r for r in rows if str(r["股票代號"]) in taifex_code_set]


def _fetch_institutional() -> list[dict[str, Any]]:
    rows, errors = build_institutional_page_context()
    for msg in errors:
        print(f"[外資操作股] {msg}")
    return rows


def _resolve_institutional_data_date() -> str:
    """回傳 institutional 頁顯示的資料日期字串 YYYY/MM/DD（T86 date 優先，其次 TPEX，其次 fallback today）"""
    try:
        trade_date = date.today()
        resolved, _used_fallback = resolve_trade_date_with_fallback(trade_date)
    except Exception:
        resolved = date.today()
    try:
        _, d86 = fetch_twse_t86(resolved)
        _, dtpx = fetch_tpex_daily_insti(resolved)
        ds = d86 or dtpx or resolved.strftime("%Y%m%d")
        if len(ds) == 8 and ds.isdigit():
            return f"{ds[:4]}/{ds[4:6]}/{ds[6:8]}"
    except Exception:
        pass
    return resolved.strftime("%Y/%m/%d")


def _resolve_disposition_data_date() -> str:
    """處置股用 boards.json updated 欄位，若無則 fallback 最後交易日日期"""
    try:
        payload = fetch_board_data()
        updated = str(payload.get("updated") or "").strip()
        if updated:
            # updated 可能是 YYYY-MM-DD HH:MM:SS 或多種格式
            m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[\sT].*)?", updated)
            if m:
                return f"{int(m.group(1)):04d}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    except Exception:
        pass
    # 沒 boards updated：使用最後有效交易日
    try:
        resolved, _ = resolve_trade_date_with_fallback(date.today())
        return resolved.strftime("%Y/%m/%d")
    except Exception:
        return date.today().strftime("%Y/%m/%d")


def create_pages() -> list[PageSpec]:
    return [
        PageSpec(
            key="disposition",
            label="處置股",
            palette=VIEW_COLORS,
            tab_order=0,
            default_active=True,
            fetch_fn=_fetch_disposition,
            cli_columns=DISPOSITION_FIELDNAMES,
            html_columns=DISPOSITION_FIELDNAMES,
            cli_format_fn=disposition_page_format_cli,
        ),
        PageSpec(
            key="institutional",
            label="外資操作股",
            palette=INSTITUTIONAL_VIEW_COLORS,
            tab_order=1,
            default_active=False,
            fetch_fn=_fetch_institutional,
            cli_columns=INSTITUTIONAL_FIELDNAMES,
            html_columns=INSTITUTIONAL_FIELDNAMES,
            cli_format_fn=institutional_cli_format,
        ),
    ]


def resolve_trade_date_with_fallback(
    target_date: date,
    *,
    max_days_back: int = 45,
    explicitly_given: bool = False,
) -> tuple[date, bool]:
    """
    取得「最後一個有**盤後完整**交易資料的日期」：
    - 如果 explicitly_given=True（使用者指定 target_date）則不 fallback，直接回傳 (target_date, False)
    - 否則（target_date=今日自動模式）：
        (a) 若台灣時間 < 15:30（盤後三大法人尚未公告），今天一律跳過不當候選；
        (b) 從 (今日 or target_date) 開始往回找，跳過週六(5)/週日(6)，
            逐天用「MI_INDEX 或 TPEX_MI 有行情」+「T86 或 TPEX_INSTI 盤後至少有一筆真實資料」驗證；
        找到有資料的即回傳，最多回找 max_days_back 天；找不到則回傳 target_date。
    回傳 (date, used_fallback_flag)
    """
    if explicitly_given:
        return target_date, False
    today_actual = date.today()
    # 只允許 target_date == today_actual 做自動 fallback；若使用者手動指定其它日期，尊重原日期
    if target_date != today_actual:
        return target_date, False

    # 台灣時區：UTC+8
    try:
        from datetime import timezone, timedelta as td
        TZ_TW = timezone(td(hours=8))
        now_tw = datetime.now(TZ_TW)
    except Exception:
        now_tw = datetime.now()
    before_1530_tw = now_tw.hour * 60 + now_tw.minute < 15 * 60 + 30

    def _has_quote_and_insti(d: date) -> bool:
        try:
            # Step 1: 最小成本判斷 — MI_INDEX (JSON, ~幾 KB)，先確定是否是交易日 (非週末/休市)
            twse = fetch_twse_mi_index(d)
            tpex_mi = fetch_tpex_mi_pricing(d)
            if not twse and not tpex_mi:
                return False
            # Step 2: 確認盤後法人資料已公告 (抓 T86 上市 1300+ 支 就足夠代表「全體盤後」)
            #         TPEX 不抓，省 1 HTTP / 每次 fallback (上櫃 6000+ 列 HTML 慢)
            try:
                t86_rows, t86_dt = fetch_twse_t86(d)
            except Exception:
                t86_rows, t86_dt = {}, ""
            # 只要 T86 有真實 1+ 列 + data_date 非空 → 視為有效盤後日
            has_inst = bool(t86_dt and t86_rows)
            if has_inst:
                return True
            # 萬一 T86 異常但 TPEX 卻已經有資料 (少見) → 再 fallback 查 TPEX_INSTI
            try:
                tpx_rows, tpx_dt = fetch_tpex_daily_insti(d)
            except Exception:
                tpx_rows, tpx_dt = {}, ""
            return bool(tpx_dt and tpx_rows)
        except Exception:
            return False

    cur = target_date
    start_offset = 0
    # 台灣時間 < 15:30 且 candidate=今天 → 跳過今天（盤後 T86/TPEX 都還沒公告），直接從昨天開始找
    if before_1530_tw:
        start_offset = 1
    for offset in range(start_offset, max_days_back + 1):
        candidate = target_date - timedelta(days=offset)
        weekday = candidate.weekday()
        if weekday >= 5:  # 週六 5, 週日 6
            if _has_quote_and_insti(candidate):
                return candidate, True
            continue
        if _has_quote_and_insti(candidate):
            return candidate, offset > 0
    return cur, start_offset > 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(open_browser: bool = True) -> dict[str, Any]:
    pages = create_pages()
    page_results: dict[str, list[dict[str, Any]]] = {}
    page_errors: dict[str, list[str]] = {}

    for page in pages:
        if page.fetch_fn is None:
            page_results[page.key] = []
            continue
        try:
            rows = page.fetch_fn()
            page_results[page.key] = rows
        except Exception as exc:
            page_errors.setdefault(page.key, []).append(str(exc))
            page_results[page.key] = []

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    disposition_rows = page_results.get("disposition") or []
    institutional_rows = page_results.get("institutional") or []

    page_dates: dict[str, str] = {}
    try:
        page_dates["disposition"] = _resolve_disposition_data_date()
    except Exception:
        page_dates["disposition"] = date.today().strftime("%Y/%m/%d")
    try:
        page_dates["institutional"] = _resolve_institutional_data_date()
    except Exception:
        page_dates["institutional"] = date.today().strftime("%Y/%m/%d")

    save_html_multi(pages, page_results, generated_at, page_dates)
    notification_results = send_notifications(disposition_rows, generated_at)

    print(f"產生時間: {generated_at}")

    # 處置股
    disp_rows = page_results.get("disposition") or []
    disp_fmt = [disposition_page_format_cli(r) for r in disp_rows]
    print_cli_table("處置股", DISPOSITION_FIELDNAMES, disp_fmt, "今天沒有有期或有權的處置股。")

    # 外資操作股
    inst_rows = page_results.get("institutional") or []
    inst_fmt = [institutional_cli_format(r) for r in inst_rows]
    print_cli_table(
        "外資操作股 (期交所股票池 300+ 檔)",
        INSTITUTIONAL_FIELDNAMES,
        inst_fmt,
        "尚未有資料(請於 15:00 後再執行)。",
    )

    for key, errs in page_errors.items():
        label = next((p.label for p in pages if p.key == key), key)
        for msg in errs:
            print(f"[{label}] 警告: {msg}")

    print(f"HTML 檔案: {HTML_PATH.resolve()}")
    for r in notification_results:
        print(f"通知狀態: {r}")

    # 找最新「固定檔名」啟動頁與「內部跳轉頁」，現在全部都放在 OUTPUT_DIR
    _launcher_abs_internal: Path | None = None
    _launcher_abs_fixed: Path | None = None
    try:
        _fixed = OUTPUT_DIR / "LATEST_請按兩下這個開啟_最新版_不會有快取.html"
        _internal = OUTPUT_DIR / "_launcher_no_cache_internal.html"
        if _fixed.exists():
            _launcher_abs_fixed = _fixed.resolve()
        if _internal.exists():
            _launcher_abs_internal = _internal.resolve()
    except Exception:
        pass

    try:
        _hint_path = _launcher_abs_fixed or HTML_PATH.resolve()
        print(f"[開啟捷徑] 永遠按這個，不會有瀏覽器快取: {_hint_path}")
    except Exception:
        pass

    if open_browser:
        # 三層備援打開瀏覽器 (一定看得到)
        try:
            # ⚠️ 所有 API 一律傳「本機檔案路徑 / 單純 file:// URI (無 query string)」，
            #    避免 Windows Shell 解析 file:// + ?query 失敗（FileNotFound 卻以為成功）。
            #    query string 已寫在啟動頁 HTML 內部的 meta refresh / JS 裡，不用再在外面加。
            _open_target_path: Path | None = _launcher_abs_fixed or _launcher_abs_internal
            _open_target_uri: str | None = None
            if _open_target_path is not None:
                try:
                    _open_target_uri = _open_target_path.resolve().as_uri()  # 無 query，乾淨
                except Exception:
                    _open_target_uri = None
            _opened = False

            # 1) Windows 原生 ShellExecute(os.startfile)：最穩，一定會彈到前景 → 傳「本機路徑字串」
            if os.name == "nt" and _open_target_path is not None:
                try:
                    # os.startfile 只能吃真實檔案系統路徑 (含反斜線)，絕對不能吃 URI / query
                    os.startfile(str(_open_target_path))  # type: ignore[attr-defined]
                    _opened = True
                except Exception:
                    _opened = False
            # 2) cmd /c start：跟使用者在「執行」對話框貼上路徑一樣
            if not _opened and _open_target_path is not None:
                try:
                    import subprocess as _sp
                    rc = _sp.call(["cmd", "/c", "start", "", str(_open_target_path)], shell=False)
                    if rc == 0:
                        _opened = True
                except Exception:
                    _opened = False
            # 3) webbrowser：只有前面失敗才送，只送 open_new (open_new_tab 功能重複容易雙開) → 傳乾淨的 URI (無 query)
            if not _opened and _open_target_uri:
                try:
                    if webbrowser.open_new(_open_target_uri):
                        _opened = True
                except Exception:
                    pass
            # 最後手段：直接打實際 output HTML (也許會快取，但至少看得到東西)
            if not _opened:
                try:
                    webbrowser.open(HTML_PATH.resolve().as_uri())
                    _opened = True
                except Exception:
                    _opened = False
            if not _opened:
                print("[提示] 自動彈窗全部失敗，請直接雙擊 output/LATEST_請按兩下這個開啟_最新版_不會有快取.html」。")
        except Exception:
            pass
    return {
        "ok": True,
        "generated_at": generated_at,
        "page_dates": page_dates,
        "html_path": str(HTML_PATH.resolve()),
        "row_counts": {k: len(v or []) for k, v in page_results.items()},
    }


_REFRESH_LOCK = threading.Lock()


def run_server(port: int) -> None:
    """
    啟動本機 HTTP 伺服器 (stdlib http.server，不需 pip)：
      - GET / 或 GET /stockwarden_futures_or_options_stocks.html → 回傳最新產生的 HTML
      - POST /api/refresh → 重新執行 main() 爬最新資料 & 重寫 HTML，回傳 JSON {ok, generated_at, page_dates}
    """
    output_dir = OUTPUT_DIR.resolve()
    output_html = HTML_PATH.resolve()

    class StockwardenHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(output_dir), **kwargs)

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[server {ts}] {fmt % args}")

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            raw_path = (self.path or "").split("?", 1)[0].split("#", 1)[0]
            if raw_path in ("/", "/index.html"):
                raw_path = f"/{output_html.name}"
            self.path = raw_path
            # 每次要求 HTML 都不要快取 (避免點更新後拿到舊檔)
            path_obj = output_dir / raw_path.lstrip("/")
            if path_obj.resolve() == output_html and path_obj.exists():
                data = output_html.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.end_headers()
                self.wfile.write(data)
                return
            try:
                super().do_GET()
            except Exception:
                self._send_json(500, {"ok": False, "error": "read_failed"})

        def do_POST(self) -> None:
            raw_path = (self.path or "").split("?", 1)[0].split("#", 1)[0]
            if raw_path == "/api/refresh":
                if _REFRESH_LOCK.locked():
                    self._send_json(409, {"ok": False, "error": "refresh_already_running"})
                    return
                try:
                    with _REFRESH_LOCK:
                        print("\n[server] 收到更新請求，重新爬取最新資料…")
                        result = main(open_browser=False)
                    print(f"[server] 更新完成：{result.get('generated_at')}  dates={result.get('page_dates')}")
                    self._send_json(200, result)
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(404, {"ok": False, "error": "not_found", "path": raw_path})

    server = ThreadingHTTPServer(("127.0.0.1", port), StockwardenHandler)
    url = f"http://127.0.0.1:{port}/"
    print("\n" + "=" * 72)
    print(f"  StockWarden 本機伺服器已啟動 -> {url}")
    print(f"  輸出資料夾        -> {output_dir}")
    print(f"  關閉此視窗 / Ctrl+C 可停止伺服器")
    print(f"  頁面右上角「更新」按鈕可隨時重新抓最新資料")
    print("=" * 72 + "\n")
    # 第一次進入 server 模式 → 先爬一次最新的 → 再開瀏覽器
    try:
        main(open_browser=False)
    except Exception as exc:
        print(f"[server] 首次預爬失敗 (仍會啟動伺服器)：{exc}")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] 收到中斷，關閉伺服器。")
        server.server_close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="StockWarden：處置股 + 外資操作股，支援本機 server 模式 (頁面按鈕直接更新)。"
    )
    parser.add_argument(
        "--serve",
        metavar="PORT",
        type=int,
        nargs="?",
        const=8765,
        default=None,
        help="啟動本機 HTTP 伺服器 (預設 port=8765)，開啟頁面後右上角「更新」按鈕可直接重抓資料。",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="只產生 HTML，不自動開啟瀏覽器 (GitHub Actions / 排程用)。",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.serve is not None:
        run_server(int(args.serve))
    else:
        main(open_browser=not args.no_browser)
