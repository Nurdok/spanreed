"""An email, and what the rule actions of the old Gmail Monitor did with it.

CASES was made by running the actions of the plugin, before they were
removed, for this email and these attachments and links. A message is as
the user read it: the plugin sent HTML, with the labels in bold. written
holds each path that was written, and whether a file there was replaced.

The workflow that a rule is turned into has to say and write the same.
"""

from typing import Any

EMAIL: dict[str, Any] = {
    "id": "m1",
    "thread_id": "t1",
    "sender": '"Shop: Billing" <billing@shop.example>',
    "subject": 'Receipt #1042 / May "2026"',
    "body": (
        "Thanks. Your invoice: https://pay.example/invoice/7?t=abc "
        "and more https://pay.example/terms"
    ),
    "snippet": "Thanks. Your invoice",
    # 2026-05-09 09:30:05
    "date": (2026, 5, 9, 9, 30, 5),
    "labels": [],
    "has_attachments": True,
}

# The name, the type and the data of each attachment. The second has no name.
ATTACHMENTS = [
    ("receipt: May.pdf", "application/pdf", b"%PDF-1"),
    ("", "application/pdf", b"%PDF-2"),
]

# What each link of the email gives: the name the server gives the file,
# and its type.
DOWNLOADS = {
    "https://pay.example/invoice/7?t=abc": ("invoice-7", "application/pdf"),
    "https://pay.example/terms": ("terms.pdf", "application/pdf"),
}

RULE_NAME = "Receipts & co"

