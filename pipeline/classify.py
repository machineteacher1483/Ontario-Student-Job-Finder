"""
Classification rules: is this an Ontario student role, and which programs
does it map to?

Keyword rules first (per implementation plan step 5). They are transparent,
debuggable and free -- resist reaching for an ML classifier until you can
show these rules failing on real data.
"""

from __future__ import annotations

import re

# --- Ontario geography -------------------------------------------------------
ONTARIO_CITIES = {
    "toronto", "ottawa", "mississauga", "brampton", "hamilton", "london",
    "markham", "vaughan", "kitchener", "waterloo", "cambridge", "guelph",
    "windsor", "oshawa", "burlington", "oakville", "barrie", "sudbury",
    "kingston", "st catharines", "niagara falls", "thunder bay", "peterborough",
    "sarnia", "brantford", "milton", "ajax", "pickering", "whitby", "newmarket",
    "richmond hill", "north york", "scarborough", "etobicoke", "waterloo region",
    "kanata", "north bay", "belleville", "cornwall", "sault ste marie",
    "chatham", "orillia", "stratford", "woodstock", "timmins", "welland",
}

ONTARIO_TOKENS = {"ontario", "on", "ont"}

# Remote roles anchored to Canada are worth keeping -- an Ontario student can
# hold them. Remote roles anchored to another country are not.
REMOTE_TOKENS = {"remote", "work from home", "wfh", "hybrid", "anywhere"}
CANADA_TOKENS = {"canada", "canadian", "ca"}


def parse_location(raw: str) -> tuple[str, str]:
    """
    Turn a free-text location into (city, province).

    Handles the common ATS formats: "Toronto, ON", "Toronto, Ontario, Canada",
    "Remote - Canada", "Ottawa".
    """
    if not raw:
        return "", ""

    cleaned = re.sub(r"\s+", " ", raw.strip())
    parts = [p.strip() for p in re.split(r"[,/|]| - ", cleaned) if p.strip()]

    city = ""
    province = ""

    for part in parts:
        low = part.lower()
        if low in ONTARIO_TOKENS or low == "ontario":
            province = "ON"
        elif low in ONTARIO_CITIES:
            city = part.title()

    if not city and parts:
        first = parts[0]
        if first.lower() not in REMOTE_TOKENS:
            city = first.title()

    return city, province


def is_ontario(raw_location: str) -> bool:
    """True if this location is plausibly reachable for an Ontario student."""
    if not raw_location:
        return False

    low = raw_location.lower()
    tokens = set(re.split(r"[^a-z]+", low)) - {""}

    if "ontario" in low:
        return True
    if any(c in low for c in ONTARIO_CITIES):
        return True
    # ", ON" / ", ON," but not the English word "on"
    if re.search(r",\s*on\b", low):
        return True
    # Remote, but explicitly Canadian
    if tokens & REMOTE_TOKENS and tokens & CANADA_TOKENS:
        return True

    return False


# --- Student-role detection --------------------------------------------------
STUDENT_ROLE_PATTERNS = [
    (r"\bco-?op\b", "co-op"),
    (r"\bintern(ship)?\b", "internship"),
    (r"\bsummer student\b", "summer"),
    (r"\bstudent (role|position|opportunity)\b", "summer"),
    (r"\bnew ?grad(uate)?\b", "new-grad"),
    (r"\bentry[- ]level\b", "new-grad"),
    (r"\bcampus\b", "internship"),
    (r"\bplacement\b", "co-op"),
]

# Roles that merely *mention* students but aren't student roles
STUDENT_ROLE_EXCLUSIONS = [
    r"\bintern(al)? medicine\b",
    r"\bmanager\b",
    r"\bdirector\b",
    r"\bsenior\b",
    r"\bprincipal\b",
    r"\blead\b",
    r"\bstaff engineer\b",
]


def detect_opportunity_type(title: str, description: str = "") -> str | None:
    """
    Return 'internship' / 'co-op' / 'summer' / 'new-grad', or None if this
    does not look like a student role.
    """
    haystack = f"{title} {description}".lower()

    for pattern in STUDENT_ROLE_EXCLUSIONS:
        if re.search(pattern, title.lower()):
            return None

    for pattern, label in STUDENT_ROLE_PATTERNS:
        if re.search(pattern, haystack):
            return label

    return None


# --- Program mapping ---------------------------------------------------------
PROGRAM_RULES: dict[str, list[str]] = {
    "CS/IT/Data": [
        "software", "developer", "engineer", "cloud", "ai", "machine learning",
        "data", "analytics", "cyber", "security", "devops", "qa", "full stack",
        "frontend", "backend", "web", "mobile", "it ", "information technology",
        "database", "network", "python", "java", "sql",
    ],
    "Logistics/Business": [
        "supply chain", "logistics", "procurement", "operations", "business",
        "finance", "accounting", "marketing", "sales", "hr", "human resources",
        "consulting", "analyst", "administration", "project coordinator",
        "warehouse", "inventory",
    ],
    "Psychology/Social Work/Health Sciences": [
        "child", "youth", "support worker", "social", "mental health",
        "counsel", "psychology", "health", "nursing", "patient", "clinical",
        "community outreach", "recreation", "developmental",
    ],
    "Engineering/Architecture": [
        "civil", "bim", "mechanical", "electrical", "structural", "architect",
        "cad", "manufacturing", "chemical engineer", "environmental",
        "construction", "surveying", "drafting", "autocad", "revit",
    ],
    "Media/Design": [
        "graphic", "design", "ux", "ui", "video", "content", "communications",
        "social media", "journalism", "photography", "animation", "brand",
    ],
}


def tag_programs(title: str, description: str = "") -> list[str]:
    """Map a role to zero or more student programs using keyword rules."""
    haystack = f"{title} {description}".lower()
    tags = []
    for program, keywords in PROGRAM_RULES.items():
        if any(kw in haystack for kw in keywords):
            tags.append(program)
    return tags


# --- Term inference ----------------------------------------------------------
TERM_PATTERNS = [
    (r"\b(summer|may[- ]aug|s/?\d{2})\b", "Summer"),
    (r"\b(fall|autumn|sep[t]?[- ]dec)\b", "Fall"),
    (r"\b(winter|jan[- ]apr)\b", "Winter"),
    (r"\bspring\b", "Spring"),
]


def infer_term(title: str, description: str = "", fallback_year: int | None = None) -> str:
    """Best-effort work-term extraction, e.g. 'Summer 2027'."""
    haystack = f"{title} {description}".lower()

    season = ""
    for pattern, label in TERM_PATTERNS:
        if re.search(pattern, haystack):
            season = label
            break

    year_match = re.search(r"\b(20\d{2})\b", haystack)
    year = year_match.group(1) if year_match else (str(fallback_year) if fallback_year else "")

    if season and year:
        return f"{season} {year}"
    if season:
        return season
    return ""
