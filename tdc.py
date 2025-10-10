#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "regex",
#   "wcwidth",
#   "rich",
#   "rich-argparse",
#   "todoist-api-python>=3.1.0",
#   "pyyaml",
#   "tomli",
#   "tomli-w",
# ]
# ///

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import namedtuple
from collections.abc import Iterable
from datetime import date, datetime

import regex
from rich.console import Console
from rich.text import Text
from rich_argparse import RawTextRichHelpFormatter
from todoist_api_python.api import TodoistAPI
from wcwidth import wcswidth

console = Console()
console_err = Console(file=sys.stderr)

LOGGER = logging.getLogger(__name__)

API_TOKEN = os.getenv("TODOIST_API_TOKEN") or os.getenv("TODOIST_API_KEY")
STRIP_EMOJIS = False

SECTION_ALL_SENTINEL = "__ALL_SECTIONS__"

DeletionResult = namedtuple("DeletionResult", ["deleted", "fatal"])

# Color constants
TASK_COLOR = "blue"
PROJECT_COLOR = "yellow"
SECTION_COLOR = "red"
ID_COLOR = "magenta"
NA_TEXT = Text("N/A", style="italic dim")


def flatten_paginated(result):
    if result is None:
        return []
    if isinstance(result, (str, bytes)):
        return [result]
    if isinstance(result, dict):
        return [result]
    if isinstance(result, Iterable):
        items = []
        for chunk in result:
            if isinstance(chunk, (list, tuple, set)):
                items.extend(chunk)
            else:
                items.append(chunk)
        return items
    return [result]


def consume_paginated(callable_, *args, **kwargs):
    return flatten_paginated(callable_(*args, **kwargs))


###############################################################################
# Utility and Formatting
###############################################################################
def task_str(task_obj):
    if type(task_obj) is dict:
        task_obj = namedtuple("Struct", task_obj.keys())(*task_obj.values())
    return f"[{TASK_COLOR}]{task_obj.content}[/{TASK_COLOR}] (ID: [{ID_COLOR}]{task_obj.id}[/{ID_COLOR}])"


def project_str(project_obj):
    if type(project_obj) is dict:
        project_obj = namedtuple("Struct", project_obj.keys())(*project_obj.values())
    return f"[{PROJECT_COLOR}]{project_obj.name}[/{PROJECT_COLOR}] (ID: [{ID_COLOR}]{project_obj.id}[/{ID_COLOR}])"


def section_str(section_obj):
    if type(section_obj) is dict:
        section_obj = namedtuple("Struct", section_obj.keys())(*section_obj.values())
    return f"[{SECTION_COLOR}]{section_obj.name}[/{SECTION_COLOR}] (ID: [{ID_COLOR}]{section_obj.id}[/{ID_COLOR}])"


ANSI_CSI_RE = regex.compile(r"\x1B[@-_][0-?]*[ -/]*[@-~]")
ANSI_OSC8_RE = regex.compile(r"\x1B]8;;.*?(?:\x07|\x1B\\)")
GRAPHEME_CLUSTER_REGEX = regex.compile(r"\X", regex.UNICODE)
EMOJI_REMOVAL_REGEX = regex.compile(
    r"(?:\p{Extended_Pictographic}\\x{FE0F}?(?:\\x{200D}\p{Extended_Pictographic}\\x{FE0F}?)*)(?:\\s*)",
    regex.UNICODE,
)
EMOJI_CLUSTER_REGEX = regex.compile(r"\p{Extended_Pictographic}", regex.UNICODE)


def strip_ansi(text):
    """Remove ANSI escape sequences from text."""

    text = ANSI_OSC8_RE.sub("", text)
    return ANSI_CSI_RE.sub("", text)


def remove_emojis(text):
    if not text:
        return text

    return EMOJI_REMOVAL_REGEX.sub("", text)


def maybe_strip_emojis(text):
    if STRIP_EMOJIS:
        return remove_emojis(text)
    return text


def _is_emoji_cluster(cluster):
    if "\u200d" in cluster or "\ufe0f" in cluster:
        return True
    return bool(EMOJI_CLUSTER_REGEX.search(cluster))


def _cluster_display_width(cluster):
    width = wcswidth(cluster)
    if width < 0:
        width = 0
    if _is_emoji_cluster(cluster) and width < 2:
        return 2
    return width


def visible_width(value):
    if isinstance(value, Text):
        raw = value.plain
    else:
        raw = str(value)
    clean = strip_ansi(raw)
    return sum(_cluster_display_width(cluster) for cluster in GRAPHEME_CLUSTER_REGEX.findall(clean))


def emoji_cell(value, *, allow_na=True):
    if value is None:
        if allow_na:
            return NA_TEXT.copy()
        return Text("")
    if isinstance(value, Text):
        return value.copy()
    return Text(str(value))


def styled_cell(value, *, style=None, allow_na=True):
    text = emoji_cell(value, allow_na=allow_na)
    if style:
        text.stylize(style)
    return text


def header_cell(name, *, style=None):
    header_style = "bold" if style is None else f"bold {style}"
    return Text(name, style=header_style)


def _format_aligned_line(cells, widths):
    line = Text()
    for idx, cell in enumerate(cells):
        cell_text = cell.copy()
        cell_width = visible_width(cell_text)
        pad = widths[idx] - cell_width
        if pad > 0:
            cell_text.append(" " * pad)
        if idx:
            line.append("  ")
        line.append_text(cell_text)
    stripped = line.rstrip()
    return stripped if stripped is not None else line


def render_aligned_table(headers, rows):
    if not headers:
        return []

    widths = [0] * len(headers)
    for idx, header in enumerate(headers):
        widths[idx] = max(widths[idx], visible_width(header))
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], visible_width(cell))

    lines = [_format_aligned_line(headers, widths)]
    for row in rows:
        lines.append(_format_aligned_line(row, widths))
    return lines


