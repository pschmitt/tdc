#!/usr/bin/env python3
import sys
import json
import argparse
import re
import asyncio

from todoist_api_python.api import TodoistAPI
from rich.console import Console
from rich.table import Table

try:
    from rich_argparse import RawTextRichHelpFormatter
except ImportError:
    print(
        "You need to install 'rich-argparse' for colorized help.\n"
        "Install it via: pip install rich-argparse"
    )
    sys.exit(1)

console = Console()

# Global toggle for stripping emojis
STRIP_EMOJIS = False

# Color constants (change them here to update formatting everywhere)
TASK_COLOR = "blue"
PROJECT_COLOR = "yellow"
SECTION_COLOR = "red"
ID_COLOR = "magenta"


def task_str(task_obj):
    """
    Format a task object.
    """
    return f"[{TASK_COLOR}]{task_obj.content}[/{TASK_COLOR}] (ID: [{ID_COLOR}]{task_obj.id}[/{ID_COLOR}])"


def project_str(project_obj):
    """
    Format a project object.
    """
    return f"[{PROJECT_COLOR}]{project_obj.name}[/{PROJECT_COLOR}] (ID: [{ID_COLOR}]{project_obj.id}[/{ID_COLOR}])"


def section_str(section_obj):
    """
    Format a section object.
    """
    return f"[{SECTION_COLOR}]{section_obj.name}[/{SECTION_COLOR}] (ID: [{ID_COLOR}]{section_obj.id}[/{ID_COLOR}])"


def remove_emojis(text):
    """
    Remove (most) emojis, zero-width joiners, and variation selectors.
    """
    if not text:
        return text

    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags
        "\U0001F700-\U0001F77F"  # alchemical symbols
        "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
        "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
        "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
        "\U0001FA00-\U0001FA6F"  # Chess symbols, etc.
        "\U0001FA70-\U0001FAFF"  # More recently added emojis
        "\u200c"  # ZERO WIDTH NON-JOINER
        "\u200d"  # ZERO WIDTH JOINER
        "\ufe0e-\ufe0f"  # VARIATION SELECTOR-15, -16
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub(r"", text).lstrip()


def maybe_strip_emojis(text):
    """
    Remove emojis if STRIP_EMOJIS is True.
    """
    if STRIP_EMOJIS:
        return remove_emojis(text)
    return text


###############################################################################
# Caching and Async Client Wrapper
###############################################################################
class TodoistClient:
    """
    Wraps the TodoistAPI and caches results locally.
    """

    def __init__(self, api):
        self.api = api
        self._projects = None  # Cached list of projects
        self._sections = {}  # Cached sections per project (keyed by project id)
        self._tasks = {}  # Cached tasks (keyed by project id or "all")

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
            if project_id in self._tasks:
                del self._tasks[project_id]
        if "all" in self._tasks:
            del self._tasks["all"]

    def invalidate_projects(self):
        self._projects = None

    def invalidate_sections(self, project_id):
        if project_id in self._sections:
            del self._sections[project_id]


###############################################################################
# Helper Functions for Lookups
###############################################################################
async def find_project_id_partial(client, project_name_partial):
    projects = await client.get_projects()
    project_name_lower = project_name_partial.lower()
    for project in projects:
        if project_name_lower in project.name.lower():
            return project.id
    return None


async def find_section_id_partial(client, project_id, section_name_partial):
    sections = await client.get_sections(project_id)
    section_name_lower = section_name_partial.lower()
    for section in sections:
        if section_name_lower in section.name.lower():
            return section.id
    return None


