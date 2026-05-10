import os
import re
import json
import threading
import time
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage,
    PushMessageRequest
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

CATEGORIES = {
    "早餐": "餐飲", "午餐": "餐飲", "晚餐": "餐飲", "宵夜": "餐飲",
    "飲料": "餐飲", "咖啡": "餐飲", "點心": "餐飲",
    "交通": "交通", "捷運": "交通", "公車": "交通", "計程車": "交通", "油費": "交通",
    "超市": "購物", "便利商店": "購物", "衣服": "購物", "網購": "購物",
    "房租": "居家", "水電": "居家", "網路": "居家",
    "娛樂": "娛樂", "電影": "娛樂", "遊戲": "娛樂",
    "醫療": "醫療", "藥": "醫療",
}

# 記錄每個用戶的對話狀態
user_states = {}

TZ = timezone(timedelta(hours=8))


def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).sheet1


def parse_expense(text):
    text = text.strip()
    pattern = r"^(.+?)\s*(\d+(?:\.\d+)?)\s*(.*)$"
    match = re.match(pattern, text)
    if not match:
        return None
    category_raw = match.group(1).strip()
    amount = float(match.group(2))
    note = match.group(3).strip()
    category = CATEGORIES.get(category_raw, category_raw)
    return {"category_raw": category_raw, "category": category, "amount": amount, "note": note}


def parse_date_input(text):
    """將用戶輸入的日期（如 4/22）轉為 YYYY/MM/DD 格式"""
    text = text.strip()
    now = datetime.now(TZ)
    match = re.match(r"^(\d{1,2})[/\-](\d{1,2})$", text)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        return f"{now.year}/{month:02d}/{day:02d}"
    # 支援直接輸入完整日期
    match2 = re.match(r"^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$", text)
    if match2:
        return f"{match2.group(1)}/{int(match2.group(2)):02d}/{int(match2.group(3)):02d}"
    return None


def append_to_sheet(expense):
    now = datetime.now(TZ)
    sheet = get_sheet()
    sheet.append_row([
        now.strftime("%Y/%m/%d"),
        now.strftime("%H:%M"),
        expense["category_raw"],
        expense["amount"],
        expense["note"],
    ])


def get_recent_records(n=5):
    sheet = get_sheet()
    rows = sheet.get_all_values()
    data_rows = rows[1:] if len(rows) > 1 else []
    return data_rows[-n:] if len(data_rows) >= n else data_rows


def get_records_by_date(date_str):
    """回傳指定日期的所有記錄，含原始 row index（1-based, 含標題列）"""
    sheet = get_sheet()
    rows = sheet.get_all_values()
    results = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) >= 4 and row[0] == date_str:
            results.append({"row_index": i, "row": row})
    return results


def get_monthly_summary():
    now = datetime.now(TZ)
    month_prefix = now.strftime("%Y/%m")
    sheet = get_sheet()
    rows = sheet.get_all_values()
    totals = {}
    grand_total = 0
    for row in rows[1:]:
        if len(row) >= 4 and row[0].startswith(month_prefix):
            try:
                amount = float(row[3])
                category = row[2]
                totals[category] = totals.get(category, 0) + amount
                grand_total += amount
            except ValueError:
                pass
    return totals, grand_total, now.strftime("%Y年%m月")


def delete_row(row_index):
    sheet = get_sheet()
    sheet.delete_rows(row_index)


def update_row(row_index, category, amount, note):
    sheet = get_sheet()
    now = datetime.now(TZ)
    sheet.update(f"C{row_index}:E{row_index}", [[category, amount, note]])


def format_records_list(records):
    lines = []
    for i, r in enumerate(records, start=1):
        row = r["row"]
        note = row[4] if len(row) > 4 and row[4] else ""
        note_str = f" ({note})" if note else ""
        lines.append(f"{i}. {row[1]} {row[2]} ${float(row[3]):.0f}{note_str}")
    return "\n".join(lines)


