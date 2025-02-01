#!/usr/bin/env python3
import sys
import json
import argparse
import re
import asyncio
from typing import List

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

STRIP_EMOJIS = False

# Color constants
TASK_COLOR = "blue"
PROJECT_COLOR = "yellow"
SECTION_COLOR = "red"
ID_COLOR = "magenta"


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
async def find_project_id_partial(client, project_name_partial):
    projects = await client.get_projects()
    psearch = project_name_partial.lower()
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
):
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

    projects = await client.get_projects()
    projects_dict = {p.id: p for p in projects}

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
        # If any tasks have a section, we'll show a "Section" column
        if any(t.section_id for t in tasks):
            show_section_col = True
            unique_pids = {t.project_id for t in tasks if t.section_id}
            for upid in unique_pids:
                secs = await client.get_sections(upid)
                for s in secs:
                    section_mapping[s.id] = s

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
                else ""
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
                "project": maybe_strip_emojis(p_name),
                "priority": task.priority,
                "due": maybe_strip_emojis(task.due.string) if task.due else None,
                "section": maybe_strip_emojis(s_name) if s_name else None,
                "parent": maybe_strip_emojis(parent_str) if parent_str else None,
            }
            data.append(entry)
        console.print_json(json.dumps(data))
        return

    # Rich table output
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
            parent_str = "N/A"
            if task.parent_id and task.parent_id in task_dict:
                parent_str = maybe_strip_emojis(task_dict[task.parent_id].content)
            row.append(parent_str)

        proj_str = "N/A"
        if task.project_id in projects_dict:
            proj_str = maybe_strip_emojis(projects_dict[task.project_id].name)
        row.append(proj_str)

        if show_section_col:
            sname = "N/A"
            if task.section_id in section_mapping:
                sname = maybe_strip_emojis(section_mapping[task.section_id].name)
            row.append(sname)

        row.append(str(task.priority))
        due_str = task.due.string if task.due else "N/A"
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

    # check duplicate
    tasks = await client.get_tasks(pid) if pid else await client.get_tasks()
    for t in tasks:
        if t.content.strip().lower() == content.strip().lower():
            console.print(
                f"[yellow]Task {task_str(t)} already exists, skipping.[/yellow]"
            )
            return

    kwargs = {"content": content}
    if priority:
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
# Projects
###############################################################################
async def list_projects(client, show_ids=False, output_json=False):
    try:
        projects = await client.get_projects()
    except Exception as e:
        console.print(f"[red]Failed to fetch projects: {e}[/red]")
        sys.exit(1)
    projects.sort(key=lambda x: x.name.lower())

    if output_json:
        data = []
        for p in projects:
            data.append(
                {
                    "id": p.id,
                    "name": maybe_strip_emojis(p.name),
                }
            )
        console.print_json(json.dumps(data))
        return

    table = Table(box=None, pad_edge=False)
    if show_ids:
        table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="white")

    for p in projects:
        row = []
        if show_ids:
            row.append(str(p.id))
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


async def delete_project(client, name_partial):
    pid = await find_project_id_partial(client, name_partial)
    if not pid:
        console.print(f"[yellow]No project found matching '{name_partial}'[/yellow]")
        return
    try:
        await asyncio.to_thread(client.api.delete_project, pid)
        console.print(f"[green]Deleted project ID {pid}[/green]")
        client.invalidate_projects()
    except Exception as e:
        console.print(f"[red]Failed to delete project '{name_partial}': {e}[/red]")
        sys.exit(1)


###############################################################################
# Sections
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
        data = []
        for s in secs:
            data.append({"id": s.id, "name": maybe_strip_emojis(s.name)})
        console.print_json(json.dumps(data))
        return

    table = Table(box=None, pad_edge=False)
    if show_ids:
        table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="white")

    for s in secs:
        row = []
        if show_ids:
            row.append(str(s.id))
        row.append(section_str(s))
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
                f"[yellow]No section found matching '{section_partial}'[/yellow]"
            )
            return
        await asyncio.to_thread(client.api.delete_section, match_id)
        console.print(f"[green]Deleted section {section_str(match_obj)}[/green]")
        client.invalidate_sections(pid)
    except Exception as e:
        console.print(f"[red]Failed to delete section '{section_partial}': {e}[/red]")
        sys.exit(1)


