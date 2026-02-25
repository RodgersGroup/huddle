"""Segment-based pricing definitions for Huddle.

Segments represent who lives in the household (solo, household, sharehouse).
Each segment has its own set of tiers with different pricing and member limits.
"""

# Set to False to enforce paid tiers and free tier limits post-launch
BETA_MODE = True

# Free tier limits for specific modules (None = unlimited)
# Only enforced when BETA_MODE = False
FREE_TIER_LIMITS = {
    "chores": 5,
    "shopping": 15,
}

# Per-tier limits for specific modules (overrides unlimited default on paid tiers)
# Format: {tier_key: {module_key: limit}}
# Only enforced when BETA_MODE = False
TIER_LIMITS = {
    "solo_plus": {"recipes": 5},
}

SEGMENTS = {
    "solo": {
        "name": "Solo & Couples",
        "description": "Living alone or as a couple",
        "max_members_by_tier": {"free": 1, "solo_plus": 1, "duo_plus": 2},
        "tiers": ["free", "solo_plus", "duo_plus"],
    },
    "household": {
        "name": "Household",
        "description": "Families, couples with kids, multi-generational",
        "max_members_by_tier": {"free": 3, "home": 4, "home_plus": 8},
        "tiers": ["free", "home", "home_plus"],
    },
    "sharehouse": {
        "name": "Sharehouse",
        "description": "Flatmates, uni students, house shares",
        "max_members_by_tier": {"free": 3, "share": 8, "share_plus": 8},
        "tiers": ["free", "share", "share_plus"],
    },
}

TIERS = {
    "free": {"name": "Free", "price_monthly": 0, "price_annual": 0},
    "solo_plus": {"name": "Solo+", "price_monthly": 2.99, "price_annual": 26},
    "duo_plus": {"name": "Duo+", "price_monthly": 4.99, "price_annual": 44},
    "home": {"name": "Home", "price_monthly": 7.99, "price_annual": 69},
    "home_plus": {"name": "Home+", "price_monthly": 14.99, "price_annual": 129, "extra_member_price": 2.99},
    "share": {"name": "Share", "price_monthly": 4.99, "price_annual": 44},
    "share_plus": {"name": "Share+", "price_monthly": 9.99, "price_annual": 89, "extra_member_price": 2.99},
}

# Which modules are available at which tier + segment combination
# "core" = available on free tier, all segments
# "paid" = available on any paid tier, all segments
# "household" = household segment only, at specified minimum tier
# "sharehouse" = sharehouse segment only, at specified minimum tier
MODULES = {
    # Core (all segments, all tiers)
    "chores":      {"name": "Chores",          "tier": "core",       "segment": "all"},
    "calendar":    {"name": "Calendar",         "tier": "core",       "segment": "all"},
    "adhoc_tasks": {"name": "Tasks",            "tier": "core",       "segment": "all"},
    "weather":     {"name": "Weather",          "tier": "core",       "segment": "all"},
    "shopping":    {"name": "Shopping",          "tier": "core",       "segment": "all"},  # limited to 15 items on free
    "feedback":    {"name": "Feedback",          "tier": "core",       "segment": "all"},
    "selfcare":    {"name": "Self Care",         "tier": "core",       "segment": "all"},

    # Shared paid (any paid tier, any segment)
    "bills":       {"name": "Bills",             "tier": "paid",       "segment": "all"},
    "inventory":   {"name": "Pantry",            "tier": "paid",       "segment": "all"},
    "polls":       {"name": "Polls",             "tier": "paid",       "segment": "all"},
    "noticeboard": {"name": "Noticeboard",       "segment_tiers": {"solo": "duo_plus", "household": "home", "sharehouse": "share"}},
    "fuel":        {"name": "Fuel Prices",       "tier": "paid",       "segment": "all"},
    "vehicles":    {"name": "Vehicles",          "tier": "paid",       "segment": "all"},

    # Multi-segment modules (different tier per segment)
    "meals":       {"name": "Meals",             "segment_tiers": {"solo": "solo_plus", "household": "home_plus", "sharehouse": "share_plus"}},
    "recipes":     {"name": "Recipes",           "segment_tiers": {"solo": "solo_plus", "household": "home_plus", "sharehouse": "share_plus"}},

    # Household extras
    "routines":    {"name": "Routines",          "tier": "home_plus",  "segment": "household"},
    "pets":        {"name": "Pets",              "tier": "home",       "segment": "household"},
    "rewards":     {"name": "Rewards & Stars",   "tier": "home_plus",  "segment": "household"},
    "allowances":  {"name": "Allowances",        "tier": "home_plus",  "segment": "household"},
    "assignments": {"name": "Assignments",       "tier": "home_plus",  "segment": "household"},

    # Sharehouse extras
    "expenses":       {"name": "Expenses",        "segment_tiers": {"household": "home_plus", "sharehouse": "share_plus"}},
    "house_rules":    {"name": "House Rules",     "tier": "share",      "segment": "sharehouse"},
    "tenancy":        {"name": "Tenancy",         "tier": "share",      "segment": "sharehouse"},
}


def get_max_members(segment: str, tier: str) -> int | None:
    """Return the max member count for a segment+tier combo. None = unlimited."""
    seg = SEGMENTS.get(segment)
    if not seg:
        return 3  # safe default
    return seg["max_members_by_tier"].get(tier, 3)


def is_tier_valid_for_segment(segment: str, tier: str) -> bool:
    """Check if a tier code is valid for the given segment."""
    if tier == "free":
        return True
    seg = SEGMENTS.get(segment)
    return seg is not None and tier in seg["tiers"]


def _get_module_tier_for_segment(mod: dict, segment: str) -> str | None:
    """Return the minimum tier required for a module in the given segment, or None if not available."""
    if "segment_tiers" in mod:
        return mod["segment_tiers"].get(segment)
    mod_segment = mod.get("segment", "all")
    if mod_segment == "all" or mod_segment == segment:
        return mod.get("tier", "core")
    return None


def is_module_available(module_key: str, segment: str, tier: str) -> bool:
    """
    Check if a module is available for a given segment + tier.
    When BETA_MODE is True, always returns True.
    """
    if BETA_MODE:
        return True

    mod = MODULES.get(module_key)
    if not mod:
        return False
    min_tier = _get_module_tier_for_segment(mod, segment)
    if min_tier is None:
        return False
    if min_tier == "core":
        return True
    if min_tier == "paid":
        return tier != "free"
    seg = SEGMENTS.get(segment)
    if not seg:
        return False
    seg_tiers = ["free"] + [t for t in seg["tiers"] if t != "free"]
    min_index = seg_tiers.index(min_tier) if min_tier in seg_tiers else 999
    current_index = seg_tiers.index(tier) if tier in seg_tiers else 0
    return current_index >= min_index


def get_segment_modules(segment: str) -> list[str]:
    """Return module keys available to a segment (ignoring tier for now)."""
    result = []
    for key, mod in MODULES.items():
        if _get_module_tier_for_segment(mod, segment) is not None:
            result.append(key)
    return result
