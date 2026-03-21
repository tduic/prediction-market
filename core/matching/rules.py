"""Rule-based matching for prediction market pairs."""

import re
from dataclasses import dataclass
from typing import Optional, Dict, List, Callable


@dataclass
class MatchResult:
    """Result of market pair matching."""

    pair_type: str  # "complement", "subset", "cross_platform", etc.
    relationship: Optional[str]  # e.g., "subset", "superset"
    confidence: float  # 0.0 to 1.0


class RuleTemplate:
    """Base class for rule templates."""

    def __init__(self, name: str, category: str):
        """
        Initialize rule template.

        Args:
            name: Template name (e.g., "FOMC_DECISION")
            category: Category (e.g., "economic", "election", "crypto")
        """
        self.name = name
        self.category = category

    def extract_key_info(self, title: str) -> Optional[Dict[str, str]]:
        """
        Extract key information from market title.

        Should be overridden by subclasses.

        Args:
            title: Market title to parse

        Returns:
            Dictionary with extracted info, or None if no match
        """
        raise NotImplementedError


class FOCMTemplate(RuleTemplate):
    """Template for FOMC interest rate decision markets."""

    def __init__(self):
        super().__init__("FOMC_DECISION", "economic")
        # Common patterns for FOMC markets
        self.patterns = [
            r"FOMC.*?(\d+bps|[\d.]+%)",  # FOMC with basis points or percent
            r"Federal.*?Funds.*?Rate.*?(\d+bps|[\d.]+%)",
            r"Fed.*?Rate.*?Decision",
            r"Interest.*?Rate.*?Decision.*?FOMC",
        ]

    def extract_key_info(self, title: str) -> Optional[Dict[str, str]]:
        """Extract FOMC decision info from title."""
        title_lower = title.lower()

        for pattern in self.patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                # Try to extract decision level if present
                level = match.group(1) if match.lastindex else None
                return {
                    "event_type": "FOMC",
                    "decision_level": level,
                    "normalized_title": re.sub(
                        r"(polymarket|kalshi|metaculus)", "", title_lower
                    ).strip(),
                }

        return None


class CPITemplate(RuleTemplate):
    """Template for CPI (inflation) markets."""

    def __init__(self):
        super().__init__("CPI", "economic")
        self.patterns = [
            r"CPI.*?([\d.]+%)",
            r"Inflation.*?([\d.]+%)",
            r"Consumer.*?Price.*?Index",
            r"Inflation.*?Rate",
        ]

    def extract_key_info(self, title: str) -> Optional[Dict[str, str]]:
        """Extract CPI decision info from title."""
        title_lower = title.lower()

        for pattern in self.patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                threshold = match.group(1) if match.lastindex else None
                return {
                    "event_type": "CPI",
                    "threshold": threshold,
                    "normalized_title": re.sub(
                        r"(polymarket|kalshi|metaculus)", "", title_lower
                    ).strip(),
                }

        return None


class ElectionTemplate(RuleTemplate):
    """Template for election markets."""

    def __init__(self):
        super().__init__("ELECTION", "political")
        self.patterns = [
            r"(2024|2026|2028).*?(Presidential|Senate|House|Governor)",
            r"(Presidential|Senate|House|Governor).*?(2024|2026|2028)",
            r"(Trump|Harris|DeSantis|Newsom).*?(President|Senate|House)",
        ]

    def extract_key_info(self, title: str) -> Optional[Dict[str, str]]:
        """Extract election info from title."""
        title_lower = title.lower()

        for pattern in self.patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                return {
                    "event_type": "ELECTION",
                    "race_type": match.group(2) if match.lastindex >= 2 else "election",
                    "normalized_title": re.sub(
                        r"(polymarket|kalshi|metaculus)", "", title_lower
                    ).strip(),
                }

        return None


class SportsTemplate(RuleTemplate):
    """Template for sports outcome markets."""

    def __init__(self):
        super().__init__("SPORTS", "sports")
        self.patterns = [
            r"(Super Bowl|World Series|Stanley Cup|NBA Finals)",
            r"(\w+)\s+(vs|@)\s+(\w+)",
            r"(AFC|NFC|NBA|MLB|NHL|Premier League)",
        ]

    def extract_key_info(self, title: str) -> Optional[Dict[str, str]]:
        """Extract sports info from title."""
        title_lower = title.lower()

        for pattern in self.patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                return {
                    "event_type": "SPORTS",
                    "sport": match.group(1) if match.lastindex else "sports",
                    "normalized_title": re.sub(
                        r"(polymarket|kalshi|metaculus)", "", title_lower
                    ).strip(),
                }

        return None