def serialize_todoist_object(obj):
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {key: serialize_todoist_object(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [serialize_todoist_object(item) for item in obj]
    if hasattr(obj, "to_dict"):
        return serialize_todoist_object(obj.to_dict())
    if hasattr(obj, "__dict__"):
        return {
            key: serialize_todoist_object(value)
            for key, value in vars(obj).items()
            if not key.startswith("_")
        }
    return str(obj)


def matches_task_lookup(task, identifier, identifier_lower, lookup_is_id):
    if lookup_is_id and str(task.id) == identifier:
        return True
    task_content = getattr(task, "content", None)
    if task_content is None:
        return False
    return str(task_content).strip().lower() == identifier_lower


def compile_content_pattern(pattern):
    if not pattern:
        return None
    try:
        return regex.compile(pattern, regex.IGNORECASE)
    except regex.error as exc:
        console_err.print(f"[red]Invalid content filter '{pattern}': {exc}[/red]")
        sys.exit(1)


def task_matches_pattern(task, compiled_pattern):
    if not compiled_pattern:
        return True
    task_content = getattr(task, "content", "") or ""
    return bool(compiled_pattern.search(task_content))


def log_operating_across_all_projects():
    console_err.print("[cyan]Operating across all projects[/cyan]")


async def log_operating_on_project(client, project_id, *, project_obj=None):
    if project_id is None:
        log_operating_across_all_projects()
        return

    project = project_obj
    if project is None:
        projects = await client.get_projects()
        project_lookup = {p.id: p for p in projects}
        project = project_lookup.get(project_id)

    if project:
        console_err.print(
            "[cyan]Operating on project "
            f"\"[{PROJECT_COLOR}]{project.name}[/{PROJECT_COLOR}]\" "
            f"(ID: [{ID_COLOR}]{project.id}[/{ID_COLOR}])[/cyan]"
        )
    else:
        console_err.print(
            "[cyan]Operating on project ID "
            f"[{ID_COLOR}]{project_id}[/{ID_COLOR}][/cyan]"
        )


async def resolve_task_identifier(
    client,
    identifier,
    project_name=None,
    project_id=None,
    todoist_filter=None,
    content_pattern=None,
):
    pid = project_id
    if project_name:
        pid = await find_project_id_partial(client, project_name)
        if not pid:
            console_err.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)
        projects = await client.get_projects()
        project_lookup = {p.id: p for p in projects}
        project_obj = project_lookup.get(pid)
        if project_obj:
            await log_operating_on_project(
                client, pid, project_obj=project_obj
            )
        else:
            await log_operating_on_project(client, pid)

    elif project_id is not None:
        pid = project_id

    lookup_is_id = identifier.isdigit()
    identifier_lower = identifier.lower()

    compiled_pattern = compile_content_pattern(content_pattern)

    scoped_tasks = await client.get_tasks(project_id=pid, filter_str=todoist_filter)
    if compiled_pattern:
        scoped_tasks = [
            task
            for task in scoped_tasks
            if task_matches_pattern(task, compiled_pattern)
        ]
    for task in scoped_tasks:
        if matches_task_lookup(task, identifier, identifier_lower, lookup_is_id):
            return task, pid, lookup_is_id

    if pid:
        all_tasks = await client.get_tasks(filter_str=todoist_filter)
        if compiled_pattern:
            all_tasks = [
                task
                for task in all_tasks
                if task_matches_pattern(task, compiled_pattern)
            ]
        for task in all_tasks:
            if matches_task_lookup(task, identifier, identifier_lower, lookup_is_id):
                projects = await client.get_projects()
                project_lookup = {p.id: p for p in projects}
                expected_project = project_lookup.get(pid)
                actual_project = project_lookup.get(getattr(task, "project_id", None))
                expected_desc = (
                    project_str(expected_project)
                    if expected_project
                    else f"project ID {pid}"
                )
                actual_pid = getattr(task, "project_id", None)
                actual_desc = (
                    project_str(actual_project)
                    if actual_project
                    else f"project ID {actual_pid}"
                )
                console_err.print(
                    "[red]Found matching task "
                    f"{task_str(task)} but it belongs to {actual_desc} instead of {expected_desc}.[/red]"
                )
                sys.exit(1)

    return None, pid, lookup_is_id


###############################################################################
# Caching and Async Client Wrapper
###############################################################################
class TodoistClient:
    def __init__(self, api):
        self.api = api
        self._projects = None
        self._sections = {}
        self._tasks = {}

    async def get_projects(self):
        if self._projects is None:
            self._projects = await asyncio.to_thread(
                consume_paginated, self.api.get_projects
            )
        return self._projects

    async def get_sections(self, project_id):
        if project_id not in self._sections:
            self._sections[project_id] = await asyncio.to_thread(
                consume_paginated, self.api.get_sections, project_id=project_id
            )
        return self._sections[project_id]

    async def get_tasks(self, project_id=None, filter_str=None):
        scope = project_id if project_id is not None else "all"
        key = (scope, filter_str)
        if key not in self._tasks:
            kwargs = {}
            if project_id is not None and not filter_str:
                kwargs["project_id"] = project_id
            if filter_str:
                tasks = await asyncio.to_thread(
                    consume_paginated, self.api.filter_tasks, query=filter_str
                )
                if project_id is not None:
                    tasks = [
                        task
                        for task in tasks
                        if getattr(task, "project_id", None) == project_id
                    ]
            else:
                tasks = await asyncio.to_thread(
                    consume_paginated, self.api.get_tasks, **kwargs
                )
            self._tasks[key] = tasks
        return self._tasks[key]

    def invalidate_tasks(self, project_id=None):
        if project_id is None:
            self._tasks.clear()
            return
        keys_to_remove = []
        for scope, filter_key in self._tasks:
            if scope == project_id or scope == "all":
                keys_to_remove.append((scope, filter_key))
        for key in keys_to_remove:
            self._tasks.pop(key, None)

    def invalidate_projects(self):
        self._projects = None

    def invalidate_sections(self, project_id):
        self._sections.pop(project_id, None)


###############################################################################
# Lookups
###############################################################################
async def find_project_id_partial(client, project_input):
    projects = await client.get_projects()
    if project_input.isdigit():
        for p in projects:
            if str(p.id) == project_input:
                return p.id
    psearch = project_input.lower()
    for p in projects:
        if psearch in p.name.lower():
            return p.id
    return None


async def find_section_id_partial(client, project_id, section_name_partial):
    secs = await client.get_sections(project_id)
    ssearch = section_name_partial.lower()
    for sec in secs:
        if ssearch in sec.name.lower():
            return sec.id
    return None


async def validate_labels(client, label_names):
    """
    Validate that all provided label names exist.
    Returns the list of valid label names, or raises an error if any don't exist.
    """
    if not label_names:
        return []

    try:
        labels = await asyncio.to_thread(consume_paginated, client.api.get_labels)
    except Exception as exc:
        console_err.print(f"[red]Failed to fetch labels: {exc}[/red]")
        sys.exit(1)

    existing_labels = {label.name.lower(): label.name for label in labels}
    valid_labels = []
    invalid_labels = []

    for label_name in label_names:
        label_lower = label_name.lower()
        if label_lower in existing_labels:
            valid_labels.append(existing_labels[label_lower])
        else:
            invalid_labels.append(label_name)

    if invalid_labels:
        console_err.print(
            f"[red]The following labels do not exist: {', '.join(invalid_labels)}[/red]"
        )
        console_err.print(
            "[yellow]Use 'tdc label list' to see available labels, or create them with 'tdc label create <name>'[/yellow]"
        )
        sys.exit(1)

    return valid_labels


###############################################################################
# Task Commands
###############################################################################
async def list_tasks(
    client,
    show_ids=False,
    show_subtasks=False,
    project_name=None,
    section_name=None,
    output_json=False,
    filter_today=False,
    filter_overdue=False,
    filter_recurring=False,
    todoist_filter=None,
    content_pattern=None,
):
    pid = None
    if project_name:
        pid = await find_project_id_partial(client, project_name)
        if not pid:
            console_err.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)

    projects = await client.get_projects()
    projects_dict = {p.id: p for p in projects}

    if pid is not None:
        project_obj = projects_dict.get(pid)
        await log_operating_on_project(
            client, pid, project_obj=project_obj
        )
    else:
        log_operating_across_all_projects()

    # Get tasks for a project (or all)
    compiled_pattern = compile_content_pattern(content_pattern)

    tasks = await client.get_tasks(project_id=pid, filter_str=todoist_filter)

    if not show_subtasks:
        tasks = [t for t in tasks if t.parent_id is None]

    section_mapping = {}
    show_section_col = False
    if section_name:
        if not project_name:
            console_err.print("[red]--section requires --project.[/red]")
            sys.exit(1)
        sid = await find_section_id_partial(client, pid, section_name)
        if not sid:
            console_err.print(
                f"[red]No section found matching '{section_name}' in project '{project_name}'.[/red]"
            )
            sys.exit(1)
        tasks = [t for t in tasks if t.section_id == sid]
        secs = await client.get_sections(pid)
        section_mapping = {s.id: s for s in secs}
        show_section_col = True
    else:
        if any(t.section_id for t in tasks):
            show_section_col = True
            unique_pids = {t.project_id for t in tasks if t.section_id}
            for upid in unique_pids:
                secs = await client.get_sections(upid)
                for s in secs:
                    section_mapping[s.id] = s

    if compiled_pattern:
        tasks = [t for t in tasks if task_matches_pattern(t, compiled_pattern)]

    # Apply extra filters (union if more than one is provided)
    today_date = date.today()
    if filter_today or filter_overdue:
        union_tasks = []
        if filter_today:
            union_tasks.extend(
                [
                    t
                    for t in tasks
                    if t.due
                    and getattr(t.due, "date", None)
                    and datetime.strptime(t.due.date, "%Y-%m-%d").date() == today_date
                ]
            )
        if filter_overdue:
            union_tasks.extend(
                [
                    t
                    for t in tasks
                    if t.due
                    and getattr(t.due, "date", None)
                    and datetime.strptime(t.due.date, "%Y-%m-%d").date() < today_date
                ]
            )
        # Remove duplicates (by task id)
        tasks = list({t.id: t for t in union_tasks}.values())

    if filter_recurring:
        tasks = [t for t in tasks if t.due and getattr(t.due, "is_recurring", False)]

    task_dict = {t.id: t for t in tasks}

    tasks.sort(
        key=lambda t: (
            (
                projects_dict[t.project_id].name.lower()
                if t.project_id in projects_dict
                else ""
            ),
            (
                section_mapping[t.section_id].name.lower()
                if t.section_id in section_mapping
                else ""
            ),
            t.content.lower(),
        )
    )

    if output_json:
        data = []
        for task in tasks:
            p_name = (
                projects_dict[task.project_id].name
                if task.project_id in projects_dict
                else None
            )
            s_name = (
                section_mapping[task.section_id].name
                if (show_section_col and task.section_id in section_mapping)
                else None
            )
            parent_str = (
                task_dict[task.parent_id].content
                if (show_subtasks and task.parent_id in task_dict)
                else None
            )
            entry = {
                "id": task.id,
                "content": maybe_strip_emojis(task.content),
                "project": maybe_strip_emojis(p_name) if p_name else None,
                "priority": task.priority,
                "due": maybe_strip_emojis(task.due.string) if task.due else None,
                "section": maybe_strip_emojis(s_name) if s_name else None,
                "parent": maybe_strip_emojis(parent_str) if parent_str else None,
                "labels": task.labels if task.labels else None,
            }
            data.append(entry)
        console.print_json(json.dumps(data))
        return

    headers = []
    if show_ids:
        headers.append(header_cell("ID", style="cyan"))
    headers.append(header_cell("Content", style="white"))
    if show_subtasks:
        headers.append(header_cell("Parent Task", style="white"))
    headers.append(header_cell("Project", style="magenta"))
    if show_section_col:
        headers.append(header_cell("Section", style="magenta"))
    headers.append(header_cell("Priority", style="yellow"))
    headers.append(header_cell("Due", style="green"))
    headers.append(header_cell("Labels", style="cyan"))

    rows = []
    for task in tasks:
        row = []
        if show_ids:
            row.append(styled_cell(str(task.id), style="cyan", allow_na=False))
        row.append(styled_cell(maybe_strip_emojis(task.content), style="white"))
        if show_subtasks:
            parent_str = None
            if task.parent_id and task.parent_id in task_dict:
                parent_str = maybe_strip_emojis(task_dict[task.parent_id].content)
            row.append(styled_cell(parent_str, style="white"))
        proj_str = None
        if task.project_id in projects_dict:
            proj_str = maybe_strip_emojis(projects_dict[task.project_id].name)
        row.append(styled_cell(proj_str, style="magenta"))
        if show_section_col:
            sname = None
            if task.section_id in section_mapping:
                sname = maybe_strip_emojis(section_mapping[task.section_id].name)
            row.append(styled_cell(sname, style="magenta"))
        row.append(styled_cell(str(task.priority), style="yellow", allow_na=False))
        due_str = maybe_strip_emojis(task.due.string) if task.due else None
        row.append(styled_cell(due_str, style="green"))
        labels_str = None
        if task.labels:
            labels = [maybe_strip_emojis(label) for label in task.labels]
            labels_str = ", ".join(labels)
        row.append(styled_cell(labels_str, style="cyan"))
        rows.append(row)

    for line in render_aligned_table(headers, rows):
        console.print(line)


