import traceback
import sys

print("Starting bot...")
try:
    import bot
    bot.main()
except Exception as e:
    print(f"\nCRASH: {type(e).__name__}: {e}")
    traceback.print_exc()
except SystemExit as e:
    print(f"\nSystemExit: {e}")

print("\n--- Bot stopped ---")
input("Press Enter to close...")
