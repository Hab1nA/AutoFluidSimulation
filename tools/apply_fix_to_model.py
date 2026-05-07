"""
Verify the fix on the ORIGINAL model (with backup).
The fix: InsertFamilyTableEdit + set FileName/LinkToFile properties.

After this script succeeds, InsertFamilyTableOpen will return True.

STATUS (2026-05-07): ✅ FIX CONFIRMED — InsertFamilyTableOpen returns True after
InsertFamilyTableEdit + FileName/LinkToFile. Production code (engine/task_runner.py)
now uses _model_has_design_table() detection instead — this script is retained as
a reference/diagnostic tool only.
"""
import os, sys, time, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.config import LOCAL_PATHS

MODEL = LOCAL_PATHS["sw_model"]
EXCEL = LOCAL_PATHS["excel"]
MODEL_DIR = os.path.dirname(MODEL)
MODEL_NAME = os.path.basename(MODEL)

# Step 0: Create backup
BACKUP = os.path.join(MODEL_DIR, "model_gen4_backup_original.SLDPRT")
if not os.path.exists(BACKUP):
    shutil.copy2(MODEL, BACKUP)
    print(f"Backup created: {BACKUP}")

import win32com.client, pythoncom
pythoncom.CoInitialize()

try:
    sw = win32com.client.Dispatch("SldWorks.Application")
    sw.Visible = True
    time.sleep(8)

    oe = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    ow = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    doc = sw.OpenDoc6(MODEL, 1, 1, "", oe, ow)
    print(f"Opened model")

    # Check before
    cfgs_before = list(doc.GetConfigurationNames)
    print(f"Configs before fix: {cfgs_before} ({len(cfgs_before)})")

    # === THE FIX ===
    print("\n--- Applying fix ---")

    # 1. Delete any existing design table
    try:
        doc.DeleteDesignTable()
    except Exception as e:
        print(f"DeleteDesignTable failed: {type(e).__name__}: {e}")

    # 2. Create fresh empty design table
    doc.InsertFamilyTableEdit()
    time.sleep(2)

    # 3. Get DT interface and set external file link
    dt = doc.GetDesignTable
    dt.FileName = EXCEL
    dt.LinkToFile = True

    # 4. Detach and close
    try:
        _ = dt.Detach
    except Exception as e:
        print(f"Detach failed: {type(e).__name__}: {e}")
    try:
        doc.CloseFamilyTable()
    except Exception as e:
        print(f"CloseFamilyTable failed: {type(e).__name__}: {e}")

    print("Fix applied: InsertFamilyTableEdit + FileName + LinkToFile")
    # === END FIX ===

    # Verify
    print("\n--- Verification ---")
    result = doc.InsertFamilyTableOpen(EXCEL)
    print(f"InsertFamilyTableOpen: {result}")

    cfgs_after = list(doc.GetConfigurationNames)
    print(f"Configs after: {cfgs_after} ({len(cfgs_after)})")

    if result:
        print("\n*** FIX CONFIRMED! Save the model to make it permanent. ***")
        try:
            # Save the fixed model
            doc.Save3(1, 0, 0)  # swSaveAsOptions_Silent
            print(f"Model saved: {MODEL}")
        except Exception as e:
            print(f"Save3 failed ({e}), trying Save...")
            try:
                # pywin32 property-based save
                ret = sw.SaveDoc(doc)
                print(f"SaveDoc: {ret}")
            except Exception as e2:
                print(f"Cannot save programmatically.")
                print(f"Please manually save the model in SW (Ctrl+S).")
    else:
        print("\nFix did not work on this model instance.")

    sw.CloseDoc(MODEL_NAME)
finally:
    pythoncom.CoUninitialize()