async def create_task(
    client,
    content,
    priority=None,
    due=None,
    reminder=None,
    project_name=None,
    section_name=None,
    labels=None,
    force=False,
):
    pid = None
    sid = None
    if project_name:
        pid = await find_project_id_partial(client, project_name)
        if not pid:
            console_err.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)
        await log_operating_on_project(client, pid)
    if section_name:
        if not pid:
            console_err.print("[red]--section requires --project[/red]")
            sys.exit(1)
        sid = await find_section_id_partial(client, pid, section_name)
        if not sid:
            console_err.print(f"[red]No section found matching '{section_name}'[/red]")
            sys.exit(1)
    # Validate labels if provided
    valid_labels = []
    if labels:
        valid_labels = await validate_labels(client, labels)

    if not force:
        tasks = await client.get_tasks(pid) if pid else await client.get_tasks()
        for t in tasks:
            if remove_emojis(t.content.strip().lower()) == remove_emojis(
                content.strip().lower()
            ):
                console_err.print(
                    f"[yellow]Task {task_str(t)} already exists, skipping.[/yellow]"
                )
                return
    kwargs = {"content": content}
    if priority is not None:
        kwargs["priority"] = priority
    if due:
        kwargs["due_string"] = due
    if pid:
        kwargs["project_id"] = pid
    if sid:
        kwargs["section_id"] = sid
    if valid_labels:
        kwargs["labels"] = valid_labels
    try:
        new_task = await asyncio.to_thread(client.api.add_task, **kwargs)
        project_note = ""
        project_id = getattr(new_task, "project_id", None)
        if project_id:
            project_note = f" in project ID [{ID_COLOR}]{project_id}[/{ID_COLOR}]"
            try:
                projects = await client.get_projects()
            except Exception as exc:
                LOGGER.debug(
                    "Unable to fetch projects when reporting task creation: %s", exc
                )
            else:
                for project in projects:
                    if project.id == project_id:
                        project_note = f" in {project_str(project)}"
                        break
        console.print(f"[green]Created {task_str(new_task)}{project_note}[/green]")
        client.invalidate_tasks(pid)
        if reminder:
            try:
                await asyncio.to_thread(
                    client.api.add_reminder, task_id=new_task.id, due_string=reminder
                )
                console.print(f"[green]Reminder set for {task_str(new_task)}[/green]")
            except Exception as e:
                console_err.print(f"[yellow]Failed to add reminder: {e}[/yellow]")
    except Exception as e:
        console_err.print(f"[red]Failed creating task '{content}': {e}[/red]")
        sys.exit(1)


