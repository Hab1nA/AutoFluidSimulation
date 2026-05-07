"""
Verify: does the model actually have configs 0-9, or just config 0?
Tests ShowConfiguration2 for all expected configs.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.config import LOCAL_PATHS

MODEL = LOCAL_PATHS["sw_model"]

import win32com.client, pythoncom
pythoncom.CoInitialize()

try:
    sw = win32com.client.Dispatch("SldWorks.Application")
    sw.Visible = True
    time.sleep(8)

    oe = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    ow = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    doc = sw.OpenDoc6(MODEL, 1, 1, "", oe, ow)
    print(f"Opened: Errors={oe.value} Warnings={ow.value}")

    # Test GetConfigurationNames various ways
    print("\n=== GetConfigurationNames ===")
    try:
        names = doc.GetConfigurationNames()
        print(f"  Method call: {names}")
    except TypeError:
        names = doc.GetConfigurationNames  # property
        print(f"  Property access: {names}")
        if isinstance(names, (tuple, list)):
            print(f"  As list: {list(names)}")
            print(f"  Count: {len(names)}")

    # Try GetConfigurationCount
    print("\n=== GetConfigurationCount ===")
    try:
        count = doc.GetConfigurationCount()
        print(f"  Method call: {count}")
    except TypeError:
        count = doc.GetConfigurationCount  # property
        print(f"  Property access: {count}")

    # Try IGetConfigurationNames
    print("\n=== IGetConfigurationNames ===")
    try:
        inames = doc.IGetConfigurationNames()
        print(f"  IGetConfigurationNames(): {inames}")
    except Exception as e:
        print(f"  IGetConfigurationNames(): {type(e).__name__}: {e}")

    # Try activating each expected config
    print("\n=== ShowConfiguration2 for configs 0-9 ===")
    config_exists = {}
    for c in range(10):
        cfg = str(c)
        try:
            result = doc.ShowConfiguration2(cfg)
            config_exists[cfg] = True
            # Check current config name after activation
            try:
                active = doc.ConfigurationManager.ActiveConfiguration.Name
                print(f"  [{cfg}] OK -> active config: {active}")
            except Exception:
                print(f"  [{cfg}] OK")
        except Exception as e:
            config_exists[cfg] = False
            print(f"  [{cfg}] FAIL: {type(e).__name__}: {e}")

    # Summary
    print(f"\n=== Summary ===")
    existing = [k for k, v in config_exists.items() if v]
    missing = [k for k, v in config_exists.items() if not v]
    print(f"Existing configs: {existing}")
    print(f"Missing configs:  {missing}")

    # If only config 0 exists, check: did the remaining 9 get deleted somehow?
    if len(existing) == 1 and '0' in existing:
        print("\n=== HYPOTHESIS ===")
        print("The model MAY have originally had 10 design-table-driven configurations.")
        print("When the design table was deleted (by a previous code run), SW may have")
        print("kept only the currently-active configuration and removed the others.")
        print()
        print("This would explain why InsertFamilyTableOpen returns False:")
        print("  The Excel references 10 configs, but only 1 exists in the model.")
        print("  SOLIDWORKS validates ALL configs before inserting the design table.")
        print()
        print("SOLUTION: Re-create the missing configurations before importing the design table.")
        print("  Or: use the COM direct parameter approach (already implemented)")
        print("  which only operates on the existing config '0'.")

    sw.CloseDoc(os.path.basename(MODEL))
finally:
    pythoncom.CoUninitialize()