###############################################################################
# TASKS
###############################################################################
async def list_tasks(
    client,
    show_ids=False,
    show_subtasks=False,
    project_name=None,
    section_name=None,
    output_json=False,
):
    if project_name:
        project_id = await find_project_id_partial(client, project_name)
        if not project_id:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)
        tasks = await client.get_tasks(project_id)
    else:
        tasks = await client.get_tasks()

    # Filter out subtasks unless requested
    if not show_subtasks:
        tasks = [t for t in tasks if t.parent_id is None]

    # Prepare projects mapping
    projects = await client.get_projects()
    projects_dict = {p.id: p for p in projects}

    # Filter by section if provided
    section_mapping = {}
    show_section_col = False
    if section_name:
        if not project_name:
            console.print(
                "[red]You must specify a --project if you provide a --section.[/red]"
            )
            sys.exit(1)
        section_id = await find_section_id_partial(client, project_id, section_name)
        if not section_id:
            console.print(
                f"[red]No section found matching '{section_name}' in project '{project_name}'.[/red]"
            )
            sys.exit(1)
        tasks = [t for t in tasks if t.section_id == section_id]
        sections = await client.get_sections(project_id)
        section_mapping = {s.id: s for s in sections}
        show_section_col = True
    else:
        if any(t.section_id for t in tasks):
            show_section_col = True
            unique_project_ids = {t.project_id for t in tasks if t.section_id}
            for pid in unique_project_ids:
                secs = await client.get_sections(pid)
                for s in secs:
                    section_mapping[s.id] = s

    # Build a lookup for parent tasks (if needed)
    task_dict = {t.id: t for t in tasks}

    # Sort tasks by project name, section name, and content
    tasks.sort(
        key=lambda t: (
            (
                projects_dict[t.project_id].name.lower()
                if t.project_id in projects_dict
                else ""
            ),
            (
                (
                    section_mapping[t.section_id].name.lower()
                    if show_section_col and t.section_id in section_mapping
                    else ""
                )
                if show_section_col
                else ""
            ),
            t.content.lower(),
        )
    )

    if output_json:
        data = []
        for task in tasks:
            project_name_str = (
                maybe_strip_emojis(projects_dict[task.project_id].name)
                if task.project_id in projects_dict
                else ""
            )
            section_name_str = (
                maybe_strip_emojis(section_mapping[task.section_id].name)
                if show_section_col and task.section_id in section_mapping
                else None
            )
            parent_task_str = (
                maybe_strip_emojis(task_dict[task.parent_id].content)
                if show_subtasks and task.parent_id and task.parent_id in task_dict
                else None
            )
            entry = {
                "id": task.id,
                "content": maybe_strip_emojis(task.content),
                "project_name": project_name_str,
                "priority": task.priority,
                "due": maybe_strip_emojis(task.due.string) if task.due else None,
            }
            if show_section_col:
                entry["section_name"] = section_name_str
            if show_subtasks:
                entry["parent_task"] = parent_task_str
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
        due_str = task.due.string if task.due else "N/A"
        project_obj = projects_dict.get(task.project_id)
        project_name_str = (
            maybe_strip_emojis(project_obj.name) if project_obj else "N/A"
        )
        section_name_str = (
            maybe_strip_emojis(section_mapping[task.section_id].name)
            if show_section_col and task.section_id in section_mapping
            else "N/A"
        )
        parent_task_str = (
            maybe_strip_emojis(task_dict[task.parent_id].content)
            if show_subtasks and task.parent_id and task.parent_id in task_dict
            else "N/A"
        )
        row = []
        if show_ids:
            row.append(str(task.id))
        row.append(maybe_strip_emojis(task.content))
        if show_subtasks:
            row.append(parent_task_str)
        row.append(project_name_str)
        if show_section_col:
            row.append(section_name_str)
        row.append(str(task.priority))
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
):
    project_id = None
    section_id = None

    if project_name:
        project_id = await find_project_id_partial(client, project_name)
        if not project_id:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)

    if section_name:
        if not project_id:
            console.print(
                "[red]You must specify --project if you provide --section.[/red]"
            )
            sys.exit(1)
        section_id = await find_section_id_partial(client, project_id, section_name)
        if not section_id:
            console.print(
                f"[red]No section found matching '{section_name}' in project '{project_name}'.[/red]"
            )
            sys.exit(1)

    # Check for an existing task with the same content (case-insensitive)
    tasks = (
        await client.get_tasks(project_id) if project_id else await client.get_tasks()
    )
    for task in tasks:
        if task.content.strip().lower() == content.strip().lower():
            console.print(
                f"[yellow]Task {task_str(task)} already exists, skipping creation.[/yellow]"
            )
            return

    add_kwargs = {"content": content}
    if priority:
        add_kwargs["priority"] = priority
    if due:
        add_kwargs["due_string"] = due
    if project_id:
        add_kwargs["project_id"] = project_id
    if section_id:
        add_kwargs["section_id"] = section_id

    try:
        new_task = await asyncio.to_thread(client.api.add_task, **add_kwargs)
        console.print(f"[green]Task {task_str(new_task)} created successfully.[/green]")
        client.invalidate_tasks(project_id)
        if reminder:
            try:
                await asyncio.to_thread(
                    client.api.add_reminder, task_id=new_task.id, due_string=reminder
                )
                console.print(
                    f"[green]Reminder set for task {task_str(new_task)} with due string '{reminder}'.[/green]"
                )
            except Exception as e:
                console.print(f"[yellow]Failed to add reminder: {e}[/yellow]")
    except Exception as e:
        console.print(f"[red]Failed to create task '{content}': {e}[/red]")
        sys.exit(1)