def reply(event, text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=text)]
            )
        )


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.get(user_id, {})
    current_step = state.get("step")

    # ── 刪除流程 ──
    if text == "刪除記錄":
        user_states[user_id] = {"step": "delete_ask_date"}
        reply(event, "🗑️ 請輸入要刪除的日期\n例如：4/22")
        return

    if current_step == "delete_ask_date":
        date_str = parse_date_input(text)
        if not date_str:
            reply(event, "❌ 日期格式不正確\n請輸入如：4/22 或 2026/04/22")
            return
        records = get_records_by_date(date_str)
        if not records:
            user_states.pop(user_id, None)
            reply(event, f"❌ {date_str} 沒有任何記錄")
            return
        user_states[user_id] = {"step": "delete_select", "records": records, "date": date_str}
        lines = format_records_list(records)
        reply(event, f"📋 {date_str} 的記錄：\n{lines}\n\n輸入編號刪除（例如：1）\n輸入「取消」返回")
        return

    if current_step == "delete_select":
        if text == "取消":
            user_states.pop(user_id, None)
            reply(event, "已取消")
            return
        records = state["records"]
        if text.isdigit() and 1 <= int(text) <= len(records):
            target = records[int(text) - 1]
            row = target["row"]
            try:
                delete_row(target["row_index"])
                user_states.pop(user_id, None)
                reply(event, f"✅ 已刪除：{row[0]} {row[2]} ${float(row[3]):.0f}")
            except Exception as e:
                reply(event, f"❌ 刪除失敗：{str(e)}")
        else:
            reply(event, f"請輸入 1 到 {len(records)} 的數字，或輸入「取消」")
        return

    # ── 修改流程 ──
    if text == "修改記錄":
        user_states[user_id] = {"step": "modify_ask_date"}
        reply(event, "✏️ 請輸入要修改的日期\n例如：4/22")
        return

    if current_step == "modify_ask_date":
        date_str = parse_date_input(text)
        if not date_str:
            reply(event, "❌ 日期格式不正確\n請輸入如：4/22")
            return
        records = get_records_by_date(date_str)
        if not records:
            user_states.pop(user_id, None)
            reply(event, f"❌ {date_str} 沒有任何記錄")
            return
        user_states[user_id] = {"step": "modify_select", "records": records, "date": date_str}
        lines = format_records_list(records)
        reply(event, f"📋 {date_str} 的記錄：\n{lines}\n\n輸入編號修改（例如：1）\n輸入「取消」返回")
        return

    if current_step == "modify_select":
        if text == "取消":
            user_states.pop(user_id, None)
            reply(event, "已取消")
            return
        records = state["records"]
        if text.isdigit() and 1 <= int(text) <= len(records):
            target = records[int(text) - 1]
            user_states[user_id] = {"step": "modify_input", "target": target}
            row = target["row"]
            reply(event, f"目前：{row[2]} ${float(row[3]):.0f}\n\n請輸入新內容（格式同記帳）\n例如：早餐 80 麥當勞")
        else:
            reply(event, f"請輸入 1 到 {len(records)} 的數字，或輸入「取消」")
        return

    if current_step == "modify_input":
        if text == "取消":
            user_states.pop(user_id, None)
            reply(event, "已取消")
            return
        expense = parse_expense(text)
        if not expense:
            reply(event, "❌ 格式不正確\n例如：早餐 80 麥當勞")
            return
        target = state["target"]
        try:
            update_row(target["row_index"], expense["category_raw"], expense["amount"], expense["note"])
            user_states.pop(user_id, None)
            reply(event, f"✅ 已修改為：{expense['category_raw']} ${expense['amount']:.0f}")
        except Exception as e:
            reply(event, f"❌ 修改失敗：{str(e)}")
        return

    # ── 查詢記錄 ──
    if text == "查詢記錄":
        try:
            records = get_recent_records(5)
            if not records:
                reply(event, "📋 目前沒有任何記錄")
                return
            lines = []
            for row in records:
                note = row[4] if len(row) > 4 and row[4] else ""
                note_str = f" ({note})" if note else ""
                lines.append(f"{row[0]} {row[2]} ${float(row[3]):.0f}{note_str}")
            reply(event, "📋 最近 5 筆記錄：\n" + "\n".join(lines))
        except Exception as e:
            reply(event, f"❌ 查詢失敗：{str(e)}")
        return

    # ── 本月統計 ──
    if text == "本月統計":
        try:
            totals, grand_total, month_label = get_monthly_summary()
            if not totals:
                reply(event, f"📊 {month_label} 尚無記錄")
                return
            sorted_totals = sorted(totals.items(), key=lambda x: -x[1])
            lines = [f"📊 {month_label} 統計："]
            for cat, amt in sorted_totals:
                lines.append(f"  {cat}：${amt:.0f}")
            lines.append(f"\n💰 總計：${grand_total:.0f}")
            reply(event, "\n".join(lines))
        except Exception as e:
            reply(event, f"❌ 統計失敗：{str(e)}")
        return

    # ── 新增記帳（點選單觸發） ──
    if text == "新增記帳":
        reply(event, "💡 請直接輸入記帳內容\n格式：類別 金額\n\n例如：\n  早餐 50\n  捷運 30\n  午餐 120 便當")
        return

    # ── 使用說明 ──
    if text == "使用說明":
        reply(event, (
            "📖 使用說明\n\n"
            "【記帳】直接輸入：\n"
            "  早餐 50\n"
            "  午餐 120 便當\n"
            "  捷運 30\n\n"
            "【選單功能】\n"
            "  新增記帳 ➜ 提示記帳格式\n"
            "  查詢記錄 ➜ 最近 5 筆\n"
            "  本月統計 ➜ 各類別加總\n"
            "  修改記錄 ➜ 依日期選擇修改\n"
            "  刪除記錄 ➜ 依日期選擇刪除"
        ))
        return

    # ── 一般記帳（直接輸入） ──
    user_states.pop(user_id, None)
    expense = parse_expense(text)
    if expense:
        try:
            append_to_sheet(expense)
            rep = f"✅ 已記錄！\n📌 {expense['category_raw']}　💰 ${expense['amount']:.0f}"
            if expense["note"]:
                rep += f"\n📝 {expense['note']}"
            reply(event, rep)
        except Exception as e:
            reply(event, f"❌ 記錄失敗：{str(e)}")
    else:
        reply(event, (
            "💡 記帳格式：類別 金額\n\n"
            "例如：\n"
            "  早餐 50\n"
            "  捷運 30\n"
            "  午餐 120 便當"
        ))


@app.route("/", methods=["GET"])
def index():
    return "LINE Bot is running!"


def keep_alive():
    """每 5 分鐘 ping 自己，防止 Render 休眠"""
    time.sleep(60)  # 等待啟動完成
    while True:
        try:
            url = os.environ.get("SELF_URL", "")
            if url:
                requests.get(url, timeout=10)
        except Exception:
            pass
        time.sleep(5 * 60)  # 每 5 分鐘


def send_startup_notification():
    """啟動時傳 LINE 通知給主人"""
    time.sleep(5)  # 等待 Flask 完全啟動
    owner_id = os.environ.get("OWNER_LINE_ID", "")
    if not owner_id:
        return
    try:
        tz = timezone(timedelta(hours=8))
        now = datetime.now(tz).strftime("%H:%M")
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=owner_id,
                    messages=[TextMessage(text=f"🤖 記帳 Bot 已啟動！\n⏰ 時間：{now}\n\n現在可以正常使用了 ✅")]
                )
            )
    except Exception:
        pass


# 啟動背景執行緒
threading.Thread(target=keep_alive, daemon=True).start()
threading.Thread(target=send_startup_notification, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
