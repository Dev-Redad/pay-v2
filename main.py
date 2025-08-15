# bot.py — Mongo-backed (PTB 13.15) + Channel-selling add-on
# - Media selling unchanged
# - Channel selling now sends a **request-to-join** link (creates_join_request=True)
# - Auto-approves join requests only for users who paid (c_orders)
# - PhonePe Business parsing; unique amount locks; configurable unpaid-QR cleanup
# - Safety patch: delivery messages wrapped to avoid crashes if user hasn’t opened DM

import os, logging, time, random, re, unicodedata
from datetime import datetime, timedelta
from urllib.parse import quote

from telegram import Update, ParseMode, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters, CallbackContext,
    ConversationHandler, CallbackQueryHandler, ChatJoinRequestHandler
)
from telegram.error import BadRequest, Unauthorized

from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError

logging.basicConfig(format="%(asctime)s %(levelname)s:%(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("upi-mongo-bot")

TOKEN = "7303696543:AAHLYJyc-dD4OJOilv2dlgAspyYGgWAmNYQ"
ADMIN_IDS = [7223414109, 6053105336, 7381642564]

STORAGE_CHANNEL_ID = -1002724249292
PAYMENT_NOTIF_CHANNEL_ID = -1002865174188

UPI_ID = "q196108861@ybl"
UPI_PAYEE_NAME = "Seller"

PAY_WINDOW_MINUTES = 5
GRACE_SECONDS = 10
DELETE_AFTER_MINUTES = 10

PROTECT_CONTENT_ENABLED = False
FORCE_SUBSCRIBE_ENABLED = True
FORCE_SUBSCRIBE_CHANNEL_IDS = []

MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb+srv://Hui:Hui@cluster0.3lpdrgm.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
)
mdb = MongoClient(MONGO_URI)["upi_bot"]

c_users    = mdb["users"]
c_products = mdb["products"]
c_config   = mdb["config"]
c_sessions = mdb["sessions"]
c_locks    = mdb["locks"]
c_paylog   = mdb["payments"]
c_orders   = mdb["orders"]

c_users.create_index([("user_id", ASCENDING)], unique=True)
c_products.create_index([("item_id", ASCENDING)], unique=True)
c_config.create_index([("key", ASCENDING)], unique=True)
c_locks.create_index([("amount_key", ASCENDING)], unique=True)
c_locks.create_index([("hard_expire_at", ASCENDING)], expireAfterSeconds=0)
c_sessions.create_index([("key", ASCENDING)], unique=True)
c_sessions.create_index([("amount_key", ASCENDING)])
c_sessions.create_index([("hard_expire_at", ASCENDING)], expireAfterSeconds=0)
c_paylog.create_index([("ts", ASCENDING)])
c_orders.create_index([("user_id", ASCENDING), ("channel_id", ASCENDING)], unique=True)

def cfg(key, default=None):
    doc = c_config.find_one({"key": key})
    return doc["value"] if doc and "value" in doc else default

def set_cfg(key, value):
    c_config.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)

def amount_key(x: float) -> str:
    return f"{x:.2f}" if abs(x - int(x)) > 1e-9 else str(int(x))

def build_upi_uri(amount: float, note: str):
    amt = f"{int(amount)}" if abs(amount-int(amount))<1e-9 else f"{amount:.2f}"
    pa = quote(UPI_ID, safe=''); pn = quote(UPI_PAYEE_NAME, safe=''); tn = quote(note, safe='')
    return f"upi://pay?pa={pa}&pn={pn}&am={amt}&cu=INR&tn={tn}"

def qr_url(data: str):
    return f"https://api.qrserver.com/v1/create-qr-code/?data={quote(data, safe='')}&size=512x512&qzone=2"

def add_user(uid, uname): c_users.update_one({"user_id": uid},{"$set":{"username":uname or ""}},upsert=True)
def get_all_user_ids(): return list(c_users.distinct("user_id"))