class TemplateRegistry:
    """Registry of matching templates."""

    def __init__(self):
        """Initialize template registry with default templates."""
        self.templates: List[RuleTemplate] = [
            FOCMTemplate(),
            CPITemplate(),
            ElectionTemplate(),
            SportsTemplate(),
        ]

    def add_template(self, template: RuleTemplate) -> None:
        """Add a template to the registry."""
        self.templates.append(template)

    def get_templates_for_category(self, category: str) -> List[RuleTemplate]:
        """Get all templates for a category."""
        return [t for t in self.templates if t.category == category]

    def extract_info_all(self, title: str) -> List[Dict[str, str]]:
        """Try all templates on a title."""
        results = []
        for template in self.templates:
            info = template.extract_key_info(title)
            if info:
                results.append(info)
        return results


# Global registry
_template_registry = TemplateRegistry()


def match_by_rules(
    market_a_title: str,
    market_b_title: str,
    category: Optional[str] = None,
    registry: Optional[TemplateRegistry] = None,
) -> Optional[MatchResult]:
    """
    Match two markets using rule-based templates.

    Attempts to match markets based on known event patterns for common
    categories like FOMC, CPI, elections, and sports.

    Args:
        market_a_title: Title of first market
        market_b_title: Title of second market
        category: Optional category to focus on (e.g., "economic", "election")
        registry: Optional custom template registry

    Returns:
        MatchResult if markets match a template, None otherwise
    """
    if registry is None:
        registry = _template_registry

    # Get templates to try
    if category:
        templates = registry.get_templates_for_category(category)
    else:
        templates = registry.templates

    if not templates:
        return None

    # Try each template
    for template in templates:
        info_a = template.extract_key_info(market_a_title)
        info_b = template.extract_key_info(market_b_title)

        if info_a and info_b:
            # Check if events match
            if _events_match(info_a, info_b):
                # Determine pair type and relationship
                pair_type = _infer_pair_type(
                    market_a_title, market_b_title, info_a, info_b
                )

                return MatchResult(
                    pair_type=pair_type,
                    relationship=None,
                    confidence=0.9,  # High confidence for rule matches
                )

    return None


def _events_match(info_a: Dict[str, str], info_b: Dict[str, str]) -> bool:
    """Check if two extracted event infos match."""
    # Events must be of same type
    if info_a.get("event_type") != info_b.get("event_type"):
        return False

    # For FOMC and CPI, must match the specific level/threshold
    if info_a.get("event_type") == "FOMC":
        return info_a.get("decision_level") == info_b.get("decision_level")

    if info_a.get("event_type") == "CPI":
        return info_a.get("threshold") == info_b.get("threshold")

    # For elections, must match race type
    if info_a.get("event_type") == "ELECTION":
        return info_a.get("race_type") == info_b.get("race_type")

    # Default: same event type is enough
    return True


def _infer_pair_type(
    title_a: str, title_b: str, info_a: Dict[str, str], info_b: Dict[str, str]
) -> str:
    """Infer the pair type from market titles and extracted info."""
    # Check for subset/superset patterns
    if "Q1" in title_a or "Q1" in title_b:
        # Quarterly data likely subset of annual
        if "2026" in title_a or "2026" in title_b:
            return "subset"

    # Check for yes/no complement patterns
    if ("yes" in title_a.lower() and "no" in title_b.lower()) or (
        "no" in title_a.lower() and "yes" in title_b.lower()
    ):
        return "complement"

    # Check for cross-platform by looking at platform names
    platforms_a = {"polymarket", "kalshi", "metaculus", "manifold"}
    platforms_b = {"polymarket", "kalshi", "metaculus", "manifold"}

    title_a_lower = title_a.lower()
    title_b_lower = title_b.lower()

    platform_a = next((p for p in platforms_a if p in title_a_lower), None)
    platform_b = next((p for p in platforms_b if p in title_b_lower), None)

    if platform_a and platform_b and platform_a != platform_b:
        return "cross_platform"

    # Default
    return "complement"


def register_custom_template(template: RuleTemplate) -> None:
    """Register a custom template globally."""
    _template_registry.add_template(template)
