"""
Test: Populate design table cells via COM after InsertFamilyTableEdit.
Goal: Automatically fix model so InsertFamilyTableOpen works.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.config import LOCAL_PATHS

EXCEL = LOCAL_PATHS["excel"]

import win32com.client, pythoncom, openpyxl
pythoncom.CoInitialize()

# Use working copy from previous fix
WORKING = os.path.join(os.path.dirname(LOCAL_PATHS["sw_model"]), "model_gen4_working_fix.SLDPRT")

# Read Excel data
wb = openpyxl.load_workbook(EXCEL, data_only=True)
ws = wb.active
row2_cells = list(ws.iter_rows(min_row=2, max_row=2))[0]
row2_vals = [c.value for c in row2_cells]
excel_params = [str(v) for v in row2_vals[1:] if v is not None]
config_data = {}
for row in ws.iter_rows(min_row=3, values_only=True):
    if row[0] is None: break
    config_data[int(row[0])] = [row[i] for i in range(1, len(excel_params)+1)]
wb.close()

try:
    sw = win32com.client.Dispatch("SldWorks.Application")
    sw.Visible = True
    time.sleep(8)
    print(f"SW Rev: {sw.RevisionNumber}")

    oe = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    ow = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    doc = sw.OpenDoc6(WORKING, 1, 1, "", oe, ow)
    print(f"Opened: Errors={oe.value} Warnings={ow.value}")

    # Step 1: Delete any existing DT
    try:
        doc.DeleteDesignTable()
        print("DeleteDesignTable: OK")
    except Exception as e:
        print(f"DeleteDesignTable: {type(e).__name__}: {e}")

    # Step 2: Create new empty DT
    print("\n--- Creating empty design table ---")
    doc.InsertFamilyTableEdit()
    time.sleep(2)
    print("InsertFamilyTableEdit: OK")

    # Step 3: Get DT interface and attach
    print("\n--- Attaching to design table ---")
    dt = doc.GetDesignTable
    print(f"DT object: {dt}, type: {type(dt)}")

    # In pywin32, Attach may be a property that runs on access
    attached = dt.Attach
    print(f"Attach result: {attached}")

    # Check dimensions now
    nrows = dt.GetTotalRowCount
    ncols = dt.GetTotalColumnCount
    print(f"DT size after attach: {nrows} x {ncols}")

    # Read current content
    print("\nCurrent DT content:")
    for r in range(min(15, nrows if nrows > 0 else 10)):
        cells = []
        for c in range(min(6, ncols if ncols > 0 else 7)):
            try:
                cell = dt.GetEntryText(r, c)
                cells.append(str(cell)[:18] if cell else "(empty)")
            except Exception as e:
                cells.append(f"ERR")
        print(f"  Row {r}: {' | '.join(cells)}")

    # Step 4: Try to write cells
    # The DT should have: Row 0=header, Row 1=headers (Family + param names), Row 2+=data
    print("\n--- Attempting to write data ---")
    
    # First, ensure we have enough rows
    # Data: 10 configs, so we need rows 2-11 (0-indexed)
    # If the DT has fewer rows, we need to add them
    
    # Try various write methods
    write_methods = []
    for attr_name in dir(dt):
        low = attr_name.lower()
        if 'set' in low or 'put' in low:
            write_methods.append(attr_name)
    print(f"Potential write methods on DT: {write_methods}")

    # Test SetEntryText specifically
    if hasattr(dt, 'SetEntryText'):
        print("Found SetEntryText! Testing...")
        try:
            dt.SetEntryText(2, 0, "test_config")
            print("  SetEntryText(2,0,'test_config'): OK")
            # Read back
            val = dt.GetEntryText(2, 0)
            print(f"  Read back: '{val}'")
        except Exception as e:
            print(f"  SetEntryText failed: {type(e).__name__}: {e}")
    else:
        print("SetEntryText not available.")
    
    # Try _oleobj_ dispatch
    print("\nTrying _oleobj_ dispatch for SetEntryText...")
    try:
        # IDesignTable::SetEntryText has DISPID? In SW type library
        # Let's try by ordinal
        dt._oleobj_.Invoke(6, 0, 0, (2, 0, "test_via_ole"))
        val = dt.GetEntryText(2, 0)
        print(f"  _oleobj_.Invoke: Read back '{val}'")
    except Exception as e:
        print(f"  _oleobj_.Invoke: {type(e).__name__}: {e}")

    # Try finding the DISPID by iterating
    print("\nSearching for write DISPID...")
    for dispid in range(1, 20):
        try:
            result = dt._oleobj_.Invoke(dispid, 0, 0, (2, 0, f"val_{dispid}"))
            val = dt.GetEntryText(2, 0)
            if f"val_{dispid}" in str(val):
                print(f"  DISPID={dispid} WORKED! SetEntryText!")
                break
        except Exception:
            pass
    else:
        print("  No write DISPID found.")

    # Step 5: Detach and update
    print("\n--- Closing design table ---")
    try:
        dt.Detach
        print("Detach: OK")
    except Exception as e:
        print(f"Detach: {type(e).__name__}: {e}")
    
    try:
        doc.CloseFamilyTable()
        print("CloseFamilyTable: OK")
    except Exception as e:
        print(f"CloseFamilyTable: {type(e).__name__}: {e}")

    # Check configs after
    cfgs = list(doc.GetConfigurationNames)
    print(f"\nConfigs after DT edit: {cfgs}")

    sw.CloseDoc(os.path.basename(WORKING))
finally:
    pythoncom.CoUninitialize()
