import logging
import textwrap

from telegram import Update, ForceReply, InlineKeyboardMarkup, InlineKeyboardButton, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler
import os


BUTTON = "Click me!"


def menu(update: Update, context: CallbackContext) -> None:
    preamble = textwrap.dedent("""\
        <b>Spanreed</b>
        What would you like to do?
    """)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(BUTTON, callback_data=BUTTON)
    ]])

    context.bot.send_message(
        update.message.from_user.id,
        menu,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


def button_tap(update: Update, context: CallbackContext) -> None:
    data = update.callback_query.data
    text = ''
    markup = None

    if data != BUTTON:
        raise RuntimeError("unexpected callback!")

    # Close the query to end the client-side loading animation
    update.callback_query.answer()

    # Update message content with corresponding menu section
    update.callback_query.message.edit_text(
        "Cool cool cool",
        ParseMode.HTML,
        reply_markup="",
    )

def main():
    updater = Updater(os.environ('TELEGRAM_API_TOKEN'))

    # Get the dispatcher to register handlers
    # Then, we register each handler and the conditions the update must meet to trigger it
    dispatcher = updater.dispatcher

    # Register commands
    dispatcher.add_handler(CommandHandler("menu", menu))

    # Register handler for inline buttons
    dispatcher.add_handler(CallbackQueryHandler(button_tap))

    # Start the Bot
    updater.start_polling()

    # Run the bot until you press Ctrl-C
    updater.idle()


if __name__ == '__main__':
    main()

