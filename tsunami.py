"""
tsunami.py
----------
Single entry point for Tsunami.
Installs dependencies, runs initial scan, launches dashboard.

Usage:
    python3 tsunami.py           # launch dashboard (scan if no data)
    python3 tsunami.py --scan    # force fresh scan then launch
    python3 tsunami.py --scan-only  # scan without launching dashboard
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REQUIRED = [
    "numpy",
    "pandas",
    "yfinance",
    "plotly",
    "dash",
    "PyWavelets",
]


CONFIG_PATH = Path.home() / ".claude_config.json"


def setup_api_key() -> None:
    """
    Ensure an Anthropic API key is saved for AI commentary.
    - If already saved, do nothing.
    - If ANTHROPIC_API_KEY env var is set, save it and move on.
    - Otherwise, prompt the user to paste it in.
    Skippable — pressing Enter with no input disables AI commentary.
    """
    import json, os

    # Already saved?
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            if cfg.get("anthropic_api_key", "").startswith("sk-"):
                print("  ✅ Anthropic API key loaded")
                return
        except Exception:
            pass

    # Env var?
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key.startswith("sk-"):
        CONFIG_PATH.write_text(json.dumps({"anthropic_api_key": env_key}, indent=2))
        print("  ✅ Anthropic API key saved from environment")
        return

    # Prompt
    print("\n  🔑 Anthropic API key not found.")
    print("     AI commentary in the Intelligence tab needs it.")
    print("     Get yours at: https://console.anthropic.com/settings/keys")
    print("     (Press Enter to skip — AI commentary will be disabled)\n")
    try:
        key = input("  Paste your API key here: ").strip()
    except (EOFError, KeyboardInterrupt):
        key = ""

    if key.startswith("sk-"):
        CONFIG_PATH.write_text(json.dumps({"anthropic_api_key": key}, indent=2))
        print("  ✅ API key saved to ~/.claude_config.json\n")
    else:
        if key:
            print("  ⚠️  That doesn't look like a valid key (should start with sk-). Skipping.\n")
        else:
            print("  ℹ️  Skipped — AI commentary disabled. Run again to add your key later.\n")


def install_deps() -> None:
    print("Checking dependencies...")
    for pkg in REQUIRED:
        try:
            __import__(pkg.lower().replace("-", "_").replace("pywavelets", "pywt"))
            print(f"  ✅ {pkg}")
        except ImportError:
            print(f"  📦 Installing {pkg}...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
                check=True
            )
            print(f"  ✅ {pkg} installed")


def main() -> None:
    # Raise macOS file descriptor limit — yfinance opens many handles during large scans
    import resource
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, hard), hard))
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="🌊 Tsunami — Market Regime Detector")
    parser.add_argument("--scan",      action="store_true", help="Force fresh scan")
    parser.add_argument("--scan-only", action="store_true", help="Scan only, no dashboard")
    parser.add_argument("--no-install",action="store_true", help="Skip dependency check")
    args = parser.parse_args()

    print("\n🌊 Tsunami — Market Regime Detector")
    print("=" * 45)

    if not args.no_install:
        install_deps()
        print()

    print("Checking API key...")
    setup_api_key()

    from tsunami_engine import init_db, load_latest, run_scan

    init_db()

    if args.scan or args.scan_only:
        print("Running full scan across all assets...")
        print("(This takes 3-5 minutes — CWT analysis is thorough)\n")
        run_scan()
        print("\nRunning universe scan — top crypto...")
        try:
            from tsunami_universe import run_universe_scan, run_tsx_scan
            run_universe_scan()
            print("\nRunning TSX sector scan...")
            run_tsx_scan()
        except Exception as e:
            print(f"  Universe scan error: {e}")
    else:
        existing = load_latest()
        if not existing:
            print("No data found. Running initial scan...")
            print("(This takes 2-3 minutes on first run)\n")
            run_scan()
        else:
            print(f"Found existing data for {len(existing)} assets.")
            print("Use --scan to force a fresh scan.\n")

    if args.scan_only:
        print("\n✅ Scan complete. Run python3 tsunami.py to launch dashboard.")
        return

    print("\n🚀 Launching dashboard...")
    print("   Open your browser to: http://localhost:8050")
    print("   Press Ctrl+C to stop\n")

    from tsunami_dashboard import app
    app.run(debug=False, port=8050, use_reloader=False)


if __name__ == "__main__":
    main()
