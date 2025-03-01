#!/usr/bin/env python3

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime

import regex
from rich.console import Console
from rich.table import Table
from rich_argparse import RawTextRichHelpFormatter
from todoist_api_python.api import TodoistAPI

console = Console()

LOGGER = logging.getLogger(__name__)

API_TOKEN = os.getenv("TODOIST_API_TOKEN") or os.getenv("TODOIST_API_KEY")
STRIP_EMOJIS = False

# Color constants
TASK_COLOR = "blue"
PROJECT_COLOR = "yellow"
SECTION_COLOR = "red"
ID_COLOR = "magenta"
NA = "[italic dim]N/A[/italic dim]"


###############################################################################
# Utility and Formatting
###############################################################################
def task_str(task_obj):
    return f"[{TASK_COLOR}]{task_obj.content}[/{TASK_COLOR}] (ID: [{ID_COLOR}]{task_obj.id}[/{ID_COLOR}])"


def project_str(project_obj):
    return f"[{PROJECT_COLOR}]{project_obj.name}[/{PROJECT_COLOR}] (ID: [{ID_COLOR}]{project_obj.id}[/{ID_COLOR}])"


def section_str(section_obj):
    return f"[{SECTION_COLOR}]{section_obj.name}[/{SECTION_COLOR}] (ID: [{ID_COLOR}]{section_obj.id}[/{ID_COLOR}])"


def remove_emojis(text):
    return regex.sub(r"\p{Emoji}\s*", "", text) if text else text


def maybe_strip_emojis(text):
    if STRIP_EMOJIS:
        return remove_emojis(text)
    return text


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
            self._projects = await asyncio.to_thread(self.api.get_projects)
        return self._projects

    async def get_sections(self, project_id):
        if project_id not in self._sections:
            self._sections[project_id] = await asyncio.to_thread(
                self.api.get_sections, project_id=project_id
            )
        return self._sections[project_id]

    async def get_tasks(self, project_id=None):
        key = project_id if project_id is not None else "all"
        if key not in self._tasks:
            if project_id:
                self._tasks[key] = await asyncio.to_thread(
                    self.api.get_tasks, project_id=project_id
                )
            else:
                self._tasks[key] = await asyncio.to_thread(self.api.get_tasks)
        return self._tasks[key]

    def invalidate_tasks(self, project_id=None):
        if project_id:
            self._tasks.pop(project_id, None)
        self._tasks.pop("all", None)

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
):
    # Get tasks for a project (or all)
    if project_name:
        pid = await find_project_id_partial(client, project_name)
        if not pid:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)
        tasks = await client.get_tasks(pid)
    else:
        tasks = await client.get_tasks()

    if not show_subtasks:
        tasks = [t for t in tasks if t.parent_id is None]

    section_mapping = {}
    show_section_col = False
    if section_name:
        if not project_name:
            console.print("[red]--section requires --project.[/red]")
            sys.exit(1)
        pid = await find_project_id_partial(client, project_name)
        if not pid:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)
        sid = await find_section_id_partial(client, pid, section_name)
        if not sid:
            console.print(
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

    projects = await client.get_projects()
    projects_dict = {p.id: p for p in projects}
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
            }
            data.append(entry)
        console.print_json(json.dumps(data))
        return

    table = Table(box=None, pad_edge=False)
    if show_ids:
        table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Content", style="white")
    if show_subtasks:
        table.add_column("Parent Task", style="white")
    table.add_column("Project", style="magenta")
    if show_section_col:
        table.add_column("Section", style="magenta")
    table.add_column("Priority", style="yellow")
    table.add_column("Due", style="green")

    for task in tasks:
        row = []
        if show_ids:
            row.append(str(task.id))
        row.append(maybe_strip_emojis(task.content))
        if show_subtasks:
            parent_str = NA
            if task.parent_id and task.parent_id in task_dict:
                parent_str = maybe_strip_emojis(task_dict[task.parent_id].content)
            row.append(parent_str)
        proj_str = NA
        if task.project_id in projects_dict:
            proj_str = maybe_strip_emojis(projects_dict[task.project_id].name)
        row.append(proj_str)
        if show_section_col:
            sname = NA
            if task.section_id in section_mapping:
                sname = maybe_strip_emojis(section_mapping[task.section_id].name)
            row.append(sname)
        row.append(str(task.priority))
        due_str = task.due.string if task.due else NA
        row.append(maybe_strip_emojis(due_str))
        table.add_row(*row)

    console.print(table)


