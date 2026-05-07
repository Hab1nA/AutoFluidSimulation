"""
Guide: Fix SW model configuration state so InsertFamilyTableOpen works.

Run: python tools/fix_model_configs.py

This script will:
1. Make a backup copy of the model
2. Open the copy and diagnose configuration state
3. Attempt to fix ghost configurations by deleting and recreating them
4. Test InsertFamilyTableOpen after the fix
5. If successful, provide instructions for applying to the real model
"""
import os, sys, time, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.config import LOCAL_PATHS

MODEL = LOCAL_PATHS["sw_model"]
EXCEL = LOCAL_PATHS["excel"]
MODEL_DIR = os.path.dirname(MODEL)
MODEL_NAME = os.path.basename(MODEL)
MODEL_BASE = os.path.splitext(MODEL_NAME)[0]

# Create backup
BACKUP = os.path.join(MODEL_DIR, MODEL_BASE + "_backup_before_fix.SLDPRT")
print("=" * 70)
print("SW Model Configuration Fix Tool")
print("=" * 70)
print(f"\nModel: {MODEL}")
print(f"Backup will be saved to: {BACKUP}")

# Make backup
shutil.copy2(MODEL, BACKUP)
print(f"Backup created: OK")

# Create working copy
WORKING = os.path.join(MODEL_DIR, MODEL_BASE + "_working_fix.SLDPRT")
shutil.copy2(MODEL, WORKING)
print(f"Working copy: {WORKING}")

import win32com.client, pythoncom, openpyxl
pythoncom.CoInitialize()

try:
    sw = win32com.client.Dispatch("SldWorks.Application")
    sw.Visible = True
    print("[SW] Started, waiting 8s...")
    time.sleep(8)

    # ---- STEP 1: Open and diagnose ----
    print("\n" + "=" * 70)
    print("STEP 1: Diagnose current state")
    print("=" * 70)

    oe = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    ow = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    doc = sw.OpenDoc6(WORKING, 1, 1, "", oe, ow)
    print(f"Opened: Errors={oe.value} Warnings={ow.value}")

    cfgs_before = list(doc.GetConfigurationNames)
    print(f"Configs before fix: {cfgs_before} (count={len(cfgs_before)})")

    # Check parameters
    test_params = ['D1@阵列(圆周)3', 'D1@阵列(圆周)4', 'D1@阵列(圆周)5', 'D1@阵列(圆周)6']
    print(f"Parameters check:")
    for p in test_params:
        try:
            pm = doc.Parameter(p)
            print(f"  {p}: Value={pm.Value}")
        except Exception as e:
            print(f"  {p}: MISSING ({type(e).__name__})")

    # ---- STEP 2: Delete ghost configs, keep only "0" ----
    print("\n" + "=" * 70)
    print("STEP 2: Clean up configurations")
    print("=" * 70)
    
    # First, activate config 0
    doc.ShowConfiguration2("0")
    print("Activated config '0'")
    
    # Get all config names and delete all except "0" and "Default"
    all_cfgs = list(doc.GetConfigurationNames)
    keep_configs = {"0", "Default", "默认"}
    to_delete = [c for c in all_cfgs if c not in keep_configs]
    
    if to_delete:
        print(f"Deleting configs: {to_delete}")
        for cfg_name in to_delete:
            try:
                doc.DeleteConfiguration2(cfg_name)
                print(f"  Deleted: {cfg_name}")
            except Exception as e:
                print(f"  FAIL deleting {cfg_name}: {type(e).__name__}: {e}")
    else:
        print("No extra configs to delete.")
    
    # Check remaining configs
    cfgs_after_delete = list(doc.GetConfigurationNames)
    print(f"Configs after cleanup: {cfgs_after_delete} (count={len(cfgs_after_delete)})")

    # ---- STEP 3: Verify parameters still exist ----
    print("\n" + "=" * 70)
    print("STEP 3: Verify parameters after cleanup")
    print("=" * 70)
    
    doc.ShowConfiguration2("0")
    all_params_ok = True
    for p in test_params:
        try:
            pm = doc.Parameter(p)
            print(f"  {p}: Value={pm.Value}")
        except Exception as e:
            print(f"  {p}: MISSING ({type(e).__name__})")
            all_params_ok = False

    if all_params_ok:
        print("All parameters intact after cleanup.")
    else:
        print("WARNING: Some parameters lost during config cleanup!")
        print("This means the parameters only existed in the deleted configs.")
        print("We need a different approach.")

    # ---- STEP 4: Try InsertFamilyTableOpen ----
    print("\n" + "=" * 70)
    print("STEP 4: Test InsertFamilyTableOpen after cleanup")
    print("=" * 70)
    
    result = doc.InsertFamilyTableOpen(EXCEL)
    print(f"InsertFamilyTableOpen: {result}")
    
    if result:
        print("\n*** SUCCESS! The configuration cleanup fixed it! ***")
        # Check configs
        cfgs_after_import = list(doc.GetConfigurationNames)
        print(f"Configs after import: {cfgs_after_import} (count={len(cfgs_after_import)})")
        
        # Save the fixed model
        try:
            doc.Save3(1, 0, 0)  # swSaveAsOptions_Silent=1
            print(f"Fixed model saved to: {WORKING}")
        except Exception as e:
            print(f"Save failed: {e}, trying Save...")
            try:
                doc.Save
                print("Save (property): OK")
            except Exception as e2:
                print(f"Save also failed: {e2}")
    else:
        print("\nStill fails. Trying alternative fix...")
        
        # ---- STEP 5: Alternative - try InsertFamilyTableEdit ----
        print("\n" + "=" * 70)
        print("STEP 5: Try InsertFamilyTableEdit approach")
        print("=" * 70)
        
        try:
            doc.CloseFamilyTable()
        except Exception:
            pass
        
        try:
            doc.InsertFamilyTableEdit()
            print("InsertFamilyTableEdit: OK")
            time.sleep(2)
            
            # Get the design table
            dt = doc.GetDesignTable
            print(f"DesignTable object: {dt}")
            
            # Try to get the Excel application and manipulate cells
            # SW's embedded Excel can be accessed via IDesignTable
            nrows = dt.GetTotalRowCount
            ncols = dt.GetTotalColumnCount
            print(f"DT size: {nrows} rows x {ncols} cols")
            
            # Read what SW auto-generated
            print("Auto-generated content:")
            for r in range(min(5, nrows)):
                cells = []
                for c in range(min(6, ncols)):
                    try:
                        cell = dt.GetEntryText(r, c)
                        cells.append(str(cell)[:20] if cell else "(empty)")
                    except Exception:
                        cells.append("ERR")
                print(f"  Row {r}: {' | '.join(cells)}")
            
            # Close without saving changes
            dt.Detach
            doc.CloseFamilyTable()
            print("Closed design table without saving.")
            
        except Exception as e:
            print(f"InsertFamilyTableEdit failed: {type(e).__name__}: {e}")

    # ---- STEP 6: Final state report ----
    print("\n" + "=" * 70)
    print("FINAL STATE")
    print("=" * 70)
    cfgs_final = list(doc.GetConfigurationNames)
    print(f"Configs: {cfgs_final} (count={len(cfgs_final)})")
    print(f"\nWorking copy: {WORKING}")
    print(f"Backup: {BACKUP}")
    print(f"\nIf the fix worked, replace your model with the working copy.")
    print(f"  To restore original: copy {BACKUP} -> {MODEL}")

    # Close without saving
    sw.CloseDoc(os.path.basename(WORKING))
    
finally:
    pythoncom.CoUninitialize()
