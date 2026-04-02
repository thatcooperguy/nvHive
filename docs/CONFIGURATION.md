# Configuration

Configuration reference for nvHive, including config file management and project-level context injection.

## Config File

Configuration lives at `~/.config/nvhive/config.yaml`. Manage it with:

```bash
nvh config                    # view current config
nvh config set default_advisor groq
nvh config set safe_mode true
nvh budget set --daily 1.00   # daily spending cap
```

## HIVE.md Context Injection

Create a `HIVE.md` file in any project directory. nvHive automatically injects it into the system prompt for every query made from that directory.

```markdown
# HIVE.md
This is a Python 3.12 FastAPI project using SQLAlchemy and PostgreSQL.
Follow Google Python Style Guide. Prefer async/await patterns.
Test with pytest. Deploy target: Ubuntu 22.04 on GKE.
```

Every advisor sees your project context automatically.

---

Back to [README](../README.md)
