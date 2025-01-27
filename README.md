# ✅ tdc: CLI for Todoist

`tdc` is a lightweight Python command-line interface for interacting with [Todoist](https://todoist.com/).

Powered by:
- [todoist-api-python](https://pypi.org/project/todoist-api-python/) (the official Todoist API client)
- [rich](https://pypi.org/project/rich) for colorful terminal output

## Features

- **List tasks** (with optional filters for project/section)
- **Create tasks** (with optional priority, due date, reminders, etc.)
- **Mark tasks as done**
- **List projects** or create new ones
- **List sections** in a project
- **Hide subtasks** by default (use `--subtasks` to show them)
- **Strip emojis** with `--strip-emojis` (helpful if emoji characters disrupt your terminal or table layout)
- **Partial matching** on project and section names (e.g., `--project "MyProj"` matches `"MyProject"`)

## Installation

```shell
uv tool install git+https://github.com/pschmitt/tdc
```

## Usage

All commands require an API key.

See https://todoist.com/help/articles/find-your-api-token-Jpzx9IIlB

### Show Help

```
tdc --help
```

### List Tasks

```
tdc --api-key <YOUR_API_KEY> task list
```

- **Filter by project** (partial match):
  ```
  tdc --api-key <YOUR_API_KEY> task list --project "Work"
  ```
- **Filter by section**:
  ```
  tdc --api-key <YOUR_API_KEY> task list --project "Work" --section "Urgent"
  ```
- **Show IDs**:
  ```
  tdc --api-key <YOUR_API_KEY> task list --ids
  ```
- **Include subtasks**:
  ```
  tdc --api-key <YOUR_API_KEY> task list --subtasks
  ```

### Create a Task

```
tdc --api-key <YOUR_API_KEY> task create "Brush teeth" \
  --priority 4 \
  --due "today 11pm" \
  --reminder "today 10pm" \
  --project "Daily Routines" \
  --section "Night"
```

### Mark a Task as Done

```
tdc --api-key <YOUR_API_KEY> task done "Brush teeth"
```

(Optional) Limit to a project:

```
tdc --api-key <YOUR_API_KEY> task done "Brush teeth" --project "Daily Routines"
```

### List / Create Projects

**List Projects**:

```
tdc --api-key <YOUR_API_KEY> project list
```

(Use `--ids` to see project IDs.)

**Create a Project**:

```
tdc --api-key <YOUR_API_KEY> project create "MyNewProject"
```

### List Sections

To list sections in a given project (partial match), use:

```
tdc --api-key <YOUR_API_KEY> section --project "MyNewProject"
```

(Again, `--ids` is available to show section IDs.)

## License

GPL-3.0 - see [LICENSE](LICENSE) for details.
