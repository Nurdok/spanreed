"""Turn a rule of the old Gmail Monitor plugin into a workflow.

A rule was a filter and a list of actions of five types. The filter is the
filter of the "New email" trigger, and each action is a few steps: get the
attachments or find the links, then for each one write it, send it, and say
what happened.

The steps say what the actions said, as plain text. What an action did when
there was nothing to do, or when a file was there already, was a branch in
its code. Here it is a message inside ``{% if … %}``: a message that comes
out empty is not sent.

This module is only used to convert the rules a user had. It can go once
every deployment has run the conversion.
"""

from __future__ import annotations

import re
import string
from typing import Any

from spanreed.workflows.model import Step, TriggerSpec, Workflow

# What a rule's action looked for when it was not told which links.
_ANY_LINK = r'https?://[^\s<>"\']+'
# What ``recipe`` holds for a workflow that was a rule: this, and the place
# of the rule in the user's list, from 1.
RULE_RECIPE = "gmail-monitor-rule"

_UNSAFE_IN_NAMES_RE = re.compile(r'[\\/:*?"<>|]')
_HAS_EXTENSION_RE = re.compile(r"\.\w{1,8}$")
_NUMBER_SPEC_RE = re.compile(r"^0?\d*d?$")


def _literal(text: str) -> str:
    """Return a text so that a template shows it as it is."""
    if "{{" in text or "{%" in text or "{#" in text:
        return "{% raw %}" + text + "{% endraw %}"
    return text


def _name_template(template: str, fields: dict[str, str], safe: bool = True) -> str:
    """Return a file name template of a rule as a template of a step.

    A rule named a file with placeholders: ``{date} {original}``.
    ``fields`` gives what each placeholder is in the workflow. With
    ``safe``, the characters that a file name cannot hold are taken out of
    the text around the placeholders, as the rule took them out of the
    whole name. Return an empty text if the template cannot be used: the
    rule then fell back to the name of the file itself.
    """
    parts: list[str] = []
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        return ""
    for text, name, spec, conversion in parsed:
        if text:
            if safe:
                text = _UNSAFE_IN_NAMES_RE.sub("", text)
            parts.append(_literal(text))
        if name is None:
            continue
        if name not in fields or conversion:
            return ""
        expression = fields[name]
        if spec:
            # Only a number can be padded: {index:02d}.
            if name != "index" or not _NUMBER_SPEC_RE.match(spec):
                return ""
            padded = spec if spec.endswith("d") else spec + "d"
            expression = f"'%{padded}'|format({expression})"
        parts.append("{{ " + expression + " }}")
    return "".join(parts).strip()


def _path(folder: Any, template: Any, file: str, fields: dict[str, str]) -> str:
    """Return the template of the path a rule saved a file under.

    ``file`` is the step or loop that gives the file: its ``name`` is what
    the rule called ``{original}``, and its ``extension`` is added to a
    name that would end without one.
    """
    original = "{{ " + file + ".name }}"
    asked = str(template or "{original}").strip()
    name = _name_template(asked, {**fields, "original": f"{file}.name"})
    if not name:
        name = original
    elif not name.endswith(original) and not _HAS_EXTENSION_RE.search(asked):
        name += "{{ " + file + ".extension }}"
    folder = str(folder or "").strip().strip("/")
    return f"{_literal(folder)}/{name}" if folder else name


def _types(config: dict[str, Any]) -> str:
    """Return the MIME types an action asked for, as the block takes them."""
    types = config.get("mime_types", ["application/pdf"])
    return ",".join(str(t) for t in types) if isinstance(types, list) else ""


def _message(step_id: str, text: str) -> Step:
    return Step(id=step_id, block="core.send-message", inputs={"message": text})


def _saved(step_id: str, save: str, rule: str) -> Step:
    """Return the step that says whether a file was saved or was there."""
    return _message(
        step_id,
        f"{{% if {save}.written %}}💾 Saved {{{{ {save}.path }}}} (rule: {rule})"
        f"{{% else %}}⚠️ {{{{ {save}.path }}}} already exists; skipped.{{% endif %}}",
    )


def _notification(config: dict[str, Any], rule: str, s: str) -> list[Step]:
    text = (
        _literal(str(config["custom_message"]))
        if config.get("custom_message")
        else f"📧 Email matched rule: {rule}"
    )
    text += (
        "\n\nFrom: {{ trigger.sender }}"
        "\n\nSubject: {{ trigger.subject }}"
        "\n\nDate: {{ trigger.received }}"
        "{% if trigger.has_attachments %}\n\n📎 Has attachments{% endif %}"
    )
    if config.get("include_snippet", True):
        text += "{% if trigger.snippet %}\n\nPreview: {{ trigger.snippet }}{% endif %}"
    if config.get("include_body", False):
        text += (
            "{% if trigger.body %}\n\nBody: {{ trigger.body[:500] }}"
            "{% if trigger.body|length > 500 %}...{% endif %}{% endif %}"
        )
    return [_message(f"notify{s}", text)]