def reserve_amount_key(k: str, hard_expire_at: datetime) -> bool:
    try:
        c_locks.insert_one({"amount_key": k,"hard_expire_at": hard_expire_at,"created_at": datetime.utcnow()})
        return True
    except DuplicateKeyError:
        return False
def release_amount_key(k: str): c_locks.delete_one({"amount_key": k})

def pick_unique_amount(lo: float, hi: float, hard_expire_at: datetime) -> float:
    lo, hi = int(lo), int(hi); ints = list(range(lo, hi+1)); random.shuffle(ints)
    for v in ints:
        if reserve_amount_key(str(v), hard_expire_at): return float(v)
    for base in ints:
        for p in range(1,100):
            key = f"{base}.{p:02d}"
            if reserve_amount_key(key, hard_expire_at): return float(f"{base}.{p:02d}")
    return float(ints[-1])

def _normalize_digits(s: str) -> str:
    out=[]
    for ch in s:
        if unicodedata.category(ch).startswith('M'):
            continue
        if ch.isdigit():
            try: out.append(str(unicodedata.digit(ch))); continue
            except Exception: pass
        out.append(ch)
    return "".join(out)

PHONEPE_RE = re.compile(
    r"(?:received\s*rs|money\s*received|you['’]ve\s*received\s*rs)\s*[.:₹\s]*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.I | re.S
)
def parse_phonepe_amount(text: str):
    norm = _normalize_digits(text or "")
    m = PHONEPE_RE.search(norm)
    if not m: return None
    try: return float(m.group(1).replace(",",""))
    except: return None

def force_subscribe(fn):
    def wrapper(update: Update, context: CallbackContext, *a, **k):
        if (not FORCE_SUBSCRIBE_ENABLED) or (not FORCE_SUBSCRIBE_CHANNEL_IDS) or (update.effective_user.id in ADMIN_IDS):
            return fn(update, context, *a, **k)
        uid = update.effective_user.id
        need=[]
        for ch in FORCE_SUBSCRIBE_CHANNEL_IDS:
            try:
                st = context.bot.get_chat_member(ch, uid).status
                if st not in ("member","administrator","creator"): need.append(ch)
            except: need.append(ch)
        if not need: return fn(update, context, *a, **k)
        context.user_data['pending_command']={'fn':fn,'update':update}
        btns=[]
        for ch in need:
            try:
                chat=context.bot.get_chat(ch)
                link=chat.invite_link or context.bot.export_chat_invite_link(ch)
                btns.append([InlineKeyboardButton(f"Join {chat.title}", url=link)])
            except Exception as e: log.warning(f"Invite link fail {ch}: {e}")
        btns.append([InlineKeyboardButton("✅ I have joined", callback_data="check_join")])
        msg = cfg("force_sub_text","Join required channels to continue.")
        photo = cfg("force_sub_photo_id")
        if photo: update.effective_message.reply_photo(photo=photo, caption=msg, reply_markup=InlineKeyboardMarkup(btns))
        else: update.effective_message.reply_text(msg, reply_markup=InlineKeyboardMarkup(btns))
    return wrapper

def check_join_cb(update: Update, context: CallbackContext):
    q=update.callback_query; uid=q.from_user.id; need=[]
    for ch in FORCE_SUBSCRIBE_CHANNEL_IDS:
        try:
            st=context.bot.get_chat_member(ch, uid).status
            if st not in ("member","administrator","creator"): need.append(ch)
        except: need.append(ch)
    if not need:
        try: q.message.delete()
        except: pass
        q.answer("Thank you!", show_alert=True)
        pend = context.user_data.pop('pending_command', None)
        if pend: return pend['fn'](pend['update'], context)
    else: q.answer("Still not joined all.", show_alert=True)

def _auto_delete_messages(context: CallbackContext):
    data = context.job.context
    chat_id = data["chat_id"]
    ids = data["message_ids"]
    for mid in ids:
        try: context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception: pass

