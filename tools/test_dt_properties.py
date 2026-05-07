"""
Test: Use IDesignTable properties (FileName, LinkToFile, SourceType)
to make SW read the design table Excel instead of InsertFamilyTableOpen.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.config import LOCAL_PATHS

MODEL = LOCAL_PATHS["sw_model"]
EXCEL = LOCAL_PATHS["excel"]

import win32com.client, pythoncom, openpyxl
pythoncom.CoInitialize()

try:
    sw = win32com.client.Dispatch("SldWorks.Application")
    sw.Visible = True
    time.sleep(8)

    oe = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    ow = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    doc = sw.OpenDoc6(MODEL, 1, 1, "", oe, ow)
    print(f"Opened: Errors={oe.value} Warnings={ow.value}")

    cfgs_before = list(doc.GetConfigurationNames)
    print(f"Configs before: {cfgs_before}")

    # Delete any existing DT
    try:
        doc.DeleteDesignTable()
        print("DeleteDesignTable: OK")
    except Exception:
        pass

    # Step 1: Create empty DT, then try to set external file
    print("\n--- Approach: Create DT then link external file ---")
    doc.InsertFamilyTableEdit()
    time.sleep(2)
    print("InsertFamilyTableEdit: OK")

    dt = doc.GetDesignTable
    print(f"DT: {dt}")

    # Check DT properties
    print("\nDT Properties:")
    prop_names = ["FileName", "LinkToFile", "SourceType", "AutoAddNewConfigs", 
                  "AutoAddNewParams", "Updatable", "Warn"]
    for pname in prop_names:
        try:
            val = getattr(dt, pname)
            print(f"  {pname} = {val}")
        except Exception as e:
            print(f"  {pname} = ERR: {type(e).__name__}")

    # Try setting FileName to our Excel
    print(f"\nSetting FileName to: {EXCEL}")
    try:
        dt.FileName = EXCEL
        print(f"  FileName set. Read back: {dt.FileName}")
    except Exception as e:
        print(f"  Set FileName failed: {type(e).__name__}: {e}")

    # Try setting LinkToFile
    print("\nSetting LinkToFile = True")
    try:
        dt.LinkToFile = True
        print(f"  LinkToFile set. Read back: {dt.LinkToFile}")
    except Exception as e:
        print(f"  Set LinkToFile failed: {type(e).__name__}: {e}")

    # Try setting SourceType (likely: 0=embedded, 1=external)
    print("\nTrying SourceType values:")
    for st in [0, 1, 2]:
        try:
            dt.SourceType = st
            print(f"  SourceType={st}: OK, read back={dt.SourceType}")
        except Exception as e:
            print(f"  SourceType={st}: {type(e).__name__}")

    # Try UpdateTable / UpdateModel after setting properties
    print("\nTrying UpdateTable / UpdateModel:")
    for method in ["UpdateTable", "UpdateModel", "EditTable2"]:
        try:
            m = getattr(dt, method)
            print(f"  {method}: {m}")
            # Try as property
            if not callable(m):
                print(f"    (property) value: {m}")
        except Exception as e:
            print(f"  {method}: {type(e).__name__}")

    # Detach and close
    try:
        dt.Detach
    except Exception:
        pass
    try:
        doc.CloseFamilyTable()
    except Exception:
        pass

    # Check if InsertFamilyTableOpen works NOW
    print("\n--- Testing InsertFamilyTableOpen after DT property setup ---")
    result = doc.InsertFamilyTableOpen(EXCEL)
    print(f"InsertFamilyTableOpen: {result}")

    cfgs_after = list(doc.GetConfigurationNames)
    print(f"Configs after: {cfgs_after}")

    sw.CloseDoc(os.path.basename(MODEL))
finally:
    pythoncom.CoUninitialize()