def _attachments(config: dict[str, Any], s: str, nothing: str) -> list[Step]:
    """Return the steps that get the attachments, and say if there are none."""
    return [
        Step(
            id=f"files{s}",
            block="gmail.get-attachments",
            inputs={"email": "{{ trigger.id }}", "types": _types(config)},
        ),
        _message(
            f"no_files{s}",
            f"{{% if files{s}.count == 0 %}}{nothing} {{{{ trigger.sender }}}}"
            "{% endif %}",
        ),
    ]


def _download_attachment(config: dict[str, Any], rule: str, s: str) -> list[Step]:
    each = f"each{s}"
    return [
        *_attachments(config, s, "📎 No matching attachment found in email from"),
        Step(
            id=each,
            block="core.for-each",
            inputs={"items": f"{{{{ files{s}.files }}}}"},
            steps=[
                Step(
                    id=f"send{s}",
                    block="core.send-file",
                    inputs={
                        "name": f"{{{{ {each}.item.name }}}}",
                        "content": f"{{{{ {each}.item.data }}}}",
                    },
                ),
                _message(
                    f"sent{s}",
                    f"📄 {{{{ {each}.item.name }}}} from {{{{ trigger.sender }}}}"
                    f"\n🏷️ Rule: {rule}",
                ),
            ],
        ),
    ]


def _save_attachment(config: dict[str, Any], rule: str, s: str) -> list[Step]:
    each = f"each{s}"
    fields = {
        "sender": "trigger.sender_name",
        "subject": "trigger.subject_name",
        "date": "trigger.date",
        "index": f"{each}.index",
    }
    path = _path(
        config.get("vault_dir"), config.get("filename_template"), f"{each}.item", fields
    )
    return [
        *_attachments(config, s, "📎 No matching attachment to save from"),
        Step(
            id=each,
            block="core.for-each",
            inputs={"items": f"{{{{ files{s}.files }}}}"},
            steps=[
                Step(
                    id=f"save{s}",
                    block="obsidian.write-file",
                    inputs={
                        "path": path,
                        "content": f"{{{{ {each}.item.data }}}}",
                        "if_exists": "overwrite" if config.get("overwrite") else "skip",
                    },
                ),
                _saved(f"saved{s}", f"save{s}", rule),
            ],
        ),
    ]


def _links(config: dict[str, Any], s: str, nothing: str) -> list[Step]:
    """Return the steps that find the links, and say if there are none."""
    url = str(config.get("url_regex") or _ANY_LINK)
    return [
        Step(
            id=f"links{s}",
            block="gmail.find-links",
            inputs={
                "text": "{{ trigger.body }}",
                "url": "" if url == _ANY_LINK else url,
                "near": str(config.get("text_regex") or ""),
                # An action took the first three links.
                "limit": 3,
            },
        ),
        _message(
            f"no_links{s}",
            f"{{% if links{s}.count == 0 %}}{nothing} {{{{ trigger.sender }}}}"
            "{% endif %}",
        ),
    ]


def _download(config: dict[str, Any], s: str, name_if_none: str) -> Step:
    return Step(
        id=f"get{s}",
        block="core.download-file",
        inputs={
            "url": f"{{{{ each{s}.item }}}}",
            "max_size_mb": config.get("max_file_size_mb", 10),
            "name_if_none": name_if_none,
        },
    )


def _only_files(s: str) -> Step:
    """Return the step that ends an item whose link led to no file."""
    return Step(
        id=f"is_file{s}",
        block="core.continue-if",
        inputs={"condition": f"{{{{ get{s}.ok }}}}"},
    )


