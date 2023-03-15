import collections
import datetime
import pathlib
import textwrap
import os
import csv
import logging
from enum import Enum
from typing import List, NamedTuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)


class EventType(Enum):
    UNKNOWN = 0
    JOURNAL = 1


class Event(NamedTuple):
    date: datetime.date
    event_type: EventType


class EventCsvStorage:
    def __init__(self, filepath=pathlib.Path('storage.csv')):
        self._filepath = filepath
        self._events: List[Event] = self._load_from_storage()

    def _load_from_storage(self) -> List[Event]:
        events = []
        with open(self._filepath, 'r') as f:
            reader = csv.reader(f)
            for row in reader:
                if row:
                    events.append(Event(datetime.date.fromisoformat(row[0]), EventType[row[1]]))
        return events

    def add(self, event: Event):
        self._events.append(event)
        self._write_to_storage()

    def exists(self, event):
        return event in self._events

    def _write_to_storage(self):
        with open(self._filepath, 'w') as f:
            writer = csv.writer(f)
            writer.writerows((event.date, event.event_type.name) for event in self._events)


async def ask_journal(context: ContextTypes.DEFAULT_TYPE):
    event_storage: EventCsvStorage = context.application.user_data[context.job.chat_id][EventCsvStorage.__name__]
    if event_storage.exists(Event(datetime.date.today(), EventType.JOURNAL)):
        return

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

async def button(update: Update,
                 context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    query = update.callback_query

    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()

    if query.data == "1":
        text = "Awesome!"
        context.user_data[EventCsvStorage.__name__].add(Event(datetime.date.today(), EventType.JOURNAL))
    elif query.data == "2":
        text = "Sure, I'll ask again later."
    elif query.data == "3":
        text = "A'ight, I won't bother you again today."

    await query.edit_message_text(text=text)

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
    context.job_queue.run_once(ask_journal, 3, chat_id=chat_id,
                               name=str(chat_id))

    context.user_data.setdefault(EventCsvStorage.__name__, EventCsvStorage())
    text = "Subscribed to journaling questions!"
    await update.effective_message.reply_text(text)


def main():
    application = ApplicationBuilder().token(
        os.environ['TELEGRAM_API_TOKEN']).build()

    start_handler = CommandHandler('s', subscribe)
    application.add_handler(start_handler)
    application.add_handler(CallbackQueryHandler(button))

    application.run_polling()


if __name__ == '__main__':
    main()