async def mark_task_done(client, content, project_name=None):
    project_id = None
    if project_name:
        project_id = await find_project_id_partial(client, project_name)
        if not project_id:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)

    tasks = (
        await client.get_tasks(project_id) if project_id else await client.get_tasks()
    )
    for task in tasks:
        if task.content.strip().lower() == content.strip().lower():
            try:
                await asyncio.to_thread(client.api.close_task, task.id)
                console.print(f"[green]Task {task_str(task)} marked as done.[/green]")
                client.invalidate_tasks(project_id)
                return
            except Exception as e:
                console.print(
                    f"[red]Failed to mark task {task_str(task)} done: {e}[/red]"
                )
                sys.exit(1)

    console.print(f"[yellow]No matching task found for '{content}'.[/yellow]")


async def delete_task(client, content, project_name=None):
    project_id = None
    if project_name:
        project_id = await find_project_id_partial(client, project_name)
        if not project_id:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)

    tasks = (
        await client.get_tasks(project_id) if project_id else await client.get_tasks()
    )
    for task in tasks:
        if task.content.strip().lower() == content.strip().lower():
            try:
                await asyncio.to_thread(client.api.delete_task, task.id)
                console.print(
                    f"[green]Task {task_str(task)} deleted successfully.[/green]"
                )
                client.invalidate_tasks(project_id)
                return
            except Exception as e:
                console.print(f"[red]Failed to delete task {task_str(task)}: {e}[/red]")
                sys.exit(1)

    console.print(f"[yellow]No task matching '{content}'.[/yellow]")


###############################################################################
# PROJECTS
###############################################################################
async def list_projects(client, show_ids=False, output_json=False):
    try:
        projects = await client.get_projects()
    except Exception as e:
        console.print(f"[red]Failed to fetch projects: {e}[/red]")
        sys.exit(1)

    projects.sort(key=lambda p: p.name.lower())

    if output_json:
        data = [{"id": p.id, "name": maybe_strip_emojis(p.name)} for p in projects]
        console.print_json(json.dumps(data))
        return

    table = Table(box=None, pad_edge=False)
    if show_ids:
        table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="white")

    for project in projects:
        row = []
        if show_ids:
            row.append(str(project.id))
        row.append(project.name)
        table.add_row(*row)

    console.print(table)


async def create_project(client, name):
    try:
        projects = await client.get_projects()
        for project in projects:
            if project.name.strip().lower() == name.strip().lower():
                console.print(
                    f"[yellow]Project {project_str(project)} already exists, skipping creation.[/yellow]"
                )
                return
        new_project = await asyncio.to_thread(client.api.add_project, name=name)
        console.print(
            f"[green]Project {project_str(new_project)} created successfully.[/green]"
        )
        client.invalidate_projects()
    except Exception as e:
        console.print(f"[red]Failed to create project '{name}': {e}[/red]")
        sys.exit(1)


