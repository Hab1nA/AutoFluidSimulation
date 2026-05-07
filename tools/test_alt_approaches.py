"""
Test alternative approaches: InsertFamilyTableEdit + COM cell manipulation.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.config import LOCAL_PATHS

MODEL = LOCAL_PATHS["sw_model"]
EXCEL = LOCAL_PATHS["excel"]

import win32com.client, pythoncom, openpyxl
pythoncom.CoInitialize()

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
    doc = sw.OpenDoc6(MODEL, 1, 1, "", oe, ow)
    print(f"Opened: Errors={oe.value} Warnings={ow.value}")

    # ================================================================
    print("\n=== APPROACH 1: InsertFamilyTableEdit (opens empty DT in Excel) ===")
    try:
        doc.InsertFamilyTableEdit()
        print("InsertFamilyTableEdit: OK (Excel should be open now)")
        # In VBA, after InsertFamilyTableEdit, you manipulate the Excel directly
        # In pywin32, we might need to use the DesignTable interface

        # Try GetDesignTable after InsertFamilyTableEdit
        time.sleep(2)
        try:
            dt = doc.GetDesignTable
            if dt is not None:
                print(f"GetDesignTable (property): {dt}")
                print(f"  Type: {type(dt)}")
                # Try Attach
                try:
                    result = dt.Attach()
                    print(f"  Attach(): {result}")
                except TypeError:
                    result = dt.Attach  # property
                    print(f"  Attach (property): {result}")
                except Exception as e:
                    print(f"  Attach failed: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"GetDesignTable failed: {type(e).__name__}: {e}")

        # Close the Excel design table
        try:
            doc.CloseFamilyTable()
            print("CloseFamilyTable: OK")
        except Exception as e:
            print(f"CloseFamilyTable: {type(e).__name__}: {e}")

    except Exception as e:
        print(f"InsertFamilyTableEdit failed: {type(e).__name__}: {e}")

    # ================================================================
    print("\n=== APPROACH 2: Try InsertFamilyTableOpen with .xls format ===")
    # Maybe SW expects .xls (legacy Excel format) not .xlsx?
    import tempfile
    tmp_xls = os.path.join(tempfile.gettempdir(), "test_dt_legacy.xls")
    try:
        import xlwt  # try legacy format
        wb_xls = xlwt.Workbook()
        ws_xls = wb_xls.add_sheet("Sheet1")
        ws_xls.write(0, 0, "Design Table for: model_gen4")
        ws_xls.write(1, 0, "")
        for i, p in enumerate(excel_params):
            ws_xls.write(1, i+1, p)
        vals = config_data[0]
        ws_xls.write(2, 0, "0")
        for i, v in enumerate(vals):
            ws_xls.write(2, i+1, v)
        wb_xls.save(tmp_xls)
        result2 = doc.InsertFamilyTableOpen(tmp_xls)
        print(f"InsertFamilyTableOpen(.xls): {result2}")
        try:
            os.remove(tmp_xls)
        except OSError as e:
            print(f"Cleanup failed for {tmp_xls}: {type(e).__name__}: {e}")
    except ImportError:
        print("xlwt not installed, skipping .xls test")

    # ================================================================
    print("\n=== APPROACH 3: Try with SW's own Excel engine ===")
    # Maybe SW uses its embedded Excel COM, not external file
    # Use ISldWorks::OpenDoc7 with special flags?

    # ================================================================
    print("\n=== APPROACH 4: Check SW add-ins / Design Table capability ===")
    # Maybe Design Table functionality requires a specific SW add-in?
    try:
        # Check if SW has Excel add-in
        addins = sw.GetAddInCount
        print(f"Add-in count: {addins}")
        # Look for Excel/DesignTable related addins
    except Exception as e:
        print(f"GetAddInCount: {type(e).__name__}: {e}")

    # ================================================================
    print("\n=== APPROACH 5: Extension.ForceRebuildAll before InsertFamilyTableOpen ===")
    try:
        doc.Extension.ForceRebuildAll()
        print("ForceRebuildAll: OK")
        result3 = doc.InsertFamilyTableOpen(EXCEL)
        print(f"InsertFamilyTableOpen after ForceRebuildAll: {result3}")
    except Exception as e:
        print(f"Failed: {type(e).__name__}: {e}")

    # Cleanup
    sw.CloseDoc(os.path.basename(MODEL))
finally:
    pythoncom.CoUninitialize()