async def update_task(
    client,
    content=None,
    new_content=None,
    priority=None,
    due=None,
    reminder=None,
    project_name=None,
    section_name=None,
    labels=None,
):
    identifier = content.strip() if content else None
    if not identifier:
        console_err.print("[red]Task content or ID is required.[/red]")
        sys.exit(2)
    target, pid, lookup_is_id = await resolve_task_identifier(
        client, identifier, project_name=project_name
    )
    if not target:
        if lookup_is_id:
            console_err.print(
                f"[yellow]No matching task found for ID '{identifier}'.[/yellow]"
            )
        else:
            console_err.print(
                f"[yellow]No matching task found for '{identifier}'.[/yellow]"
            )
        return

    # Validate labels if provided
    valid_labels = []
    if labels:
        valid_labels = await validate_labels(client, labels)

    update_kwargs = {}
    if new_content:
        update_kwargs["content"] = new_content
    if priority is not None:
        update_kwargs["priority"] = priority
    if due:
        update_kwargs["due_string"] = due
    if valid_labels:
        update_kwargs["labels"] = valid_labels
    try:
        updated = await asyncio.to_thread(
            client.api.update_task, target.id, **update_kwargs
        )
        console.print(f"[green]Updated task: {task_str(updated)}[/green]")
        invalidate_pid = pid if pid is not None else getattr(target, "project_id", None)
        client.invalidate_tasks(invalidate_pid)
    except Exception as e:
        console_err.print(f"[red]Failed to update task '{identifier}': {e}[/red]")
        sys.exit(1)


async def mark_task_done(client, content=None, project_name=None):
    identifier = content.strip() if content else None
    if not identifier:
        console_err.print("[red]Task content or ID is required.[/red]")
        sys.exit(2)
    target, pid, lookup_is_id = await resolve_task_identifier(
        client, identifier, project_name=project_name
    )
    if target:
        try:
            await asyncio.to_thread(client.api.close_task, target.id)
            console.print(f"[green]Marked done: {task_str(target)}[/green]")
            invalidate_pid = (
                pid if pid is not None else getattr(target, "project_id", None)
            )
            client.invalidate_tasks(invalidate_pid)
            return
        except Exception as e:
            console_err.print(
                f"[red]Failed to mark done: {task_str(target)}: {e}[/red]"
            )
            sys.exit(1)
    if lookup_is_id:
        console_err.print(
            f"[yellow]No matching task found for ID '{identifier}'.[/yellow]"
        )
    else:
        console_err.print(
            f"[yellow]No matching task found for '{identifier}'.[/yellow]"
        )


async def delete_task(
    client,
    contents=None,
    project_name=None,
    todoist_filter=None,
    content_pattern=None,
):
    identifiers = []
    if contents:
        for entry in contents:
            if not entry:
                continue
            stripped = entry.strip()
            if stripped:
                identifiers.append(stripped)

    pattern_input = content_pattern.strip() if content_pattern else None

    if not identifiers and not pattern_input:
        console_err.print("[red]Task content or ID is required.[/red]")
        sys.exit(2)

    project_id = None
    fatal_error = False

    async def delete_task_object(task, pid_hint):
        nonlocal fatal_error
        try:
            await asyncio.to_thread(client.api.delete_task, task.id)
            console.print(f"[green]Deleted {task_str(task)}[/green]")
            invalidate_pid = (
                pid_hint if pid_hint is not None else getattr(task, "project_id", None)
            )
            client.invalidate_tasks(invalidate_pid)
            return DeletionResult(True, False)
        except Exception as exc:
            console_err.print(f"[red]Failed to delete {task_str(task)}: {exc}[/red]")
            fatal_error = True
            return DeletionResult(False, True)

    async def delete_matches_for_pattern(pid, pattern_source):
        compiled = compile_content_pattern(pattern_source)
        scoped_tasks = await client.get_tasks(project_id=pid, filter_str=todoist_filter)
        matches = [t for t in scoped_tasks if task_matches_pattern(t, compiled)]
        if not matches:
            console_err.print(
                f"[yellow]No task matching pattern '{pattern_source}'.[/yellow]"
            )
            return DeletionResult(False, False)
        result = DeletionResult(False, False)
        for match in matches:
            deletion = await delete_task_object(match, pid)
            result = DeletionResult(
                result.deleted or deletion.deleted,
                result.fatal or deletion.fatal,
            )
        return result

    async def delete_identifier(identifier):
        nonlocal project_id
        resolved_project_name = project_name if project_id is None else None
        target, pid, lookup_is_id = await resolve_task_identifier(
            client,
            identifier,
            project_name=resolved_project_name,
            project_id=project_id,
            todoist_filter=todoist_filter,
            content_pattern=content_pattern,
        )
        if project_id is None:
            project_id = pid
        if target:
            return await delete_task_object(target, pid)

        pattern_source = None
        if content_pattern and content_pattern.strip():
            pattern_source = content_pattern.strip()
        elif identifier and not lookup_is_id:
            pattern_source = identifier

        if pattern_source:
            return await delete_matches_for_pattern(pid, pattern_source)

        if lookup_is_id:
            console_err.print(f"[yellow]No task matching ID '{identifier}'.[/yellow]")
        else:
            console_err.print(f"[yellow]No task matching '{identifier}'.[/yellow]")
        return DeletionResult(False, False)

    for identifier in identifiers:
        await delete_identifier(identifier)

    if not identifiers and pattern_input:
        async def resolve_project_for_pattern():
            nonlocal project_id
            if project_id is not None:
                return project_id
            if not project_name:
                return None
            pid = await find_project_id_partial(client, project_name)
            if not pid:
                console_err.print(
                    f"[red]No project found matching '{project_name}'.[/red]"
                )
                sys.exit(1)
            projects = await client.get_projects()
            project_lookup = {p.id: p for p in projects}
            project_obj = project_lookup.get(pid)
            await log_operating_on_project(
                client, pid, project_obj=project_obj
            )
            project_id = pid
            return pid

        pid = await resolve_project_for_pattern()
        await delete_matches_for_pattern(pid, pattern_input)

    if fatal_error:
        sys.exit(1)


###############################################################################
# Project Commands
###############################################################################
async def list_projects(client, show_ids=False, output_json=False):
    try:
        projects = await client.get_projects()
    except Exception as e:
        console_err.print(f"[red]Failed to fetch projects: {e}[/red]")
        sys.exit(1)
    projects.sort(key=lambda x: x.name.lower())
    if output_json:
        data = [{"id": p.id, "name": maybe_strip_emojis(p.name)} for p in projects]
        console.print_json(json.dumps(data))
        return
    headers = []
    if show_ids:
        headers.append(header_cell("ID", style="cyan"))
    headers.append(header_cell("Name", style="white"))

    rows = []
    for p in projects:
        row = []
        if show_ids:
            row.append(styled_cell(str(p.id), style="cyan", allow_na=False))
        row.append(styled_cell(maybe_strip_emojis(p.name), style="white", allow_na=False))
        rows.append(row)

    for line in render_aligned_table(headers, rows):
        console.print(line)


