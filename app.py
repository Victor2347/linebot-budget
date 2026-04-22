import os
import re
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
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
    "早餐": "餐飲", "午餐": "餐飲", "晚餐": "餐飲", "宵夜": "餐飲", "飲料": "餐飲", "咖啡": "餐飲",
    "交通": "交通", "捷運": "交通", "公車": "交通", "計程車": "交通", "油費": "交通",
    "超市": "購物", "便利商店": "購物", "衣服": "購物", "網購": "購物",
    "房租": "居家", "水電": "居家", "網路": "居家",
    "娛樂": "娛樂", "電影": "娛樂", "遊戲": "娛樂",
    "醫療": "醫療", "藥": "醫療",
}

def get_sheet():
    import json
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).sheet1

def parse_expense(text):
    text = text.strip()
    # 支援格式: "早餐 50", "早餐50", "早餐 50 麥當勞"
    pattern = r"^(.+?)\s*(\d+(?:\.\d+)?)\s*(.*)$"
    match = re.match(pattern, text)
    if not match:
        return None
    category_raw = match.group(1).strip()
    amount = float(match.group(2))
    note = match.group(3).strip()
    category = CATEGORIES.get(category_raw, category_raw)
    return {"category_raw": category_raw, "category": category, "amount": amount, "note": note}

def append_to_sheet(expense):
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    sheet = get_sheet()
    sheet.append_row([
        now.strftime("%Y/%m/%d"),
        now.strftime("%H:%M"),
        expense["category_raw"],
        expense["amount"],
        expense["note"],
    ])

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
    text = event.message.text
    expense = parse_expense(text)

    if expense:
        try:
            append_to_sheet(expense)
            reply = f"✅ 已記錄！\n📌 {expense['category_raw']}　💰 ${expense['amount']:.0f}"
            if expense["note"]:
                reply += f"\n📝 {expense['note']}"
        except Exception as e:
            reply = f"❌ 記錄失敗：{str(e)}"
    else:
        reply = (
            "💡 記帳格式：\n"
            "　類別 金額\n\n"
            "例如：\n"
            "　早餐 50\n"
            "　捷運 30\n"
            "　午餐 120 便當"
        )

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)]
            )
        )

@app.route("/", methods=["GET"])
def index():
    return "LINE Bot is running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