async def create_task(
    client,
    content,
    priority=None,
    due=None,
    reminder=None,
    project_name=None,
    section_name=None,
    force=False,
):
    pid = None
    sid = None
    if project_name:
        pid = await find_project_id_partial(client, project_name)
        if not pid:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)
    if section_name:
        if not pid:
            console.print("[red]--section requires --project[/red]")
            sys.exit(1)
        sid = await find_section_id_partial(client, pid, section_name)
        if not sid:
            console.print(f"[red]No section found matching '{section_name}'[/red]")
            sys.exit(1)
    if not force:
        tasks = await client.get_tasks(pid) if pid else await client.get_tasks()
        for t in tasks:
            if remove_emojis(t.content.strip().lower()) == remove_emojis(content.strip().lower()):
                console.print(
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
    try:
        new_task = await asyncio.to_thread(client.api.add_task, **kwargs)
        console.print(f"[green]Created {task_str(new_task)}[/green]")
        client.invalidate_tasks(pid)
        if reminder:
            try:
                await asyncio.to_thread(
                    client.api.add_reminder, task_id=new_task.id, due_string=reminder
                )
                console.print(f"[green]Reminder set for {task_str(new_task)}[/green]")
            except Exception as e:
                console.print(f"[yellow]Failed to add reminder: {e}[/yellow]")
    except Exception as e:
        console.print(f"[red]Failed creating task '{content}': {e}[/red]")
        sys.exit(1)


async def update_task(
    client,
    content,
    new_content=None,
    priority=None,
    due=None,
    reminder=None,
    project_name=None,
    section_name=None,
):
    pid = None
    if project_name:
        pid = await find_project_id_partial(client, project_name)
        if not pid:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)
    tasks = await client.get_tasks(pid) if pid else await client.get_tasks()
    target = None
    for t in tasks:
        if t.content.strip().lower() == content.strip().lower():
            target = t
            break
    if not target:
        console.print(f"[yellow]No matching task found for '{content}'.[/yellow]")
        return
    update_kwargs = {}
    if new_content:
        update_kwargs["content"] = new_content
    if priority is not None:
        update_kwargs["priority"] = priority
    if due:
        update_kwargs["due_string"] = due
    try:
        updated = await asyncio.to_thread(
            client.api.update_task, target.id, **update_kwargs
        )
        console.print(f"[green]Updated task: {task_str(updated)}[/green]")
        client.invalidate_tasks(pid)
    except Exception as e:
        console.print(f"[red]Failed to update task '{content}': {e}[/red]")
        sys.exit(1)


async def mark_task_done(client, content, project_name=None):
    pid = None
    if project_name:
        pid = await find_project_id_partial(client, project_name)
        if not pid:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)
    tasks = await client.get_tasks(pid) if pid else await client.get_tasks()
    for t in tasks:
        if t.content.strip().lower() == content.strip().lower():
            try:
                await asyncio.to_thread(client.api.close_task, t.id)
                console.print(f"[green]Marked done: {task_str(t)}[/green]")
                client.invalidate_tasks(pid)
                return
            except Exception as e:
                console.print(f"[red]Failed to mark done: {task_str(t)}: {e}[/red]")
                sys.exit(1)
    console.print(f"[yellow]No matching task found for '{content}'.[/yellow]")


async def delete_task(client, content, project_name=None):
    pid = None
    if project_name:
        pid = await find_project_id_partial(client, project_name)
        if not pid:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)
    tasks = await client.get_tasks(pid) if pid else await client.get_tasks()
    for t in tasks:
        if t.content.strip().lower() == content.strip().lower():
            try:
                await asyncio.to_thread(client.api.delete_task, t.id)
                console.print(f"[green]Deleted {task_str(t)}[/green]")
                client.invalidate_tasks(pid)
                return
            except Exception as e:
                console.print(f"[red]Failed to delete {task_str(t)}: {e}[/red]")
                sys.exit(1)
    console.print(f"[yellow]No task matching '{content}'.[/yellow]")