async def create_project(client, name):
    try:
        projects = await client.get_projects()
        for p in projects:
            if p.name.strip().lower() == name.strip().lower():
                console_err.print(
                    f"[yellow]Project {project_str(p)} already exists.[/yellow]"
                )
                return
        newp = await asyncio.to_thread(client.api.add_project, name=name)
        console.print(f"[green]Created project {project_str(newp)}[/green]")
        client.invalidate_projects()
    except Exception as e:
        console_err.print(f"[red]Failed to create project '{name}': {e}[/red]")
        sys.exit(1)


async def update_project(client, name, new_name):
    projects = await client.get_projects()
    target = None
    for p in projects:
        if p.name.strip().lower() == name.strip().lower():
            target = p
            break
    if not target:
        console_err.print(f"[yellow]No matching project found for '{name}'.[/yellow]")
        return

    await log_operating_on_project(client, target.id, project_obj=target)

    try:
        updated = await asyncio.to_thread(
            client.api.update_project, target.id, name=new_name
        )
        console.print(f"[green]Updated project: {project_str(updated)}[/green]")
        client.invalidate_projects()
    except Exception as e:
        console_err.print(f"[red]Failed to update project '{name}': {e}[/red]")
        sys.exit(1)


async def delete_project(client, name_partial):
    pid = await find_project_id_partial(client, name_partial)
    if not pid:
        console_err.print(
            f"[yellow]No project found matching '{name_partial}'.[/yellow]"
        )
        return

    await log_operating_on_project(client, pid)

    try:
        await asyncio.to_thread(client.api.delete_project, pid)
        console.print(f"[green]Deleted project ID {pid}[/green]")
        client.invalidate_projects()
    except Exception as e:
        console_err.print(f"[red]Failed to delete project '{name_partial}': {e}[/red]")
        sys.exit(1)


async def clear_project(client, name_partial, delete_sections=False):
    pid = await find_project_id_partial(client, name_partial)
    if not pid:
        console_err.print(
            f"[yellow]No project found matching '{name_partial}'.[/yellow]"
        )
        return

    projects = await client.get_projects()
    project_lookup = {p.id: p for p in projects}
    project_obj = project_lookup.get(pid)
    project_desc = (
        project_str(project_obj) if project_obj else f"project ID {pid}"
    )

    await log_operating_on_project(client, pid, project_obj=project_obj)

    fatal_error = False

    try:
        tasks = await client.get_tasks(project_id=pid)
    except Exception as exc:
        console_err.print(
            f"[red]Failed to fetch tasks for {project_desc}: {exc}[/red]"
        )
        sys.exit(1)

    if tasks:
        for task in tasks:
            try:
                await asyncio.to_thread(client.api.delete_task, task.id)
                console.print(f"[green]Deleted {task_str(task)}[/green]")
            except Exception as exc:
                console_err.print(
                    f"[red]Failed to delete {task_str(task)}: {exc}[/red]"
                )
                fatal_error = True
        client.invalidate_tasks(pid)
    else:
        console.print(
            f"[yellow]No tasks found in {project_desc} to delete.[/yellow]"
        )

    if delete_sections:
        try:
            sections = await client.get_sections(pid)
        except Exception as exc:
            console_err.print(
                f"[red]Failed to fetch sections for {project_desc}: {exc}[/red]"
            )
            fatal_error = True
        else:
            if sections:
                for section in sections:
                    try:
                        await asyncio.to_thread(
                            client.api.delete_section, section.id
                        )
                        console.print(
                            f"[green]Deleted section {section_str(section)}[/green]"
                        )
                    except Exception as exc:
                        console_err.print(
                            f"[red]Failed to delete section {section_str(section)}: {exc}[/red]"
                        )
                        fatal_error = True
                client.invalidate_sections(pid)
            else:
                console.print(
                    f"[yellow]No sections found in {project_desc} to delete.[/yellow]"
                )

    if fatal_error:
        sys.exit(1)


###############################################################################
# Section Commands
###############################################################################
async def list_sections(client, show_ids, project_name, output_json=False):
    pid = await find_project_id_partial(client, project_name)
    if not pid:
        console_err.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)

    await log_operating_on_project(client, pid)

    try:
        secs = await client.get_sections(pid)
    except Exception as e:
        console_err.print(f"[red]Failed fetching sections: {e}[/red]")
        sys.exit(1)
    secs.sort(key=lambda x: x.name.lower())
    if output_json:
        data = [{"id": s.id, "name": maybe_strip_emojis(s.name)} for s in secs]
        console.print_json(json.dumps(data))
        return
    headers = []
    if show_ids:
        headers.append(header_cell("ID", style="cyan"))
    headers.append(header_cell("Name", style="white"))

    rows = []
    for s in secs:
        row = []
        if show_ids:
            row.append(styled_cell(str(s.id), style="cyan", allow_na=False))
        row.append(styled_cell(maybe_strip_emojis(s.name), style="white", allow_na=False))
        rows.append(row)

    for line in render_aligned_table(headers, rows):
        console.print(line)


async def create_section(client, project_name, section_name):
    pid = await find_project_id_partial(client, project_name)
    if not pid:
        console_err.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)

    await log_operating_on_project(client, pid)

    try:
        secs = await client.get_sections(pid)
        for s in secs:
            if s.name.strip().lower() == section_name.strip().lower():
                console_err.print(
                    f"[yellow]Section {section_str(s)} already exists.[/yellow]"
                )
                return
        new_sec = await asyncio.to_thread(
            client.api.add_section, name=section_name, project_id=pid
        )
        console.print(f"[green]Created section {section_str(new_sec)}[/green]")
        client.invalidate_sections(pid)
    except Exception as e:
        console_err.print(f"[red]Failed to create section '{section_name}': {e}[/red]")
        sys.exit(1)


async def update_section(client, project_name, section_name, new_name):
    pid = await find_project_id_partial(client, project_name)
    if not pid:
        console_err.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)
    await log_operating_on_project(client, pid)
    secs = await client.get_sections(pid)
    target = None
    for s in secs:
        if s.name.strip().lower() == section_name.strip().lower():
            target = s
            break
    if not target:
        console_err.print(
            f"[yellow]No matching section found for '{section_name}' in project '{project_name}'.[/yellow]"
        )
        return
    try:
        updated = await asyncio.to_thread(
            client.api.update_section, target.id, name=new_name
        )
        console.print(f"[green]Updated section: {section_str(updated)}[/green]")
        client.invalidate_sections(pid)
    except Exception as e:
        console_err.print(f"[red]Failed to update section '{section_name}': {e}[/red]")
        sys.exit(1)


async def delete_section(client, project_name, section_partial):
    pid = await find_project_id_partial(client, project_name)
    if not pid:
        console_err.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)
    await log_operating_on_project(client, pid)
    try:
        secs = await client.get_sections(pid)
        match_id = None
        match_obj = None
        ssearch = section_partial.lower()
        for s in secs:
            if ssearch in s.name.lower():
                match_id = s.id
                match_obj = s
                break
        if not match_id:
            console_err.print(
                f"[yellow]No section found matching '{section_partial}'.[/yellow]"
            )
            return
        await asyncio.to_thread(client.api.delete_section, match_id)
        console.print(f"[green]Deleted section {section_str(match_obj)}[/green]")
        client.invalidate_sections(pid)
    except Exception as e:
        console_err.print(
            f"[red]Failed to delete section '{section_partial}': {e}[/red]"
        )
        sys.exit(1)


