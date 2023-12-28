# Spanreed
<small>A *personal* personal assistant!</small>

Spanreed is a personal automation platform for programmers. 

Spanreed's philosophy follows Alan Kay's famous quote:

> Simple things should be simple,  
> Complex things should be possible

Platforms like IFTTT and Zapier are great for automating simple tasks, but they are limited in their functionality. Spanreed is a platform that allows you to write your own automation scripts in Python while still enjoying pre-written APIs for common services.

Spanreed is intended to be available whenever and wherever you need it. It will eventually support a variety of surfaces, including desktop, mobile, chat apps, and voice assistants.

Spanreed allows users to write their own automations (plugins) or reuse pre-written ones. Automations can be user-invoked commands or event-driven scripts. Automations can also request user input asynchonously when needed, wherever the user might be available.

Spanreed's current UI is through a Telegram bot, where it can either accept commands, send notifications, request user input, etc.

Some examples:

- I am using Todoist for task management. Some tasks are daily chores and I never want them to be "overdue". Spanreed should automatically change these tasks' due date to "today" if they are overdue.
- I have a weekly therapy session. I want Spanreed to ask me if it actually happened. If so, I want it to track how much I owe my therapist in a Todoist task. If I have a pre-existing task with an unpaid amount, it should be added to the same task and track all the therapy session dates I haven't paid for yet. It should then add a new title for today's date in my "Therapy Log" note in my Obsidian vault with a "#todo" item to fill out details from the session.
- I would like to track my mood and how I'm feeling randomly throughout the day. I want Spanreed to occassionally ask me how I'm feeling via Telegram and store the responses in my Obsidian daily note.
- When someone recommends a book to me, I would like to issue a command to Spanreed to make a note of it in my Obsidian vault, automatically fetch book details from an external service and fill them out in the note, as well as record who recommended that book to me.
- I would like Spanreed to know which book I'm currently reading (by accessing my Obsidian vault) and ask me if I'm still reading it, and what I think about it so far. It should query Kindle / Audible and know what page I'm on, and if I'm reading it on my Kindle or listening to it on Audible. It should also know if I've finished the book and ask me to rate it.