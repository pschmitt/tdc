#!/usr/bin/env python3

import sys
import argparse
import re

from todoist_api_python.api import TodoistAPI
from todoist_api_python.models import Task
from rich.console import Console
from rich.table import Table

# For colorizing help output:
try:
  from rich_argparse import RawTextRichHelpFormatter
except ImportError:
  print(
    "You need to install 'rich-argparse' for colorized help.\n"
    "Install it via: pip install rich-argparse"
  )
  sys.exit(1)

console = Console()

# Global toggles (will be set after arg parsing)
STRIP_EMOJIS = False

def remove_emojis(text):
  """
  Remove common emojis from a string using a regex pattern.
  Adjust or expand pattern if needed to handle additional Unicode ranges.
  """
  if not text:
    return text

  emoji_pattern = re.compile("["
      u"\U0001F600-\U0001F64F"  # emoticons
      u"\U0001F300-\U0001F5FF"  # symbols & pictographs
      u"\U0001F680-\U0001F6FF"  # transport & map symbols
      u"\U0001F1E0-\U0001F1FF"  # flags (iOS)
      "]+", flags=re.UNICODE)

  return emoji_pattern.sub(r'', text)

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
    # Compare partial
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
    console.print(f"[red]Failed to fetch sections for project {project_id}: {e}[/red]")
    sys.exit(1)

  section_name_lower = section_name_partial.lower()
  for section in sections:
    if section_name_lower in section.name.lower():
      return section.id
  return None

def list_tasks(api, show_ids=False, show_subtasks=False, project_name=None, section_name=None):
  """
  List tasks, optionally filtered by project name (partial) and/or section name (partial).
  By default, subtasks (parent_id != None) are excluded unless show_subtasks=True.
  """
  try:
    all_tasks = api.get_tasks()
  except Exception as e:
    console.print(f"[red]Failed to fetch tasks: {e}[/red]")
    sys.exit(1)

  # Filter out subtasks unless --subtasks is provided
  if not show_subtasks:
    all_tasks = [t for t in all_tasks if t.parent_id is None]

  # Filter by project partial match
  if project_name:
    project_id = find_project_id_partial(api, project_name)
    if not project_id:
      console.print(f"[red]No project found matching '{project_name}'.[/red]")
      sys.exit(1)
    all_tasks = [t for t in all_tasks if t.project_id == project_id]

  # Filter by section partial match
  if section_name:
    if not project_name:
      console.print("[red]You must specify a --project if you provide a --section.[/red]")
      sys.exit(1)
    project_id = find_project_id_partial(api, project_name)
    section_id = find_section_id_partial(api, project_id, section_name)
    if not section_id:
      console.print(f"[red]No section found matching '{section_name}' in project '{project_name}'.[/red]")
      sys.exit(1)
    all_tasks = [t for t in all_tasks if t.section_id == section_id]

  # Sort by task content
  all_tasks.sort(key=lambda t: t.content.lower())

  # Fetch all projects so we can display project names
  try:
    projects_list = api.get_projects()
    projects_dict = {p.id: p for p in projects_list}
  except Exception as e:
    console.print(f"[red]Failed to fetch projects: {e}[/red]")
    sys.exit(1)

  # Prepare a rich table (no borders)
  table = Table(title="Todoist Tasks", box=None)
  if show_ids:
    table.add_column("ID", style="cyan", no_wrap=True)
  table.add_column("Content", style="white")
  table.add_column("Project", style="magenta")  # New column for project name
  if show_ids:
    table.add_column("Section ID", style="magenta")
  table.add_column("Priority", style="yellow")
  table.add_column("Due", style="green")

  for task in all_tasks:
    due_str = task.due.string if task.due else ""

    # Resolve project name
    project_name_str = ""
    if task.project_id in projects_dict:
      project_name_str = maybe_strip_emojis(projects_dict[task.project_id].name)

    row = []
    if show_ids:
      row.append(str(task.id))
    row.append(maybe_strip_emojis(task.content))
    row.append(project_name_str)
    if show_ids:
      row.append(str(task.section_id) if task.section_id else "")
    row.append(str(task.priority))
    row.append(maybe_strip_emojis(due_str))

    table.add_row(*row)

  console.print(table)

def create_task(api, content, priority=None, due=None, reminder=None, project_name=None, section_name=None):
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
      console.print("[red]You must specify --project if you provide a --section.[/red]")
      sys.exit(1)
    section_id = find_section_id_partial(api, project_id, section_name)
    if not section_id:
      console.print(f"[red]No section found matching '{section_name}' in project '{project_name}'.[/red]")
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
      console.print(f"[yellow]Task '{content}' already exists, skipping creation.[/yellow]")
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
    console.print(f"[green]Task '{content}' created successfully (ID: {new_task.id}).[/green]")
    # If a reminder was specified, try to create it
    if reminder:
      try:
        api.add_reminder(
          task_id=new_task.id,
          due_string=reminder
        )
        console.print(f"[green]Reminder set for task '{content}' with due string '{reminder}'.[/green]")
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

def list_projects(api, show_ids=False):
  """
  List all projects, sorted by name.
  """
  try:
    projects = api.get_projects()
  except Exception as e:
    console.print(f"[red]Failed to fetch projects: {e}[/red]")
    sys.exit(1)

  # Sort by project name
  projects.sort(key=lambda p: p.name.lower())

  table = Table(title="Todoist Projects", box=None)
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
        console.print(f"[yellow]Project '{name}' already exists, skipping creation.[/yellow]")
        return
    new_project = api.add_project(name=name)
    console.print(f"[green]Project '{name}' created successfully (ID: {new_project.id}).[/green]")
  except Exception as e:
    console.print(f"[red]Failed to create project '{name}': {e}[/red]")
    sys.exit(1)