def _delete_unpaid_qr(context: CallbackContext):
    data = context.job.context
    if c_sessions.find_one({"key": data["sess_key"]}):
        try: context.bot.delete_message(chat_id=data["chat_id"], message_id=data["qr_message_id"])
        except Exception: pass

def start_purchase(ctx: CallbackContext, chat_id: int, uid: int, item_id: str):
    prod = c_products.find_one({"item_id": item_id})
    if not prod: return ctx.bot.send_message(chat_id, "❌ Item not found.")
    mn, mx = prod.get("min_price"), prod.get("max_price")
    if mn is None or mx is None:
        v=float(prod.get("price",0))
        if v<=0: return ctx.bot.send_message(chat_id,"❌ Price not set.")
        mn=mx=v

    created = datetime.utcnow()
    hard_expire_at = created + timedelta(minutes=PAY_WINDOW_MINUTES, seconds=GRACE_SECONDS)
    amt = pick_unique_amount(mn, mx, hard_expire_at); akey = amount_key(amt)

    uri = build_upi_uri(amt, f"order_uid_{uid}")
    img = qr_url(uri)
    display_amt = int(amt) if abs(amt-int(amt))<1e-9 else f"{amt:.2f}"
    caption = (
         f"Pay ₹{display_amt} for the item\n\n"
    f"Upi id - `{UPI_ID}`.\n\n"
    "Instructions:\n"
    "• Scan this QR or copy the UPI ID\n"
    f"• Pay exactly ₹{display_amt} within {PAY_WINDOW_MINUTES} minutes\n"
    "Verification is automatic. Delivery right after payment."
    )
    sent = ctx.bot.send_photo(chat_id=chat_id, photo=img, caption=caption, parse_mode=ParseMode.MARKDOWN)

    sess_key = f"{uid}:{item_id}:{int(time.time())}"
    c_sessions.insert_one({
        "key": sess_key,
        "user_id": uid,
        "chat_id": chat_id,
        "item_id": item_id,
        "amount": float(amt),
        "amount_key": akey,
        "created_at": created,
        "hard_expire_at": hard_expire_at,
        "qr_message_id": sent.message_id,
    })

    qr_timeout_mins = int(cfg("qr_unpaid_delete_minutes", PAY_WINDOW_MINUTES))
    ctx.job_queue.run_once(
        _delete_unpaid_qr,
        timedelta(minutes=qr_timeout_mins, seconds=1),
        context={"sess_key": sess_key, "chat_id": chat_id, "qr_message_id": sent.message_id},
        name=f"qr_expire_{uid}_{int(time.time())}"
    )

def deliver(ctx: CallbackContext, uid: int, item_id: str, return_ids: bool = False):
    """
    Deliver product:
      - Files: copy messages + warning (warning not deleted)
      - Channel: create a **request-to-join** invite link and DM it
    """
    prod = c_products.find_one({"item_id": item_id})
    if not prod:
        try: ctx.bot.send_message(uid, "❌ Item missing.")
        except Exception as e: log.error(f"Notify missing item failed (to {uid}): {e}")
        return [] if return_ids else None

    # --- Channel product path ---
    if "channel_id" in prod:
        ch_id = prod["channel_id"]
        link = None
        try:
            # Always create a fresh JOIN-REQUEST link (not a normal invite)
            cil = ctx.bot.create_chat_invite_link(ch_id, creates_join_request=True)
            link = cil.invite_link
        except Exception as e:
            log.warning(f"Create join-request link failed for {ch_id}: {e}")
            # Do NOT fall back to normal link (as requested)
            try:
                ctx.bot.send_message(uid, "⚠️ Channel link is temporarily unavailable. Please try again in a moment.")
            except Exception as ee:
                log.error(f"Notify link-missing failed (to {uid}): {ee}")
            return [] if return_ids else None

        try:
            m = ctx.bot.send_message(
                uid,
                f"🔗 Request-to-join link:\n{link}\n\nTap *Request*, and I’ll auto-approve you for this account.",
                parse_mode=ParseMode.MARKDOWN
            )
            return [m.message_id] if return_ids else None
        except Exception as e:
            log.error(f"Send channel link failed (to {uid}): {e}")
            return [] if return_ids else None

    # --- Files product path (original behavior) ---
    file_msg_ids = []
    for f in prod.get("files", []):
        try:
            m = ctx.bot.copy_message(
                chat_id=uid,
                from_chat_id=f["channel_id"],
                message_id=f["message_id"],
                protect_content=PROTECT_CONTENT_ENABLED
            )
            file_msg_ids.append(m.message_id)
            time.sleep(0.35)
        except Exception as e:
            log.error(f"Deliver fail (to {uid} from {f.get('channel_id')}): {e}")

    try:
        ctx.bot.send_message(uid, "⚠️ Files auto-delete here in 10 minutes. Save now.")
    except Exception as e:
        log.error(f"Warn send fail (to {uid}): {e}")

    return file_msg_ids if return_ids else None

