#!/usr/bin/env python3

import sys
import json
import argparse
import re

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

# Global toggles (set after arg parsing)
STRIP_EMOJIS = False


def remove_emojis(text):
    """
    Remove (most) emojis, zero-width joiners, and variation selectors.
    This pattern covers a broad range of Unicode blocks where emojis live,
    plus ZWJ (U+200D), ZWNJ (U+200C), and Variation Selectors (U+FE0E, U+FE0F).
    """
    if not text:
        return text

    emoji_pattern = re.compile(
        "["
        # Original ranges:
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags
        # Additional emoji ranges:
        "\U0001F700-\U0001F77F"  # alchemical symbols
        "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
        "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
        "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs (e.g. 🧪)
        "\U0001FA00-\U0001FA6F"  # Chess symbols, etc.
        "\U0001FA70-\U0001FAFF"  # More recently added emojis
        # Miscellaneous Symbols and Dingbats, etc. can be partially covered by
        # U+2600-U+26FF, but be mindful this can strip out some non-emoji symbols.
        # Add if needed:
        # u"\u2600-\u26FF"
        # Zero-width joiners/non-joiners, variation selectors:
        "\u200c"  # ZERO WIDTH NON-JOINER
        "\u200d"  # ZERO WIDTH JOINER
        "\ufe0e-\ufe0f"  # VARIATION SELECTOR-15, -16
        "]+",
        flags=re.UNICODE,
    )

    # remove emojis and leading whitespace
    return emoji_pattern.sub(r"", text).lstrip()


def maybe_strip_emojis(text):
    """
    Conditionally strip emojis if global STRIP_EMOJIS is True.
    """
    if STRIP_EMOJIS:
        return remove_emojis(text)
    return text


def find_project_id_partial(api, project_name_partial):
    """
    Return the first project ID whose name contains the given partial (case-insensitive).
    If none is found, return None.
    """
    try:
        projects = api.get_projects()
    except Exception as e:
        console.print(f"[red]Failed to fetch projects: {e}[/red]")
        sys.exit(1)

    project_name_lower = project_name_partial.lower()
    for project in projects:
        if project_name_lower in project.name.lower():
            return project.id
    return None


def find_section_id_partial(api, project_id, section_name_partial):
    """
    Return the first section ID (within a project) whose name contains the given partial (case-insensitive).
    If none is found, return None.
    """
    try:
        sections = api.get_sections(project_id=project_id)
    except Exception as e:
        console.print(
            f"[red]Failed to fetch sections for project {project_id}: {e}[/red]"
        )
        sys.exit(1)

    section_name_lower = section_name_partial.lower()
    for section in sections:
        if section_name_lower in section.name.lower():
            return section.id
    return None


##########################
# TASKS
##########################


