"""
Load initial curated market pairs from JSON configuration.

Reads from a JSON file, validates structure, and inserts into the database.
"""

import argparse
import asyncio
import json
import logging
from pathlib import Path

import aiosqlite
from pydantic import BaseModel, ValidationError, validator

logger = logging.getLogger(__name__)


class MarketLeg(BaseModel):
    """Schema for a single market leg in a pair."""

    platform: str
    market_id: str
    description: str

    @validator("platform")
    def validate_platform(cls, v: str) -> str:
        if v.lower() not in ("polymarket", "kalshi"):
            raise ValueError("platform must be 'polymarket' or 'kalshi'")
        return v.lower()


class MarketPair(BaseModel):
    """Schema for a market pair."""

    name: str
    description: str
    category: str
    resolution_criteria: str
    leg_a: MarketLeg
    leg_b: MarketLeg
    match_method: str = "manual"

    @validator("match_method")
    def validate_match_method(cls, v: str) -> str:
        if v.lower() not in ("manual", "auto"):
            raise ValueError("match_method must be 'manual' or 'auto'")
        return v.lower()


class PairSeeder:
    """Load market pairs from JSON file into database."""

    def __init__(self, db_path: str) -> None:
        """
        Initialize the pair seeder.

        Args:
            db_path: Path to the SQLite database
        """
        self.db_path = db_path

    async def load_pairs_from_file(self, file_path: str) -> list[MarketPair]:
        """
        Load and validate market pairs from JSON file.

        Args:
            file_path: Path to JSON file with pairs

        Returns:
            List of validated MarketPair objects

        Raises:
            FileNotFoundError: If file doesn't exist
            json.JSONDecodeError: If JSON is invalid
            ValidationError: If pairs don't match schema
        """
        path = Path(file_path)

        if not path.exists():
            raise FileNotFoundError(f"Pairs file not found: {file_path}")

        logger.info("Loading pairs from %s", file_path)

        with open(path) as f:
            data = json.load(f)

        pairs_data = data.get("pairs", [])
        logger.info("Found %d pairs in file", len(pairs_data))

        pairs: list[MarketPair] = []

        for idx, pair_data in enumerate(pairs_data):
            try:
                pair = MarketPair(**pair_data)
                pairs.append(pair)
                logger.debug("Validated pair %d: %s", idx, pair.name)
            except ValidationError as e:
                logger.error("Validation error for pair %d: %s", idx, e)
                raise

        return pairs

    async def seed_pairs_to_db(self, pairs: list[MarketPair]) -> int:
        """
        Insert pairs into the database.

        Args:
            pairs: List of MarketPair objects

        Returns:
            Number of pairs inserted
        """
        async with aiosqlite.connect(self.db_path) as db:
            inserted_count = 0

            for pair in pairs:
                try:
                    # Check if pair already exists
                    cursor = await db.execute(
                        """
                        SELECT COUNT(*) FROM market_pairs
                        WHERE leg_a_market_id = ? AND leg_b_market_id = ?
                        """,
                        (pair.leg_a.market_id, pair.leg_b.market_id),
                    )
                    row = await cursor.fetchone()

                    if row and row[0] > 0:
                        logger.info("Pair already exists: %s, skipping", pair.name)
                        continue

                    # Insert pair
                    await db.execute(
                        """
                        INSERT INTO market_pairs
                        (name, description, category, resolution_criteria,
                         leg_a_platform, leg_a_market_id, leg_a_description,
                         leg_b_platform, leg_b_market_id, leg_b_description,
                         match_method, verified)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pair.name,
                            pair.description,
                            pair.category,
                            pair.resolution_criteria,
                            pair.leg_a.platform,
                            pair.leg_a.market_id,
                            pair.leg_a.description,
                            pair.leg_b.platform,
                            pair.leg_b.market_id,
                            pair.leg_b.description,
                            pair.match_method,
                            1,  # Mark as verified=1
                        ),
                    )
                    inserted_count += 1
                    logger.info("Inserted pair: %s", pair.name)

                except Exception as e:
                    logger.error("Error inserting pair %s: %s", pair.name, e)

            await db.commit()

        logger.info("Seeded %d pairs to database", inserted_count)
        return inserted_count

    async def seed(self, file_path: str) -> int:
        """
        Run the seeding process.

        Args:
            file_path: Path to JSON pairs file

        Returns:
            Number of pairs inserted
        """
        try:
            pairs = await self.load_pairs_from_file(file_path)
            count = await self.seed_pairs_to_db(pairs)
            return count
        except Exception as e:
            logger.error("Error during seeding: %s", exc_info=e)
            return 0


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Seed market pairs from JSON file",
    )
    parser.add_argument(
        "--file",
        default="config/pairs_seed.json",
        help="Path to JSON pairs file",
    )
    parser.add_argument(
        "--db",
        default="prediction_market.db",
        help="Path to SQLite database",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    seeder = PairSeeder(db_path=args.db)

    try:
        count = await seeder.seed(file_path=args.file)
        logger.info("Seeding completed: %d pairs inserted", count)
    except FileNotFoundError as e:
        logger.error("File error: %s", e)
    except json.JSONDecodeError as e:
        logger.error("JSON error: %s", e)
    except ValidationError as e:
        logger.error("Validation error: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