###############################################################################
# Project Commands
###############################################################################
async def list_projects(client, show_ids=False, output_json=False):
    try:
        projects = await client.get_projects()
    except Exception as e:
        console.print(f"[red]Failed to fetch projects: {e}[/red]")
        sys.exit(1)
    projects.sort(key=lambda x: x.name.lower())
    if output_json:
        data = [{"id": p.id, "name": maybe_strip_emojis(p.name)} for p in projects]
        console.print_json(json.dumps(data))
        return
    table = Table(box=None, pad_edge=False)
    if show_ids:
        table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="white")
    for p in projects:
        row = [str(p.id)] if show_ids else []
        row.append(maybe_strip_emojis(p.name))
        table.add_row(*row)
    console.print(table)


async def create_project(client, name):
    try:
        projects = await client.get_projects()
        for p in projects:
            if p.name.strip().lower() == name.strip().lower():
                console.print(
                    f"[yellow]Project {project_str(p)} already exists.[/yellow]"
                )
                return
        newp = await asyncio.to_thread(client.api.add_project, name=name)
        console.print(f"[green]Created project {project_str(newp)}[/green]")
        client.invalidate_projects()
    except Exception as e:
        console.print(f"[red]Failed to create project '{name}': {e}[/red]")
        sys.exit(1)


async def update_project(client, name, new_name):
    projects = await client.get_projects()
    target = None
    for p in projects:
        if p.name.strip().lower() == name.strip().lower():
            target = p
            break
    if not target:
        console.print(f"[yellow]No matching project found for '{name}'.[/yellow]")
        return
    try:
        updated = await asyncio.to_thread(
            client.api.update_project, target.id, name=new_name
        )
        console.print(f"[green]Updated project: {project_str(updated)}[/green]")
        client.invalidate_projects()
    except Exception as e:
        console.print(f"[red]Failed to update project '{name}': {e}[/red]")
        sys.exit(1)


async def delete_project(client, name_partial):
    pid = await find_project_id_partial(client, name_partial)
    if not pid:
        console.print(f"[yellow]No project found matching '{name_partial}'.[/yellow]")
        return
    try:
        await asyncio.to_thread(client.api.delete_project, pid)
        console.print(f"[green]Deleted project ID {pid}[/green]")
        client.invalidate_projects()
    except Exception as e:
        console.print(f"[red]Failed to delete project '{name_partial}': {e}[/red]")
        sys.exit(1)


###############################################################################
# Section Commands
###############################################################################
async def list_sections(client, show_ids, project_name, output_json=False):
    pid = await find_project_id_partial(client, project_name)
    if not pid:
        console.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)
    try:
        secs = await client.get_sections(pid)
    except Exception as e:
        console.print(f"[red]Failed fetching sections: {e}[/red]")
        sys.exit(1)
    secs.sort(key=lambda x: x.name.lower())
    if output_json:
        data = [{"id": s.id, "name": maybe_strip_emojis(s.name)} for s in secs]
        console.print_json(json.dumps(data))
        return
    table = Table(box=None, pad_edge=False)
    if show_ids:
        table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="white")
    for s in secs:
        row = [str(s.id)] if show_ids else []
        row.append(maybe_strip_emojis(s.name))
        table.add_row(*row)
    console.print(table)


async def create_section(client, project_name, section_name):
    pid = await find_project_id_partial(client, project_name)
    if not pid:
        console.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)
    try:
        secs = await client.get_sections(pid)
        for s in secs:
            if s.name.strip().lower() == section_name.strip().lower():
                console.print(
                    f"[yellow]Section {section_str(s)} already exists.[/yellow]"
                )
                return
        new_sec = await asyncio.to_thread(
            client.api.add_section, name=section_name, project_id=pid
        )
        console.print(f"[green]Created section {section_str(new_sec)}[/green]")
        client.invalidate_sections(pid)
    except Exception as e:
        console.print(f"[red]Failed to create section '{section_name}': {e}[/red]")
        sys.exit(1)


async def update_section(client, project_name, section_name, new_name):
    pid = await find_project_id_partial(client, project_name)
    if not pid:
        console.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)
    secs = await client.get_sections(pid)
    target = None
    for s in secs:
        if s.name.strip().lower() == section_name.strip().lower():
            target = s
            break
    if not target:
        console.print(
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
        console.print(f"[red]Failed to update section '{section_name}': {e}[/red]")
        sys.exit(1)


async def delete_section(client, project_name, section_partial):
    pid = await find_project_id_partial(client, project_name)
    if not pid:
        console.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)
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
            console.print(
                f"[yellow]No section found matching '{section_partial}'.[/yellow]"
            )
            return
        await asyncio.to_thread(client.api.delete_section, match_id)
        console.print(f"[green]Deleted section {section_str(match_obj)}[/green]")
        client.invalidate_sections(pid)
    except Exception as e:
        console.print(f"[red]Failed to delete section '{section_partial}': {e}[/red]")
        sys.exit(1)