async def delete_project(client, name_partial):
    project_id = await find_project_id_partial(client, name_partial)
    if not project_id:
        console.print(f"[yellow]No project matching '{name_partial}' found.[/yellow]")
        return

    try:
        await asyncio.to_thread(client.api.delete_project, project_id)
        console.print(
            f"[green]Project with ID ([{ID_COLOR}]{project_id}[/{ID_COLOR}]) deleted successfully.[/green]"
        )
        client.invalidate_projects()
    except Exception as e:
        console.print(
            f"[red]Failed to delete project matching '{name_partial}': {e}[/red]"
        )
        sys.exit(1)


###############################################################################
# SECTIONS
###############################################################################
async def list_sections(client, show_ids, project_name, output_json=False):
    project_id = await find_project_id_partial(client, project_name)
    if not project_id:
        console.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)

    try:
        sections = await client.get_sections(project_id)
    except Exception as e:
        console.print(f"[red]Failed to fetch sections: {e}[/red]")
        sys.exit(1)

    sections.sort(key=lambda s: s.name.lower())

    if output_json:
        data = [{"id": s.id, "name": maybe_strip_emojis(s.name)} for s in sections]
        console.print_json(json.dumps(data))
        return

    table = Table(box=None, pad_edge=False)
    if show_ids:
        table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="white")

    for section in sections:
        row = []
        if show_ids:
            row.append(str(section.id))
        row.append(section_str(section))
        table.add_row(*row)

    console.print(table)


async def create_section(client, project_name, section_name):
    project_id = await find_project_id_partial(client, project_name)
    if not project_id:
        console.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)

    try:
        sections = await client.get_sections(project_id)
    except Exception as e:
        console.print(
            f"[red]Failed to fetch sections for project '{project_name}': {e}[/red]"
        )
        sys.exit(1)

    for section in sections:
        if section.name.strip().lower() == section_name.strip().lower():
            console.print(
                f"[yellow]Section {section_str(section)} already exists in project {project_name}, skipping creation.[/yellow]"
            )
            return

    try:
        new_section = await asyncio.to_thread(
            client.api.add_section, name=section_name, project_id=project_id
        )
        console.print(
            f"[green]Section {section_str(new_section)} created successfully in project (ID: [{ID_COLOR}]{project_id}[/{ID_COLOR}]).[/green]"
        )
        client.invalidate_sections(project_id)
    except Exception as e:
        console.print(
            f"[red]Failed to create section '{section_name}' in project '{project_name}': {e}[/red]"
        )
        sys.exit(1)


async def delete_section(client, project_name, section_partial):
    project_id = await find_project_id_partial(client, project_name)
    if not project_id:
        console.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)

    try:
        sections = await client.get_sections(project_id)
    except Exception as e:
        console.print(f"[red]Failed to fetch sections: {e}[/red]")
        sys.exit(1)

    section_id = None
    section_obj = None
    section_partial_lower = section_partial.lower()
    for s in sections:
        if section_partial_lower in s.name.lower():
            section_id = s.id
            section_obj = s
            break

    if not section_id:
        console.print(
            f"[yellow]No section found matching '{section_partial}'.[/yellow]"
        )
        return

    try:
        await asyncio.to_thread(client.api.delete_section, section_id)
        console.print(
            f"[green]Section {section_str(section_obj)} deleted successfully.[/green]"
        )
        client.invalidate_sections(project_id)
    except Exception as e:
        console.print(
            f"[red]Failed to delete section {section_str(section_obj)}: {e}[/red]"
        )
        sys.exit(1)


