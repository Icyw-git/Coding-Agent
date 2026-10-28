"""工具 schema 定义（按注册顺序排列）。

顺序属于对外契约：system prompt 里的工具清单、`_tools_fingerprint` 与请求前缀
缓存都依赖它。新增工具请追加到末尾，并同步 tests/test_tool_schemas.py 的 ORDER
与 SCHEMA_SHA1。

内容从运行时 `tools.TOOLS` 生成，重新生成时用 `pprint(sort_dicts=False)` 保持
列表顺序与 dict 键序，例如：

    import pprint, sys
    sys.path[:0] = ['.', 'src']
    import tools
    print(pprint.pformat(tools.TOOLS, width=99, sort_dicts=False))
"""
from __future__ import annotations

TOOL_SCHEMAS = [{'type': 'function',
  'function': {'name': 'bash',
               'description': 'Execute shell commands. Slow commands (install/build/test) run in '
                              'the background by default; use background_output to read their '
                              'output and kill_background to stop them.',
               'parameters': {'type': 'object',
                              'properties': {'command': {'type': 'string',
                                                         'description': 'The shell command to '
                                                                        "execute, e.g. 'ls'"},
                                             'cwd': {'type': 'string',
                                                     'description': 'Optional working directory '
                                                                    '(relative to the workspace '
                                                                    'or absolute)'},
                                             'timeout': {'type': 'number',
                                                         'description': 'Optional timeout in '
                                                                        'seconds (max 3600; '
                                                                        'background default 3600, '
                                                                        'foreground 120)'},
                                             'run_in_background': {'type': 'boolean',
                                                                   'description': 'Force '
                                                                                  'background '
                                                                                  '(true) or '
                                                                                  'foreground '
                                                                                  '(false) '
                                                                                  'execution, '
                                                                                  'overriding the '
                                                                                  'slow-command '
                                                                                  'heuristic'}},
                              'required': ['command']}}},
 {'type': 'function',
  'function': {'name': 'read',
               'description': 'Read a file. Absolute paths and paths outside the workspace are '
                              'allowed; protected secret files are denied.',
               'parameters': {'type': 'object',
                              'properties': {'path': {'type': 'string',
                                                      'description': 'File path: relative to the '
                                                                     'workspace (or cwd), or '
                                                                     'absolute'},
                                             'limit': {'type': 'integer',
                                                       'description': 'Optional maximum number of '
                                                                      'lines to read'},
                                             'offset': {'type': 'integer',
                                                        'description': 'Optional 0-based line '
                                                                       'offset to start reading '
                                                                       'from (default 0)'}},
                              'required': ['path']}}},
 {'type': 'function',
  'function': {'name': 'view_image',
               'description': 'Read a local image (PNG/JPEG/GIF/WebP/BMP) and attach it to the '
                              'conversation for visual inspection. Requires a vision-capable '
                              'model.',
               'parameters': {'type': 'object',
                              'properties': {'path': {'type': 'string',
                                                      'description': 'Image path: relative to the '
                                                                     'workspace (or cwd), or '
                                                                     'absolute'},
                                             'cwd': {'type': 'string',
                                                     'description': 'Optional directory to '
                                                                    'resolve a relative path '
                                                                    'against'}},
                              'required': ['path']}}},
 {'type': 'function',
  'function': {'name': 'read_document',
               'description': 'Extract text from a local PDF, DOCX, PPTX, or XLSX file. PDF/PPTX '
                              "accept a 1-based pages range such as '1-10,15'; XLSX accepts sheet "
                              'names or 1-based indexes. Scanned PDFs without a text layer are '
                              'reported; call render_document for the affected pages so a vision-capable '
                              'model can read the rendered images.',
               'parameters': {'type': 'object',
                              'properties': {'path': {'type': 'string',
                                                      'description': 'Document path: relative to '
                                                                     'the workspace (or cwd), or '
                                                                     'absolute'},
                                             'pages': {'type': 'string',
                                                       'description': 'Optional PDF page or PPTX '
                                                                      'slide range, 1-based (e.g. '
                                                                      "'1-10,15'). Default: first "
                                                                      '20.'},
                                             'sheets': {'type': 'string',
                                                        'description': 'Optional XLSX sheet names '
                                                                       'or 1-based indexes, '
                                                                       'comma-separated. Default: '
                                                                       'first 8 sheets.'},
                                             'cwd': {'type': 'string',
                                                     'description': 'Optional directory to '
                                                                    'resolve a relative path '
                                                                    'against'}},
                              'required': ['path']}}},
 {'type': 'function',
  'function': {'name': 'render_document',
               'description': 'Render selected pages of a PDF, DOCX, or PPTX to PNG images for '
                              'visual inspection of layout, charts, tables, and scanned pages. '
                              'Images are attached in the next user message. Prefer read_document '
                              'for plain text. Both need optional extras: PDF needs pypdfium2, '
                              'DOCX/PPTX conversion needs LibreOffice; read_document keeps '
                              'working without them.',
               'parameters': {'type': 'object',
                              'properties': {'path': {'type': 'string',
                                                      'description': 'Document path: relative to '
                                                                     'the workspace (or cwd), or '
                                                                     'absolute'},
                                             'pages': {'type': 'string',
                                                       'description': 'Optional 1-based '
                                                                      'page/slide range such as '
                                                                      "'1-3,5'. Default: first 3; "
                                                                      'max 8 per call.'},
                                             'dpi': {'type': 'integer',
                                                     'description': 'Render resolution, 72-200 '
                                                                    '(default 110). Higher costs '
                                                                    'more tokens.'},
                                             'cwd': {'type': 'string',
                                                     'description': 'Optional directory to '
                                                                    'resolve a relative path '
                                                                    'against'}},
                              'required': ['path']}}},
 {'type': 'function',
  'function': {'name': 'write',
               'description': 'Write a file. Workspace paths are written directly; paths outside '
                              'the workspace require approval.',
               'parameters': {'type': 'object',
                              'properties': {'path': {'type': 'string',
                                                      'description': 'File path: relative to the '
                                                                     'workspace (or cwd), or '
                                                                     'absolute'},
                                             'content': {'type': 'string',
                                                         'description': 'Content to write to the '
                                                                        'file'}},
                              'required': ['path', 'content']}}},
 {'type': 'function',
  'function': {'name': 'edit',
               'description': 'Replace a unique text fragment in a file. Paths outside the '
                              'workspace require approval.',
               'parameters': {'type': 'object',
                              'properties': {'path': {'type': 'string',
                                                      'description': 'File path: relative to the '
                                                                     'workspace (or cwd), or '
                                                                     'absolute'},
                                             'old_text': {'type': 'string',
                                                          'description': 'Exact text to replace'},
                                             'new_text': {'type': 'string',
                                                          'description': 'Replacement text'},
                                             'replace_all': {'type': 'boolean',
                                                             'description': 'Replace every '
                                                                            'occurrence (default '
                                                                            'false; otherwise '
                                                                            'old_text must be '
                                                                            'unique)'}},
                              'required': ['path', 'old_text', 'new_text']}}},
 {'type': 'function',
  'function': {'name': 'glob',
               'description': 'List files matching a glob pattern. Read-only; the root may be '
                              'outside the workspace.',
               'parameters': {'type': 'object',
                              'properties': {'pattern': {'type': 'string',
                                                         'description': 'Glob pattern relative to '
                                                                        "the root, e.g. '*.py' or "
                                                                        "'src/**/*.ts'"},
                                             'path': {'type': 'string',
                                                      'description': 'Root directory (or file). '
                                                                     'Relative to the '
                                                                     'workspace/cwd, or absolute; '
                                                                     'defaults to the workspace'}},
                              'required': ['pattern']}}},
 {'type': 'function',
  'function': {'name': 'grep',
               'description': 'Search file contents with a regular expression. Read-only; '
                              'protected secret files are skipped.',
               'parameters': {'type': 'object',
                              'properties': {'pattern': {'type': 'string',
                                                         'description': 'Python regular '
                                                                        'expression to search '
                                                                        'for'},
                                             'path': {'type': 'string',
                                                      'description': 'File or directory to '
                                                                     'search. Relative to the '
                                                                     'workspace/cwd, or absolute; '
                                                                     'defaults to the workspace. '
                                                                     'Ignored when scope is '
                                                                     '"workspaces"'},
                                             'scope': {'type': 'string',
                                                       'enum': ['workspace', 'workspaces'],
                                                       'description': '"workspace" (default) '
                                                                      'searches one path; '
                                                                      '"workspaces" searches '
                                                                      'every workspace registered '
                                                                      'with /workspace add and '
                                                                      'groups the hits by '
                                                                      'project. Use it to answer '
                                                                      '"where did I see this?"'},
                                             'glob': {'type': 'string',
                                                      'description': 'Only search files matching '
                                                                     "this glob, e.g. '*.py' or "
                                                                     "'src/**/*.ts'"},
                                             'case_insensitive': {'type': 'boolean',
                                                                  'description': 'Case-insensitive '
                                                                                 'matching '
                                                                                 '(default '
                                                                                 'false)'},
                                             'max_results': {'type': 'integer',
                                                             'description': 'Maximum matching '
                                                                            'lines to return '
                                                                            '(default 100, capped '
                                                                            'at 1000)'}},
                              'required': ['pattern']}}},
 {'type': 'function',
  'function': {'name': 'ls',
               'description': 'List a directory. Read-only; the path may be outside the '
                              'workspace.',
               'parameters': {'type': 'object',
                              'properties': {'path': {'type': 'string',
                                                      'description': 'Directory or file to list. '
                                                                     'Relative to the '
                                                                     'workspace/cwd, or absolute; '
                                                                     'defaults to the workspace'},
                                             'all': {'type': 'boolean',
                                                     'description': 'Include hidden entries '
                                                                    '(default false)'}}}}},
 {'type': 'function',
  'function': {'name': 'todo_write',
               'description': 'Replace the current task list. Use it to track multi-step work; '
                              'keep exactly one item in_progress, and mark items completed as you '
                              'finish them.',
               'parameters': {'type': 'object',
                              'properties': {'todos': {'type': 'array',
                                                       'description': 'Full task list (replaces '
                                                                      'the previous one), each '
                                                                      "with 'content' and "
                                                                      "'status' "
                                                                      '(pending/in_progress/completed)',
                                                       'items': {'type': 'object',
                                                                 'properties': {'content': {'type': 'string',
                                                                                            'description': 'Task '
                                                                                                           'description'},
                                                                                'status': {'type': 'string',
                                                                                           'enum': ['pending',
                                                                                                    'in_progress',
                                                                                                    'completed'],
                                                                                           'description': 'Task '
                                                                                                          'status'}},
                                                                 'required': ['content',
                                                                              'status']}}},
                              'required': ['todos']}}},
 {'type': 'function',
  'function': {'name': 'todo_read',
               'description': 'Read the current task list for this session.',
               'parameters': {'type': 'object', 'properties': {}}}},
 {'type': 'function',
  'function': {'name': 'create_task',
               'description': 'Create a new task in the task system (persisted to .cache/tasks).',
               'parameters': {'type': 'object',
                              'properties': {'subject': {'type': 'string',
                                                         'description': 'One-line task subject'},
                                             'description': {'type': 'string',
                                                             'description': 'Detailed task '
                                                                            'description'},
                                             'blockedBy': {'type': 'array',
                                                           'description': 'Task ids that must be '
                                                                          'completed before this '
                                                                          'task can start',
                                                           'items': {'type': 'string'}}},
                              'required': ['subject']}}},
 {'type': 'function',
  'function': {'name': 'list_tasks',
               'description': 'List all tasks with id, subject, status, owner and dependencies.',
               'parameters': {'type': 'object', 'properties': {}}}},
 {'type': 'function',
  'function': {'name': 'get_task',
               'description': "Get a task's full detail by id.",
               'parameters': {'type': 'object',
                              'properties': {'task_id': {'type': 'string',
                                                         'description': 'Task id, e.g. '
                                                                        'task_1700000000_abcd'}},
                              'required': ['task_id']}}},
 {'type': 'function',
  'function': {'name': 'claim_task',
               'description': 'Claim a pending task or recover an expired in-progress lease '
                              '(sets a one-hour lease). Fails if dependencies are not completed '
                              "or another owner's lease is still active.",
               'parameters': {'type': 'object',
                              'properties': {'task_id': {'type': 'string',
                                                         'description': 'Task id to claim'}},
                              'required': ['task_id']}}},
 {'type': 'function',
  'function': {'name': 'complete_task',
               'description': 'Mark a task as completed and report newly unblocked pending tasks.',
               'parameters': {'type': 'object',
                              'properties': {'task_id': {'type': 'string',
                                                         'description': 'Task id to complete'}},
                              'required': ['task_id']}}},
 {'type': 'function',
  'function': {'name': 'schedule_cron',
               'description': 'Create a reminder or recurring job for the current chat. Use this '
                              'whenever the user asks to be reminded or to do something '
                              'periodically; do not tell them to type a slash command. The prompt '
                              'runs unattended at matching local times and its result is delivered '
                              'back to the current chat automatically. Jobs are read-only unless '
                              'the user explicitly authorized side effects.',
               'parameters': {'type': 'object',
                              'properties': {'cron': {'type': 'string',
                                                      'description': '5-field cron expression, '
                                                                     "e.g. '*/5 * * * *' (minute "
                                                                     'hour day-of-month month '
                                                                     'day-of-week)'},
                                             'prompt': {'type': 'string',
                                                        'description': 'Instruction injected when '
                                                                       'the job fires'},
                                             'recurring': {'type': 'boolean',
                                                           'description': 'Whether the job '
                                                                          'repeats every matching '
                                                                          'time (default true)'},
                                             'durable': {'type': 'boolean',
                                                         'description': 'Whether the job survives '
                                                                        'restarts (default '
                                                                        'true)'},
                                             'idle_after_minutes': {
                                                 'type': 'integer',
                                                 'minimum': 0,
                                                 'maximum': 1440,
                                                 'description': 'Only run once the machine has '
                                                                'been idle for this many minutes '
                                                                '(0 = run at the scheduled time). '
                                                                'If the trigger time is busy, it '
                                                                'waits up to 12 hours, then skips '
                                                                'that occurrence.'}},
                              'required': ['cron', 'prompt']}}},
 {'type': 'function',
  'function': {'name': 'list_crons',
               'description': 'List scheduled jobs with their schedule, delivery target and '
                              'read-only/writable mode. Use when the user asks what reminders '
                              'exist.',
               'parameters': {'type': 'object', 'properties': {}}}},
 {'type': 'function',
  'function': {'name': 'cancel_cron',
               'description': 'Cancel a scheduled job by id. Use when the user asks to stop or '
                              'remove a reminder.',
               'parameters': {'type': 'object',
                              'properties': {'job_id': {'type': 'string',
                                                        'description': 'Cron job id, e.g. '
                                                                       'cron_123456'}},
                              'required': ['job_id']}}},
 {'type': 'function',
  'function': {'name': 'find_sessions',
               'description': 'Search past conversations by keyword and return matching sessions '
                              'with a short snippet. Use when the user refers to something '
                              'discussed earlier or asks to find an old conversation.',
               'parameters': {'type': 'object',
                              'properties': {'query': {'type': 'string',
                                                       'description': 'Keyword or phrase to '
                                                                      'search for'},
                                             'limit': {'type': 'integer',
                                                       'description': 'Maximum sessions to '
                                                                      'return (default 5, max 20)'}},
                              'required': ['query']}}},
 {'type': 'function',
  'function': {'name': 'rename_session',
               'description': 'Rename the current session. Use when the user asks to change the '
                              'title of this conversation; user-set titles are not overwritten by '
                              'automatic titling.',
               'parameters': {'type': 'object',
                              'properties': {'title': {'type': 'string',
                                                       'description': 'New session title '
                                                                      '(max 80 chars)'}},
                              'required': ['title']}}},
 {'type': 'function',
  'function': {'name': 'revoke_permissions',
               'description': 'Revoke all permission grants remembered for the current session, '
                              'including directory write grants this session created. '
                              'Use when the user asks to undo earlier approvals. This removes '
                              'approvals only; changes already made are not undone.',
               'parameters': {'type': 'object', 'properties': {}}}},
 {'type': 'function',
  'function': {'name': 'spawn_teammate',
               'description': 'Spawn a teammate agent in a background thread to work on a '
                              'sub-task.',
               'parameters': {'type': 'object',
                              'properties': {'name': {'type': 'string',
                                                      'description': 'Unique teammate name'},
                                             'role': {'type': 'string',
                                                      'description': "Role, e.g. 'researcher'"},
                                             'prompt': {'type': 'string',
                                                        'description': 'Task instructions for the '
                                                                       'teammate'}},
                              'required': ['name', 'role', 'prompt']}}},
 {'type': 'function',
  'function': {'name': 'send_message',
               'description': 'Send a message to a teammate via MessageBus.',
               'parameters': {'type': 'object',
                              'properties': {'to': {'type': 'string',
                                                    'description': 'Recipient teammate name'},
                                             'content': {'type': 'string'}},
                              'required': ['to', 'content']}}},
 {'type': 'function',
  'function': {'name': 'check_inbox',
               'description': "Check Lead's inbox for teammate messages.",
               'parameters': {'type': 'object', 'properties': {}}}},
 {'type': 'function',
  'function': {'name': 'background_output',
               'description': "Read a background task's output. Set wait=true to block until it "
                              'finishes (or the timeout elapses).',
               'parameters': {'type': 'object',
                              'properties': {'bg_id': {'type': 'string',
                                                       'description': 'Background task id, e.g. '
                                                                      "'bg_0001'"},
                                             'wait': {'type': 'boolean',
                                                      'description': 'Block until the task '
                                                                     'reaches a terminal state '
                                                                     '(default false)'},
                                             'timeout': {'type': 'number',
                                                         'description': 'Max seconds to wait when '
                                                                        'wait=true (default 30, '
                                                                        'max 600)'}},
                              'required': ['bg_id']}}},
 {'type': 'function',
  'function': {'name': 'kill_background',
               'description': 'Request cancellation of a running background task and kill its '
                              'process tree.',
               'parameters': {'type': 'object',
                              'properties': {'bg_id': {'type': 'string',
                                                       'description': 'Background task id, e.g. '
                                                                      "'bg_0001'"}},
                              'required': ['bg_id']}}},
 {'type': 'function',
  'function': {'name': 'web_fetch',
               'description': 'Fetch a public http(s) URL and return readable text (HTML is '
                              'converted). Requires the same outbound approval as curl; '
                              'private/loopback addresses and redirects to them are blocked. '
                              'Prefer this over curl for reading pages.',
               'parameters': {'type': 'object',
                              'properties': {'url': {'type': 'string',
                                                     'description': 'Absolute http(s) URL to '
                                                                    'fetch'},
                                             'max_chars': {'type': 'integer',
                                                           'description': 'Maximum characters of '
                                                                          'extracted text to '
                                                                          'return (default 20000, '
                                                                          'max 100000)'}},
                              'required': ['url']}}},
 {'type': 'function',
  'function': {'name': 'apply_patch',
               'description': 'Apply a structured patch to one or more files. Supports '
                              'add/update/delete/move and applies all changes atomically (rolls '
                              'back on failure). Use this instead of multiple edit calls for '
                              'multi-file changes.',
               'parameters': {'type': 'object',
                              'properties': {'patch': {'type': 'string',
                                                       'description': "Patch text framed by '*** "
                                                                      "Begin Patch' and '*** End "
                                                                      "Patch', using '*** Add "
                                                                      "File:', '*** Update "
                                                                      "File:', '*** Delete File:' "
                                                                      "headers and '*** Move to:' "
                                                                      'for renames. Hunk lines '
                                                                      "start with ' ', '+' or "
                                                                      "'-'."},
                                             'cwd': {'type': 'string',
                                                     'description': 'Optional base directory for '
                                                                    'relative paths (default: '
                                                                    'workspace)'}},
                              'required': ['patch']}}},
 {'type': 'function',
  'function': {'name': 'checkpoint',
               'description': 'Create a rewindable checkpoint of the current conversation and '
                              'workspace. Call it before risky multi-file refactors so the user '
                              'can rewind with /rewind. Only the user can trigger a rewind.',
               'parameters': {'type': 'object',
                              'properties': {'label': {'type': 'string',
                                                       'description': 'Short label describing the '
                                                                      "safe point, e.g. 'before "
                                                                      "refactor'"}},
                              'required': []}}},
 {'type': 'function',
  'function': {'name': 'task',
               'description': 'Spawn a subagent to complete a task',
               'parameters': {'type': 'object',
                              'properties': {'description': {'type': 'string',
                                                             'description': 'The task '
                                                                            'description'}},
                              'required': ['description']}}},
 {'type': 'function',
  'function': {'name': 'load_skill',
               'description': "Load a registered skill's instructions by its name. When the "
                              "system prompt lists skills (e.g. 'file-summarizer'), call this "
                              'tool with that exact name to get the skill body instead of '
                              'searching the filesystem manually.',
               'parameters': {'type': 'object',
                              'properties': {'name': {'type': 'string',
                                                      'description': 'The exact skill name shown '
                                                                     'in the Skills available '
                                                                     'list, e.g. '
                                                                     "'file-summarizer'"}},
                              'required': ['name']}}},
 {'type': 'function',
  'function': {'name': 'compact',
               'description': 'Summarize the conversation history to free up context. Call this '
                              'when the conversation is getting too long or you want to clear old '
                              'tool outputs.',
               'parameters': {'type': 'object', 'properties': {}}}},
 {'type': 'function',
  'function': {'name': 'request_shutdown',
               'description': 'Ask a teammate agent to shut down gracefully.',
               'parameters': {'type': 'object',
                              'properties': {'teammate': {'type': 'string',
                                                          'description': 'Teammate name to shut '
                                                                         'down'}},
                              'required': ['teammate']}}},
 {'type': 'function',
  'function': {'name': 'request_plan',
               'description': 'Ask a teammate to submit a plan for a task.',
               'parameters': {'type': 'object',
                              'properties': {'teammate': {'type': 'string',
                                                          'description': 'Teammate name'},
                                             'task': {'type': 'string',
                                                      'description': 'The task to plan for'}},
                              'required': ['teammate', 'task']}}},
 {'type': 'function',
  'function': {'name': 'review_plan',
               'description': "Approve or reject a teammate's submitted plan (by request_id).",
               'parameters': {'type': 'object',
                              'properties': {'request_id': {'type': 'string',
                                                            'description': 'Plan request id from '
                                                                           'check_inbox'},
                                             'approve': {'type': 'boolean',
                                                         'description': 'Whether to approve the '
                                                                        'plan'},
                                             'feedback': {'type': 'string',
                                                          'description': 'Optional feedback when '
                                                                         'rejecting'}},
                              'required': ['request_id', 'approve']}}},
 {'type': 'function',
  'function': {'name': 'create_worktree',
               'description': 'Create a git worktree (isolated branch) for a task; optionally '
                              'bind it to task_id.',
               'parameters': {'type': 'object',
                              'properties': {'name': {'type': 'string',
                                                      'description': 'Worktree name '
                                                                     '(letters/digits/._-, 1-64 '
                                                                     'chars)'},
                                             'task_id': {'type': 'string',
                                                         'description': 'Optional task id to bind '
                                                                        'to this worktree'}},
                              'required': ['name']}}},
 {'type': 'function',
  'function': {'name': 'remove_worktree',
               'description': 'Remove a worktree; refuses if it has uncommitted/unpushed changes '
                              'unless discard_changes=true.',
               'parameters': {'type': 'object',
                              'properties': {'name': {'type': 'string'},
                                             'discard_changes': {'type': 'boolean',
                                                                 'description': 'Force removal, '
                                                                                'discarding '
                                                                                'changes'}},
                              'required': ['name']}}},
 {'type': 'function',
  'function': {'name': 'keep_worktree',
               'description': 'Keep a worktree for review (logs the event).',
               'parameters': {'type': 'object',
                              'properties': {'name': {'type': 'string'}},
                              'required': ['name']}}},
 {'type': 'function',
  'function': {'name': 'screen_capture',
               'description': 'Capture the local screen to a PNG and attach it for '
                              'inspection (requires approval). Use screen_ocr for text.',
               'parameters': {'type': 'object',
                              'properties': {
                                  'screen': {'type': 'string',
                                             'enum': ['primary', 'all'],
                                             'description': "Screen to capture: 'primary' "
                                                            "(default) or 'all' monitors"},
                                  'region': {'type': 'string',
                                             'description': "Optional crop 'x,y,w,h' "
                                                            'relative to the captured screen'},
                                  'delay_ms': {'type': 'number',
                                               'description': 'Wait 0-5000 ms before '
                                                              'capturing'}},
                              'required': []}}},
 {'type': 'function',
  'function': {'name': 'screen_ocr',
               'description': 'Extract text from a screenshot with the local Windows OCR '
                              'engine (requires approval).',
               'parameters': {'type': 'object',
                              'properties': {
                                  'path': {'type': 'string',
                                           'description': 'Screenshot path from '
                                                          'screen_capture; defaults to the '
                                                          'latest screenshot'},
                                  'lang': {'type': 'string',
                                           'description': "OCR language tag (default "
                                                          "zh-Hans-CN; 'auto' follows the "
                                                          'system languages)'},
                                  'region': {'type': 'string',
                                             'description': "Optional crop 'x,y,w,h' in "
                                                            'image pixels'}},
                              'required': []}}},
 {'type': 'function',
  'function': {'name': 'clipboard_read',
               'description': 'Read the current local clipboard text (requires approval).',
               'parameters': {'type': 'object',
                              'properties': {},
                              'required': []}}},
 {'type': 'function',
  'function': {'name': 'clipboard_write',
               'description': 'Replace the local clipboard text (requires approval; '
                              'overwrites what the user copied).',
               'parameters': {'type': 'object',
                              'properties': {'text': {'type': 'string',
                                                      'description': 'Text to put on the '
                                                                     'local clipboard'}},
                              'required': ['text']}}},
 {'type': 'function',
  'function': {'name': 'desktop_snapshot',
               'description': 'Inspect the foreground window and focused UI control. '
                              'Requires HARNESS_DESKTOP_CONTROL=1 and explicit approval '
                              'each time. Use pid and window_id for a following action.',
               'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
 {'type': 'function',
  'function': {'name': 'desktop_windows',
               'description': 'List visible top-level windows with pid, window_id and '
                              'title. Read-only; use it to discover a target instead of '
                              'asking the user to switch windows manually.',
               'parameters': {'type': 'object',
                              'properties': {
                                  'limit': {'type': 'integer',
                                            'description': 'Max windows to return (1-100, default 20)'}},
                              'required': []}}},
 {'type': 'function',
  'function': {'name': 'desktop_action',
               'description': 'Perform approved mouse or keyboard input in the '
                              'foreground desktop: either one action, or a '
                              'deliberate sequence of steps (action=sequence) '
                              'executed left to right under a single approval — '
                              'use it to combine click, typing, and keys into one '
                              'plan. Requires HARNESS_DESKTOP_CONTROL=1 and '
                              'explicit approval every time. First call '
                              'desktop_snapshot; expected_pid and expected_window_id '
                              'must match that window. Every step re-checks the '
                              'foreground window; the run stops at the first change '
                              'or sensitive window. Sensitive windows are blocked.',
               'parameters': {'type': 'object',
                              'properties': {
                                  'action': {'type': 'string',
                                             'enum': ['move', 'click', 'type', 'key',
                                                      'scroll', 'sequence']},
                                  'expected_pid': {'type': 'integer',
                                                   'description': 'PID from desktop_snapshot'},
                                  'expected_window_id': {'type': 'string',
                                                         'description': 'Hexadecimal window id'},
                                  'x': {'type': 'integer',
                                        'description': 'Pixel coordinate inside the target window'},
                                  'y': {'type': 'integer',
                                        'description': 'Pixel coordinate inside the target window'},
                                  'text': {'type': 'string',
                                           'description': 'Text to type, max 1000 characters'},
                                  'key': {'type': 'string',
                                          'description': 'Key name (enter, tab, escape, F1) '
                                                         'or a combo joined with + such as '
                                                         'ctrl+v, ctrl+shift+s, alt+f4'},
                                  'amount': {'type': 'integer',
                                             'description': 'Scroll notches from -20 to 20, excluding 0'},
                                  'button': {'type': 'string',
                                             'enum': ['left', 'right', 'middle'],
                                             'description': 'Mouse button for click (default left)'},
                                  'count': {'type': 'integer', 'enum': [1, 2],
                                            'description': 'click count: 2 is a double-click'},
                                  'steps': {'type': 'array', 'maxItems': 12,
                                            'description': 'For action=sequence: 1-12 '
                                                           'ordered steps, each an object '
                                                           'with action (move/click/type/key/'
                                                           'scroll/wait) and that action\'s '
                                                           'parameters (x/y/button/count, text, '
                                                           'key, amount, ms). One approval '
                                                           'covers the whole plan.',
                                            'items': {'type': 'object',
                                                      'properties': {
                                                          'action': {'type': 'string',
                                                                     'enum': ['move', 'click',
                                                                              'type', 'key',
                                                                              'scroll', 'wait']},
                                                          'x': {'type': 'integer'},
                                                          'y': {'type': 'integer'},
                                                          'text': {'type': 'string'},
                                                          'key': {'type': 'string'},
                                                          'amount': {'type': 'integer'},
                                                          'button': {'type': 'string',
                                                                     'enum': ['left', 'right',
                                                                              'middle']},
                                                          'count': {'type': 'integer',
                                                                    'enum': [1, 2]},
                                                          'ms': {'type': 'integer',
                                                                 'description': 'wait step: '
                                                                                '50-3000 ms'}}}}},
                              'required': ['action', 'expected_pid',
                                           'expected_window_id']}}},
 {'type': 'function',
  'function': {'name': 'delete_paths',
               'description': 'Move files or directories to the Recycle Bin so they '
                              'can be restored. Prefer this over `del`/`rm` in bash: '
                              'those are unrecoverable. Paths must already exist.',
               'parameters': {'type': 'object',
                              'properties': {
                                  'paths': {'type': 'array',
                                            'items': {'type': 'string'},
                                            'maxItems': 50,
                                            'description': '1-50 existing paths to move to '
                                                           'the recycle bin'}},
                              'required': ['paths']}}},
 {'type': 'function',
  'function': {'name': 'remind',
               'description': 'Set a one-shot reminder at a natural-language time. '
                              'Prefer this over schedule_cron for "remind me to X at '
                              'Y": you do not need to compute a cron expression, and '
                              'the user sees a readable time back instead of a cron '
                              'string. Fires once, then removes itself.',
               'parameters': {'type': 'object',
                              'properties': {
                                  'text': {'type': 'string',
                                           'description': 'What to remind the user '
                                                          'about, in their own words'},
                                  'when': {'type': 'string',
                                           'description': 'When, in natural language: '
                                                          '"2026-11-04 15:00", "15:00", '
                                                          '"in 2h", "tomorrow 9am", '
                                                          '"明天下午3点"'}},
                              'required': ['text', 'when']}}},
 {'type': 'function',
  'function': {'name': 'move_paths',
               'description': 'Move or rename files and directories. Dry-run by '
                              'default: the first call returns a plan and changes '
                              'nothing. Call again with dry_run=false to perform '
                              'it. Every batch can be undone with undo_moves. '
                              'Validates the whole batch first — one bad item '
                              'rejects everything, so you never get a half-done '
                              'move. Prefer this over `mv`/`move`/`ren` in bash, '
                              'which cannot be undone.',
               'parameters': {'type': 'object',
                              'properties': {
                                  'moves': {'type': 'array',
                                            'maxItems': 50,
                                            'description': '1-50 {from, to} pairs; '
                                                           'a relative target is '
                                                           'resolved against the '
                                                           'source directory',
                                            'items': {'type': 'object',
                                                      'properties': {
                                                          'from': {'type': 'string'},
                                                          'to': {'type': 'string'}},
                                                      'required': ['from', 'to']}},
                                  'dry_run': {'type': 'boolean',
                                              'description': 'true (default) only '
                                                             'returns the plan'}},
                              'required': ['moves']}}},
 {'type': 'function',
  'function': {'name': 'undo_moves',
               'description': 'Undo a batch of moves made by move_paths. Defaults '
                              'to the most recent batch. Never overwrites: an item '
                              'is skipped if its original location is occupied.',
               'parameters': {'type': 'object',
                              'properties': {
                                  'batch_id': {'type': 'string',
                                               'description': 'Batch id from '
                                                              'move_paths; omit for '
                                                              'the latest batch'}},
                              'required': []}}},
 {'type': 'function',
  'function': {'name': 'remember',
               'description': 'Save one long-term memory that will be recalled in later '
                              'sessions. Use when the user says to remember something, '
                              'corrects you, or states a stable preference. Do NOT use '
                              'for secrets, passwords, or one-off details. Prefer this '
                              'over writing files into the memories directory.',
               'parameters': {'type': 'object',
                              'properties': {
                                  'text': {'type': 'string',
                                           'description': 'The memory itself, in one or a '
                                                          'few sentences'},
                                  'type': {'type': 'string',
                                           'enum': ['preference', 'correction', 'fact',
                                                    'context'],
                                           'description': 'preference = stable personal '
                                                          'preference; correction = a '
                                                          'correction the user made; fact = '
                                                          'something that took effort to '
                                                          'establish; context = background '
                                                          'about a project or environment'},
                                  'name': {'type': 'string',
                                           'description': 'Short stable name; derived from '
                                                          'text when omitted'},
                                  'description': {'type': 'string',
                                                  'description': 'One-line summary used '
                                                                 'in the memory index'}},
                              'required': ['text']}}},
 {'type': 'function',
  'function': {'name': 'forget',
               'description': 'Delete one saved memory permanently. Requires user '
                              'approval. Use recall first to find the exact name.',
               'parameters': {'type': 'object',
                              'properties': {
                                  'name': {'type': 'string',
                                           'description': 'Name or file name of the memory '
                                                          'to delete'}},
                              'required': ['name']}}},
 {'type': 'function',
  'function': {'name': 'recall',
               'description': 'Search saved memories by keyword. An empty query lists '
                              'every memory with its type and description.',
               'parameters': {'type': 'object',
                              'properties': {
                                  'query': {'type': 'string',
                                            'description': 'Keyword to look for; empty lists '
                                                           'all memories'},
                                  'limit': {'type': 'integer',
                                            'minimum': 1,
                                            'maximum': 50,
                                            'description': 'Maximum results (default 10)'}},
                              'required': []}}}]
