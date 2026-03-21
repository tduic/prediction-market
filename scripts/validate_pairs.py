"""
Interactive CLI tool to review and validate auto-discovered market pairs.

Shows pairs side-by-side with resolution criteria and allows manual approval/rejection.
"""

import argparse
import asyncio
import logging

import aiosqlite

logger = logging.getLogger(__name__)


class PairValidator:
    """Interactive pair validation tool."""

    def __init__(self, db_path: str) -> None:
        """
        Initialize the pair validator.

        Args:
            db_path: Path to the SQLite database
        """
        self.db_path = db_path

    async def get_pairs_by_status(self, verified: int | None) -> list[dict]:
        """
        Get pairs filtered by verification status.

        Args:
            verified: 0 for unverified, 1 for verified, None for all

        Returns:
            List of pair records
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if verified is None:
                query = "SELECT * FROM market_pairs ORDER BY created_at DESC"
                cursor = await db.execute(query)
            else:
                query = "SELECT * FROM market_pairs WHERE verified = ? ORDER BY created_at DESC"
                cursor = await db.execute(query, (verified,))

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    def display_pair(self, pair: dict, idx: int, total: int) -> None:
        """
        Display a pair for review.

        Args:
            pair: The pair record
            idx: Current index
            total: Total number of pairs
        """
        print("\n" + "=" * 80)
        print(f"Pair {idx + 1}/{total}")
        print("=" * 80)

        print(f"\nName: {pair['name']}")
        print(f"Description: {pair['description']}")
        print(f"Category: {pair['category']}")
        print("\nResolution Criteria:")
        print(f"  {pair['resolution_criteria']}")

        print(f"\nLeg A ({pair['leg_a_platform'].upper()}):")
        print(f"  Market ID: {pair['leg_a_market_id']}")
        print(f"  Description: {pair['leg_a_description']}")

        print(f"\nLeg B ({pair['leg_b_platform'].upper()}):")
        print(f"  Market ID: {pair['leg_b_market_id']}")
        print(f"  Description: {pair['leg_b_description']}")

        print(f"\nCreated: {pair['created_at']}")
        print(f"Match Method: {pair['match_method']}")

    async def validate_pair(
        self,
        pair: dict,
    ) -> bool | None:
        """
        Interactively validate a single pair.

        Args:
            pair: The pair to validate

        Returns:
            True if accepted, False if rejected, None if skipped
        """
        while True:
            response = input("\n(a)ccept, (r)eject, (s)kip, (q)uit? ").lower().strip()

            if response == "a":
                return True
            elif response == "r":
                return False
            elif response == "s":
                return None
            elif response == "q":
                raise KeyboardInterrupt()
            else:
                print("Invalid response. Please enter 'a', 'r', 's', or 'q'.")

    async def update_pair_status(
        self,
        pair_id: int,
        verified: int,
    ) -> None:
        """
        Update the verification status of a pair.

        Args:
            pair_id: The pair ID
            verified: 1 for verified, 0 for rejected
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE market_pairs SET verified = ? WHERE pair_id = ?",
                (verified, pair_id),
            )
            await db.commit()

    async def validate_pairs(self, status: str = "unverified") -> None:
        """
        Run interactive pair validation.

        Args:
            status: "unverified", "verified", or "all"
        """
        # Map status to verified column value
        if status.lower() == "unverified":
            verified_filter = 0
        elif status.lower() == "verified":
            verified_filter = 1
        else:
            verified_filter = None

        logger.info("Loading pairs with status: %s", status)
        pairs = await self.get_pairs_by_status(verified_filter)

        if not pairs:
            print(f"\nNo pairs found with status '{status}'")
            return

        print(f"\nLoaded {len(pairs)} pairs for review")

        accepted_count = 0
        rejected_count = 0
        skipped_count = 0

        try:
            for idx, pair in enumerate(pairs):
                self.display_pair(pair, idx, len(pairs))

                result = await self.validate_pair(pair)

                if result is True:
                    await self.update_pair_status(pair["pair_id"], 1)
                    print("✓ Pair accepted")
                    accepted_count += 1

                elif result is False:
                    await self.update_pair_status(pair["pair_id"], 0)
                    print("✗ Pair rejected")
                    rejected_count += 1

                else:
                    print("↷ Pair skipped")
                    skipped_count += 1

        except KeyboardInterrupt:
            print("\n\nValidation cancelled by user")

        # Summary
        print("\n" + "=" * 80)
        print("Validation Summary")
        print("=" * 80)
        print(f"Accepted: {accepted_count}")
        print(f"Rejected: {rejected_count}")
        print(f"Skipped: {skipped_count}")
        print(f"Total: {len(pairs)}")


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Validate auto-discovered market pairs",
    )
    parser.add_argument(
        "--status",
        choices=["unverified", "verified", "all"],
        default="unverified",
        help="Filter pairs by verification status",
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

    validator = PairValidator(db_path=args.db)

    try:
        await validator.validate_pairs(status=args.status)
    except Exception as e:
        logger.error("Error during validation: %s", exc_info=e)


if __name__ == "__main__":
    asyncio.run(main())
