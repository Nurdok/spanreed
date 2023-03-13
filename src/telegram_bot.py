import textwrap
import os
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)


async def ask_journal(context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("Yes", callback_data="1"),
            InlineKeyboardButton("Not yet", callback_data="2"),
            InlineKeyboardButton("Not happening", callback_data="3"),
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        context.job.chat_id,
        text="Did you journal today?",
        reply_markup=reply_markup
    )


def remove_job_if_exists(name: str,
                         context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Remove job with given name. Returns whether job was removed."""
    current_jobs = context.job_queue.get_jobs_by_name(name)
    if not current_jobs:
        return False
    for job in current_jobs:
        job.schedule_removal()
    return True


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_message.chat_id

    remove_job_if_exists(str(chat_id), context)
    context.job_queue.run_once(ask_journal, 10, chat_id=chat_id,
                               name=str(chat_id))

    text = "Subscribed to journaling questions!"
    await update.effective_message.reply_text(text)


def main():
    application = ApplicationBuilder().token(
        os.environ['TELEGRAM_API_TOKEN']).build()

    start_handler = CommandHandler('s', subscribe)
    application.add_handler(start_handler)

    application.run_polling()


if __name__ == '__main__':
    main()