###############################################################################
# Label Commands
###############################################################################
async def list_labels(client, show_ids=False, output_json=False):
    try:
        labels = await asyncio.to_thread(client.api.get_labels)
    except Exception as e:
        console.print(f"[red]Failed to fetch labels: {e}[/red]")
        sys.exit(1)
    labels.sort(key=lambda la: la.name.lower())
    if output_json:
        data = [{"id": la.id, "name": maybe_strip_emojis(la.name)} for la in labels]
        console.print_json(json.dumps(data))
        return
    table = Table(box=None, pad_edge=False)
    if show_ids:
        table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="white")
    for la in labels:
        row = [str(la.id)] if show_ids else []
        row.append(maybe_strip_emojis(la.name))
        table.add_row(*row)
    console.print(table)


async def create_label(client, name):
    try:
        labels = await asyncio.to_thread(client.api.get_labels)
        for la in labels:
            if la.name.strip().lower() == name.strip().lower():
                console.print(f"[yellow]Label {la.name} already exists.[/yellow]")
                return
        new_label = await asyncio.to_thread(client.api.add_label, name=name)
        console.print(
            f"[green]Created label {new_label.name} (ID: {new_label.id})[/green]"
        )
    except Exception as e:
        console.print(f"[red]Failed to create label '{name}': {e}[/red]")
        sys.exit(1)


async def update_label(client, name, new_name):
    try:
        labels = await asyncio.to_thread(client.api.get_labels)
        target = None
        for la in labels:
            if la.name.strip().lower() == name.strip().lower():
                target = la
                break
        if not target:
            console.print(f"[yellow]No matching label found for '{name}'.[/yellow]")
            return
        updated = await asyncio.to_thread(
            client.api.update_label, target.id, name=new_name
        )
        console.print(
            f"[green]Updated label: {updated.name} (ID: {updated.id})[/green]"
        )
    except Exception as e:
        console.print(f"[red]Failed to update label '{name}': {e}[/red]")
        sys.exit(1)


async def delete_label(client, name_partial):
    try:
        labels = await asyncio.to_thread(client.api.get_labels)
        target = None
        for la in labels:
            if name_partial.lower() in la.name.lower():
                target = la
                break
        if not target:
            console.print(f"[yellow]No label found matching '{name_partial}'.[/yellow]")
            return
        await asyncio.to_thread(client.api.delete_label, target.id)
        console.print(f"[green]Deleted label {target.name} (ID: {target.id})[/green]")
    except Exception as e:
        console.print(f"[red]Failed to delete label '{name_partial}': {e}[/red]")
        sys.exit(1)


