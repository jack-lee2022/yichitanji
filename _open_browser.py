from pathlib import Path
import sys
import os
import time
import subprocess

HERE = Path(__file__).parent.resolve()
os.chdir(HERE)
OUTPUT_DIR = HERE / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def open_with_four_fallbacks(output_dir: Path, output_html: Path) -> int:
    LATEST = output_dir / "LATEST_請按兩下這個開啟_最新版_不會有快取.html"
    INTERNAL = output_dir / "_launcher_no_cache_internal.html"
    target_path = LATEST if LATEST.exists() else (INTERNAL if INTERNAL.exists() else output_html)
    target_uri = None
    try:
        target_uri = target_path.resolve().as_uri()
    except Exception:
        target_uri = None

    opened = False

    # 1) os.startfile (Windows-only, most reliable)
    if os.name == "nt":
        try:
            os.startfile(str(target_path))  # type: ignore[attr-defined]
            opened = True
            print(f"  [a] os.startfile({target_path.name}): OK")
        except Exception as e:
            print(f"  [a] os.startfile FAIL: {type(e).__name__}: {e}")

    # 2) cmd /c start
    if not opened and os.name == "nt":
        try:
            rc = subprocess.call(["cmd", "/c", "start", "", str(target_path)], shell=False)
            if rc == 0:
                opened = True
                print(f"  [b] cmd start rc={rc} OK")
            else:
                print(f"  [b] cmd start rc={rc}")
        except Exception as e:
            print(f"  [b] cmd start FAIL: {type(e).__name__}: {e}")

    # 3) webbrowser (only if above failed)
    import webbrowser
    if not opened and target_uri is not None:
        try:
            if webbrowser.open_new(target_uri):
                opened = True
                print("  [c] webbrowser.open_new(clean URI): ret=True")
        except Exception as e:
            print(f"  [c] webbrowser.open_new FAIL: {type(e).__name__}: {e}")

    # 4) fallback: directly open raw output HTML
    if not opened:
        try:
            webbrowser.open(output_html.resolve().as_uri())
            opened = True
            print("  [d] fallback: opened raw output HTML directly")
        except Exception as e:
            print(f"  [d] fallback FAIL: {type(e).__name__}: {e}")

    if opened:
        print("  OK: popup request delivered (double-click LATEST_*.html if you see nothing)")
        return 0
    print("  FAIL: all 4 layers failed - double-click LATEST_*.html manually")
    return 1


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        _base = Path(sys.argv[1]).resolve()
        OUTPUT_DIR = _base / "output" if _base.name != "output" else _base
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _output_html = OUTPUT_DIR / "stockwarden_futures_or_options_stocks.html"
    _latest = OUTPUT_DIR / "LATEST_請按兩下這個開啟_最新版_不會有快取.html"
    if _latest.exists():
        print(f"[INFO] Target launcher: {_latest}")
    else:
        print(f"[WARN] No fixed launcher found, will use raw: {_output_html}")
    rc = open_with_four_fallbacks(OUTPUT_DIR, _output_html)
    sys.exit(rc)
