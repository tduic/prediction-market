"""External data feed clients: CME, Cleveland Fed, Metaculus, BLS."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class FedWatchData:
    """CME FedWatch data."""

    meeting_date: datetime
    implied_prob_hike: float
    implied_prob_hold: float
    implied_prob_cut: float
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)


@dataclass
class NowcastData:
    """Cleveland Fed Nowcast data."""

    forecast_date: datetime
    cpi_nowcast: float
    cpi_nowcast_std: float
    gdp_nowcast: float
    gdp_nowcast_std: float
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)


@dataclass
class MetaculusQuestion:
    """Metaculus question data."""

    question_id: int
    title: str
    description: str
    resolution_date: datetime | None
    community_prediction: float
    created_at: datetime
    updated_at: datetime
    url: str


@dataclass
class BLSRelease:
    """BLS economic release."""

    release_name: str
    release_date: datetime
    data_date: datetime | None
    notes: str


class CMEFedWatchScraper:
    """Scrapes CME FedWatch Tool for FOMC rate decision probabilities."""

    URL = "https://www.cmegroup.com/markets/miscellaneous/fed-funds.quotes.html"

    async def fetch_fed_watch(self) -> FedWatchData | None:
        """
        Scrape CME FedWatch data.

        Returns:
            FedWatchData or None on parse failure
        """
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(self.URL)
                response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # Find the fed funds table/container
            implied_probs = self._extract_implied_probabilities(soup)

            if not implied_probs:
                logger.warning(
                    "Could not parse CME FedWatch data - page structure may have changed"
                )
                return None

            return FedWatchData(
                meeting_date=implied_probs["meeting_date"],
                implied_prob_hike=implied_probs["hike"],
                implied_prob_hold=implied_probs["hold"],
                implied_prob_cut=implied_probs["cut"],
            )

        except httpx.HTTPError as e:
            logger.error(f"Error fetching CME FedWatch: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error scraping CME FedWatch: {e}")
            return None

    def _extract_implied_probabilities(self, soup: BeautifulSoup) -> dict | None:
        """Extract rate probabilities from parsed HTML."""
        try:
            # This is a simplified extraction - actual CME structure varies
            table = soup.find("table", {"class": re.compile(r".*fed.*", re.I)})
            if not table:
                return None

            rows = table.find_all("tr")
            if not rows:
                return None

            # Parse first data row
            cells = rows[1].find_all("td")
            if len(cells) < 4:
                return None

            return {
                "meeting_date": datetime.now(timezone.utc),
                "hike": float(cells[1].text.strip().rstrip("%")) / 100,
                "hold": float(cells[2].text.strip().rstrip("%")) / 100,
                "cut": float(cells[3].text.strip().rstrip("%")) / 100,
            }
        except (ValueError, IndexError, AttributeError) as e:
            logger.warning(f"Failed to extract CME FedWatch probabilities: {e}")
            return None


class ClevelandFedScraper:
    """Fetches Cleveland Fed Nowcast predictions."""

    URL = "https://www.clevelandfed.org/indicators-and-data/nowcast/"

    async def fetch_nowcast(self) -> NowcastData | None:
        """
        Fetch Cleveland Fed Nowcast.

        Returns:
            NowcastData or None on parse failure
        """
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(self.URL)
                response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            nowcast_data = self._extract_nowcast(soup)

            if not nowcast_data:
                logger.warning(
                    "Could not parse Cleveland Fed Nowcast - page structure may have changed"
                )
                return None

            return NowcastData(
                forecast_date=nowcast_data["forecast_date"],
                cpi_nowcast=nowcast_data["cpi"],
                cpi_nowcast_std=nowcast_data["cpi_std"],
                gdp_nowcast=nowcast_data["gdp"],
                gdp_nowcast_std=nowcast_data["gdp_std"],
            )

        except httpx.HTTPError as e:
            logger.error(f"Error fetching Cleveland Fed Nowcast: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error scraping Cleveland Fed Nowcast: {e}")
            return None

    def _extract_nowcast(self, soup: BeautifulSoup) -> dict | None:
        """Extract nowcast values from parsed HTML."""
        try:
            # Simplified extraction - actual page structure varies
            text = soup.get_text()

            # Look for patterns like "GDP Nowcast: X.XX%"
            gdp_match = re.search(r"GDP Nowcast[:\s]+([0-9.]+)%", text)
            cpi_match = re.search(r"CPI Nowcast[:\s]+([0-9.]+)%", text)

            if not (gdp_match and cpi_match):
                return None

            return {
                "forecast_date": datetime.now(timezone.utc),
                "gdp": float(gdp_match.group(1)) / 100,
                "gdp_std": 0.01,  # Placeholder
                "cpi": float(cpi_match.group(1)) / 100,
                "cpi_std": 0.01,  # Placeholder
            }
        except (ValueError, AttributeError) as e:
            logger.warning(f"Failed to extract Cleveland Fed Nowcast: {e}")
            return None


class MetaculusClient:
    """Metaculus API client for question data."""

    BASE_URL = "https://www.metaculus.com"
    API_BASE = "https://www.metaculus.com/api2"

    async def fetch_questions(
        self, category: str | None = None
    ) -> list[MetaculusQuestion]:
        """
        Fetch questions from Metaculus.

        Args:
            category: Optional category filter

        Returns:
            List of MetaculusQuestion objects
        """
        try:
            endpoint = f"{self.API_BASE}/questions/"
            params: dict[str, str | int] = {"limit": 100}

            if category:
                params["search"] = category

            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(endpoint, params=params)
                response.raise_for_status()

            data = response.json()
            questions = []

            for item in data.get("results", []):
                q = self._parse_question(item)
                if q:
                    questions.append(q)

            logger.info(f"Fetched {len(questions)} questions from Metaculus")
            return questions

        except httpx.HTTPError as e:
            logger.error(f"Error fetching Metaculus questions: {e}")
            return []

    def _parse_question(self, item: dict) -> MetaculusQuestion | None:
        """Parse Metaculus API response into MetaculusQuestion."""
        try:
            return MetaculusQuestion(
                question_id=item.get("id", 0),
                title=item.get("title", ""),
                description=item.get("description", ""),
                resolution_date=(
                    datetime.fromisoformat(item["resolve_time"])
                    if item.get("resolve_time")
                    else None
                ),
                community_prediction=float(item.get("community_prediction", 0.5)),
                created_at=(
                    datetime.fromisoformat(item["created_at"])
                    if item.get("created_at")
                    else datetime.now(timezone.utc)
                ),
                updated_at=(
                    datetime.fromisoformat(item["updated_at"])
                    if item.get("updated_at")
                    else datetime.now(timezone.utc)
                ),
                url=f"{self.BASE_URL}/questions/{item.get('slug', '')}",
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Failed to parse Metaculus question: {e}")
            return None


class BLSCalendarFetcher:
    """Fetches BLS economic release calendar."""

    URL = "https://www.bls.gov/bls/rls_schedule.htm"

    async def fetch_releases(self) -> list[BLSRelease]:
        """
        Fetch BLS economic release calendar.

        Returns:
            List of BLSRelease objects
        """
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(self.URL)
                response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            releases = self._extract_releases(soup)

            logger.info(f"Fetched {len(releases)} BLS releases")
            return releases

        except httpx.HTTPError as e:
            logger.error(f"Error fetching BLS calendar: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching BLS calendar: {e}")
            return []

    def _extract_releases(self, soup: BeautifulSoup) -> list[BLSRelease]:
        """Extract release calendar from parsed HTML."""
        releases = []
        try:
            table = soup.find("table")
            if not table:
                return releases

            rows = table.find_all("tr")[1:]  # Skip header
            for row in rows:
                cells = row.find_all("td")
                if len(cells) >= 3:
                    try:
                        release = BLSRelease(
                            release_name=cells[0].text.strip(),
                            release_date=datetime.strptime(
                                cells[1].text.strip(), "%m/%d/%Y"
                            ),
                            data_date=None,
                            notes=cells[2].text.strip() if len(cells) > 2 else "",
                        )
                        releases.append(release)
                    except ValueError:
                        continue

        except Exception as e:
            logger.warning(f"Error extracting BLS releases: {e}")

        return releases