###############################################################################
# Main with Subparsers, Aliases, and Cumulative Filters
###############################################################################
async def async_main():
    global STRIP_EMOJIS

    # Define global alias dictionaries.
    cmd_aliases = {
        "task": ["tasks", "t", "ta"],
        "project": ["projects" "proj", "pro", "p"],
        "section": ["sections" "sect", "sec", "s"],
        "label": ["labels" "lab", "lbl"],
    }
    subcmd_aliases = {
        "list": ["ls", "l"],
        "create": ["cr", "c", "add", "a"],
        "update": ["upd", "u"],
        "delete": ["del", "d", "remove", "rm"],
        "today": ["td", "to"],
    }

    parser = argparse.ArgumentParser(
        prog="tdc",
        formatter_class=RawTextRichHelpFormatter,
        description=("[bold cyan]CLI for Todoist[/bold cyan]"),
    )
    # Global options
    parser.add_argument(
        "-d", "--debug", action="store_true", help="Enable debug logging"
    )
    parser.add_argument(
        "-k",
        "--api-key",
        "--api-token",
        help="Your Todoist API key",
        required=not bool(API_TOKEN),
    )
    parser.add_argument(
        "-E",
        "--strip-emojis",
        action="store_true",
        help="Remove emojis from displayed text.",
    )
    parser.add_argument("-i", "--ids", action="store_true", help="Show ID columns")
    parser.add_argument("-j", "--json", action="store_true", help="Output JSON")
    parser.add_argument("-p", "--project", help="Project partial name match")
    parser.add_argument(
        "-s", "--subtasks", action="store_true", help="Include subtasks"
    )
    parser.add_argument("-S", "--section", help="Section partial name match")

    subparsers = parser.add_subparsers(
        dest="command", required=True, help="Subcommand to run"
    )

    # Top-level command: task
    task_parser = subparsers.add_parser(
        "task",
        aliases=cmd_aliases["task"],
        help="Manage tasks",
        formatter_class=RawTextRichHelpFormatter,
    )
    task_subparsers = task_parser.add_subparsers(
        dest="task_command", required=True, help="Task subcommand"
    )
    list_task_parser = task_subparsers.add_parser(
        "list",
        aliases=subcmd_aliases["list"],
        help="List tasks",
        formatter_class=RawTextRichHelpFormatter,
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
    task_subparsers.add_parser(
        "today",
        aliases=subcmd_aliases["today"],
        help="List tasks due today or overdue",
        formatter_class=RawTextRichHelpFormatter,
    )
    create_task_parser = task_subparsers.add_parser(
        "create",
        aliases=subcmd_aliases["create"],
        help="Create a new task",
        formatter_class=RawTextRichHelpFormatter,
    )
    create_task_parser.add_argument("content", help="Task content")
    create_task_parser.add_argument("--priority", type=int, default=None)
    create_task_parser.add_argument("--due", default=None)
    create_task_parser.add_argument("--reminder", default=None)
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
    )
    update_task_parser.add_argument("content", help="Existing task content to match")
    update_task_parser.add_argument("--new-content", help="New task content")
    update_task_parser.add_argument("--priority", type=int, default=None)
    update_task_parser.add_argument("--due", help="New due string")
    done_parser = task_subparsers.add_parser(
        "done", help="Mark a task as done", formatter_class=RawTextRichHelpFormatter
    )
    done_parser.add_argument("content", help="Task content")
    delete_task_parser = task_subparsers.add_parser(
        "delete",
        aliases=subcmd_aliases["delete"],
        help="Delete a task",
        formatter_class=RawTextRichHelpFormatter,
    )
    delete_task_parser.add_argument("content", help="Task content")

    # Top-level command: project
    project_parser = subparsers.add_parser(
        "project",
        aliases=cmd_aliases["project"],
        help="Manage projects",
        formatter_class=RawTextRichHelpFormatter,
    )
    project_subparsers = project_parser.add_subparsers(
        dest="project_command", required=True, help="Project subcommand"
    )
    project_subparsers.add_parser(
        "list",
        aliases=subcmd_aliases["list"],
        help="List projects",
        formatter_class=RawTextRichHelpFormatter,
    )
    proj_create = project_subparsers.add_parser(
        "create",
        aliases=subcmd_aliases["create"],
        help="Create a new project",
        formatter_class=RawTextRichHelpFormatter,
    )
    proj_create.add_argument("name", help="Project name")
    proj_update = project_subparsers.add_parser(
        "update",
        aliases=subcmd_aliases["update"],
        help="Update a project",
        formatter_class=RawTextRichHelpFormatter,
    )
    proj_update.add_argument("name", help="Existing project name to match")
    proj_update.add_argument("--new-name", required=True, help="New project name")
    proj_delete = project_subparsers.add_parser(
        "delete",
        aliases=subcmd_aliases["delete"],
        help="Delete a project",
        formatter_class=RawTextRichHelpFormatter,
    )
    proj_delete.add_argument("name", help="Project name (or partial)")

    # Top-level command: section
    section_parser = subparsers.add_parser(
        "section",
        aliases=cmd_aliases["section"],
        help="Manage sections",
        formatter_class=RawTextRichHelpFormatter,
    )
    section_subparsers = section_parser.add_subparsers(
        dest="section_command", required=True, help="Section subcommand"
    )
    section_subparsers.add_parser(
        "list",
        aliases=subcmd_aliases["list"],
        help="List sections",
        formatter_class=RawTextRichHelpFormatter,
    )
    sec_create = section_subparsers.add_parser(
        "create",
        aliases=subcmd_aliases["create"],
        help="Create a new section",
        formatter_class=RawTextRichHelpFormatter,
    )
    sec_create.add_argument("section_name", help="Section name")
    sec_update = section_subparsers.add_parser(
        "update",
        aliases=subcmd_aliases["update"],
        help="Update a section",
        formatter_class=RawTextRichHelpFormatter,
    )
    sec_update.add_argument("section_name", help="Existing section name to match")
    sec_update.add_argument("--new-name", required=True, help="New section name")
    sec_delete = section_subparsers.add_parser(
        "delete",
        aliases=subcmd_aliases["delete"],
        help="Delete a section",
        formatter_class=RawTextRichHelpFormatter,
    )
    sec_delete.add_argument("section_name", help="Section name (or partial)")

    # Top-level command: label
    label_parser = subparsers.add_parser(
        "label",
        aliases=cmd_aliases["label"],
        help="Manage labels",
        formatter_class=RawTextRichHelpFormatter,
    )
    label_subparsers = label_parser.add_subparsers(
        dest="label_command", required=True, help="Label subcommand"
    )
    label_subparsers.add_parser(
        "list",
        aliases=subcmd_aliases["list"],
        help="List labels",
        formatter_class=RawTextRichHelpFormatter,
    )
    lab_create = label_subparsers.add_parser(
        "create",
        aliases=subcmd_aliases["create"],
        help="Create a new label",
        formatter_class=RawTextRichHelpFormatter,
    )
    lab_create.add_argument("name", help="Label name")
    lab_update = label_subparsers.add_parser(
        "update",
        aliases=subcmd_aliases["update"],
        help="Update a label",
        formatter_class=RawTextRichHelpFormatter,
    )
    lab_update.add_argument("name", help="Existing label name to match")
    lab_update.add_argument("--new-name", required=True, help="New label name")
    lab_delete = label_subparsers.add_parser(
        "delete",
        aliases=subcmd_aliases["delete"],
        help="Delete a label",
        formatter_class=RawTextRichHelpFormatter,
    )
    lab_delete.add_argument("name", help="Label name (or partial)")

    args = parser.parse_args()

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

    STRIP_EMOJIS = args.strip_emojis
    api_key = args.api_key or API_TOKEN
    if not api_key:
        console.print("[red]Error: API key is required.[/red]")
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
            )
        elif args.task_command == "done":
            await mark_task_done(
                client, content=args.content, project_name=args.project
            )
        elif args.task_command == "delete":
            await delete_task(client, content=args.content, project_name=args.project)

    elif args.command == "project":
        if args.project_command == "list":
            await list_projects(client, show_ids=args.ids, output_json=args.json)
        elif args.project_command == "create":
            await create_project(client, name=args.name)
        elif args.project_command == "update":
            await update_project(client, name=args.name, new_name=args.new_name)
        elif args.project_command == "delete":
            await delete_project(client, name_partial=args.name)

    elif args.command == "section":
        if args.section_command == "list":
            if not args.project:
                console.print(
                    "[red]Please provide --project for listing sections[/red]"
                )
                sys.exit(1)
            await list_sections(
                client,
                show_ids=args.ids,
                project_name=args.project,
                output_json=args.json,
            )
        elif args.section_command == "create":
            if not args.section_name:
                console.print("[red]Please provide a section name.[/red]")
                sys.exit(1)
            if not args.project:
                console.print(
                    "[red]Please provide --project for creating a section[/red]"
                )
                sys.exit(1)
            await create_section(
                client, project_name=args.project, section_name=args.section_name
            )
        elif args.section_command == "update":
            if not args.section_name:
                console.print("[red]Please provide a section name to update.[/red]")
                sys.exit(1)
            if not args.project:
                console.print(
                    "[red]Please provide --project for updating a section[/red]"
                )
                sys.exit(1)
            await update_section(
                client,
                project_name=args.project,
                section_name=args.section_name,
                new_name=args.new_name,
            )
        elif args.section_command == "delete":
            if not args.section_name:
                console.print("[red]Please provide a section name to delete.[/red]")
                sys.exit(1)
            if not args.project:
                console.print(
                    "[red]Please provide --project for deleting a section[/red]"
                )
                sys.exit(1)
            await delete_section(
                client, project_name=args.project, section_partial=args.section_name
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


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