###############################################################################
# Main
###############################################################################
async def async_main():
    ######################################################################
    # 1) Parse global args first (plus partial leftover)
    ######################################################################
    global_parser = argparse.ArgumentParser(
        prog="tdc",
        description=(
            "[bold cyan]A Python CLI for Todoist[/bold cyan], leveraging "
            "[yellow]Rich[/yellow] for display and the official [green]Todoist API[/green]."
        ),
        allow_abbrev=False,
        formatter_class=RawTextRichHelpFormatter,
    )
    global_parser.add_argument("-k", "--api-key", required=True, help="Todoist API key")
    global_parser.add_argument(
        "-E",
        "--strip-emojis",
        action="store_true",
        help="Remove emojis from displayed text.",
    )
    global_parser.add_argument(
        "-i", "--ids", action="store_true", help="Show ID columns"
    )
    global_parser.add_argument("-j", "--json", action="store_true", help="Output JSON")
    global_parser.add_argument("-p", "--project", help="Project partial name match")
    global_parser.add_argument(
        "-s", "--subtasks", action="store_true", help="Include subtasks"
    )
    global_parser.add_argument("-S", "--section", help="Section partial name match")

    # We'll insert a "command" positional argument *optionally*:
    global_parser.add_argument(
        "command", nargs="?", help="Subcommand: task, project, section"
    )
    # We won't define sub-sub commands here, so that we can parse them manually

    args, remaining_argv = global_parser.parse_known_args(sys.argv[1:])
    # Now we have a partial parse of "global" flags plus possibly something in 'command'

    # If user gave no command, default to "task list"
    command = args.command
    if not command:
        command = "task"

    # We also let user pass the sub-sub-command in remaining_argv
    # e.g.  tdc.py project list  => command = "project", remaining_argv=["list"]
    #
    # We'll define smaller subparsers for each command below:

    ######################################################################
    # 2) Based on 'command', define another parser for sub-commands
    #    (like "list", "create", "delete", etc.).
    #
    #    We parse the leftover arguments.
    ######################################################################

    # Make a small function to parse subcommand + leftover for each 'command':
    def parse_task_args(argv: List[str]):
        # For "task" command, sub-commands: list, create, done, delete
        p = argparse.ArgumentParser(
            prog="tdc task",
            formatter_class=RawTextRichHelpFormatter,
            allow_abbrev=False,
        )
        p.add_argument(
            "task_command",
            nargs="?",
            default="list",
            choices=[
                "list",
                "ls",
                "l",
                "create",
                "cr",
                "c",
                "add",
                "a",
                "done",
                "delete",
                "del",
                "d",
                "remove",
                "rm",
            ],
            help="Task subcommand",
        )
        # Additional args for 'create'
        p.add_argument(
            "content", nargs="?", help="Task content (if create/done/delete)"
        )
        p.add_argument("--priority", type=int, default=None)
        p.add_argument("--due", default=None)
        p.add_argument("--reminder", default=None)
        parsed = p.parse_args(argv)
        return parsed

    def parse_project_args(argv: List[str]):
        p = argparse.ArgumentParser(
            prog="tdc project",
            formatter_class=RawTextRichHelpFormatter,
            allow_abbrev=False,
        )
        p.add_argument(
            "project_command",
            nargs="?",
            default="list",
            choices=[
                "list",
                "ls",
                "l",
                "create",
                "cr",
                "c",
                "add",
                "a",
                "delete",
                "del",
                "d",
                "remove",
                "rm",
            ],
            help="Project subcommand",
        )
        p.add_argument("name", nargs="?", help="Project name or partial name")
        return p.parse_args(argv)

    def parse_section_args(argv: List[str]):
        p = argparse.ArgumentParser(
            prog="tdc section",
            formatter_class=RawTextRichHelpFormatter,
            allow_abbrev=False,
        )
        p.add_argument(
            "section_command",
            nargs="?",
            default="list",
            choices=[
                "list",
                "ls",
                "l",
                "create",
                "cr",
                "c",
                "add",
                "a",
                "delete",
                "del",
                "d",
                "remove",
                "rm",
            ],
            help="Section subcommand",
        )
        p.add_argument("section_name", nargs="?", help="Section name (create/delete)")
        return p.parse_args(argv)

    # Now parse subcommand-level arguments:
    if command in ["task", "tasks", "t"]:
        task_args = parse_task_args(remaining_argv)
        subcommand = task_args.task_command
    elif command in ["project", "proj", "pro", "p"]:
        task_args = parse_project_args(remaining_argv)
        subcommand = task_args.project_command
    elif command in ["section", "sect", "sec", "s"]:
        task_args = parse_section_args(remaining_argv)
        subcommand = task_args.section_command
    else:
        # If we get here, user typed unknown command
        console.print(f"[red]Unknown command '{command}'.[/red]")
        sys.exit(1)

    # Now that we have the final parse: combine the two sets of arguments:
    global STRIP_EMOJIS
    STRIP_EMOJIS = args.strip_emojis

    api = TodoistAPI(args.api_key)
    client = TodoistClient(api)

    # ---------------------------------------------------------------------
    # 3) Command dispatch
    # ---------------------------------------------------------------------
    if command in ["task", "tasks", "t"]:
        # subcommand = e.g. "list", "create", "done", "delete", ...
        if subcommand in ["list", "ls", "l"]:
            await list_tasks(
                client,
                show_ids=args.ids,
                show_subtasks=args.subtasks,
                project_name=args.project,
                section_name=args.section,
                output_json=args.json,
            )
        elif subcommand in ["create", "cr", "c", "add", "a"]:
            # 'content' is in task_args.content
            if not task_args.content:
                console.print("[red]Please provide a task content.[/red]")
                sys.exit(1)
            await create_task(
                client,
                content=task_args.content,
                priority=task_args.priority,
                due=task_args.due,
                reminder=task_args.reminder,
                project_name=args.project,
                section_name=args.section,
            )
        elif subcommand == "done":
            if not task_args.content:
                console.print("[red]Please provide a task content to mark done.[/red]")
                sys.exit(1)
            await mark_task_done(
                client, content=task_args.content, project_name=args.project
            )
        elif subcommand in ["delete", "del", "d", "remove", "rm"]:
            if not task_args.content:
                console.print("[red]Please provide a task content to delete.[/red]")
                sys.exit(1)
            await delete_task(
                client, content=task_args.content, project_name=args.project
            )
        else:
            # Fallback
            await list_tasks(
                client,
                show_ids=args.ids,
                show_subtasks=args.subtasks,
                project_name=args.project,
                section_name=args.section,
                output_json=args.json,
            )

    elif command in ["project", "proj", "pro", "p"]:
        if subcommand in ["list", "ls", "l"]:
            await list_projects(client, show_ids=args.ids, output_json=args.json)
        elif subcommand in ["create", "cr", "c", "add", "a"]:
            if not task_args.name:
                console.print("[red]Please provide a project name.[/red]")
                sys.exit(1)
            await create_project(client, task_args.name)
        elif subcommand in ["delete", "del", "d", "remove", "rm"]:
            if not task_args.name:
                console.print("[red]Please provide a project name to delete.[/red]")
                sys.exit(1)
            await delete_project(client, task_args.name)
        else:
            # default "list"
            await list_projects(client, show_ids=args.ids, output_json=args.json)

    elif command in ["section", "sect", "sec", "s"]:
        if subcommand in ["list", "ls", "l"]:
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
        elif subcommand in ["create", "cr", "c", "add", "a"]:
            if not task_args.section_name:
                console.print("[red]Please provide a section name.[/red]")
                sys.exit(1)
            if not args.project:
                console.print(
                    "[red]Please provide --project for creating a section[/red]"
                )
                sys.exit(1)
            await create_section(
                client, project_name=args.project, section_name=task_args.section_name
            )
        elif subcommand in ["delete", "del", "d", "remove", "rm"]:
            if not task_args.section_name:
                console.print("[red]Please provide a section name to delete.[/red]")
                sys.exit(1)
            if not args.project:
                console.print(
                    "[red]Please provide --project for deleting a section[/red]"
                )
                sys.exit(1)
            await delete_section(
                client,
                project_name=args.project,
                section_partial=task_args.section_name,
            )
        else:
            # default "list"
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


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