def list_tasks(
    api,
    show_ids=False,
    show_subtasks=False,
    project_name=None,
    section_name=None,
    output_json=False,
):
    try:
        all_tasks = api.get_tasks()
    except Exception as e:
        console.print(f"[red]Failed to fetch tasks: {e}[/red]")
        sys.exit(1)

    # Filter out subtasks unless --subtasks is provided
    if not show_subtasks:
        all_tasks = [t for t in all_tasks if t.parent_id is None]

    # Filter by project partial match
    project_id = None
    if project_name:
        project_id = find_project_id_partial(api, project_name)
        if not project_id:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)
        all_tasks = [t for t in all_tasks if t.project_id == project_id]

    # Filter by section partial match
    if section_name:
        if not project_name:
            console.print(
                "[red]You must specify a --project if you provide a --section.[/red]"
            )
            sys.exit(1)
        section_id = find_section_id_partial(api, project_id, section_name)
        if not section_id:
            console.print(
                f"[red]No section found matching '{section_name}' in project '{project_name}'.[/red]"
            )
            sys.exit(1)
        all_tasks = [t for t in all_tasks if t.section_id == section_id]

    # Fetch all projects so we can display project names
    try:
        projects_list = api.get_projects()
        projects_dict = {p.id: p for p in projects_list}
    except Exception as e:
        console.print(f"[red]Failed to fetch projects: {e}[/red]")
        sys.exit(1)

    # Determine if the section column should be displayed and build a mapping from section IDs to sections.
    # If a project is provided, only show the section column if that project actually has sections.
    show_section_col = False
    section_mapping = {}
    if project_name:
        try:
            sections = api.get_sections(project_id=project_id)
            if sections:
                show_section_col = True
                section_mapping = {s.id: s for s in sections}
        except Exception:
            show_section_col = False
    else:
        # When no project filter is provided, show section column if any task has a section.
        if any(t.section_id for t in all_tasks):
            show_section_col = True
            unique_project_ids = {t.project_id for t in all_tasks if t.section_id}
            for pid in unique_project_ids:
                try:
                    secs = api.get_sections(project_id=pid)
                    for s in secs:
                        section_mapping[s.id] = s
                except Exception:
                    continue

    # Build a dictionary for looking up parent task names.
    task_dict = {t.id: t for t in all_tasks}

    # Sort tasks by project name, then by section name (if applicable), and finally by task content.
    all_tasks.sort(
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
        for task in all_tasks:
            project_name_str = ""
            if task.project_id in projects_dict:
                project_name_str = maybe_strip_emojis(
                    projects_dict[task.project_id].name
                )
            section_name_str = None
            if show_section_col and task.section_id in section_mapping:
                section_name_str = maybe_strip_emojis(
                    section_mapping[task.section_id].name
                )
            parent_task_str = None
            if show_subtasks and task.parent_id:
                if task.parent_id in task_dict:
                    parent_task_str = maybe_strip_emojis(
                        task_dict[task.parent_id].content
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

    # Build the Rich table with extra columns as needed.
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

    for task in all_tasks:
        due_str = task.due.string if task.due else "N/A"
        project_name_str = "N/A"
        if task.project_id in projects_dict:
            project_name_str = maybe_strip_emojis(projects_dict[task.project_id].name)
        section_name_str = "N/A"
        if show_section_col and task.section_id in section_mapping:
            section_name_str = maybe_strip_emojis(section_mapping[task.section_id].name)
        parent_task_str = "N/A"
        if show_subtasks and task.parent_id:
            if task.parent_id in task_dict:
                parent_task_str = maybe_strip_emojis(task_dict[task.parent_id].content)
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


def create_task(
    api,
    content,
    priority=None,
    due=None,
    reminder=None,
    project_name=None,
    section_name=None,
):
    """
    Create a new task if it does not already exist (by exact content match) in the same project.
    Optionally specify priority, due date, reminder, etc.
    """
    project_id = None
    section_id = None

    # If a project is specified, find its ID by partial match
    if project_name:
        project_id = find_project_id_partial(api, project_name)
        if not project_id:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)

    # If a section is specified, find its ID by partial match
    if section_name:
        if not project_id:
            console.print(
                "[red]You must specify --project if you provide a --section.[/red]"
            )
            sys.exit(1)
        section_id = find_section_id_partial(api, project_id, section_name)
        if not section_id:
            console.print(
                f"[red]No section found matching '{section_name}' in project '{project_name}'.[/red]"
            )
            sys.exit(1)

    # Fetch existing tasks in that project (or all tasks if no project specified)
    try:
        tasks = api.get_tasks(project_id=project_id) if project_id else api.get_tasks()
    except Exception as e:
        console.print(f"[red]Failed to fetch tasks: {e}[/red]")
        sys.exit(1)

    # Check if a task with the same content already exists (case-insensitive match)
    for task in tasks:
        if task.content.strip().lower() == content.strip().lower():
            console.print(
                f"[yellow]Task '{content}' already exists, skipping creation.[/yellow]"
            )
            return

    # Build parameters for adding a task
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
        new_task = api.add_task(**add_kwargs)
        console.print(
            f"[green]Task '{content}' created successfully (ID: {new_task.id}).[/green]"
        )
        # If a reminder was specified, try to create it
        if reminder:
            try:
                api.add_reminder(task_id=new_task.id, due_string=reminder)
                console.print(
                    f"[green]Reminder set for task '{content}' with due string '{reminder}'.[/green]"
                )
            except Exception as e:
                console.print(f"[yellow]Failed to add reminder: {e}[/yellow]")
    except Exception as e:
        console.print(f"[red]Failed to create task '{content}': {e}[/red]")
        sys.exit(1)


def mark_task_done(api, content, project_name=None):
    """
    Marks the first matching task with the given content as complete.
    Optionally limit to a project by partial name.
    """
    project_id = None
    if project_name:
        project_id = find_project_id_partial(api, project_name)
        if not project_id:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)

    try:
        tasks = api.get_tasks(project_id=project_id) if project_id else api.get_tasks()
    except Exception as e:
        console.print(f"[red]Failed to fetch tasks: {e}[/red]")
        sys.exit(1)

    for task in tasks:
        if task.content.strip().lower() == content.strip().lower():
            try:
                api.close_task(task.id)
                console.print(f"[green]Task '{content}' marked as done.[/green]")
                return
            except Exception as e:
                console.print(f"[red]Failed to mark task '{content}' done: {e}[/red]")
                sys.exit(1)

    console.print(f"[yellow]No matching task found for '{content}'.[/yellow]")


