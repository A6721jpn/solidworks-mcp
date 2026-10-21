# Publication

This document replaces the upstream release instructions in the A6721jpn fork.

This fork is distributed as GitHub source on the `codex-enhancements` branch.
It does not publish a PyPI package or an MCP Registry entry.
The upstream publishing workflow and registry metadata are removed to avoid
claiming the upstream package or registry identity.

Run the existing offline checks before pushing changes:

```powershell
python -m unittest discover -s tests
```

Keep LICENSE, NOTICE, and existing copyright notices.
Mark modified source files and describe the changes in FORK.md.
Exclude logs, CAD files, credentials, local configuration, and benchmark output.