CASES: dict[str, dict[str, Any]] = {
    "notify_default": {
        "type": "telegram_notification",
        "config": {},
        "messages": [
            "📧 Email matched rule: Receipts & co\n"
            "\n"
            'From: "Shop: Billing" <billing@shop.example>\n'
            "\n"
            'Subject: Receipt #1042 / May "2026"\n'
            "\n"
            "Date: 2026-05-09 09:30:05\n"
            "\n"
            "📎 Has attachments\n"
            "\n"
            "Preview: Thanks. Your invoice"
        ],
        "written": [],
    },
    "notify_all": {
        "type": "telegram_notification",
        "config": {
            "include_body": True,
            "include_snippet": True,
            "custom_message": "New receipt <1>",
        },
        "messages": [
            "New receipt <1>\n"
            "\n"
            'From: "Shop: Billing" <billing@shop.example>\n'
            "\n"
            'Subject: Receipt #1042 / May "2026"\n'
            "\n"
            "Date: 2026-05-09 09:30:05\n"
            "\n"
            "📎 Has attachments\n"
            "\n"
            "Preview: Thanks. Your invoice\n"
            "\n"
            "Body: Thanks. Your invoice: https://pay.example/invoice/7?t=abc "
            "and more https://pay.example/terms"
        ],
        "written": [],
    },
    "notify_bare": {
        "type": "telegram_notification",
        "config": {"include_snippet": False},
        "messages": [
            "📧 Email matched rule: Receipts & co\n"
            "\n"
            'From: "Shop: Billing" <billing@shop.example>\n'
            "\n"
            'Subject: Receipt #1042 / May "2026"\n'
            "\n"
            "Date: 2026-05-09 09:30:05\n"
            "\n"
            "📎 Has attachments"
        ],
        "written": [],
    },
    "save_attachment_original": {
        "type": "save_attachment_to_vault",
        "config": {"vault_dir": "/Receipts/2026/", "filename_template": "{original}"},
        "messages": [
            "💾 Saved Receipts/2026/receipt May.pdf (rule: " "Receipts & co)",
            "💾 Saved Receipts/2026/attachment-2 (rule: Receipts " "& co)",
        ],
        "written": [
            ["Receipts/2026/receipt May.pdf", False],
            ["Receipts/2026/attachment-2", False],
        ],
    },
    "save_attachment_dated": {
        "type": "save_attachment_to_vault",
        "config": {
            "vault_dir": "/Receipts/2026/",
            "filename_template": "{date} {sender} {original}",
        },
        "messages": [
            "💾 Saved Receipts/2026/2026-05-09 Shop Billing billing "
            "receipt May.pdf (rule: Receipts & co)",
            "💾 Saved Receipts/2026/2026-05-09 Shop Billing billing "
            "attachment-2 (rule: Receipts & co)",
        ],
        "written": [
            ["Receipts/2026/2026-05-09 Shop Billing billing receipt " "May.pdf", False],
            ["Receipts/2026/2026-05-09 Shop Billing billing " "attachment-2", False],
        ],
    },
    "save_attachment_no_ext": {
        "type": "save_attachment_to_vault",
        "config": {
            "vault_dir": "/Receipts/2026/",
            "filename_template": "{date} receipt {index}",
        },
        "messages": [
            "💾 Saved Receipts/2026/2026-05-09 receipt 1.pdf " "(rule: Receipts & co)",
            "💾 Saved Receipts/2026/2026-05-09 receipt 2 (rule: " "Receipts & co)",
        ],
        "written": [
            ["Receipts/2026/2026-05-09 receipt 1.pdf", False],
            ["Receipts/2026/2026-05-09 receipt 2", False],
        ],
    },
    "save_attachment_subject": {
        "type": "save_attachment_to_vault",
        "config": {
            "vault_dir": "/Receipts/2026/",
            "filename_template": "{subject}: {original}",
        },
        "messages": [
            "💾 Saved Receipts/2026/Receipt _1042 _ May _2026_ "
            "receipt May.pdf (rule: Receipts & co)",
            "💾 Saved Receipts/2026/Receipt _1042 _ May _2026_ "
            "attachment-2 (rule: Receipts & co)",
        ],
        "written": [
            ["Receipts/2026/Receipt _1042 _ May _2026_ receipt " "May.pdf", False],
            ["Receipts/2026/Receipt _1042 _ May _2026_ " "attachment-2", False],
        ],
    },
    "save_attachment_bad": {
        "type": "save_attachment_to_vault",
        "config": {
            "vault_dir": "/Receipts/2026/",
            "filename_template": "{nope} {original}",
        },
        "messages": [
            "💾 Saved Receipts/2026/receipt May.pdf (rule: Receipts & " "co)",
            "💾 Saved Receipts/2026/attachment-2 (rule: Receipts & " "co)",
        ],
        "written": [
            ["Receipts/2026/receipt May.pdf", False],
            ["Receipts/2026/attachment-2", False],
        ],
    },
    "save_attachment_fixed_ext": {
        "type": "save_attachment_to_vault",
        "config": {"vault_dir": "/Receipts/2026/", "filename_template": "receipt.pdf"},
        "messages": [
            "💾 Saved Receipts/2026/receipt.pdf (rule: Receipts " "& co)",
            "💾 Saved Receipts/2026/receipt.pdf (rule: Receipts " "& co)",
        ],
        "written": [
            ["Receipts/2026/receipt.pdf", False],
            ["Receipts/2026/receipt.pdf", False],
        ],
    },
    "save_attachment_root_overwrite": {
        "type": "save_attachment_to_vault",
        "config": {
            "vault_dir": "",
            "overwrite": True,
            "mime_types": ["application/pdf", "image/png"],
        },
        "messages": [
            "💾 Saved receipt May.pdf (rule: Receipts & " "co)",
            "💾 Saved attachment-2 (rule: Receipts & co)",
        ],
        "written": [["receipt May.pdf", True], ["attachment-2", True]],
    },
    "save_attachment_exists": {
        "type": "save_attachment_to_vault",
        "config": {"vault_dir": "Receipts"},
        "messages": [
            "⚠️ Receipts/receipt May.pdf already exists; skipped.",
            "💾 Saved Receipts/attachment-2 (rule: Receipts & co)",
        ],
        "written": [
            ["Receipts/receipt May.pdf", False],
            ["Receipts/attachment-2", False],
        ],
    },
    "save_attachment_none": {
        "type": "save_attachment_to_vault",
        "config": {"vault_dir": "Receipts"},
        "messages": [
            '📎 No matching attachment to save from "Shop: Billing" '
            "<billing@shop.example>"
        ],
        "written": [],
    },
    "save_link_default": {
        "type": "save_link_to_vault",
        "config": {"vault_dir": "Invoices"},
        "messages": [
            "💾 Saved Invoices/invoice-7.pdf (rule: Receipts & co)",
            "💾 Saved Invoices/terms.pdf (rule: Receipts & co)",
        ],
        "written": [["Invoices/invoice-7.pdf", False], ["Invoices/terms.pdf", False]],
    },
    "save_link_filtered": {
        "type": "save_link_to_vault",
        "config": {
            "vault_dir": "Invoices",
            "url_regex": "invoice",
            "filename_template": "{date} {original}",
        },
        "messages": [
            "💾 Saved Invoices/2026-05-09 invoice-7.pdf (rule: " "Receipts & co)"
        ],
        "written": [["Invoices/2026-05-09 invoice-7.pdf", False]],
    },
    "save_link_none": {
        "type": "save_link_to_vault",
        "config": {"vault_dir": "Invoices", "url_regex": "nothing-matches"},
        "messages": [
            '🔗 No matching links to save from "Shop: Billing" '
            "<billing@shop.example>"
        ],
        "written": [],
    },
}