###############################################################################
# Label Commands
###############################################################################
async def list_labels(client, show_ids=False, output_json=False):
    try:
        labels = await asyncio.to_thread(consume_paginated, client.api.get_labels)
    except Exception as e:
        console_err.print(f"[red]Failed to fetch labels: {e}[/red]")
        sys.exit(1)
    labels.sort(key=lambda la: la.name.lower())
    if output_json:
        data = [{"id": la.id, "name": maybe_strip_emojis(la.name)} for la in labels]
        console.print_json(json.dumps(data))
        return
    headers = []
    if show_ids:
        headers.append(header_cell("ID", style="cyan"))
    headers.append(header_cell("Name", style="white"))

    rows = []
    for la in labels:
        row = []
        if show_ids:
            row.append(styled_cell(str(la.id), style="cyan", allow_na=False))
        row.append(styled_cell(maybe_strip_emojis(la.name), style="white", allow_na=False))
        rows.append(row)

    for line in render_aligned_table(headers, rows):
        console.print(line)


async def create_label(client, name):
    try:
        labels = await asyncio.to_thread(consume_paginated, client.api.get_labels)
        for la in labels:
            if la.name.strip().lower() == name.strip().lower():
                console_err.print(f"[yellow]Label {la.name} already exists.[/yellow]")
                return
        new_label = await asyncio.to_thread(client.api.add_label, name=name)
        console.print(
            f"[green]Created label {new_label.name} (ID: {new_label.id})[/green]"
        )
    except Exception as e:
        console_err.print(f"[red]Failed to create label '{name}': {e}[/red]")
        sys.exit(1)


async def update_label(client, name, new_name):
    try:
        labels = await asyncio.to_thread(consume_paginated, client.api.get_labels)
        target = None
        for la in labels:
            if la.name.strip().lower() == name.strip().lower():
                target = la
                break
        if not target:
            console_err.print(f"[yellow]No matching label found for '{name}'.[/yellow]")
            return
        updated = await asyncio.to_thread(
            client.api.update_label, target.id, name=new_name
        )
        console.print(
            f"[green]Updated label: {updated.name} (ID: {updated.id})[/green]"
        )
    except Exception as e:
        console_err.print(f"[red]Failed to update label '{name}': {e}[/red]")
        sys.exit(1)


async def delete_label(client, name_partial):
    try:
        labels = await asyncio.to_thread(consume_paginated, client.api.get_labels)
        target = None
        for la in labels:
            if name_partial.lower() in la.name.lower():
                target = la
                break
        if not target:
            console_err.print(
                f"[yellow]No label found matching '{name_partial}'.[/yellow]"
            )
            return
        await asyncio.to_thread(client.api.delete_label, target.id)
        console.print(f"[green]Deleted label {target.name} (ID: {target.id})[/green]")
    except Exception as e:
        console_err.print(f"[red]Failed to delete label '{name_partial}': {e}[/red]")
        sys.exit(1)


###############################################################################
# Dump Command
###############################################################################
async def dump_all_data(client, output_path=None, indent=None):
    try:
        projects = await client.get_projects()
        tasks = await client.get_tasks()
        sections = []
        seen_section_ids = set()
        for project in projects:
            try:
                project_sections = await client.get_sections(project.id)
            except Exception as exc:
                console_err.print(
                    f"[red]Failed to fetch sections for project {project.id}: {exc}[/red]"
                )
                sys.exit(1)
            for section in project_sections:
                if section.id in seen_section_ids:
                    continue
                seen_section_ids.add(section.id)
                sections.append(section)
        labels = await asyncio.to_thread(consume_paginated, client.api.get_labels)
    except Exception as exc:
        console_err.print(f"[red]Failed to fetch Todoist data: {exc}[/red]")
        sys.exit(1)

    shared_labels = []
    if hasattr(client.api, "get_shared_labels"):
        try:
            shared_labels = await asyncio.to_thread(
                consume_paginated, client.api.get_shared_labels
            )
        except Exception as exc:
            console_err.print(f"[red]Failed to fetch shared labels: {exc}[/red]")
            sys.exit(1)

    comments_by_project = {}
    if hasattr(client.api, "get_comments"):
        for project in projects:
            try:
                project_comments = await asyncio.to_thread(
                    consume_paginated, client.api.get_comments, project_id=project.id
                )
            except Exception as exc:
                console_err.print(
                    f"[red]Failed to fetch comments for project {project.id}: {exc}[/red]"
                )
                sys.exit(1)
            if project_comments:
                comments_by_project[str(project.id)] = project_comments

    collaborators_by_project = {}
    if hasattr(client.api, "get_collaborators"):
        for project in projects:
            try:
                project_collaborators = await asyncio.to_thread(
                    consume_paginated,
                    client.api.get_collaborators,
                    project_id=project.id,
                )
            except Exception as exc:
                console_err.print(
                    f"[red]Failed to fetch collaborators for project {project.id}: {exc}[/red]"
                )
                sys.exit(1)
            if project_collaborators:
                collaborators_by_project[str(project.id)] = project_collaborators

    dump_payload = {
        "projects": projects,
        "sections": sections,
        "tasks": tasks,
        "labels": labels,
    }
    if shared_labels:
        dump_payload["shared_labels"] = shared_labels
    if comments_by_project:
        dump_payload["comments"] = {"by_project": comments_by_project}
    if collaborators_by_project:
        dump_payload["collaborators"] = {"by_project": collaborators_by_project}

    serialized = serialize_todoist_object(dump_payload)
    indent_value = 2 if indent is None else indent
    json_output = json.dumps(
        serialized, ensure_ascii=False, indent=indent_value, sort_keys=True
    )

    if output_path:
        try:
            with open(output_path, "w", encoding="utf-8") as handle:
                handle.write(json_output)
        except Exception as exc:
            console_err.print(
                f"[red]Failed to write dump to {output_path}: {exc}[/red]"
            )
            sys.exit(1)
        console.print(f"[green]Wrote Todoist data dump to {output_path}[/green]")
        return

    console.print_json(json_output)