# ---- Admin: QR timeout config ----
def qr_timeout_show(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS: return
    mins = cfg("qr_unpaid_delete_minutes", PAY_WINDOW_MINUTES)
    update.message.reply_text(f"QR auto-delete if unpaid: {mins} minutes.")

def set_qr_timeout(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS: return
    if not context.args:
        return update.message.reply_text("Usage: /set_qr_timeout <minutes>")
    try:
        mins = int(float(context.args[0]))
        if mins < 1 or mins > 180:
            return update.message.reply_text("Choose 1–180 minutes.")
    except Exception:
        return update.message.reply_text("Invalid number. Example: /set_qr_timeout 5")
    set_cfg("qr_unpaid_delete_minutes", mins)
    update.message.reply_text(f"QR auto-delete timeout set to {mins} minutes.")

# ---- Product add (files) ----
GET_PRODUCT_FILES, PRICE, GET_BROADCAST_FILES, GET_BROADCAST_TEXT, BROADCAST_CONFIRM = range(5)

def add_product_start(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS: return
    context.user_data['new_files']=[]
    if update.message.effective_attachment:
        try:
            fwd=context.bot.forward_message(STORAGE_CHANNEL_ID, update.message.chat_id, update.message.message_id)
            context.user_data['new_files'].append({"channel_id": fwd.chat_id,"message_id": fwd.message_id})
            update.message.reply_text("✅ First file added. Send more or /done.")
        except Exception as e:
            log.error(f"Store fail on first file: {e}"); update.message.reply_text("Failed to store first file.")
    else:
        update.message.reply_text("Send product files now. Use /done when finished.")
    return GET_PRODUCT_FILES

def get_product_files(update: Update, context: CallbackContext):
    if not update.message.effective_attachment:
        update.message.reply_text("Not a file. Send again or /done."); return GET_PRODUCT_FILES
    try:
        fwd=context.bot.forward_message(STORAGE_CHANNEL_ID, update.message.chat_id, update.message.message_id)
        context.user_data['new_files'].append({"channel_id": fwd.chat_id,"message_id": fwd.message_id})
        update.message.reply_text("✅ Added. Send more or /done."); return GET_PRODUCT_FILES
    except Exception as e:
        log.error(e); update.message.reply_text("Store failed."); return ConversationHandler.END

def finish_adding_files(update: Update, context: CallbackContext):
    if not context.user_data.get('new_files'):
        update.message.reply_text("No files yet. Send one or /cancel."); return GET_PRODUCT_FILES
    update.message.reply_text("Now send price or range (10 or 10-30)."); return PRICE

# ---- Product add (channel) ----
CHANNEL_REF_RE = re.compile(r"^\s*(?:-100\d{5,}|@[\w\d_]{5,}|https?://t\.me/[\w\d_+/]+)\s*$")

def _get_bot_id(context: CallbackContext) -> int:
    bid = context.bot_data.get("__bot_id__")
    if bid: return bid
    me = context.bot.get_me(); context.bot_data["__bot_id__"] = me.id
    return me.id

def _resolve_channel(context: CallbackContext, ref: str):
    ref = ref.strip()
    if ref.startswith("-100") and ref[4:].isdigit():
        chat = context.bot.get_chat(int(ref))
    else:
        key = re.search(r"t\.me/([^/?\s]+)", ref).group(1) if ref.startswith("http") else ref
        chat = context.bot.get_chat(key)
    return chat.id

def _bot_is_admin(context: CallbackContext, chat_id: int) -> bool:
    try:
        bot_id = _get_bot_id(context)
        st = context.bot.get_chat_member(chat_id, bot_id).status
        return st in ("administrator","creator")
    except Exception as e:
        log.info(f"Admin check failed for {chat_id}: {e}")
        return False

def add_channel_start(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS: return
    text = (update.message.text or "").strip()
    if not CHANNEL_REF_RE.match(text): return
    try:
        ch_id = _resolve_channel(context, text)
    except (BadRequest, Unauthorized) as e:
        update.message.reply_text(f"❌ I couldn't access that channel: {e}"); return
    if not _bot_is_admin(context, ch_id):
        update.message.reply_text("❌ I'm not an admin there. Add me and try again."); return
    context.user_data.clear()
    context.user_data["channel_id"] = ch_id
    update.message.reply_text("Channel recognized. Now send price or range (10 or 10-30).")
    return PRICE

def get_price(update: Update, context: CallbackContext):
    t=update.message.text.strip()
    try:
        if "-" in t: a,b=t.split("-",1); mn,mx=float(a),float(b); assert mn>0 and mx>=mn
        else: v=float(t); assert v>0; mn=mx=v
    except:
        update.message.reply_text("Invalid. Send like 10 or 10-30."); return PRICE

    item_id=f"item_{int(time.time())}"
    if "channel_id" in context.user_data:
        doc={"item_id": item_id,"min_price": mn,"max_price": mx,"channel_id": int(context.user_data["channel_id"])}
        if mn==mx: doc["price"]=mn
        c_products.insert_one(doc)
        link=f"https://t.me/{context.bot.username}?start={item_id}"
        update.message.reply_text(f"✅ Channel product added.\nLink:\n`{link}`", parse_mode=ParseMode.MARKDOWN)
        context.user_data.clear(); return ConversationHandler.END

    if not context.user_data.get('new_files'):
        update.message.reply_text("No files yet. Send a file or /cancel."); return PRICE
    doc={"item_id":item_id,"min_price":mn,"max_price":mx,"files":context.user_data['new_files']}
    if mn==mx: doc["price"]=mn
    c_products.insert_one(doc)
    link=f"https://t.me/{context.bot.username}?start={item_id}"
    update.message.reply_text(f"✅ Product added.\nLink:\n`{link}`", parse_mode=ParseMode.MARKDOWN)
    context.user_data.clear(); return ConversationHandler.END

def cancel_conv(update: Update, context: CallbackContext):
    context.user_data.clear(); update.message.reply_text("Canceled."); return ConversationHandler.END

# ---- Broadcast (unchanged) ----
def bc_start(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS: return
    context.user_data['b_files']=[]; context.user_data['b_text']=None
    update.message.reply_text("Send files for broadcast. /done when finished."); return GET_BROADCAST_FILES
def bc_files(update, context):
    if update.message.effective_attachment:
        context.user_data['b_files'].append(update.message); update.message.reply_text("File added. /done when finished.")
    else: update.message.reply_text("Send a file or /done.")
    return GET_BROADCAST_FILES
def bc_done_files(update, context): update.message.reply_text("Now send the text (or /skip)."); return GET_BROADCAST_TEXT
def bc_text(update, context): context.user_data['b_text']=update.message.text; return bc_confirm(update, context)
def bc_skip(update, context): return bc_confirm(update, context)
def bc_confirm(update, context):
    total=c_users.count_documents({})
    buttons=[[InlineKeyboardButton("✅ Send", callback_data="send_bc")],[InlineKeyboardButton("❌ Cancel", callback_data="cancel_bc")]]
    update.message.reply_text(f"Broadcast to {total} users. Proceed?", reply_markup=InlineKeyboardMarkup(buttons))
    return BROADCAST_CONFIRM
def bc_send(update, context):
    q=update.callback_query; q.answer(); q.edit_message_text("Broadcasting…")
    files=context.user_data.get('b_files',[]); text=context.user_data.get('b_text'); ok=fail=0
    for uid in get_all_user_ids():
        try:
            for m in files: context.bot.copy_message(uid, m.chat_id, m.message_id); time.sleep(0.1)
            if text: context.bot.send_message(uid, text)
            ok+=1
        except Exception as e: log.error(e); fail+=1
    q.message.reply_text(f"Done. Sent:{ok} Fail:{fail}"); context.user_data.clear(); return ConversationHandler.END

def on_cb(update: Update, context: CallbackContext):
    q=update.callback_query; q.answer()
    if q.data=="check_join": return check_join_cb(update, context)

# ---- Payment sniffer (PhonePe Business) ----
def on_channel_post(update: Update, context: CallbackContext):
    msg=update.channel_post
    if not msg or msg.chat_id!=PAYMENT_NOTIF_CHANNEL_ID: return
    text = msg.text or msg.caption or ""; low=text.lower()
    if ("phonepe business" not in low) or (("received rs" not in low and "money received" not in low)): return
    amt = parse_phonepe_amount(text)
    if amt is None: return

    ts = (msg.date or datetime.utcnow()).replace(tzinfo=None); akey = amount_key(amt)
    try: c_paylog.insert_one({"key": akey, "ts": ts, "raw": text[:500]})
    except: pass

    matches=list(c_sessions.find({"amount_key": akey, "created_at":{"$lte":ts}, "hard_expire_at":{"$gte":ts}}))
    for s in matches:
        qr_mid = s.get("qr_message_id")
        if qr_mid:
            try: context.bot.delete_message(chat_id=s["chat_id"], message_id=qr_mid)
            except Exception as e: log.debug(f"Delete QR failed: {e}")

        try:
            confirm_msg = context.bot.send_message(s["chat_id"], "✅ Payment received. Delivering your item…")
            confirm_msg_id = confirm_msg.message_id
        except Exception as e:
            log.warning(f"Notify user fail: {e}"); confirm_msg_id = None

        ids_to_delete = []
        if confirm_msg_id: ids_to_delete.append(confirm_msg_id)

        deliver_ids = deliver(context, s["user_id"], s["item_id"], return_ids=True)
        ids_to_delete.extend(deliver_ids or [])

        prod = c_products.find_one({"item_id": s["item_id"]}) or {}
        if "channel_id" in prod:
            try:
                c_orders.update_one(
                    {"user_id": s["user_id"], "channel_id": int(prod["channel_id"])},
                    {"$set": {"item_id": s["item_id"], "paid_at": ts, "status": "paid"}},
                    upsert=True
                )
            except Exception as e: log.error(f"Order upsert failed: {e}")

        if ids_to_delete:
            context.job_queue.run_once(
                _auto_delete_messages,
                timedelta(minutes=DELETE_AFTER_MINUTES),
                context={"chat_id": s["chat_id"], "message_ids": ids_to_delete},
                name=f"del_{s['user_id']}_{int(time.time())}"
            )

        c_sessions.delete_one({"_id": s["_id"]}); release_amount_key(akey)

# ---- Auto-approve join-requests for paid buyers ----
def on_join_request(update: Update, context: CallbackContext):
    req = update.chat_join_request
    if not req: return
    uid = req.from_user.id
    ch_id = req.chat.id
    has_access = c_orders.find_one({"user_id": uid, "channel_id": ch_id})
    if has_access:
        try: context.bot.approve_chat_join_request(ch_id, uid)
        except Exception as e: log.error(f"Approve join failed: {e}")

def stats(update, context):
    users=c_users.count_documents({}); sessions=c_sessions.count_documents({})
    update.message.reply_text(f"Users: {users}\nPending sessions: {sessions}")

def protect_on(update, context):
    global PROTECT_CONTENT_ENABLED; PROTECT_CONTENT_ENABLED=True
    update.message.reply_text("Content protection ON.")
def protect_off(update, context):
    global PROTECT_CONTENT_ENABLED; PROTECT_CONTENT_ENABLED=False
    update.message.reply_text("Content protection OFF.")

def cmd_start(update: Update, context: CallbackContext):
    uid=update.effective_user.id; add_user(uid, update.effective_user.username)
    msg = update.message or (update.callback_query and update.callback_query.message)
    chat_id = msg.chat_id
    if context.args:
        return start_purchase(context, chat_id, uid, context.args[0])
    photo = cfg("welcome_photo_id"); text = cfg("welcome_text","Welcome!")
    (msg.reply_photo(photo=photo, caption=text) if photo else msg.reply_text(text))

def main():
    set_cfg("welcome_text", cfg("welcome_text","Welcome!"))
    set_cfg("force_sub_text", cfg("force_sub_text","Join required channels to continue."))
    if cfg("qr_unpaid_delete_minutes") is None:
        set_cfg("qr_unpaid_delete_minutes", PAY_WINDOW_MINUTES)

    os.system(f'curl -s "https://api.telegram.org/bot{TOKEN}/deleteWebhook" >/dev/null')

    updater=Updater(TOKEN, use_context=True)
    dp=updater.dispatcher
    admin=Filters.user(ADMIN_IDS)

    # Files product flow
    add_conv=ConversationHandler(
        entry_points=[MessageHandler((Filters.document|Filters.video|Filters.photo)&admin, add_product_start)],
        states={
            GET_PRODUCT_FILES:[MessageHandler((Filters.document|Filters.video|Filters.photo)&~Filters.command, get_product_files),
                               CommandHandler('done', finish_adding_files, filters=admin)],
            PRICE:[MessageHandler(Filters.text & ~Filters.command, get_price)]
        },
        fallbacks=[CommandHandler('cancel', cancel_conv, filters=admin)]
    )

    # Channel product flow — trigger only on channel reference
    add_channel_conv=ConversationHandler(
        entry_points=[MessageHandler(Filters.regex(CHANNEL_REF_RE) & ~Filters.command & admin, add_channel_start)],
        states={ PRICE:[MessageHandler(Filters.text & ~Filters.command, get_price)] },
        fallbacks=[CommandHandler('cancel', cancel_conv, filters=admin)],
        name="add_channel_conv",
        persistent=False
    )

    dp.add_handler(add_conv, group=0)
    dp.add_handler(add_channel_conv, group=0)

    # Broadcast & misc
    dp.add_handler(CommandHandler("broadcast", bc_start, filters=admin))
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("stats", stats, filters=admin))
    dp.add_handler(CommandHandler("qr_timeout", qr_timeout_show, filters=admin))
    dp.add_handler(CommandHandler("set_qr_timeout", set_qr_timeout, filters=admin))
    dp.add_handler(CommandHandler("protect_on", protect_on, filters=admin))
    dp.add_handler(CommandHandler("protect_off", protect_off, filters=admin))
    dp.add_handler(CallbackQueryHandler(on_cb, pattern="^(check_join)$"))

    # Payments + join requests
    dp.add_handler(MessageHandler(Filters.update.channel_post & Filters.chat(PAYMENT_NOTIF_CHANNEL_ID) & Filters.text, on_channel_post))
    dp.add_handler(ChatJoinRequestHandler(on_join_request))

    logging.info("Bot running…"); updater.start_polling(); updater.idle()

if __name__=="__main__": main()