def _download_link(config: dict[str, Any], rule: str, s: str) -> list[Step]:
    each, get = f"each{s}", f"get{s}"
    name = ""
    if config.get("custom_filename"):
        name = _name_template(
            str(config["custom_filename"]),
            {
                "sender": "trigger.sender_name",
                "subject": "trigger.subject_name|replace(' ', '_')",
                "date": "trigger.date",
                "rule": repr(rule),
                # The action counted the links from 0.
                "index": f"{each}.index - 1",
            },
            safe=False,
        )
    return [
        *_links(config, s, "🔗 No matching links found in email from"),
        Step(
            id=each,
            block="core.for-each",
            inputs={"items": f"{{{{ links{s}.links }}}}"},
            steps=[
                _download(
                    config,
                    s,
                    f"document_{{{{ trigger.date }}}}_{{{{ {each}.index - 1 }}}}",
                ),
                _message(
                    f"no_file{s}",
                    f"{{% if not {get}.ok %}}❌ Failed to download from "
                    f"{{{{ {each}.item[:100] }}}}: {{{{ {get}.problem }}}}{{% endif %}}",
                ),
                _only_files(s),
                Step(
                    id=f"send{s}",
                    block="core.send-file",
                    inputs={
                        "name": name or f"{{{{ {get}.name }}}}",
                        "content": f"{{{{ {get}.data }}}}",
                    },
                ),
                _message(
                    f"sent{s}",
                    "📄 File from {{ trigger.sender }}\n📧 {{ trigger.subject }}"
                    f"\n🏷️ Rule: {rule}",
                ),
            ],
        ),
    ]


def _save_link(config: dict[str, Any], rule: str, s: str) -> list[Step]:
    each, get = f"each{s}", f"get{s}"
    fields = {
        "sender": "trigger.sender_name",
        "subject": "trigger.subject_name",
        "date": "trigger.date",
        "index": f"{each}.index",
    }
    path = _path(config.get("vault_dir"), config.get("filename_template"), get, fields)
    return [
        *_links(config, s, "🔗 No matching links to save from"),
        Step(
            id=each,
            block="core.for-each",
            inputs={"items": f"{{{{ links{s}.links }}}}"},
            steps=[
                _download(config, s, f"download-{{{{ {each}.index }}}}"),
                _message(
                    f"no_file{s}",
                    f"{{% if not {get}.ok %}}⚠️ The link for “{rule}” was not saved. "
                    f"{{{{ {get}.problem }}}} Open it manually:\n"
                    f"{{{{ {each}.item[:150] }}}}{{% endif %}}",
                ),
                _only_files(s),
                Step(
                    id=f"save{s}",
                    block="obsidian.write-file",
                    inputs={
                        "path": path,
                        "content": f"{{{{ {get}.data }}}}",
                        "if_exists": "overwrite" if config.get("overwrite") else "skip",
                    },
                ),
                _saved(f"saved{s}", f"save{s}", rule),
            ],
        ),
    ]


_ACTIONS = {
    "telegram_notification": _notification,
    "download_attachment": _download_attachment,
    "save_attachment_to_vault": _save_attachment,
    "download_link": _download_link,
    "save_link_to_vault": _save_link,
}


def steps_for(actions: Any, rule_name: str) -> list[Step]:
    """Return the steps that do what the actions of a rule did, in order.

    An action of a type that the plugin did not know did nothing, and gives
    no steps. With more than one action, the ids of the steps of each end
    in its place: ``files_2``.
    """
    known = [
        action
        for action in (actions if isinstance(actions, list) else [])
        if isinstance(action, dict) and action.get("type") in _ACTIONS
    ]
    rule = _literal(rule_name)
    steps: list[Step] = []
    for place, action in enumerate(known, start=1):
        config = action.get("config")
        suffix = f"_{place}" if len(known) > 1 else ""
        steps += _ACTIONS[action["type"]](
            config if isinstance(config, dict) else {}, rule, suffix
        )
    return steps


def workflow_for_rule(
    workflow_id: str, user_id: int, rule: dict[str, Any], minutes: Any, place: int
) -> Workflow:
    """Return the workflow that does what a rule did.

    ``rule`` is the rule as the plugin saved it, ``minutes`` is how often
    the plugin looked for mail, and ``place`` is the place of the rule in
    the user's list, from 1. The workflow is on if the rule was. Its
    command is hidden: the plugin had one command for all rules.
    """
    filter_ = rule.get("filter") if isinstance(rule.get("filter"), dict) else {}
    assert isinstance(filter_, dict)
    name = str(rule.get("name") or f"Email rule {place}")
    has_attachments = filter_.get("has_attachments")
    try:
        every = max(int(minutes), 1)
    except (TypeError, ValueError):
        every = 15
    return Workflow(
        id=workflow_id,
        user_id=user_id,
        name=name,
        trigger=TriggerSpec(
            block="gmail.new-email",
            params={
                "every": every,
                "unit": "minutes",
                "sender": str(filter_.get("sender_regex") or ""),
                "subject": str(filter_.get("subject_regex") or ""),
                "body": str(filter_.get("body_regex") or ""),
                "attachments": (
                    "any"
                    if has_attachments is None
                    else "yes" if has_attachments else "no"
                ),
            },
        ),
        steps=steps_for(rule.get("actions"), name),
        enabled=bool(rule.get("enabled", True)),
        command_hidden=True,
        recipe=f"{RULE_RECIPE}-{place}",
    )
