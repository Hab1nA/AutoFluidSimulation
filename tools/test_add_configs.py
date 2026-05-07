"""
Test: Can we fix InsertFamilyTableOpen by adding missing configurations?
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.config import LOCAL_PATHS

MODEL = LOCAL_PATHS["sw_model"]
EXCEL = LOCAL_PATHS["excel"]
# Copy model to temp so we don't damage original
import shutil, tempfile
tmp_model = os.path.join(tempfile.gettempdir(), "test_model_copy.SLDPRT")
shutil.copy2(MODEL, tmp_model)
print(f"Working on copy: {tmp_model}")

import win32com.client, pythoncom
pythoncom.CoInitialize()

try:
    sw = win32com.client.Dispatch("SldWorks.Application")
    sw.Visible = True
    time.sleep(8)

    oe = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    ow = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    doc = sw.OpenDoc6(tmp_model, 1, 1, "", oe, ow)
    print(f"Opened: Errors={oe.value} Warnings={ow.value}")

    # Check current configs
    cfgs = list(doc.GetConfigurationNames) if hasattr(doc, 'GetConfigurationNames') else []
    print(f"\nCurrent configs: {cfgs}")
    print(f"Config count: {doc.GetConfigurationCount}")

    # Try: Add missing configurations 1-9
    print("\n=== Adding configurations 1-9 ===")
    for c in range(1, 10):
        cfg_name = str(c)
        try:
            # AddConfiguration3(Name, Comment, AlternateName, Options)
            # Options: 0 = none, 1 = suppress features, 2 = suppress new features
            doc.AddConfiguration3(cfg_name, f"Auto-created config {c}", "", 0)
            print(f"  [{c}] Added config '{cfg_name}'")
        except Exception as e:
            # Try older AddConfiguration2
            try:
                doc.AddConfiguration2(cfg_name, f"Auto-created config {c}", "")
                print(f"  [{c}] Added (v2) config '{cfg_name}'")
            except Exception as e2:
                print(f"  [{c}] FAIL: {type(e).__name__} / {type(e2).__name__}")

    # Check configs again
    cfgs2 = list(doc.GetConfigurationNames)
    print(f"\nConfigs after add: {cfgs2}")

    # Try InsertFamilyTableOpen now
    print("\n=== Testing InsertFamilyTableOpen ===")
    result = doc.InsertFamilyTableOpen(EXCEL)
    print(f"InsertFamilyTableOpen: {result}")

    if result:
        print("SUCCESS! Adding missing configs fixed it.")
        # Verify design table was applied
        try:
            dt = doc.GetDesignTable
            if dt is not None:
                print(f"Design table: {dt}")
        except Exception:
            pass
    else:
        print("Still fails even after adding configs.")

    # Cleanup
    sw.CloseDoc(os.path.basename(tmp_model))
finally:
    pythoncom.CoUninitialize()
    try: os.remove(tmp_model)
    except: pass
