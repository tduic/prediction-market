"""
Export analytics tables to CSV files.

Dumps trade_outcomes, pnl_snapshots, and violations summary to CSV.
"""

import argparse
import asyncio
import logging
from pathlib import Path

import aiosqlite
import pandas as pd

logger = logging.getLogger(__name__)


class AnalyticsExporter:
    """Export analytics data to CSV format."""

    def __init__(self, db_path: str) -> None:
        """
        Initialize the analytics exporter.

        Args:
            db_path: Path to the SQLite database
        """
        self.db_path = db_path

    async def export_table_to_csv(
        self,
        table_name: str,
        output_path: str,
    ) -> int:
        """
        Export a database table to CSV.

        Args:
            table_name: Name of the table to export
            output_path: Path where CSV will be written

        Returns:
            Number of rows exported
        """
        try:
            logger.info("Exporting table '%s' to %s", table_name, output_path)

            async with aiosqlite.connect(self.db_path) as db:
                # Read table into pandas DataFrame
                query = f"SELECT * FROM {table_name}"
                df = pd.read_sql_query(query, await db.connection())

            if df.empty:
                logger.warning("Table '%s' is empty", table_name)
                return 0

            # Write to CSV
            df.to_csv(output_path, index=False)
            logger.info(
                "Exported %d rows from '%s'",
                len(df),
                table_name,
            )

            return len(df)

        except Exception as e:
            logger.error("Error exporting table '%s': %s", table_name, exc_info=e)
            return 0

    async def export_trade_outcomes(self, output_path: str) -> int:
        """
        Export trade_outcomes table.

        Args:
            output_path: Path where CSV will be written

        Returns:
            Number of rows exported
        """
        return await self.export_table_to_csv("trade_outcomes", output_path)

    async def export_pnl_snapshots(self, output_path: str) -> int:
        """
        Export pnl_snapshots table.

        Args:
            output_path: Path where CSV will be written

        Returns:
            Number of rows exported
        """
        return await self.export_table_to_csv("pnl_snapshots", output_path)

    async def export_violations_summary(self, output_path: str) -> int:
        """
        Export violations summary from constraint_violations table.

        Args:
            output_path: Path where CSV will be written

        Returns:
            Number of rows exported
        """
        try:
            logger.info("Exporting violations summary to %s", output_path)

            async with aiosqlite.connect(self.db_path) as db:
                # Create violations summary
                query = """
                    SELECT
                        violation_type,
                        COUNT(*) as count,
                        AVG(severity) as avg_severity,
                        MIN(timestamp_utc) as first_violation,
                        MAX(timestamp_utc) as last_violation
                    FROM constraint_violations
                    GROUP BY violation_type
                    ORDER BY count DESC
                """

                df = pd.read_sql_query(query, await db.connection())

            if df.empty:
                logger.warning("No violations found")
                return 0

            df.to_csv(output_path, index=False)
            logger.info("Exported violations summary with %d rows", len(df))

            return len(df)

        except Exception as e:
            logger.error("Error exporting violations summary: %s", exc_info=e)
            return 0

    async def export_all(
        self,
        output_dir: str | None = None,
    ) -> dict[str, int]:
        """
        Export all analytics tables.

        Args:
            output_dir: Directory where CSVs will be written

        Returns:
            Dictionary of table_name -> row_count
        """
        if output_dir is None:
            output_dir = "analytics_export"

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        results = {}

        # Export trade_outcomes
        outcomes_file = output_path / "trade_outcomes.csv"
        results["trade_outcomes"] = await self.export_trade_outcomes(str(outcomes_file))

        # Export PnL snapshots
        pnl_file = output_path / "pnl_snapshots.csv"
        results["pnl_snapshots"] = await self.export_pnl_snapshots(str(pnl_file))

        # Export per-strategy PnL snapshots
        strategy_pnl_file = output_path / "strategy_pnl_snapshots.csv"
        results["strategy_pnl_snapshots"] = await self.export_table_to_csv(
            "strategy_pnl_snapshots", str(strategy_pnl_file)
        )

        # Export violations summary
        violations_file = output_path / "violations_summary.csv"
        results["violations_summary"] = await self.export_violations_summary(
            str(violations_file)
        )

        return results


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Export analytics tables to CSV",
    )
    parser.add_argument(
        "--output",
        default="analytics_export",
        help="Output directory for CSV files",
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

    exporter = AnalyticsExporter(db_path=args.db)

    try:
        results = await exporter.export_all(output_dir=args.output)

        # Print summary
        print("\nExport Summary")
        print("=" * 60)
        for table_name, row_count in results.items():
            print(f"{table_name}: {row_count} rows")
        print("=" * 60)
        print(f"\nFiles written to: {args.output}")

    except Exception as e:
        logger.error("Error during export: %s", exc_info=e)


if __name__ == "__main__":
    asyncio.run(main())
