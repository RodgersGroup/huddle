"""Background loop that drives self-healing and preventive agents."""
import asyncio
import logging

logger = logging.getLogger("huddle.agents")


async def agent_loop_task():
    """Run all agents periodically."""
    from agents.error_healer import run_system_checks as heal_system
    from agents.preventive_scanner import run_system_checks as scan_system
    from agents.preventive_scanner import run_household_checks as scan_household

    # Wait 60 seconds after startup to let the app stabilise
    await asyncio.sleep(60)
    logger.info("Self-healing agent loop started")

    while True:
        try:
            # System-wide checks (not per-household)
            await asyncio.to_thread(heal_system)
            await asyncio.to_thread(scan_system)

            # Per-household checks
            from background import _get_all_household_ids
            for hid in _get_all_household_ids():
                try:
                    await asyncio.to_thread(scan_household, hid)
                except Exception as e:
                    logger.warning("Agent household scan error for %d: %s", hid, e)

        except Exception as e:
            logger.error("Agent loop error: %s", e, exc_info=True)

        await asyncio.sleep(300)  # Check every 5 minutes