def list_sections(api, show_ids, project_name):
  """
  List sections for a given project (partial match).
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

  table = Table(title=f"Sections in Project '{project_name}'", box=None)
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

def main():
  parser = argparse.ArgumentParser(
    prog="tdc",
    description="[bold cyan]A Python CLI for Todoist[/bold cyan], leveraging [yellow]Rich[/yellow] for display and the official [green]Todoist API[/green].",
    formatter_class=RawTextRichHelpFormatter
  )

  parser.add_argument(
    "--api-key",
    help="Your Todoist API key",
    required=True
  )

  parser.add_argument(
    "--strip-emojis",
    action="store_true",
    help="Remove emojis from displayed text."
  )

  subparsers = parser.add_subparsers(dest="command", help="[magenta]Available commands[/magenta]")

  # Task subcommand
  task_parser = subparsers.add_parser(
    "task",
    help="[cyan]Manage tasks[/cyan]",
    formatter_class=RawTextRichHelpFormatter
  )
  task_subparsers = task_parser.add_subparsers(dest="task_command", help="[magenta]Task commands[/magenta]")

  # tdc --api-key XXX task list [--project MYPROJECT] [--section xxx] [--ids] [--subtasks]
  task_list_parser = task_subparsers.add_parser(
    "list",
    help="List tasks",
    formatter_class=RawTextRichHelpFormatter
  )
  task_list_parser.add_argument("--project", help="Filter tasks by project name (partial match)")
  task_list_parser.add_argument("--section", help="Filter tasks by section name (partial match)")
  task_list_parser.add_argument("--ids", action="store_true", help="Show ID columns")
  task_list_parser.add_argument("--subtasks", action="store_true", help="Include subtasks")

  # tdc --api-key XXX task create "brush teeth" --priority 4 --due xxx --reminder xxx --project XXX --section YYY
  task_create_parser = task_subparsers.add_parser(
    "create",
    help="Create a new task",
    formatter_class=RawTextRichHelpFormatter
  )
  task_create_parser.add_argument("content", help="Task content (e.g., 'Brush teeth')")
  task_create_parser.add_argument("--priority", type=int, default=None, help="Priority (1-4)")
  task_create_parser.add_argument("--due", default=None, help="Due date/time string (e.g., 'tomorrow')")
  task_create_parser.add_argument("--reminder", default=None, help="Reminder due date/time string")
  task_create_parser.add_argument("--project", default=None, help="Project name (partial match)")
  task_create_parser.add_argument("--section", default=None, help="Section name (partial match) (requires --project)")

  # tdc task done "brush teeth" [--project XXX]
  task_done_parser = task_subparsers.add_parser(
    "done",
    help="Mark a task as done",
    formatter_class=RawTextRichHelpFormatter
  )
  task_done_parser.add_argument("content", help="Task content to mark as done")
  task_done_parser.add_argument("--project", default=None, help="Project name (partial match)")

  # Project subcommand
  project_parser = subparsers.add_parser(
    "project",
    help="[cyan]Manage projects[/cyan]",
    formatter_class=RawTextRichHelpFormatter
  )
  project_subparsers = project_parser.add_subparsers(dest="project_command", help="[magenta]Project commands[/magenta]")

  # tdc --api-key XXX project list [--ids]
  project_list_parser = project_subparsers.add_parser(
    "list",
    help="List all projects",
    formatter_class=RawTextRichHelpFormatter
  )
  project_list_parser.add_argument("--ids", action="store_true", help="Show ID columns")

  # tdc --api-key XXX project create "MyProject"
  project_create_parser = project_subparsers.add_parser(
    "create",
    help="Create a new project",
    formatter_class=RawTextRichHelpFormatter
  )
  project_create_parser.add_argument("name", help="Project name")

  # Section subcommand
  section_parser = subparsers.add_parser(
    "section",
    help="[cyan]Manage sections[/cyan]",
    formatter_class=RawTextRichHelpFormatter
  )
  section_parser.add_argument("--project", required=True, help="Project name (partial match) to list sections for")
  section_parser.add_argument("--ids", action="store_true", help="Show ID columns")

  args = parser.parse_args()

  # Set global strip-emojis
  global STRIP_EMOJIS
  STRIP_EMOJIS = args.strip_emojis

  # Instantiate the Todoist API
  api = TodoistAPI(args.api_key)

  if args.command == "task":
    if args.task_command == "list":
      list_tasks(
        api,
        show_ids=args.ids,
        show_subtasks=args.subtasks,
        project_name=args.project,
        section_name=args.section
      )
    elif args.task_command == "create":
      create_task(
        api,
        content=args.content,
        priority=args.priority,
        due=args.due,
        reminder=args.reminder,
        project_name=args.project,
        section_name=args.section
      )
    elif args.task_command == "done":
      mark_task_done(api, content=args.content, project_name=args.project)
    else:
      task_parser.print_help()

  elif args.command == "project":
    if args.project_command == "list":
      list_projects(api, show_ids=args.ids)
    elif args.project_command == "create":
      create_project(api, args.name)
    else:
      project_parser.print_help()

  elif args.command == "section":
    list_sections(api, show_ids=args.ids, project_name=args.project)

  else:
    parser.print_help()

if __name__ == "__main__":
  main()
