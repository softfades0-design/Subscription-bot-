---
name: Python dependency environment
description: Replit startup behavior when a Python package is declared but unavailable to the active interpreter
---

The active Replit Python environment may not have a package installed even when the project already declares it in `requirements.txt`. The correct first response is to install the declared package through the environment package manager, not to edit application imports or remove the dependency.

**Why:** The imported bot failed at `from flask import Flask` despite Flask being listed in `requirements.txt`; installing the declared package restored startup without changing application behavior.

**How to apply:** When startup reports `ModuleNotFoundError` for a package already declared by the project, install that exact declared dependency, restart the existing workflow, and then investigate the next log-level blocker.