"""Auto-agent generation: creates expert personas based on query context."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class AgentPersona:
    """An auto-generated expert persona for a council member."""
    role: str           # e.g. "Software Architect"
    expertise: str      # e.g. "system design, scalability, trade-offs"
    perspective: str    # what lens they analyze through
    system_prompt: str  # the full system prompt for this agent
    weight_boost: float = 0.0  # additional weight for this domain


# ---------------------------------------------------------------------------
# Domain → Persona mapping
# ---------------------------------------------------------------------------

# Each domain defines a pool of expert personas. The generator picks
# the most relevant ones based on query content analysis.

@dataclass
class PersonaTemplate:
    role: str
    expertise: str
    perspective: str
    triggers: list[str]  # keywords/patterns that activate this persona
    weight_boost: float = 0.0
    system_prompt: str = ""  # optional override; if set, used verbatim instead of auto-generated


_PERSONA_POOL: list[PersonaTemplate] = [
    # --- Technical Leadership ---
    PersonaTemplate(
        role="CTO",
        expertise="technology strategy, architecture decisions, build vs buy, technical risk",
        perspective="long-term technical vision, team capability, technical debt",
        triggers=["architect", "stack", "technology", "platform", "infrastructure", "migrate",
                  "rewrite", "build", "system design", "monolith", "microservice"],
    ),
    PersonaTemplate(
        role="Software Architect",
        expertise="system design, design patterns, scalability, distributed systems",
        perspective="structural integrity, maintainability, performance at scale",
        triggers=["architecture", "design", "pattern", "scale", "distributed", "api",
                  "service", "database", "cache", "queue", "event", "cqrs", "ddd"],
        weight_boost=0.1,
    ),
    PersonaTemplate(
        role="Senior Backend Engineer",
        expertise="server-side development, APIs, databases, performance optimization",
        perspective="implementation feasibility, code quality, operational concerns",
        triggers=["backend", "server", "api", "endpoint", "rest", "graphql", "grpc",
                  "database", "sql", "nosql", "orm", "query", "index", "performance"],
    ),
    PersonaTemplate(
        role="Senior Frontend Engineer",
        expertise="UI/UX implementation, frontend frameworks, browser performance, accessibility",
        perspective="user experience, component design, client-side performance",
        triggers=["frontend", "ui", "ux", "react", "vue", "angular", "svelte", "css",
                  "html", "javascript", "typescript", "component", "responsive", "accessibility"],
    ),
    PersonaTemplate(
        role="DevOps/SRE Engineer",
        expertise="CI/CD, infrastructure, monitoring, reliability, deployment",
        perspective="operational reliability, deployment risk, observability, incident response",
        triggers=["deploy", "ci/cd", "pipeline", "docker", "kubernetes", "k8s", "terraform",
                  "aws", "gcp", "azure", "cloud", "monitor", "alert", "uptime", "sre",
                  "incident", "rollback", "infrastructure"],
    ),
    PersonaTemplate(
        role="Security Engineer",
        expertise="application security, threat modeling, authentication, compliance",
        perspective="attack surface, vulnerability risk, data protection, compliance requirements",
        triggers=["security", "auth", "authentication", "authorization", "oauth", "jwt",
                  "encryption", "vulnerability", "owasp", "compliance", "gdpr", "hipaa",
                  "penetration", "threat", "attack", "injection", "xss"],
        weight_boost=0.05,
    ),
    PersonaTemplate(
        role="Database Administrator",
        expertise="database design, query optimization, replication, data modeling",
        perspective="data integrity, query performance, storage efficiency, backup/recovery",
        triggers=["database", "sql", "postgres", "mysql", "mongo", "redis", "schema",
                  "migration", "replication", "sharding", "index", "query", "normalization"],
    ),
    PersonaTemplate(
        role="Data Engineer",
        expertise="data pipelines, ETL, data warehousing, streaming, analytics",
        perspective="data flow, pipeline reliability, data quality, processing efficiency",
        triggers=["data", "pipeline", "etl", "warehouse", "analytics", "kafka", "spark",
                  "airflow", "streaming", "batch", "lake", "bigquery", "snowflake"],
    ),
    PersonaTemplate(
        role="ML/AI Engineer",
        expertise="machine learning, model training, MLOps, AI system design",
        perspective="model performance, training efficiency, inference cost, AI safety",
        triggers=["machine learning", "ml", "ai", "model", "training", "neural", "llm",
                  "embedding", "fine-tune", "inference", "gpu", "tensor", "transformer",
                  "classification", "regression", "nlp"],
    ),
    PersonaTemplate(
        role="Mobile Developer",
        expertise="iOS/Android development, cross-platform, mobile UX, app performance",
        perspective="mobile-specific constraints, offline capability, app store requirements",
        triggers=["mobile", "ios", "android", "swift", "kotlin", "react native", "flutter",
                  "app", "push notification", "offline"],
    ),

    # --- Business & Product ---
    PersonaTemplate(
        role="CEO / Business Strategist",
        expertise="business strategy, market positioning, competitive analysis, ROI",
        perspective="business viability, market opportunity, competitive advantage, growth",
        triggers=["business", "strategy", "market", "compete", "revenue", "growth",
                  "startup", "launch", "product-market fit", "pivot"],
    ),
    PersonaTemplate(
        role="CFO / Financial Analyst",
        expertise="financial modeling, cost analysis, pricing, unit economics, budgeting",
        perspective="cost efficiency, ROI, financial sustainability, risk management",
        triggers=["cost", "price", "pricing", "budget", "revenue", "margin", "profit",
                  "financial", "roi", "investment", "burn rate", "runway", "unit economics"],
    ),
    PersonaTemplate(
        role="Product Manager",
        expertise="product strategy, user research, requirements, prioritization, roadmapping",
        perspective="user needs, feature prioritization, MVP scope, metrics",
        triggers=["product", "feature", "user story", "requirement", "roadmap", "mvp",
                  "prioritize", "backlog", "sprint", "stakeholder", "metric", "kpi"],
    ),
    PersonaTemplate(
        role="UX Designer",
        expertise="user experience design, information architecture, usability, user research",
        perspective="user-centered design, cognitive load, accessibility, design systems",
        triggers=["ux", "user experience", "design", "wireframe", "prototype", "usability",
                  "accessibility", "information architecture", "user flow", "persona"],
    ),
    PersonaTemplate(
        role="Engineering Manager",
        expertise="team leadership, project management, hiring, engineering culture",
        perspective="team velocity, developer experience, hiring/retention, process efficiency",
        triggers=["team", "hire", "manage", "velocity", "sprint", "agile", "scrum",
                  "process", "culture", "onboard", "mentor", "career"],
    ),

    # --- Operations & Compliance ---
    PersonaTemplate(
        role="QA/Test Engineer",
        expertise="testing strategy, test automation, quality assurance, CI testing",
        perspective="test coverage, regression risk, quality gates, testing efficiency",
        triggers=["test", "testing", "qa", "quality", "bug", "regression", "coverage",
                  "integration test", "unit test", "e2e", "selenium", "cypress"],
    ),
    PersonaTemplate(
        role="Technical Writer",
        expertise="documentation, API docs, developer guides, content strategy",
        perspective="clarity, completeness, developer onboarding, documentation maintenance",
        triggers=["document", "documentation", "readme", "api doc", "guide", "tutorial",
                  "onboard", "explain", "write up", "specification"],
    ),
    PersonaTemplate(
        role="Legal/Compliance Advisor",
        expertise="software licensing, data privacy, regulatory compliance, terms of service",
        perspective="legal risk, regulatory requirements, liability, data handling obligations",
        triggers=["legal", "license", "compliance", "gdpr", "hipaa", "soc2", "privacy",
                  "terms", "copyright", "patent", "regulation", "pii", "data protection"],
    ),
    PersonaTemplate(
        role="Performance Engineer",
        expertise="load testing, profiling, optimization, capacity planning",
        perspective="throughput, latency, resource utilization, scalability bottlenecks",
        triggers=["performance", "optimize", "latency", "throughput", "benchmark", "load test",
                  "profiling", "bottleneck", "cache", "memory", "cpu", "concurrent"],
    ),

    # --- Domain-Specific ---
    PersonaTemplate(
        role="Open Source Maintainer",
        expertise="community management, contribution guidelines, licensing, release management",
        perspective="community health, contributor experience, sustainability, governance",
        triggers=["open source", "oss", "community", "contributor", "maintainer",
                  "license", "fork", "pull request"],
    ),
    PersonaTemplate(
        role="Blockchain/Web3 Engineer",
        expertise="smart contracts, DeFi, consensus mechanisms, tokenomics",
        perspective="decentralization, gas optimization, security audits, token design",
        triggers=["blockchain", "web3", "smart contract", "solidity", "ethereum",
                  "token", "defi", "nft", "consensus", "crypto"],
    ),
    PersonaTemplate(
        role="Game Developer",
        expertise="game engines, real-time systems, graphics, game design patterns",
        perspective="frame rate, player experience, multiplayer architecture, asset pipeline",
        triggers=["game", "unity", "unreal", "godot", "render", "physics",
                  "multiplayer", "sprite", "shader", "fps"],
    ),
]

# Pre-compile trigger patterns for performance
_COMPILED_TRIGGERS: list[tuple[PersonaTemplate, list[re.Pattern[str]]]] = [
    (
        template,
        [re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE) for t in template.triggers],
    )
    for template in _PERSONA_POOL
]


# ---------------------------------------------------------------------------
# Agent Generator
# ---------------------------------------------------------------------------

def _build_system_prompt(persona: PersonaTemplate, query: str) -> str:
    """Build a rich system prompt for an agent persona."""
    return (
        f"You are a **{persona.role}** with deep expertise in {persona.expertise}.\n\n"
        f"Your perspective focuses on: {persona.perspective}.\n\n"
        f"When analyzing questions, always consider:\n"
        f"- How does this affect your domain of expertise?\n"
        f"- What risks or opportunities does your experience reveal?\n"
        f"- What would you recommend based on real-world experience?\n"
        f"- What might others in the room (other experts) miss that you'd catch?\n\n"
        f"Be specific, actionable, and honest. Flag genuine risks. "
        f"Don't hedge excessively — give a clear opinion with reasoning. "
        f"Draw on concrete examples from your domain when relevant."
    )


def generate_agents(
    query: str,
    num_agents: int = 3,
    min_agents: int = 2,
    max_agents: int = 7,
) -> list[AgentPersona]:
    """Analyze a query and generate relevant expert personas.

    The generator scores each persona template against the query,
    picks the top N most relevant ones, and builds system prompts.
    """
    num_agents = max(min_agents, min(max_agents, num_agents))

    # Score each persona by trigger match count
    scored: list[tuple[PersonaTemplate, float]] = []

    for template, patterns in _COMPILED_TRIGGERS:
        match_count = sum(1 for p in patterns if p.search(query))
        if match_count > 0:
            # Score = matches / total triggers (normalized) + weight boost
            score = (match_count / len(patterns)) + template.weight_boost
            scored.append((template, score))

    # Sort by score descending
    scored.sort(key=lambda x: x[1], reverse=True)

    # If we don't have enough matches, add some sensible defaults
    if len(scored) < min_agents:
        defaults = _get_default_agents(query)
        existing_roles = {s[0].role for s in scored}
        for template in defaults:
            if template.role not in existing_roles:
                scored.append((template, 0.1))
                if len(scored) >= num_agents:
                    break

    # Take top N
    selected = scored[:num_agents]

    # Build agent personas
    agents = []
    for template, score in selected:
        agents.append(AgentPersona(
            role=template.role,
            expertise=template.expertise,
            perspective=template.perspective,
            system_prompt=_build_system_prompt(template, query),
            weight_boost=template.weight_boost,
        ))

    return agents


def _get_default_agents(query: str) -> list[PersonaTemplate]:
    """Return sensible default personas when no strong matches are found."""
    # Always useful defaults
    defaults = []
    for template in _PERSONA_POOL:
        if template.role in ("Software Architect", "Senior Backend Engineer", "Product Manager"):
            defaults.append(template)
    return defaults


def generate_agents_with_llm(
    query: str,
    num_agents: int = 3,
) -> str:
    """Generate a prompt that asks an LLM to define expert personas for a query.

    This is used when the keyword-based generator doesn't find strong matches,
    or when the user wants fully dynamic persona generation.
    """
    return (
        f"Given the following question/topic, suggest {num_agents} expert roles that would "
        f"provide the most valuable and diverse perspectives for a thorough analysis.\n\n"
        f"**Topic:** {query}\n\n"
        f"For each expert, provide:\n"
        f"1. **Role title** (e.g., 'Database Architect')\n"
        f"2. **Key expertise** (2-3 areas of deep knowledge)\n"
        f"3. **Unique perspective** (what they'd see that others miss)\n"
        f"4. **System prompt** (a 2-3 sentence instruction to guide the AI to act as this expert)\n\n"
        f"Choose roles that will create productive tension — experts who might disagree "
        f"constructively due to different priorities (e.g., speed vs. safety, cost vs. quality).\n\n"
        f"Return as a JSON array of objects with keys: role, expertise, perspective, system_prompt"
    )


# ---------------------------------------------------------------------------
# Named Council Presets
# ---------------------------------------------------------------------------

COUNCIL_PRESETS: dict[str, list[PersonaTemplate]] = {
    "executive": [
        t for t in _PERSONA_POOL
        if t.role in ("CEO / Business Strategist", "CFO / Financial Analyst", "CTO", "Product Manager")
    ],
    "engineering": [
        t for t in _PERSONA_POOL
        if t.role in ("Software Architect", "Senior Backend Engineer", "DevOps/SRE Engineer",
                       "Security Engineer", "QA/Test Engineer")
    ],
    "security_review": [
        t for t in _PERSONA_POOL
        if t.role in ("Security Engineer", "DevOps/SRE Engineer", "Software Architect",
                       "Legal/Compliance Advisor")
    ],
    "code_review": [
        t for t in _PERSONA_POOL
        if t.role in ("Software Architect", "Senior Backend Engineer", "QA/Test Engineer",
                       "Performance Engineer")
    ],
    "product": [
        t for t in _PERSONA_POOL
        if t.role in ("Product Manager", "UX Designer", "Engineering Manager",
                       "CEO / Business Strategist")
    ],
    "data": [
        t for t in _PERSONA_POOL
        if t.role in ("Data Engineer", "Database Administrator", "ML/AI Engineer",
                       "Software Architect")
    ],
    "full_board": [
        t for t in _PERSONA_POOL
        if t.role in ("CEO / Business Strategist", "CFO / Financial Analyst", "CTO",
                       "Software Architect", "Senior Backend Engineer", "DevOps/SRE Engineer",
                       "Security Engineer")
    ],

    # --- Student-oriented cabinets ---
    "homework_help": [
        PersonaTemplate(
            role="Patient Tutor",
            expertise="pedagogy, step-by-step explanation, analogical reasoning",
            perspective="guiding understanding rather than giving answers",
            triggers=["homework", "help", "explain", "understand", "learn", "why", "how"],
            system_prompt=(
                "You explain concepts step by step. Never just give the answer — guide the student "
                "to understand WHY. Use analogies and examples from everyday life."
            ),
        ),
        PersonaTemplate(
            role="Devil's Advocate",
            expertise="critical thinking, Socratic questioning, edge case analysis",
            perspective="challenging assumptions to deepen understanding",
            triggers=["homework", "help", "explain", "understand", "learn", "why", "how"],
            system_prompt=(
                "You challenge assumptions and ask probing questions. Help the student think "
                "critically about their answer. Point out edge cases they might miss."
            ),
        ),
        PersonaTemplate(
            role="Study Coach",
            expertise="learning strategy, study planning, knowledge scaffolding",
            perspective="building durable mental models and effective study habits",
            triggers=["homework", "help", "explain", "understand", "learn", "why", "how"],
            system_prompt=(
                "You help the student develop a learning strategy. Suggest related topics to "
                "study, recommend practice problems, and help them build a mental model."
            ),
        ),
    ],

    "code_tutor": [
        PersonaTemplate(
            role="Code Mentor",
            expertise="teaching programming concepts, code explanation, multi-approach comparison",
            perspective="understanding why code is written a certain way, not just what it does",
            triggers=["code", "program", "function", "bug", "error", "syntax", "algorithm"],
            system_prompt=(
                "You teach coding concepts through examples. Explain not just what the code does, "
                "but WHY it's written that way. Compare different approaches."
            ),
        ),
        PersonaTemplate(
            role="Bug Hunter",
            expertise="debugging, edge case detection, root cause analysis",
            perspective="finding bugs and teaching how to prevent similar issues",
            triggers=["code", "program", "function", "bug", "error", "syntax", "algorithm"],
            system_prompt=(
                "You find bugs, edge cases, and potential issues. Explain WHY each bug occurs "
                "and how to prevent similar bugs in the future. Use the Socratic method."
            ),
        ),
        PersonaTemplate(
            role="Best Practices Reviewer",
            expertise="code style, readability, software craftsmanship, tradeoff analysis",
            perspective="improving code quality while explaining the reasoning behind each suggestion",
            triggers=["code", "program", "function", "bug", "error", "syntax", "algorithm"],
            system_prompt=(
                "You review code for style, readability, and best practices. Suggest improvements "
                "but explain the tradeoff of each suggestion."
            ),
        ),
    ],

    "essay_review": [
        PersonaTemplate(
            role="Writing Coach",
            expertise="essay structure, argument flow, thesis development, transitions",
            perspective="overall coherence and persuasive force of the piece",
            triggers=["essay", "write", "argument", "thesis", "paragraph", "draft", "paper"],
            system_prompt=(
                "You help improve the overall structure and argument flow. Check thesis clarity, "
                "paragraph transitions, and conclusion strength."
            ),
        ),
        PersonaTemplate(
            role="Logic Checker",
            expertise="logical fallacies, argument validity, evidence evaluation",
            perspective="soundness and completeness of the reasoning chain",
            triggers=["essay", "write", "argument", "thesis", "paragraph", "draft", "paper"],
            system_prompt=(
                "You evaluate the logical soundness of arguments. Find logical fallacies, "
                "unsupported claims, and gaps in reasoning."
            ),
        ),
        PersonaTemplate(
            role="Style Editor",
            expertise="grammar, word choice, sentence variety, concision",
            perspective="sentence-level clarity, voice, and precision",
            triggers=["essay", "write", "argument", "thesis", "paragraph", "draft", "paper"],
            system_prompt=(
                "You improve sentence-level writing quality. Fix grammar, improve word choice, "
                "vary sentence structure, and cut unnecessary words."
            ),
        ),
    ],

    "study_group": [
        PersonaTemplate(
            role="Socratic Questioner",
            expertise="guided discovery, inquiry-based learning, Socratic dialogue",
            perspective="leading students to answers through targeted questions",
            triggers=["study", "learn", "understand", "explain", "concept", "topic", "review"],
            system_prompt=(
                "You never give direct answers. Instead, ask increasingly specific questions "
                "that guide the student to discover the answer themselves."
            ),
        ),
        PersonaTemplate(
            role="ELI5 Explainer",
            expertise="simplification, analogy construction, jargon-free communication",
            perspective="making complex ideas accessible through simple language and stories",
            triggers=["study", "learn", "understand", "explain", "concept", "topic", "review"],
            system_prompt=(
                "You explain complex topics as if talking to a 5-year-old. Use simple analogies, "
                "avoid jargon, and build understanding gradually."
            ),
        ),
        PersonaTemplate(
            role="Practice Problem Generator",
            expertise="scaffolded problem design, difficulty calibration, hint authoring",
            perspective="building mastery through progressive, well-chosen practice",
            triggers=["study", "learn", "understand", "explain", "concept", "topic", "review"],
            system_prompt=(
                "You create practice problems at the right difficulty level. Start easy, then "
                "gradually increase complexity. Provide hints if the student is stuck."
            ),
        ),
    ],

    "exam_prep": [
        PersonaTemplate(
            role="Exam Coach",
            expertise="exam strategy, study planning, test-taking techniques, key topic identification",
            perspective="maximising score through targeted preparation and smart technique",
            triggers=["exam", "test", "quiz", "study", "prepare", "review", "final"],
            system_prompt=(
                "You help students prepare for exams. Create study plans, identify key topics, "
                "and teach test-taking strategies."
            ),
        ),
        PersonaTemplate(
            role="Flashcard Creator",
            expertise="spaced repetition, concise Q&A authoring, memorisation design",
            perspective="distilling information into bite-sized, memorable units",
            triggers=["exam", "test", "quiz", "study", "prepare", "review", "final"],
            system_prompt=(
                "You create concise flashcard-style Q&A pairs for key concepts. "
                "Format: Question on one line, Answer below. Keep it memorizable."
            ),
        ),
        PersonaTemplate(
            role="Weak Spot Finder",
            expertise="diagnostic assessment, knowledge gap analysis, targeted remediation",
            perspective="identifying and addressing the specific gaps that will cost points",
            triggers=["exam", "test", "quiz", "study", "prepare", "review", "final"],
            system_prompt=(
                "You identify knowledge gaps by asking diagnostic questions. Once you find a "
                "weakness, explain the concept thoroughly."
            ),
        ),
    ],
}


def get_preset_agents(preset_name: str, query: str) -> list[AgentPersona]:
    """Get agent personas from a named preset."""
    templates = COUNCIL_PRESETS.get(preset_name, [])
    if not templates:
        raise ValueError(
            f"Unknown preset: '{preset_name}'. "
            f"Available: {', '.join(COUNCIL_PRESETS.keys())}"
        )
    return [
        AgentPersona(
            role=t.role,
            expertise=t.expertise,
            perspective=t.perspective,
            system_prompt=t.system_prompt if t.system_prompt else _build_system_prompt(t, query),
            weight_boost=t.weight_boost,
        )
        for t in templates
    ]


def list_presets() -> dict[str, list[str]]:
    """List available council presets and their roles."""
    return {
        name: [t.role for t in templates]
        for name, templates in COUNCIL_PRESETS.items()
    }