###############################################################################
# Main with Subparsers, Aliases, and Cumulative Filters
###############################################################################
async def async_main():
    global STRIP_EMOJIS

    # Define global alias dictionaries.
    cmd_aliases = {
        "task": ["tasks", "t", "ta"],
        "project": ["projects", "proj", "pro", "p"],
        "section": ["sections", "sect", "sec", "s"],
        "label": ["labels", "lab", "lbl"],
        "dump": ["export", "backup"],
    }
    subcmd_aliases = {
        "list": ["ls", "l"],
        "create": ["cr", "c", "add", "a"],
        "update": ["upd", "u"],
        "delete": ["del", "d", "remove", "rm"],
        "today": ["td", "to"],
    }

    # Create a common parent parser for --project and --section options.
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "-p",
        "--project",
        help="Project partial name match",
        default=argparse.SUPPRESS,
    )
    common_parser.add_argument(
        "-S",
        "--section",
        nargs="?",
        const=SECTION_ALL_SENTINEL,
        help=(
            "Section partial name match. For 'project clear', pass the flag without a value"
            " to delete all sections in the project."
        ),
        default=argparse.SUPPRESS,
    )
    common_parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Enable debug logging",
        default=argparse.SUPPRESS,
    )
    common_parser.add_argument(
        "-E",
        "--strip-emojis",
        action="store_true",
        help="Remove emojis from displayed text.",
        default=argparse.SUPPRESS,
    )
    common_parser.add_argument(
        "-i",
        "--ids",
        action="store_true",
        help="Show ID columns",
        default=argparse.SUPPRESS,
    )
    common_parser.add_argument(
        "-j",
        "--json",
        action="store_true",
        help="Output JSON",
        default=argparse.SUPPRESS,
    )

    # Main parser (global options can appear before the subcommand)
    parser = argparse.ArgumentParser(
        prog="tdc",
        formatter_class=RawTextRichHelpFormatter,
        description=("[bold cyan]CLI for Todoist[/bold cyan]"),
        parents=[common_parser],
    )

    # Global options
    parser.add_argument(
        "-k",
        "--api-key",
        "--api-token",
        help="Your Todoist API key",
        required=not bool(API_TOKEN),
    )
    parser.add_argument(
        "-s", "--subtasks", action="store_true", help="Include subtasks"
    )

    subparsers = parser.add_subparsers(
        dest="command", required=True, help="Subcommand to run"
    )

    # Top-level command: task
    task_parser = subparsers.add_parser(
        "task",
        aliases=cmd_aliases["task"],
        help="Manage tasks",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    task_subparsers = task_parser.add_subparsers(
        dest="task_command", required=True, help="Task subcommand"
    )
    list_task_parser = task_subparsers.add_parser(
        "list",
        aliases=subcmd_aliases["list"],
        help="List tasks",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    # Extra filtering options (these flags are cumulative)
    list_task_parser.add_argument(
        "--today", action="store_true", help="Limit to tasks due today"
    )
    list_task_parser.add_argument(
        "--overdue", action="store_true", help="Limit to tasks that are overdue"
    )
    list_task_parser.add_argument(
        "--recurring", action="store_true", help="Limit to recurring tasks"
    )
    list_task_parser.add_argument(
        "--filter",
        dest="todoist_filter",
        help="Todoist filter query to apply when fetching tasks",
    )
    list_task_parser.add_argument(
        "content_pattern",
        nargs="?",
        help="Regex (case-insensitive) to match task content",
    )
    task_subparsers.add_parser(
        "today",
        aliases=subcmd_aliases["today"],
        help="List tasks due today or overdue",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    create_task_parser = task_subparsers.add_parser(
        "create",
        aliases=subcmd_aliases["create"],
        help="Create a new task",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    create_task_parser.add_argument("content", help="Task content")
    create_task_parser.add_argument("--priority", type=int, default=None)
    create_task_parser.add_argument("--due", default=None)
    create_task_parser.add_argument("--reminder", default=None)
    create_task_parser.add_argument(
        "--label",
        action="append",
        dest="labels",
        default=None,
        help="Add label to task (can be used multiple times: --label l1 --label l2)",
    )
    create_task_parser.add_argument(
        "--force",
        default=False,
        action="store_true",
        help="Allow creating tasks even though a task with the same content already exists",
    )
    update_task_parser = task_subparsers.add_parser(
        "update",
        aliases=subcmd_aliases["update"],
        help="Update a task",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    update_task_parser.add_argument(
        "content",
        nargs="?",
        help="Existing task content or ID to match (case-insensitive for content)",
    )
    update_task_parser.add_argument("--new-content", help="New task content")
    update_task_parser.add_argument("--priority", type=int, default=None)
    update_task_parser.add_argument("--due", help="New due string")
    update_task_parser.add_argument(
        "--label",
        action="append",
        dest="labels",
        default=None,
        help="Set labels for task (can be used multiple times: --label l1 --label l2). This replaces all existing labels.",
    )
    done_parser = task_subparsers.add_parser(
        "done",
        help="Mark a task as done",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    done_parser.add_argument(
        "content",
        nargs="?",
        help="Task content or ID to mark done (case-insensitive for content)",
    )
    delete_task_parser = task_subparsers.add_parser(
        "delete",
        aliases=subcmd_aliases["delete"],
        help="Delete a task",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    delete_task_parser.add_argument(
        "contents",
        nargs="*",
        help="Task content or IDs to delete (case-insensitive for content)",
    )
    delete_task_parser.add_argument(
        "--filter",
        dest="todoist_filter",
        help="Todoist filter query to apply when resolving the task",
    )
    delete_task_parser.add_argument(
        "--pattern",
        dest="content_pattern",
        help="Regex (case-insensitive) to match task content when resolving",
        default=argparse.SUPPRESS,
    )

    # Top-level command: project
    project_parser = subparsers.add_parser(
        "project",
        aliases=cmd_aliases["project"],
        help="Manage projects",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    project_subparsers = project_parser.add_subparsers(
        dest="project_command", required=True, help="Project subcommand"
    )
    project_subparsers.add_parser(
        "list",
        aliases=subcmd_aliases["list"],
        help="List projects",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    proj_create = project_subparsers.add_parser(
        "create",
        aliases=subcmd_aliases["create"],
        help="Create a new project",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    proj_create.add_argument("name", help="Project name")
    proj_update = project_subparsers.add_parser(
        "update",
        aliases=subcmd_aliases["update"],
        help="Update a project",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    proj_update.add_argument("name", help="Existing project name to match")
    proj_update.add_argument("--new-name", required=True, help="New project name")
    proj_delete = project_subparsers.add_parser(
        "delete",
        aliases=subcmd_aliases["delete"],
        help="Delete a project",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    proj_delete.add_argument("name", help="Project name (or partial)")

    proj_clear = project_subparsers.add_parser(
        "clear",
        help="Delete all tasks in a project",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    proj_clear.add_argument("name", help="Project name (or partial)")

    # Top-level command: section
    section_parser = subparsers.add_parser(
        "section",
        aliases=cmd_aliases["section"],
        help="Manage sections",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    section_subparsers = section_parser.add_subparsers(
        dest="section_command", required=True, help="Section subcommand"
    )
    section_subparsers.add_parser(
        "list",
        aliases=subcmd_aliases["list"],
        help="List sections",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    sec_create = section_subparsers.add_parser(
        "create",
        aliases=subcmd_aliases["create"],
        help="Create a new section",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    sec_create.add_argument("section", help="Section name")

    sec_update = section_subparsers.add_parser(
        "update",
        aliases=subcmd_aliases["update"],
        help="Update a section",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    sec_update.add_argument("section", help="Section name")
    sec_update.add_argument("new_section_name", help="New section name")
    sec_delete = section_subparsers.add_parser(
        "delete",
        aliases=subcmd_aliases["delete"],
        help="Delete a section",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    sec_delete.add_argument("section", help="Section name (or partial)")

    # Top-level command: label
    label_parser = subparsers.add_parser(
        "label",
        aliases=cmd_aliases["label"],
        help="Manage labels",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    label_subparsers = label_parser.add_subparsers(
        dest="label_command", required=True, help="Label subcommand"
    )
    label_subparsers.add_parser(
        "list",
        aliases=subcmd_aliases["list"],
        help="List labels",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    lab_create = label_subparsers.add_parser(
        "create",
        aliases=subcmd_aliases["create"],
        help="Create a new label",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    lab_create.add_argument("name", help="Label name")
    lab_update = label_subparsers.add_parser(
        "update",
        aliases=subcmd_aliases["update"],
        help="Update a label",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    lab_update.add_argument("name", help="Existing label name to match")
    lab_update.add_argument("--new-name", required=True, help="New label name")
    lab_delete = label_subparsers.add_parser(
        "delete",
        aliases=subcmd_aliases["delete"],
        help="Delete a label",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    lab_delete.add_argument("name", help="Label name (or partial)")

    dump_parser = subparsers.add_parser(
        "dump",
        aliases=cmd_aliases["dump"],
        help="Dump all Todoist data as JSON",
        formatter_class=RawTextRichHelpFormatter,
        parents=[common_parser],
    )
    dump_parser.add_argument(
        "-o",
        "--output",
        help="Write JSON dump to a file instead of stdout",
        default=argparse.SUPPRESS,
    )
    dump_parser.add_argument(
        "--indent",
        type=int,
        help="Indent level for JSON output (default: 2)",
        default=argparse.SUPPRESS,
    )

    args = parser.parse_args()

    for attr, default in (
        ("project", None),
        ("section", None),
        ("debug", False),
        ("strip_emojis", False),
        ("ids", False),
        ("json", False),
        ("todoist_filter", None),
        ("content_pattern", None),
        ("output", None),
        ("indent", None),
        ("labels", None),
    ):
        if not hasattr(args, attr):
            setattr(args, attr, default)

    delete_all_sections = args.section == SECTION_ALL_SENTINEL
    if delete_all_sections:
        args.section = None
    setattr(args, "delete_all_sections", delete_all_sections)

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
        LOGGER.debug("args: %s", args)

    # Normalize top-level command using our aliases.
    for canonical, aliases in cmd_aliases.items():
        if args.command == canonical or args.command in aliases:
            args.command = canonical
            break
    # Normalize subcommand for each top-level command.
    if args.command == "task":
        for canonical, aliases in subcmd_aliases.items():
            if args.task_command == canonical or args.task_command in aliases:
                args.task_command = canonical
                break
    elif args.command == "project":
        for canonical, aliases in subcmd_aliases.items():
            if args.project_command == canonical or args.project_command in aliases:
                args.project_command = canonical
                break
    elif args.command == "section":
        for canonical, aliases in subcmd_aliases.items():
            if args.section_command == canonical or args.section_command in aliases:
                args.section_command = canonical
                break
    elif args.command == "label":
        for canonical, aliases in subcmd_aliases.items():
            if args.label_command == canonical or args.label_command in aliases:
                args.label_command = canonical
                break

    if args.delete_all_sections:
        project_command = getattr(args, "project_command", None)
        if not (args.command == "project" and project_command == "clear"):
            console_err.print(
                "[red]--section for this command requires a section name.[/red]"
            )
            sys.exit(2)

    STRIP_EMOJIS = args.strip_emojis
    api_key = args.api_key or API_TOKEN
    if not api_key:
        console_err.print("[red]Error: API key is required.[/red]")
        sys.exit(2)
    api = TodoistAPI(api_key)
    client = TodoistClient(api)

    # Dispatch based on subcommand
    if args.command == "task":
        if args.task_command == "list":
            await list_tasks(
                client,
                show_ids=args.ids,
                show_subtasks=args.subtasks,
                project_name=args.project,
                section_name=args.section,
                output_json=args.json,
                filter_today=args.today,
                filter_overdue=args.overdue,
                filter_recurring=args.recurring,
                todoist_filter=args.todoist_filter,
                content_pattern=args.content_pattern,
            )
        if args.task_command == "today":
            # "today" subcommand now shows tasks due today or overdue (union)
            await list_tasks(
                client,
                show_ids=args.ids,
                show_subtasks=args.subtasks,
                project_name=args.project,
                section_name=args.section,
                output_json=args.json,
                filter_today=True,
                filter_overdue=True,
                todoist_filter=args.todoist_filter,
                content_pattern=args.content_pattern,
            )
        elif args.task_command == "create":
            await create_task(
                client,
                content=args.content,
                priority=args.priority,
                due=args.due,
                reminder=args.reminder,
                project_name=args.project,
                section_name=args.section,
                labels=args.labels,
                force=args.force,
            )
        elif args.task_command == "update":
            await update_task(
                client,
                content=args.content,
                new_content=args.new_content,
                priority=args.priority,
                due=args.due,
                project_name=args.project,
                section_name=args.section,
                labels=args.labels,
            )
        elif args.task_command == "done":
            await mark_task_done(
                client,
                content=args.content,
                project_name=args.project,
            )
        elif args.task_command == "delete":
            await delete_task(
                client,
                contents=args.contents,
                project_name=args.project,
                todoist_filter=args.todoist_filter,
                content_pattern=args.content_pattern,
            )

    elif args.command == "project":
        if args.project_command == "list":
            await list_projects(client, show_ids=args.ids, output_json=args.json)
        elif args.project_command == "create":
            await create_project(client, name=args.name)
        elif args.project_command == "update":
            await update_project(client, name=args.name, new_name=args.new_name)
        elif args.project_command == "delete":
            await delete_project(client, name_partial=args.name)
        elif args.project_command == "clear":
            await clear_project(
                client,
                name_partial=args.name,
                delete_sections=args.delete_all_sections,
            )

    elif args.command == "section":
        if args.section_command == "list":
            if not args.project:
                console_err.print(
                    "[red]Please provide --project for listing sections[/red]"
                )
                sys.exit(2)
            await list_sections(
                client,
                show_ids=args.ids,
                project_name=args.project,
                output_json=args.json,
            )
        elif args.section_command == "create":
            if not args.section:
                console_err.print(
                    "[red]Please provide section name for creating a section[/red]"
                )
                sys.exit(2)
            if not args.project:
                console_err.print(
                    "[red]Please provide --project for creating a section[/red]"
                )
                sys.exit(2)
            await create_section(
                client, project_name=args.project, section_name=args.section
            )
        elif args.section_command == "update":
            if not args.section:
                console_err.print(
                    "[red]Please provide a section for updating a section.[/red]"
                )
                sys.exit(2)
            if not args.project:
                console_err.print(
                    "[red]Please provide --project for updating a section[/red]"
                )
                sys.exit(2)
            await update_section(
                client,
                project_name=args.project,
                section_name=args.section,
                new_name=args.new_section_name,
            )
        elif args.section_command == "delete":
            if not args.section:
                console_err.print(
                    "[red]Please provide a section for deleting a section.[/red]"
                )
                sys.exit(2)
            if not args.project:
                console_err.print(
                    "[red]Please provide --project for deleting a section[/red]"
                )
                sys.exit(2)
            await delete_section(
                client, project_name=args.project, section_partial=args.section
            )

    elif args.command == "label":
        if args.label_command == "list":
            await list_labels(client, show_ids=args.ids, output_json=args.json)
        elif args.label_command == "create":
            await create_label(client, name=args.name)
        elif args.label_command == "update":
            await update_label(client, name=args.name, new_name=args.new_name)
        elif args.label_command == "delete":
            await delete_label(client, name_partial=args.name)

    elif args.command == "dump":
        await dump_all_data(
            client,
            output_path=args.output,
            indent=args.indent,
        )


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