def delete_task(api, content, project_name=None):
    """
    Delete the first matching task with the given content (case-insensitive).
    Optionally limit to a project by partial name.
    """
    project_id = None
    if project_name:
        project_id = find_project_id_partial(api, project_name)
        if not project_id:
            console.print(f"[red]No project found matching '{project_name}'.[/red]")
            sys.exit(1)

    try:
        tasks = api.get_tasks(project_id=project_id) if project_id else api.get_tasks()
    except Exception as e:
        console.print(f"[red]Failed to fetch tasks: {e}[/red]")
        sys.exit(1)

    for task in tasks:
        if task.content.strip().lower() == content.strip().lower():
            try:
                api.delete_task(task.id)
                console.print(f"[green]Task '{content}' deleted successfully.[/green]")
                return
            except Exception as e:
                console.print(f"[red]Failed to delete task '{content}': {e}[/red]")
                sys.exit(1)

    console.print(f"[yellow]No matching task found for '{content}'.[/yellow]")


##########################
# PROJECTS
##########################


def list_projects(api, show_ids=False, output_json=False):
    """
    List all projects, sorted by name. If output_json=True, prints JSON instead of a table.
    """
    try:
        projects = api.get_projects()
    except Exception as e:
        console.print(f"[red]Failed to fetch projects: {e}[/red]")
        sys.exit(1)

    # Sort by project name
    projects.sort(key=lambda p: p.name.lower())

    if output_json:
        data = []
        for project in projects:
            data.append({"id": project.id, "name": maybe_strip_emojis(project.name)})
        console.print_json(json.dumps(data))
        return

    # Otherwise, use a Rich table
    table = Table(box=None, pad_edge=False)
    if show_ids:
        table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="white")

    for project in projects:
        name_str = maybe_strip_emojis(project.name)
        row = []
        if show_ids:
            row.append(str(project.id))
        row.append(name_str)
        table.add_row(*row)

    console.print(table)


def create_project(api, name):
    """
    Create a new project with the specified name if it doesn't already exist (case-insensitive exact match).
    """
    try:
        projects = api.get_projects()
        for project in projects:
            if project.name.strip().lower() == name.strip().lower():
                console.print(
                    f"[yellow]Project '{name}' already exists, skipping creation.[/yellow]"
                )
                return
        new_project = api.add_project(name=name)
        console.print(
            f"[green]Project '{name}' created successfully (ID: {new_project.id}).[/green]"
        )
    except Exception as e:
        console.print(f"[red]Failed to create project '{name}': {e}[/red]")
        sys.exit(1)


def delete_project(api, name_partial):
    """
    Delete the first project whose name contains the given partial (case-insensitive).
    """
    project_id = find_project_id_partial(api, name_partial)
    if not project_id:
        console.print(f"[yellow]No project found matching '{name_partial}'.[/yellow]")
        return

    try:
        api.delete_project(project_id)
        console.print(
            f"[green]Project matching '{name_partial}' deleted successfully.[/green]"
        )
    except Exception as e:
        console.print(
            f"[red]Failed to delete project matching '{name_partial}': {e}[/red]"
        )
        sys.exit(1)


##########################
# SECTIONS
##########################


def list_sections(api, show_ids, project_name, output_json=False):
    """
    List sections for a given project (partial match).
    If output_json=True, prints JSON instead of a table.
    """
    project_id = find_project_id_partial(api, project_name)
    if not project_id:
        console.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)

    try:
        sections = api.get_sections(project_id=project_id)
    except Exception as e:
        console.print(f"[red]Failed to fetch sections: {e}[/red]")
        sys.exit(1)

    # Sort by section name
    sections.sort(key=lambda s: s.name.lower())

    if output_json:
        data = []
        for section in sections:
            data.append({"id": section.id, "name": maybe_strip_emojis(section.name)})
        console.print_json(json.dumps(data))
        return

    table = Table(box=None, pad_edge=False)
    if show_ids:
        table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="white")

    for section in sections:
        name_str = maybe_strip_emojis(section.name)
        row = []
        if show_ids:
            row.append(str(section.id))
        row.append(name_str)
        table.add_row(*row)

    console.print(table)


def create_section(api, project_name, section_name):
    """
    Create a new section in the specified project if it doesn't already exist.
    """
    project_id = find_project_id_partial(api, project_name)
    if not project_id:
        console.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)

    try:
        sections = api.get_sections(project_id=project_id)
    except Exception as e:
        console.print(
            f"[red]Failed to fetch sections for project '{project_name}': {e}[/red]"
        )
        sys.exit(1)

    # Check if a section with the same name already exists (case-insensitive)
    for section in sections:
        if section.name.strip().lower() == section_name.strip().lower():
            console.print(
                f"[yellow]Section '{section_name}' already exists in project '{project_name}', skipping creation.[/yellow]"
            )
            return

    try:
        new_section = api.add_section(name=section_name, project_id=project_id)
        console.print(
            f"[green]Section '{section_name}' created successfully in project '{project_name}' (ID: {new_section.id}).[/green]"
        )
    except Exception as e:
        console.print(
            f"[red]Failed to create section '{section_name}' in project '{project_name}': {e}[/red]"
        )
        sys.exit(1)


