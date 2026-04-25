---
name: PowerShell project directory
description: Always cd to the project directory before running PowerShell commands
type: feedback
---

Always run PowerShell commands from within the project directory `c:\Users\gilme\Documents\DEV_Claude\recipe-crawler`.

**Why:** User preference — consistent working directory for all tool calls.
**How to apply:** Prefix every PowerShell command with `cd "c:\Users\gilme\Documents\DEV_Claude\recipe-crawler" &&` or `Set-Location` before any command that operates on project files.
