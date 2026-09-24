import asyncio, sys, logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
from civ_mcp import game_launcher

async def main():
    name = sys.argv[1] if len(sys.argv) > 1 else None
    print("=== load_save_from_menu:", name)
    print(await game_launcher.load_save_from_menu(name))

asyncio.run(main())