def delete_section(api, project_name, section_partial):
    """
    Delete the first section in the specified project (partial match)
    whose name contains the given partial (case-insensitive).
    """
    project_id = find_project_id_partial(api, project_name)
    if not project_id:
        console.print(f"[red]No project found matching '{project_name}'.[/red]")
        sys.exit(1)

    try:
        sections = api.get_sections(project_id=project_id)
    except Exception as e:
        console.print(f"[red]Failed to fetch sections: {e}[/red]")
        sys.exit(1)

    section_id = None
    section_partial_lower = section_partial.lower()
    for s in sections:
        if section_partial_lower in s.name.lower():
            section_id = s.id
            break

    if not section_id:
        console.print(
            f"[yellow]No section found matching '{section_partial}'.[/yellow]"
        )
        return

    try:
        api.delete_section(section_id)
        console.print(
            f"[green]Section matching '{section_partial}' deleted successfully.[/green]"
        )
    except Exception as e:
        console.print(
            f"[red]Failed to delete section matching '{section_partial}': {e}[/red]"
        )
        sys.exit(1)


##########################
# MAIN
##########################


def main():
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

    ################
    # task
    ################
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

    # task list
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

    # task create
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

    # task done
    task_done_parser = task_subparsers.add_parser(
        "done", help="Mark a task as done", formatter_class=RawTextRichHelpFormatter
    )
    task_done_parser.add_argument("content", help="Task content to mark as done")
    task_done_parser.add_argument(
        "-p", "--project", default=None, help="Project name (partial match)"
    )

    # task delete
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

    ################
    # project
    ################
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

    # project list
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

    # project create
    project_create_parser = project_subparsers.add_parser(
        "create",
        aliases=["cr", "c", "add", "a"],
        help="Create a new project",
        formatter_class=RawTextRichHelpFormatter,
    )
    project_create_parser.add_argument("name", help="Project name")

    # project delete
    project_delete_parser = project_subparsers.add_parser(
        "delete",
        aliases=["del", "d", "remove", "rm"],
        help="Delete a project",
        formatter_class=RawTextRichHelpFormatter,
    )
    project_delete_parser.add_argument(
        "name", help="Partial name match for project to delete"
    )

    ################
    # section
    ################
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

    # section list
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

    # section create
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

    # section delete
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
        "section_name", help="Name of the section to create"
    )

    args = parser.parse_args()

    # Check for missing subcommands and default to `task list`
    if args.command is None:
        args.command = "task"
        args.task_command = "list"

    global STRIP_EMOJIS
    STRIP_EMOJIS = args.strip_emojis

    # Instantiate the Todoist API
    api = TodoistAPI(args.api_key)

    if args.command in ["task", "tasks", "t"]:
        if args.task_command in ["create", "cr", "c", "add", "a"]:
            create_task(
                api,
                content=args.content,
                priority=args.priority,
                due=args.due,
                reminder=args.reminder,
                project_name=args.project,
                section_name=args.section,
            )
        elif args.task_command == "done":
            mark_task_done(api, content=args.content, project_name=args.project)
        elif args.task_command in ["delete", "del", "d", "remove", "rm"]:
            delete_task(api, content=args.content, project_name=args.project)
        else:
            list_tasks(
                api,
                show_ids=args.ids,
                show_subtasks=args.subtasks,
                project_name=args.project,
                section_name=args.section,
                output_json=args.json,
            )

    elif args.command in ["projects", "project", "proj", "p"]:
        if args.project_command in ["create", "cr", "c", "add", "a"]:
            create_project(api, args.name)
        elif args.project_command in ["delete", "del", "d", "remove", "rm"]:
            delete_project(api, args.name)
        else:
            list_projects(api, show_ids=args.ids, output_json=args.json)

    elif args.command in ["sections", "section", "sect", "sec", "s"]:
        if args.section_command in ["delete", "del", "d", "remove", "rm"]:
            delete_section(
                api, project_name=args.project, section_partial=args.section_name
            )
        elif args.section_command in ["create", "cr", "c", "add", "a"]:
            create_section(
                api, project_name=args.project, section_name=args.section_name
            )
        else:
            if not args.project:
                section_parser.print_help()
            else:
                list_sections(
                    api,
                    show_ids=args.ids,
                    project_name=args.project,
                    output_json=args.json,
                )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
