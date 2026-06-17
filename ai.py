import os
import io
import re
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from openai import OpenAI

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = "8698628485:AAHjfEvEJIX1q15uxYNE7llJ-RwT2onMf9k"
GITHUB_TOKEN    = "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""
# GitHub Models endpoint
client = OpenAI(
    base_url="https://models.inference.ai.azure.com",
    api_key=GITHUB_TOKEN,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── AVAILABLE MODELS ────────────────────────────────────────────────────────
MODELS = {
    "gpt4o": {
        "id":    "gpt-4o",
        "name":  "GPT-4o",
        "emoji": "🟢",
        "desc":  "Best overall — fast & smart",
        "tokens": 4096,
    },
    "deepseek": {
        "id":    "DeepSeek-R1",
        "name":  "DeepSeek R1",
        "emoji": "🔵",
        "desc":  "Best for complex logic & algorithms",
        "tokens": 4096,
    },
    "codestral": {
        "id":    "Codestral-2501",
        "name":  "Codestral",
        "emoji": "🟣",
        "desc":  "Built purely for coding tasks ⭐",
        "tokens": 4096,
    },
    "llama": {
        "id":    "Meta-Llama-3.1-405B-Instruct",
        "name":  "Llama 3.1 405B",
        "emoji": "🦙",
        "desc":  "Massive open-source model",
        "tokens": 4096,
    },
    "phi4": {
        "id":    "Phi-4",
        "name":  "Phi-4",
        "emoji": "⚡",
        "desc":  "Fastest responses, lightweight",
        "tokens": 4096,
    },
}

DEFAULT_MODEL = "codestral"   # best for coding

# ─── SYSTEM PROMPT ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are DevMate AI — an elite coding assistant for developers of all levels.

STRICT RULES — never break these:
1. NEVER truncate or shorten code. Write COMPLETE, production-ready code every time.
2. If a file needs 500 lines, write all 500. NEVER use "// rest of code here" or similar.
3. When creating files, wrap each in: <FILE:filename.ext> ... </FILE>
4. When you see an error: analyze it, explain the cause, then provide the FULL fixed code.
5. Always state: language, dependencies to install, and how to run the code.
6. For project requests: create ALL files completely — never skip any file.
7. Be encouraging and clear — explain things so beginners can understand.
8. Format code in proper markdown code blocks with the language specified.

FILE FORMAT:
<FILE:server.js>
// complete code
</FILE>

<FILE:package.json>
{ ... complete json ... }
</FILE>
"""

# ─── USER STATE ───────────────────────────────────────────────────────────────
user_sessions  = {}   # { user_id: [ {role, content}, ... ] }
user_mode      = {}   # { user_id: "generate" | "fix" | ... }
user_model     = {}   # { user_id: "codestral" | "gpt4o" | ... }

TASK_MODES = {
    "generate": "🧠 Generate Code",
    "fix":      "🔧 Fix / Debug",
    "explain":  "📖 Explain Code",
    "project":  "📁 Full Project",
    "review":   "🔍 Code Review",
}

MODE_PROMPTS = {
    "generate": "Generate complete, production-ready code for: ",
    "fix":      "Fix and debug this code. Explain the problem, then give the FULL corrected code: ",
    "explain":  "Explain this code step by step so a beginner can understand: ",
    "project":  "Create a complete project with ALL files fully written for: ",
    "review":   "Do a thorough code review. List issues and provide improved full code: ",
}

# ─── KEYBOARDS ────────────────────────────────────────────────────────────────
def get_main_keyboard(user_id=None):
    model_key  = user_model.get(user_id, DEFAULT_MODEL) if user_id else DEFAULT_MODEL
    model_info = MODELS[model_key]
    keyboard = [
        [
            InlineKeyboardButton("🧠 Generate Code",  callback_data="mode_generate"),
            InlineKeyboardButton("🔧 Fix / Debug",    callback_data="mode_fix"),
        ],
        [
            InlineKeyboardButton("📖 Explain Code",   callback_data="mode_explain"),
            InlineKeyboardButton("📁 Full Project",   callback_data="mode_project"),
        ],
        [
            InlineKeyboardButton("🔍 Code Review",    callback_data="mode_review"),
            InlineKeyboardButton("🗑 Clear Chat",     callback_data="clear_chat"),
        ],
        [
            InlineKeyboardButton(
                f"🤖 Model: {model_info['emoji']} {model_info['name']}",
                callback_data="show_models"
            ),
        ],
        [
            InlineKeyboardButton("ℹ️ Help", callback_data="help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_model_keyboard(user_id=None):
    current = user_model.get(user_id, DEFAULT_MODEL) if user_id else DEFAULT_MODEL
    rows = []
    for key, m in MODELS.items():
        tick = " ✅" if key == current else ""
        rows.append([InlineKeyboardButton(
            f"{m['emoji']} {m['name']} — {m['desc']}{tick}",
            callback_data=f"setmodel_{key}"
        )])
    rows.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def get_session(user_id):
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    return user_sessions[user_id]

def extract_files(text):
    pattern = r'<FILE:(.*?)>(.*?)</FILE>'
    return re.findall(pattern, text, re.DOTALL)

def clean_for_display(text):
    text = re.sub(r'<FILE:(.*?)>', r'\n📄 **\1**\n```', text)
    text = re.sub(r'</FILE>', '```\n', text)
    return text

def split_message(text, max_len=4000):
    if len(text) <= max_len:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            chunks.append(current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current:
        chunks.append(current)
    return chunks

# ─── AI CALL ──────────────────────────────────────────────────────────────────
async def call_ai(user_id, user_message, mode=None):
    history   = get_session(user_id)
    model_key = user_model.get(user_id, DEFAULT_MODEL)
    model_cfg = MODELS[model_key]

    prefix       = MODE_PROMPTS.get(mode, "") if mode else ""
    full_message = prefix + user_message if prefix else user_message

    history.append({"role": "user", "content": full_message})

    response = client.chat.completions.create(
        model=model_cfg["id"],
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
        max_tokens=model_cfg["tokens"],
        temperature=0.2,
    )

    reply = response.choices[0].message.content
    history.append({"role": "assistant", "content": reply})

    # Keep last 20 messages
    if len(history) > 20:
        user_sessions[user_id] = history[-20:]

    return reply, model_cfg

# ─── COMMAND HANDLERS ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user     = update.effective_user
    user_id  = user.id
    m        = MODELS[user_model.get(user_id, DEFAULT_MODEL)]
    text = (
        f"👋 Hey **{user.first_name}**! I'm **DevMate AI** 🤖\n\n"
        f"Your personal coding assistant — powered by GitHub Models.\n\n"
        f"Current model: {m['emoji']} **{m['name']}** — {m['desc']}\n\n"
        "━━━━━━━━━━━━━━━━━\n"
        "🧠 Generate complete code\n"
        "🔧 Fix & debug errors\n"
        "📖 Explain any code\n"
        "📁 Build full projects\n"
        "🔍 Review your code\n"
        "🤖 Switch AI models anytime\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        "Pick a mode or just type what you need!"
    )
    await update.message.reply_text(text, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = []
    user_mode[user_id]     = None
    await update.message.reply_text(
        "🗑 Chat cleared! Fresh start — what do you want to build?",
        reply_markup=get_main_keyboard(user_id)
    )

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(
        "🤖 **Choose your AI Model:**\n\n_Each model has different strengths!_",
        parse_mode="Markdown",
        reply_markup=get_model_keyboard(user_id)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (
        "📚 **DevMate AI — Help**\n\n"
        "**Task Modes:**\n"
        "🧠 *Generate* — describe what you want built\n"
        "🔧 *Fix/Debug* — paste code + error message\n"
        "📖 *Explain* — paste code to understand it\n"
        "📁 *Full Project* — get a complete multi-file app\n"
        "🔍 *Code Review* — get improvement suggestions\n\n"
        "**AI Models:**\n"
        "🟢 GPT-4o — best overall\n"
        "🔵 DeepSeek R1 — complex logic\n"
        "🟣 Codestral — pure coding ⭐\n"
        "🦙 Llama 405B — open source powerhouse\n"
        "⚡ Phi-4 — fastest\n\n"
        "**Commands:**\n"
        "/start — main menu\n"
        "/clear — reset conversation\n"
        "/model — switch AI model\n\n"
        "**Pro tip:** Share your error message WITH the code for best results!\n"
        "I always write COMPLETE code — never truncated! 💪"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_keyboard(user_id))

# ─── BUTTON HANDLER ───────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data    = query.data

    # ── Task mode selection ──
    if data.startswith("mode_"):
        mode = data.replace("mode_", "")
        user_mode[user_id] = mode
        mode_name = TASK_MODES[mode]
        hints = {
            "generate": "What do you want me to build?\n_e.g. 'a REST API with Node.js, Express and MongoDB'_",
            "fix":      "Paste your code + error message.\nI'll analyze and fix it completely! 🔧",
            "explain":  "Paste the code you want me to explain.\nI'll break it down step by step! 📖",
            "project":  "Describe the full project.\n_e.g. 'todo app with React frontend + Flask backend'_",
            "review":   "Paste your code.\nI'll review it and suggest improvements! 🔍",
        }
        m = MODELS[user_model.get(user_id, DEFAULT_MODEL)]
        await query.edit_message_text(
            f"✅ Mode: **{mode_name}**\n"
            f"🤖 Using: {m['emoji']} {m['name']}\n\n"
            f"{hints.get(mode, 'How can I help?')}",
            parse_mode="Markdown"
        )

    # ── Show model selection ──
    elif data == "show_models":
        await query.edit_message_text(
            "🤖 **Choose your AI Model:**\n\n"
            "Each model has different strengths — pick what fits your task!",
            parse_mode="Markdown",
            reply_markup=get_model_keyboard(user_id)
        )

    # ── Set model ──
    elif data.startswith("setmodel_"):
        model_key = data.replace("setmodel_", "")
        if model_key in MODELS:
            user_model[user_id] = model_key
            user_sessions[user_id] = []   # clear history on model switch
            m = MODELS[model_key]
            await query.edit_message_text(
                f"✅ Model switched to {m['emoji']} **{m['name']}**\n"
                f"_{m['desc']}_\n\n"
                f"💡 Chat history cleared for fresh context.\n"
                f"Ready to code! What do you need?",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard(user_id)
            )

    # ── Back to main menu ──
    elif data == "back_main":
        m = MODELS[user_model.get(user_id, DEFAULT_MODEL)]
        await query.edit_message_text(
            f"🤖 Active model: {m['emoji']} **{m['name']}**\n\nWhat do you want to do?",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(user_id)
        )

    # ── Clear chat ──
    elif data == "clear_chat":
        user_sessions[user_id] = []
        user_mode[user_id]     = None
        await query.edit_message_text(
            "🗑 Chat cleared! Pick a mode or just type your question.",
            reply_markup=get_main_keyboard(user_id)
        )

    # ── Help ──
    elif data == "help":
        await query.edit_message_text(
            "📚 **DevMate AI — Quick Guide**\n\n"
            "Pick a mode → type your request → get complete code!\n\n"
            "🧠 Generate — build something new\n"
            "🔧 Fix — paste code + error\n"
            "📖 Explain — understand any code\n"
            "📁 Project — full multi-file apps\n"
            "🔍 Review — improve your code\n\n"
            "🤖 Switch models anytime with the Model button!",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(user_id)
        )

# ─── MESSAGE HANDLER ──────────────────────────────────────────────────────────
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = update.message.text
    mode    = user_mode.get(user_id, None)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        reply, model_cfg = await call_ai(user_id, text, mode)

        files        = extract_files(reply)
        display_text = clean_for_display(reply)
        chunks       = split_message(display_text)

        # Send response (split if long)
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            try:
                await update.message.reply_text(
                    chunk,
                    parse_mode="Markdown",
                    reply_markup=get_main_keyboard(user_id) if is_last else None
                )
            except Exception:
                # Fallback: send without markdown if parse error
                await update.message.reply_text(
                    chunk,
                    reply_markup=get_main_keyboard(user_id) if is_last else None
                )

        # Send files as downloadable documents
        if files:
            await update.message.reply_text(
                f"📁 **{len(files)} file(s) ready — downloading below...**",
                parse_mode="Markdown"
            )
            for filename, content in files:
                file_bytes = content.strip().encode("utf-8")
                file_obj   = io.BytesIO(file_bytes)
                file_obj.name = filename
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=file_obj,
                    filename=filename,
                    caption=f"📄 `{filename}` — by {model_cfg['emoji']} {model_cfg['name']}",
                    parse_mode="Markdown"
                )

    except Exception as e:
        logger.error(f"Error for user {user_id}: {e}")
        await update.message.reply_text(
            f"❌ Error: `{str(e)}`\n\nTry /clear and start again, or switch model with /model",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(user_id)
        )

# ─── HEALTH CHECK SERVER ──────────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK - DevMate AI Bot is alive!")

    def log_message(self, format, *args):
        pass  # silence HTTP logs

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health check server running on port {port}")
    server.serve_forever()

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("help",   help_command))
    app.add_handler(CommandHandler("clear",  clear_command))
    app.add_handler(CommandHandler("model",  model_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    logger.info("🤖 DevMate AI Bot running with GitHub Models!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
