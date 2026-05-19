# Security Policy

AutoPipeline runs local benchmark commands, agent backends, and optional probe
commands inside user-provided workspaces. Treat it as a developer tool, not a
hosted sandbox.

## Supported Versions

Security fixes target the default branch until formal releases are published.

## Reporting A Vulnerability

Please report security issues privately to the maintainers instead of opening a
public issue. If you do not have a private contact, open a GitHub security
advisory for the repository.

Include:

- affected command or agent backend
- minimal reproduction steps
- whether arbitrary command execution, path traversal, credential exposure, or
  sandbox escape is involved
- relevant logs with secrets removed

## Local Safety Notes

- Do not run untrusted agent commands with access to secrets.
- Treat `--planner-command`, `--executor-command`, `--reflector-command`,
  command probes, and python probes as trusted-code execution.
- For lower-risk runs, avoid external role commands and set
  `--max-probe-calls 0` on `autopipeline tree run` when active sensing is not
  needed.
- Review generated kernels and scripts before publishing them.
- Keep `workspace/`, logs, profiler traces, and agent outputs out of commits.
- The Codex app-server backend records event and request logs; inspect logs
  before sharing them publicly.