###############################################################################
# MAIN
###############################################################################
async def async_main():
    parser = argparse.ArgumentParser(
        prog="tdc",
        description="[bold cyan]A Python CLI for Todoist[/bold cyan], leveraging [yellow]Rich[/yellow] for display and the official [green]Todoist API[/green].",
        formatter_class=RawTextRichHelpFormatter,
    )
    parser.add_argument("-k", "--api-key", help="Your Todoist API key", required=True)
    parser.add_argument(
        "-E",
        "--strip-emojis",
        action="store_true",
        help="Remove emojis from displayed text.",
    )
    parser.add_argument("-i", "--ids", action="store_true", help="Show ID columns")
    parser.add_argument(
        "-j", "--json", action="store_true", help="Output in JSON format"
    )
    parser.add_argument(
        "-p", "--project", help="Filter tasks by project name (partial match)"
    )
    parser.add_argument(
        "-s", "--subtasks", action="store_true", help="Include subtasks"
    )
    parser.add_argument(
        "-S", "--section", help="Filter tasks by section name (partial match)"
    )

    subparsers = parser.add_subparsers(
        dest="command", help="[magenta]Available commands[/magenta]"
    )

    # TASK subcommands
    task_parser = subparsers.add_parser(
        "task",
        aliases=["tasks", "tas", "ta", "t"],
        help="[cyan]Manage tasks[/cyan]",
        formatter_class=RawTextRichHelpFormatter,
    )
    task_subparsers = task_parser.add_subparsers(
        dest="task_command", help="[magenta]Task commands[/magenta]"
    )
    task_parser.set_defaults(task_command="list")

    task_list_parser = task_subparsers.add_parser(
        "list",
        aliases=["ls", "l"],
        help="List tasks",
        formatter_class=RawTextRichHelpFormatter,
    )
    task_list_parser.add_argument(
        "-i", "--ids", action="store_true", help="Show ID columns"
    )
    task_list_parser.add_argument(
        "-j", "--json", action="store_true", help="Output in JSON format"
    )
    task_list_parser.add_argument(
        "-p", "--project", help="Filter tasks by project name (partial match)"
    )
    task_list_parser.add_argument(
        "-S", "--section", help="Filter tasks by section name (partial match)"
    )
    task_list_parser.add_argument(
        "-s", "--subtasks", action="store_true", help="Include subtasks"
    )

    task_create_parser = task_subparsers.add_parser(
        "create",
        aliases=["cr", "c", "add", "a"],
        help="Create a new task",
        formatter_class=RawTextRichHelpFormatter,
    )
    task_create_parser.add_argument(
        "content", help="Task content (e.g., 'Brush teeth')"
    )
    task_create_parser.add_argument(
        "--priority", type=int, default=None, help="Priority (1-4)"
    )
    task_create_parser.add_argument(
        "--due", default=None, help="Due date/time string (e.g., 'tomorrow')"
    )
    task_create_parser.add_argument(
        "--reminder", default=None, help="Reminder due date/time string"
    )
    task_create_parser.add_argument(
        "-p", "--project", default=None, help="Project name (partial match)"
    )
    task_create_parser.add_argument(
        "-S",
        "--section",
        default=None,
        help="Section name (partial match) (requires --project)",
    )

    task_done_parser = task_subparsers.add_parser(
        "done", help="Mark a task as done", formatter_class=RawTextRichHelpFormatter
    )
    task_done_parser.add_argument("content", help="Task content to mark as done")
    task_done_parser.add_argument(
        "-p", "--project", default=None, help="Project name (partial match)"
    )

    task_delete_parser = task_subparsers.add_parser(
        "delete",
        aliases=["del", "d", "remove", "rm"],
        help="Delete a task",
        formatter_class=RawTextRichHelpFormatter,
    )
    task_delete_parser.add_argument("content", help="Task content to delete")
    task_delete_parser.add_argument(
        "-p", "--project", default=None, help="Project name (partial match)"
    )

    # PROJECT subcommands
    project_parser = subparsers.add_parser(
        "project",
        aliases=["proj", "pro", "p"],
        help="[cyan]Manage projects[/cyan]",
        formatter_class=RawTextRichHelpFormatter,
    )
    project_parser.set_defaults(project_command="list")
    project_subparsers = project_parser.add_subparsers(
        dest="project_command", help="[magenta]Project commands[/magenta]"
    )

    project_list_parser = project_subparsers.add_parser(
        "list",
        aliases=["ls", "l"],
        help="List all projects",
        formatter_class=RawTextRichHelpFormatter,
    )
    project_list_parser.add_argument(
        "-i", "--ids", action="store_true", help="Show ID columns"
    )
    project_list_parser.add_argument(
        "-j", "--json", action="store_true", help="Output in JSON format"
    )

    project_create_parser = project_subparsers.add_parser(
        "create",
        aliases=["cr", "c", "add", "a"],
        help="Create a new project",
        formatter_class=RawTextRichHelpFormatter,
    )
    project_create_parser.add_argument("name", help="Project name")

    project_delete_parser = project_subparsers.add_parser(
        "delete",
        aliases=["del", "d", "remove", "rm"],
        help="Delete a project",
        formatter_class=RawTextRichHelpFormatter,
    )
    project_delete_parser.add_argument(
        "name", help="Partial name match for project to delete"
    )

    # SECTION subcommands
    section_parser = subparsers.add_parser(
        "section",
        aliases=["sect", "sec", "s"],
        help="[cyan]Manage sections[/cyan]",
        formatter_class=RawTextRichHelpFormatter,
    )
    section_subparsers = section_parser.add_subparsers(
        dest="section_command", help="[magenta]Section commands[/magenta]"
    )
    section_parser.set_defaults(section_command="list")

    section_list_parser = section_subparsers.add_parser(
        "list",
        aliases=["ls", "l"],
        help="List sections in a project",
        formatter_class=RawTextRichHelpFormatter,
    )
    section_list_parser.add_argument(
        "-p", "--project", required=True, help="Project name (partial match)"
    )
    section_list_parser.add_argument(
        "-i", "--ids", action="store_true", help="Show ID columns"
    )
    section_list_parser.add_argument(
        "-j", "--json", action="store_true", help="Output in JSON format"
    )

    section_create_parser = section_subparsers.add_parser(
        "create",
        aliases=["cr", "c", "add", "a"],
        help="Create a new section in a project",
        formatter_class=RawTextRichHelpFormatter,
    )
    section_create_parser.add_argument(
        "section_name", help="Name of the section to create"
    )
    section_create_parser.add_argument(
        "-p", "--project", required=True, help="Project name (partial match)"
    )

    section_delete_parser = section_subparsers.add_parser(
        "delete",
        aliases=["del", "d", "remove", "rm"],
        help="Delete a section in a project",
        formatter_class=RawTextRichHelpFormatter,
    )
    section_delete_parser.add_argument(
        "-p", "--project", required=True, help="Project name (partial match)"
    )
    section_delete_parser.add_argument(
        "section_name", help="Name of the section to delete"
    )

    args = parser.parse_args()
    if args.command is None:
        args.command = "task"
        args.task_command = "list"

    global STRIP_EMOJIS
    STRIP_EMOJIS = args.strip_emojis

    # Instantiate the API and wrap it in our caching client
    api = TodoistAPI(args.api_key)
    client = TodoistClient(api)

    if args.command in ["task", "tasks", "t"]:
        if args.task_command in ["create", "cr", "c", "add", "a"]:
            await create_task(
                client,
                content=args.content,
                priority=args.priority,
                due=args.due,
                reminder=args.reminder,
                project_name=args.project,
                section_name=args.section,
            )
        elif args.task_command == "done":
            await mark_task_done(
                client, content=args.content, project_name=args.project
            )
        elif args.task_command in ["delete", "del", "d", "remove", "rm"]:
            await delete_task(client, content=args.content, project_name=args.project)
        else:
            await list_tasks(
                client,
                show_ids=args.ids,
                show_subtasks=args.subtasks,
                project_name=args.project,
                section_name=args.section,
                output_json=args.json,
            )
    elif args.command in ["projects", "project", "proj", "p"]:
        if args.project_command in ["create", "cr", "c", "add", "a"]:
            await create_project(client, args.name)
        elif args.project_command in ["delete", "del", "d", "remove", "rm"]:
            await delete_project(client, args.name)
        else:
            await list_projects(client, show_ids=args.ids, output_json=args.json)
    elif args.command in ["sections", "section", "sect", "sec", "s"]:
        if args.section_command in ["delete", "del", "d", "remove", "rm"]:
            await delete_section(
                client, project_name=args.project, section_partial=args.section_name
            )
        elif args.section_command in ["create", "cr", "c", "add", "a"]:
            await create_section(
                client, project_name=args.project, section_name=args.section_name
            )
        else:
            if not args.project:
                section_parser.print_help()
            else:
                await list_sections(
                    client,
                    show_ids=args.ids,
                    project_name=args.project,
                    output_json=args.json,
                )
    else:
        parser.print_help()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
