# Auto-Agent Council System

When you run `nvh convene`, nvHive analyzes your question and generates a panel of expert personas to debate it. Each agent has a defined role, expertise area, and analytical perspective.

## 12 Cabinets

Pre-configured expert panels:

| Cabinet | Experts |
|---------|---------|
| `executive` | CEO, CFO, CTO, Product Manager |
| `engineering` | Architect, Backend Engineer, DevOps/SRE, Security, QA |
| `security_review` | Security Engineer, DevOps/SRE, Architect, Legal/Compliance |
| `code_review` | Architect, Backend Engineer, QA, Performance Engineer |
| `product` | Product Manager, UX Designer, Engineering Manager, CEO |
| `data` | Data Engineer, DBA, ML/AI Engineer, Architect |
| `full_board` | CEO, CFO, CTO, Architect, Backend, DevOps, Security |
| `homework_help` | Patient Tutor, Devil's Advocate, Study Coach |
| `code_tutor` | Code Mentor, Bug Hunter, Best Practices Reviewer |
| `essay_review` | Writing Coach, Logic Checker, Style Editor |
| `study_group` | Socratic Questioner, ELI5 Explainer, Practice Problem Generator |
| `exam_prep` | Exam Coach, Flashcard Creator, Weak Spot Finder |

## Examples

```bash
nvh convene "Should we migrate to microservices?" --cabinet engineering
nvh convene "Review my essay on climate policy" --cabinet essay_review
```

---

Back to [README](../README.md)
